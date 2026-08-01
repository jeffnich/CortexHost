#!/usr/bin/env python3
"""
Cortex daily brief: the proactive layer.

Once a day it reads everything that crossed the brain in the last ~26h plus the
latest Oura signal and any open loops, synthesizes a sharp morning brief +
cross-source patterns via an LLM, stores it back into the brain
(source=cortex-brief, dedupe_key=brief:YYYY-MM-DD so it shows in the map feed and
is queryable), and renders a hosted private page.

Reaches Qdrant over Railway's private network (same as cortex-map). All knobs via
env: BRIEF_MODEL, BRIEF_LOOKBACK_H, plus the shared DEDUP_QDRANT_URL /
QDRANT_CLOUD_API_KEY / QDRANT_COLLECTION / CORTEX_TENANT_ID / CORTEX_USER_ID /
OPENAI_API_KEY.
"""

import html
import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import requests
from openai import OpenAI

QDRANT = (os.getenv("QDRANT_URL") or os.getenv("DEDUP_QDRANT_URL", "http://qdrant:6333")).rstrip("/")
KEY = os.getenv("QDRANT_API_KEY") or os.getenv("QDRANT_CLOUD_API_KEY", "")
COLL = os.getenv("QDRANT_COLLECTION", "memories")
TENANT = os.getenv("CORTEX_TENANT_ID", "")
USER = os.getenv("CORTEX_USER_ID", "")
SCOPED = f"{TENANT}:{USER}"
MODEL = os.getenv("BRIEF_MODEL", "gpt-5-mini")
EMBED_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
LOOKBACK_H = float(os.getenv("BRIEF_LOOKBACK_H", "26"))

_oa = OpenAI()
HDR = {"api-key": KEY, "Content-Type": "application/json"}

USER_NAME = os.getenv("CORTEX_USER_NAME", "the user")
USER_CONTEXT = os.getenv("CORTEX_USER_CONTEXT", "")
USER_GOALS = os.getenv("CORTEX_USER_GOALS", "")

SYS = (
    "You write __CORTEX_USER__'s morning brief: a short, honest account of what actually "
    "CHANGED in the last day. __CORTEX_CTX__Direct, concrete, no filler, no hedging, "
    "no motivational fluff, no em dashes, second person.\n"
    "You are given (a) the memories that landed in the last ~26h and (b) your own recent "
    "briefs. Rules:\n"
    "- Report the DELTA: what they actually did, decided, discussed, or shipped yesterday. "
    "Name specifics (projects, artifacts, people, decisions). Do NOT re-describe ongoing "
    "states, evergreen facts, or frameworks they already live in. Those are context, not news.\n"
    "- Do NOT repeat your prior briefs. If a thread is unchanged from before, DROP it; "
    "mention it only if something new happened on it. If yesterday was genuinely quiet or a "
    "repeat of the day before, SAY SO in one honest line. A short true brief beats a long "
    "manufactured one.\n"
    "- No forced connections. Note a pattern only if it clearly recurs in the NEW data and "
    "is useful. Never correlate unrelated domains (sleep vs work, a hobby vs the job) unless "
    "the link is obvious and actionable. Prefer an empty list over an invented insight.\n"
    'Return STRICT JSON only: {"headline": "one specific sentence about what yesterday '
    'actually was, not a recurring theme", "narrative": "2-3 sentences on what happened and '
    'what is worth carrying forward", "patterns": ["only real, recurring, useful; else empty"], '
    '"open_loops": ["a genuinely unfinished thread + next step, not one already in prior briefs"], '
    '"focus": ["1-3 concrete things for today"]}. '
    "Keep each list to at most 4 items. Return an empty array whenever there is nothing real to say."
)
SYS = SYS.replace("__CORTEX_USER__", USER_NAME).replace("__CORTEX_CTX__", (USER_CONTEXT.strip() + " ") if USER_CONTEXT.strip() else "")


def _scroll(flt, limit=600):
    out, nxt = [], None
    while True:
        body = {"limit": 200, "with_payload": True, "with_vector": False, "filter": flt}
        if nxt:
            body["offset"] = nxt
        r = requests.post(f"{QDRANT}/collections/{COLL}/points/scroll", json=body, headers=HDR, timeout=60)
        r.raise_for_status()
        res = r.json().get("result", {})
        pts = res.get("points", [])
        out += pts
        nxt = res.get("next_page_offset")
        if not nxt or len(out) >= limit:
            break
    return out


def _user_flt(extra=None):
    must = [{"key": "user_id", "match": {"value": SCOPED}}]
    if extra:
        must += extra
    return {"must": must}


def recent(hours=LOOKBACK_H):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    flt = _user_flt([{"key": "created_at_ts", "range": {"gte": cutoff}}])
    # don't feed the brief its own past output (or foresight) back in -> echo chamber
    # exclude self-output AND machine-written sources (e.g. obsidian agent notes):
    # the brief reports what the USER did, not what their automations wrote
    flt["must_not"] = [{"key": "source", "match": {"any": ["cortex-brief", "foresight", "obsidian", "cortex-health"]}}]
    pts = _scroll(flt, limit=800)
    rows = []
    for p in pts:
        pl = p.get("payload", {})
        rows.append({"text": pl.get("text", ""), "source": pl.get("source", "?"), "ts": pl.get("created_at_ts", 0)})
    rows.sort(key=lambda x: x["ts"] or 0)
    return rows


def latest_by_source(source, n=3, max_age_h=None):
    pts = _scroll(_user_flt([{"key": "source", "match": {"value": source}}]), limit=120)
    pts.sort(key=lambda p: p.get("payload", {}).get("created_at_ts", 0), reverse=True)
    if max_age_h:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_h * 3600
        pts = [p for p in pts if (p.get("payload", {}).get("created_at_ts") or 0) >= cutoff]
    return [p.get("payload", {}).get("text", "") for p in pts[:n]]


def _context(rows, oura):
    by_src = {}
    for r in rows:
        by_src.setdefault(r["source"], []).append(r["text"])
    lines = []
    for src, texts in sorted(by_src.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"\n## {src} ({len(texts)})")
        for t in texts[:40]:
            lines.append(f"- {t[:300]}")
    if oura:
        lines.append("\n## oura (latest readings)")
        for t in oura:
            lines.append(f"- {t[:300]}")
    return "\n".join(lines)[:48000]


def synthesize(rows, oura, prior=None):
    if not rows and not oura:
        return {
            "headline": "Quiet day. Nothing new crossed the brain.",
            "narrative": "No memories landed in the last day. The brain is idle, waiting for signal.",
            "patterns": [], "open_loops": [], "focus": [],
        }
    ctx = _context(rows, oura)
    prior_block = ""
    if prior:
        heads = "\n".join(f"- {p.get('headline', '')}" for p in prior if p.get("headline"))
        if heads:
            prior_block = ("\n\n---\nYour last few briefs (do NOT repeat these; report only what "
                           "is new or changed since):\n" + heads)
    resp = _oa.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": SYS},
                  {"role": "user", "content": f"Memories from the last day:\n{ctx}{prior_block}"}],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    for k in ("patterns", "open_loops", "focus"):
        data[k] = [s for s in (data.get(k) or []) if isinstance(s, str) and s.strip()][:4]
    data.setdefault("headline", "Daily brief")
    data.setdefault("narrative", "")
    data["_counts"] = {"memories": len(rows), "sources": len({r["source"] for r in rows})}
    return data


def _brief_text(b, date_str):
    parts = [f"Daily brief {date_str}: {b.get('headline','')}", b.get("narrative", "")]
    if b.get("patterns"):
        parts.append("Patterns: " + " ".join(b["patterns"]))
    if b.get("open_loops"):
        parts.append("Open loops: " + " ".join(b["open_loops"]))
    if b.get("focus"):
        parts.append("Suggested focus: " + " ".join(b["focus"]))
    return "\n".join(p for p in parts if p)


def store_brief(b, date_str):
    text = _brief_text(b, date_str)
    vec = _oa.embeddings.create(model=EMBED_MODEL, input=[text[:8000]]).data[0].embedding
    mid = str(uuid.uuid5(uuid.NAMESPACE_URL, "cortex:brief:" + date_str))
    now = datetime.now(timezone.utc)
    payload = {
        "memory_id": mid, "id": mid, "text": text, "user_id": SCOPED,
        "tenant_id": TENANT, "tenantId": TENANT,
        "created_at": now.isoformat(), "created_at_ts": now.timestamp(),
        "updated_at": now.isoformat(), "updated_at_ts": now.timestamp(),
        "source": "cortex-brief", "tags": ["cortex-brief", "brief"], "type_hint": "brief",
        "metadata": {"channel": "cortex-brief", "date": date_str, "brief": b},
    }
    requests.put(f"{QDRANT}/collections/{COLL}/points?wait=true",
                 json={"points": [{"id": mid, "vector": vec, "payload": payload}]},
                 headers=HDR, timeout=60).raise_for_status()
    return mid


def history(n=8):
    pts = _scroll(_user_flt([{"key": "source", "match": {"value": "cortex-brief"}}]), limit=120)
    pts.sort(key=lambda p: p.get("payload", {}).get("created_at_ts", 0), reverse=True)
    out = []
    for p in pts[:n]:
        pl = p.get("payload", {})
        meta = pl.get("metadata", {})
        out.append({"date": meta.get("date", ""), "brief": meta.get("brief", {}), "ts": pl.get("created_at_ts", 0)})
    return out


def generate():
    """Pull -> synthesize -> store. Returns (date_str, brief_dict)."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = recent()
    oura = latest_by_source("oura", 3, max_age_h=48)  # dead feeds must not masquerade as yesterday
    prior = [h["brief"] for h in history(3) if h.get("brief")]
    b = synthesize(rows, oura, prior=prior)
    try:
        store_brief(b, date_str)
    except Exception as e:
        print(f"store_brief error: {type(e).__name__}: {e}", flush=True)
    return date_str, b


# ---------- render ----------

def _esc(s):
    return html.escape(str(s or ""))


def _section(title, items):
    if not items:
        return ""
    lis = "".join(f"<li>{_esc(x)}</li>" for x in items)
    return f'<div class="sec"><h2>{_esc(title)}</h2><ul>{lis}</ul></div>'


def render_page(latest, hist, embed=False):
    briefs = []
    for h in hist:
        b = h.get("brief", {})
        c = b.get("_counts", {})
        briefs.append({
            "date": h.get("date", ""),
            "headline": b.get("headline", "Daily brief"),
            "narrative": b.get("narrative", ""),
            "sub": (f'{c.get("memories",0)} memories · {c.get("sources",0)} sources in the last day' if c else ""),
            "patterns": [s for s in (b.get("patterns") or []) if s],
            "open_loops": [s for s in (b.get("open_loops") or []) if s],
            "focus": [s for s in (b.get("focus") or []) if s],
        })
    if not briefs:
        briefs = [{"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                   "headline": "No brief yet", "narrative": "The brief composes each morning.",
                   "sub": "", "patterns": [], "open_loops": [], "focus": []}]
    body_cls = "embed" if embed else ""
    data_json = json.dumps(briefs)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CortexHost · Daily Brief</title>
<style>
  :root {{ --bg:#07070d; --ink:#eef0f8; --mut:#8a8aa2; --accent:#ff8a5c; --line:#ffffff14; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:radial-gradient(1200px 700px at 70% -10%, #15101f 0%, var(--bg) 55%); color:var(--ink);
         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif; line-height:1.5; font-size:13px; }}
  body.embed {{ background:transparent; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:40px 22px 60px; }}
  body.embed .wrap {{ padding:28px 18px 30px; }}
  .nav {{ display:flex; align-items:center; gap:8px; margin-bottom:7px; }}
  .nav .date {{ color:var(--accent); font-weight:600; font-size:11px; letter-spacing:.1em; text-transform:uppercase; }}
  .nav .sp {{ flex:1; }}
  .navbtn {{ background:#ffffff0d; border:1px solid var(--line); color:var(--ink); width:24px; height:24px; border-radius:7px;
             cursor:pointer; font-size:14px; line-height:1; display:flex; align-items:center; justify-content:center; padding:0; transition:.15s; }}
  .navbtn:hover:not(:disabled) {{ background:#ffffff1f; border-color:#ffffff2e; }}
  .navbtn:disabled {{ opacity:.25; cursor:default; }}
  h1 {{ font-size:19px; line-height:1.3; margin:4px 0 5px; font-weight:660; }}
  .sub {{ color:var(--mut); font-size:11px; margin-bottom:20px; }}
  .narr {{ font-size:13.5px; color:#d7d9ea; margin:0 0 22px; line-height:1.55; }}
  .sec {{ background:#ffffff0a; border:1px solid var(--line); border-radius:14px; padding:13px 15px; margin:10px 0; backdrop-filter:blur(6px); }}
  .sec h2 {{ font-size:10px; letter-spacing:.16em; text-transform:uppercase; color:var(--accent); margin:0 0 9px; font-weight:700; }}
  .sec ul {{ margin:0; padding-left:16px; }}
  .sec li {{ margin:6px 0; color:#dfe1ee; font-size:12px; line-height:1.5; }}
  .foot {{ margin-top:24px; color:#4a4a5e; font-size:10px; letter-spacing:.05em; }}
</style></head>
<body class="{body_cls}"><div class="wrap">
  <div class="nav"><span class="date" id="date"></span><span class="sp"></span><button class="navbtn" id="prev" title="older brief">‹</button><button class="navbtn" id="next" title="newer brief">›</button></div>
  <div id="body"></div>
  <div class="foot">Generated by CortexHost from the last {int(LOOKBACK_H)}h of memory.</div>
</div>
<script>
const B={data_json};let i=0;
function esc(s){{return (s||'').replace(/[&<>]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;'}}[c]}})}}
function sec(t,items){{return (items&&items.length)?'<div class="sec"><h2>'+t+'</h2><ul>'+items.map(function(x){{return '<li>'+esc(x)+'</li>'}}).join('')+'</ul></div>':''}}
function show(n){{i=Math.max(0,Math.min(B.length-1,n));var b=B[i];
  document.getElementById('date').textContent='Daily brief · '+b.date;
  document.getElementById('body').innerHTML='<h1>'+esc(b.headline)+'</h1><div class="sub">'+esc(b.sub)+'</div><p class="narr">'+esc(b.narrative)+'</p>'+sec('Patterns',b.patterns)+sec('Open loops',b.open_loops)+sec("Today's focus",b.focus);
  document.getElementById('prev').disabled=(i>=B.length-1);
  document.getElementById('next').disabled=(i<=0);}}
document.getElementById('prev').onclick=function(){{show(i+1)}};
document.getElementById('next').onclick=function(){{show(i-1)}};
show(0);
</script>
</body></html>"""


def foresight_items(limit=12):
    """Latest type=foresight memories (forecasts + patterns), newest first."""
    pts = _scroll(_user_flt([{"key": "tags", "match": {"value": "foresight"}}]), limit=80)
    rows = []
    for p in pts:
        pl = p.get("payload", {})
        ts = pl.get("created_at_ts", 0) or 0
        try:
            d = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""
        except Exception:
            d = ""
        rows.append({"text": pl.get("text", ""), "ts": ts, "tags": pl.get("tags", []), "date": d})
    rows.sort(key=lambda x: x["ts"], reverse=True)
    return rows[:limit]


FORESIGHT_SYS = (
    "You are __CORTEX_USER__'s behavioral foresight analyst. Given a slice of their recent "
    "memory corpus and your PRIOR forecasts, do four things: (1) score each prior forecast "
    "against what actually happened in the recent memories (held / missed / too-early) and "
    "give a one-line overall hit-rate note; (2) identify open threads tied to goals they hold "
    "(__CORTEX_GOALS__); (3) forecast which are at risk of going dark in ~30 days, each with a "
    "calibrated probability and the single leading indicator driving it; (4) name ONE "
    "highest-leverage next action tied to a goal they hold. Describe behavior and consequence, "
    "never character. Distinguish healthy pruning from real drift; do not manufacture guilt. "
    "Be terse and specific.\n"
    'Return STRICT JSON only: {"hit_rate":"one line on prior forecasts","forecasts":'
    '[{"thread":"short name","probability":"~NN%","indicator":"the one leading signal",'
    '"text":"one-sentence forecast"}],"pattern":"one durable behavioral pattern with '
    'evidence","leverage":"the single highest-leverage next action"}'
)
FORESIGHT_SYS = FORESIGHT_SYS.replace("__CORTEX_USER__", USER_NAME).replace(
    "__CORTEX_GOALS__", USER_GOALS.strip() or "whatever genuinely recurs in the corpus")


def _store_foresight(text, dkey, tags):
    try:
        vec = _oa.embeddings.create(model=EMBED_MODEL, input=[text[:8000]]).data[0].embedding
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, "cortex:" + dkey))
        now = datetime.now(timezone.utc)
        payload = {
            "memory_id": mid, "id": mid, "text": text, "user_id": SCOPED,
            "tenant_id": TENANT, "tenantId": TENANT,
            "created_at": now.isoformat(), "created_at_ts": now.timestamp(),
            "updated_at": now.isoformat(), "updated_at_ts": now.timestamp(),
            "source": "foresight", "tags": tags, "type_hint": "foresight",
        }
        requests.put(f"{QDRANT}/collections/{COLL}/points?wait=true",
                     json={"points": [{"id": mid, "vector": vec, "payload": payload}]},
                     headers=HDR, timeout=60).raise_for_status()
        return True
    except Exception as e:
        print(f"store_foresight error: {type(e).__name__}: {e}", flush=True)
        return False


def generate_foresight():
    """Weekly behavioral foresight pass: score priors, forecast at-risk threads, store
    type=foresight memories (the foresight tab renders these). Returns the run dict."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    horizon = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%d")
    rows = recent(hours=24 * 45)
    priors = [(p.get("payload", {}) or {}).get("text", "")
              for p in _scroll(_user_flt([{"key": "tags", "match": {"value": "foresight"}}]), limit=40)]
    ctx = _context(rows, None)
    prior_txt = "\n".join("- " + t[:300] for t in priors[:20]) or "(none yet)"
    resp = _oa.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": FORESIGHT_SYS},
                  {"role": "user", "content": f"PRIOR FORECASTS:\n{prior_txt}\n\nRECENT MEMORIES (last 45 days):\n{ctx}"}],
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    stored = 0
    for i, f in enumerate(data.get("forecasts", [])[:6]):
        thread = (f.get("thread") or ("thread" + str(i)))
        txt = (f"FORESIGHT {today} (horizon {horizon}): [{thread}] {f.get('probability','')} "
               f"{f.get('text','')} Indicator: {f.get('indicator','')}")
        slug = "".join(c if c.isalnum() else "-" for c in thread.lower())[:40]
        if _store_foresight(txt, f"foresight:{slug}:{today}", ["foresight", slug]):
            stored += 1
    if data.get("pattern"):
        _store_foresight("FORESIGHT PATTERN (" + today + "): " + data["pattern"], f"foresight:pattern:{today}", ["foresight", "pattern"])
    if data.get("leverage"):
        _store_foresight("FORESIGHT LEVERAGE (" + today + "): " + data["leverage"], f"foresight:leverage:{today}", ["foresight", "leverage"])
    print(f"foresight generated: {stored} forecasts | hit_rate: {str(data.get('hit_rate',''))[:90]}", flush=True)
    return data


def render_foresight(items, embed=False):
    body_cls = "embed" if embed else ""
    latest = items[0]["date"] if items else ""
    cards = ""
    for it in items:
        is_pat = "pattern" in (it.get("tags") or [])
        cards += ('<div class="fcard"><div class="flabel">'
                  + ("PATTERN" if is_pat else "FORECAST")
                  + '</div><div class="ftext">' + _esc(it["text"]) + "</div></div>")
    if not cards:
        cards = '<div class="empty">No forecasts yet. Run /foresight to generate the first pass.</div>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CortexHost · Foresight</title>
<style>
  :root {{ --bg:#07070d; --ink:#eef0f8; --mut:#8a8aa2; --accent:#ff8a5c; --line:#ffffff14; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:radial-gradient(1200px 700px at 70% -10%, #15101f 0%, var(--bg) 55%); color:var(--ink);
         font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif; line-height:1.5; font-size:13px; }}
  body.embed {{ background:transparent; }}
  .wrap {{ max-width:760px; margin:0 auto; padding:40px 22px 60px; }}
  body.embed .wrap {{ padding:28px 18px 30px; }}
  .date {{ color:var(--accent); font-weight:600; font-size:11px; letter-spacing:.1em; text-transform:uppercase; margin-bottom:14px; }}
  .fcard {{ background:#ffffff0a; border:1px solid var(--line); border-radius:14px; padding:13px 15px; margin:10px 0; backdrop-filter:blur(6px); }}
  .flabel {{ font-size:9.5px; letter-spacing:.18em; text-transform:uppercase; color:var(--accent); font-weight:700; margin-bottom:6px; }}
  .ftext {{ font-size:12.5px; line-height:1.55; color:#dfe1ee; white-space:pre-wrap; }}
  .empty {{ padding:18px; color:var(--mut); line-height:1.5; }}
</style></head>
<body class="{body_cls}"><div class="wrap">
  <div class="date">Foresight · {_esc(latest)}</div>
  {cards}
</div></body></html>"""


def has_recent_foresight(days=5):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24 * days)).timestamp()
    pts = _scroll(_user_flt([{"key": "source", "match": {"value": "foresight"}},
                             {"key": "created_at_ts", "range": {"gte": cutoff}}]), limit=5)
    return len(pts) > 0
