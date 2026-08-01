#!/usr/bin/env python3
"""
Generate an interactive 2D "constellation" of the Cortex corpus.

Pulls memories (vectors + payload) from Qdrant, projects to 2D (UMAP, PCA
fallback), clusters into topics, and writes a single self-contained HTML file:
a pan/zoom canvas where every memory is a point, colored by source or topic,
hover to read, search to highlight, plus a live "Recent Sparks" feed.

Caches the projected data so UI-only changes can re-render instantly with
--render-only (no re-pull, no re-projection).

  python3 mapgen.py                  # pull + project + render
  python3 mapgen.py --render-only    # rebuild HTML from cache
"""

import argparse
import json
import os
import re
import time
from collections import Counter
from pathlib import Path

import numpy as np
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

URL = (os.getenv("QDRANT_URL") or os.getenv("DEDUP_QDRANT_URL", "http://qdrant:6333")).rstrip("/")
KEY = os.getenv("QDRANT_API_KEY") or os.getenv("QDRANT_CLOUD_API_KEY", "")
COLLECTION = os.getenv("QDRANT_COLLECTION", "memories")
SCOPED_USER = f"{os.getenv('CORTEX_TENANT_ID','')}:{os.getenv('CORTEX_USER_ID','')}"
USER_NAME = os.getenv("CORTEX_USER_NAME", "the user")
PRIVATE_EXTRA = os.getenv("CORTEX_EXCLUDE", "")
# Sources kept OUT of the map render entirely (e.g. raw chatgpt-backup that's redundant
# with the distilled layer). Reversible: just unset MAP_EXCLUDE_SOURCES. Data is untouched.
EXCLUDE_SOURCES = {s.strip() for s in os.getenv("MAP_EXCLUDE_SOURCES", "").split(",") if s.strip()}
CACHE = ROOT / "viz" / "brain_map_cache.json"

STOP = set("the a an and or but if then for to of in on at by with from as is are was were be been "
           "this that these those it its i you my me we our your they them he she his her not no so do "
           "does did have has had will would can could should about into out up down over what which who "
           "when where how why all any can just like get got make made want need use using one two also "
           "more most some any dont im youre its there their here have a's s t re ve ll".split())


def pull(limit=None):
    vecs, texts, sources, types, dates, tss, personal = [], [], [], [], [], [], []
    offset = None
    while True:
        body = {"limit": 1000, "with_payload": True, "with_vector": True,
                "filter": {"must": [{"key": "user_id", "match": {"value": SCOPED_USER}}]}}
        if offset is not None:
            body["offset"] = offset
        r = requests.post(f"{URL}/collections/{COLLECTION}/points/scroll", json=body,
                          headers={"api-key": KEY}, timeout=120)
        r.raise_for_status()
        res = r.json()["result"]
        for p in res["points"]:
            v = p.get("vector")
            pay = p.get("payload") or {}
            t = (pay.get("text") or "").strip()
            src = pay.get("source") or "unknown"
            if not v or not t or src in EXCLUDE_SOURCES:
                continue
            vecs.append(v)
            texts.append(t[:240].replace("\n", " "))
            sources.append(src)
            types.append(pay.get("type_hint") or "")
            dates.append((pay.get("created_at") or pay.get("timestamp") or "")[:10])
            tss.append(float(pay.get("created_at_ts") or 0))
            # demo_personal is set by classify_untagged(); untagged -> hidden (privacy-safe default)
            personal.append(bool(pay.get("demo_personal", True)))
        offset = res.get("next_page_offset")
        print(f"  pulled {len(vecs)}...", flush=True)
        if offset is None or (limit and len(vecs) >= limit):
            break
    return np.array(vecs, dtype=np.float32), texts, sources, types, dates, tss, personal


def project(vecs):
    try:
        import umap
        print("  projecting with UMAP...", flush=True)
        return umap.UMAP(n_neighbors=15, min_dist=0.1, metric="cosine", random_state=42).fit_transform(vecs)
    except Exception as e:
        print(f"  UMAP unavailable ({type(e).__name__}); using PCA", flush=True)
        from sklearn.decomposition import PCA
        return PCA(n_components=2, random_state=42).fit_transform(vecs)


def cluster(xy, k=50):
    from sklearn.cluster import KMeans
    k = min(k, max(2, len(xy) // 200))
    print(f"  clustering into {k} topics...", flush=True)
    return KMeans(n_clusters=k, random_state=42, n_init=4).fit_predict(xy), k


def label_clusters(texts, labels, k):
    names = {}
    for c in range(k):
        words = Counter()
        for i, lab in enumerate(labels):
            if lab == c:
                for w in re.findall(r"[a-zA-Z][a-zA-Z'-]{2,}", texts[i].lower()):
                    if w not in STOP:
                        words[w] += 1
        top = [w for w, _ in words.most_common(3)]
        names[c] = " / ".join(top) if top else f"cluster {c}"
    return names


def normalize(xy):
    xy = np.asarray(xy, dtype=float)
    mn, mx = xy.min(0), xy.max(0)
    span = np.where((mx - mn) == 0, 1, mx - mn)
    return (xy - mn) / span * 1000.0


# Demo classification: personal memories are grayed + text-stripped in /demo.html;
# public ones are shown. Always-hidden / always-shown sources are decided by
# heuristic; everything else is judged per-memory by the LLM and cached in the
# point payload (demo_personal) so it's a one-time cost.
ALWAYS_PERSONAL = {"imessage", "oura", "claude-chat", "claude-native-memory",
                   "cortex-brief", "foresight", "cortex-health", "chatgpt-backup"}
ALWAYS_PUBLIC = {"claude-code"}
PERSONAL_SOURCES = ALWAYS_PERSONAL  # fallback when a point has no cached verdict

# Lucide "lock" icon (inline, inherits text color) for the demo's private markers.
LOCK_SVG = ('<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" '
            'style="vertical-align:-2px;margin-right:5px"><rect width="18" height="11" x="3" y="11" '
            'rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>')

CLASSIFY_SYS = (
    "You triage memories from __CORTEX_USER__'s personal second brain for a PUBLIC demo they will "
    "show colleagues and strangers. For each numbered memory return \"P\" (personal) "
    "or \"U\" (usable/public).\n"
    "PERSONAL = health, mental health, medication, therapy, diagnoses, biometrics; "
    "relationships, family, friends' private lives; personal finances, investments, trading or collectibles portfolios (positions, prices, valuations, buy/sell plans); private feelings "
    "or venting; employer-internal/confidential specifics tied to real people.\n"
    "USABLE = product/design thinking, frameworks, methodology, technical notes, code, "
    "architecture, tooling, writing/book ideas, generic facts and how-to knowledge.\n"
    "When unsure, choose P. Privacy wins ties.\n"
    'Return ONLY JSON: {"labels":["P","U",...]} with one label per memory, in order.'
)
CLASSIFY_SYS = CLASSIFY_SYS.replace("__CORTEX_USER__", USER_NAME)
if PRIVATE_EXTRA:
    # inject into the definition body, not after the format spec (models
    # underweight trailing addenda)
    CLASSIFY_SYS = CLASSIFY_SYS.replace(
        "When unsure, choose P.",
        "ALSO PERSONAL for this user: " + PRIVATE_EXTRA + "\nWhen unsure, choose P.")


def _llm_personal(client, batch):
    """batch: list of (id, source, text) -> list of bool personal (True=personal)."""
    body = "\n".join(f"{i+1}. [{s}] {t}" for i, (_, s, t) in enumerate(batch))
    for attempt in range(4):
        try:
            r = client.chat.completions.create(
                model=os.getenv("CLASSIFY_MODEL", "gpt-4o-mini"),
                messages=[{"role": "system", "content": CLASSIFY_SYS},
                          {"role": "user", "content": body}],
                response_format={"type": "json_object"})
            labels = json.loads(r.choices[0].message.content).get("labels", [])
            return [(str(labels[i]).upper().startswith("P") if i < len(labels) else True)
                    for i in range(len(batch))]
        except Exception:
            if attempt == 3:
                return [True] * len(batch)  # default private on failure
            time.sleep(1.5 * (attempt + 1))


def _set_demo_personal(ids, flag):
    for i in range(0, len(ids), 256):
        # wait=true so the verdict is visible to the very next pull/render
        requests.post(f"{URL}/collections/{COLLECTION}/points/payload?wait=true",
                      json={"payload": {"demo_personal": flag}, "points": ids[i:i + 256]},
                      headers={"api-key": KEY}, timeout=60).raise_for_status()


def retire_tagged(pairs, log=print):
    """One-shot: permanently delete points matching (source, tag) pairs."""
    total = 0
    for src, tag in pairs:
        flt = {"must": [{"key": "user_id", "match": {"value": SCOPED_USER}},
                        {"key": "source", "match": {"value": src}},
                        {"key": "tags", "match": {"value": tag}}]}
        n = requests.post(f"{URL}/collections/{COLLECTION}/points/count",
                          json={"filter": flt, "exact": True},
                          headers={"api-key": KEY}, timeout=60).json()["result"]["count"]
        requests.post(f"{URL}/collections/{COLLECTION}/points/delete?wait=true",
                      json={"filter": flt}, headers={"api-key": KEY}, timeout=300).raise_for_status()
        log(f"retire: deleted {n} points (source={src}, tag={tag})")
        total += n
    return total


def classify_untagged(limit=None, log=print):
    """Scroll points lacking a demo_personal verdict, classify (heuristic + LLM),
    cache the verdict in the payload. Incremental + idempotent."""
    from concurrent.futures import ThreadPoolExecutor
    items, offset = [], None
    while True:
        body = {"limit": 1000, "with_payload": True, "with_vector": False,
                "filter": {"must": [{"key": "user_id", "match": {"value": SCOPED_USER}}]}}
        if offset is not None:
            body["offset"] = offset
        res = requests.post(f"{URL}/collections/{COLLECTION}/points/scroll", json=body,
                            headers={"api-key": KEY}, timeout=120).json()["result"]
        for p in res["points"]:
            pay = p.get("payload") or {}
            if "demo_personal" in pay:
                continue
            t = (pay.get("text") or "").strip()
            if t:
                items.append((p["id"], pay.get("source") or "unknown", t[:380].replace("\n", " ")))
        offset = res.get("next_page_offset")
        if offset is None or (limit and len(items) >= limit):
            break
    items = items[:limit] if limit else items
    if not items:
        log("classify: nothing untagged")
        return 0
    personal_ids, public_ids, llm = [], [], []
    for pid, src, txt in items:
        if src in ALWAYS_PERSONAL:
            personal_ids.append(pid)
        elif src in ALWAYS_PUBLIC:
            public_ids.append(pid)
        else:
            llm.append((pid, src, txt))
    log(f"classify: {len(items)} untagged | {len(personal_ids)} personal, "
        f"{len(public_ids)} public by source, {len(llm)} to LLM")
    # Persist heuristic verdicts immediately so the demo updates even before the LLM
    # finishes, and so progress survives a restart (next boot resumes incrementally).
    _set_demo_personal(personal_ids, True)
    _set_demo_personal(public_ids, False)
    tagged = len(personal_ids) + len(public_ids)
    if llm:
        try:
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        except Exception as e:
            log(f"classify: OpenAI unavailable ({type(e).__name__}); mixed left untagged")
            client = None
        if client:
            B = 40
            batches = [llm[i:i + B] for i in range(0, len(llm), B)]
            done = 0
            with ThreadPoolExecutor(max_workers=4) as ex:
                for batch, flags in zip(batches, ex.map(lambda b: _llm_personal(client, b), batches)):
                    p = [pid for (pid, _, _), f in zip(batch, flags) if f]
                    u = [pid for (pid, _, _), f in zip(batch, flags) if not f]
                    if p:
                        _set_demo_personal(p, True)
                    if u:
                        _set_demo_personal(u, False)
                    done += len(batch)
                    tagged += len(batch)
                    if done % 2000 < B:
                        log(f"classify: LLM {done}/{len(llm)}")
    log(f"classify: done, tagged {tagged} points total")
    return tagged


def retire_sources(sources, log=print):
    """Permanently delete every point for the given sources (scoped to the user).
    Used to drop a redundant raw layer (e.g. chatgpt-backup) to cut Qdrant RAM + storage.
    Irreversible; gated behind the RETIRE_SOURCES env var on the map service."""
    removed = 0
    for src in sources:
        flt = {"must": [{"key": "source", "match": {"value": src}},
                        {"key": "user_id", "match": {"value": SCOPED_USER}}]}
        try:
            c = requests.post(f"{URL}/collections/{COLLECTION}/points/count",
                              json={"filter": flt, "exact": True}, headers={"api-key": KEY}, timeout=60)
            n = int(c.json().get("result", {}).get("count", 0))
        except Exception:
            n = 0
        log(f"retire: deleting source={src} ({n} points)")
        r = requests.post(f"{URL}/collections/{COLLECTION}/points/delete?wait=true",
                          json={"filter": flt}, headers={"api-key": KEY}, timeout=180)
        r.raise_for_status()
        removed += n
    return removed


def build_html(norm, texts, sources, types, dates, tss, labels, cluster_names, out, personal=None, demo=False):
    # In demo mode only PERSONAL memories are stripped + grayed; public memories keep
    # their text (feed, hover, topics stay populated). `personal` is the per-memory
    # verdict from classify_untagged(); fall back to a source heuristic if absent.
    if personal is None:
        personal = [sources[i] in PERSONAL_SOURCES for i in range(len(texts))]
    # deterministic belt over the LLM verdicts: anything matching
    # CORTEX_PRIVATE_REGEX renders personal, including future captures
    priv_re = os.getenv("CORTEX_PRIVATE_REGEX", "").strip()
    if priv_re:
        try:
            rx = re.compile(priv_re, re.I)
            personal = [bool(personal[i]) or bool(rx.search(texts[i])) for i in range(len(texts))]
        except re.error as e:
            print(f"  bad CORTEX_PRIVATE_REGEX ignored: {e}", flush=True)
    pts = []
    for i in range(len(texts)):
        pers = 1 if personal[i] else 0
        hide = bool(demo and pers)
        pts.append([round(float(norm[i, 0]), 1), round(float(norm[i, 1]), 1), sources[i],
                    int(labels[i]), ("" if hide else texts[i]), ("" if hide else dates[i]), pers])
    # newest 60 for the Recent feed; in demo, restrict to public BEFORE taking the
    # top 60 (else recent-but-personal memories crowd out the public ones -> empty feed)
    cand = [i for i in range(len(texts)) if not (demo and personal[i])]
    order = sorted(cand, key=lambda i: tss[i], reverse=True)[:60]
    feed = [{"i": i, "s": sources[i], "t": texts[i], "d": dates[i]} for i in order if tss[i] > 0]
    src_counts = Counter(sources)
    data = {"points": pts, "sources": [s for s, _ in src_counts.most_common()],
            "clusterNames": cluster_names, "total": len(pts),
            "srcCounts": dict(src_counts), "feed": feed, "demo": demo}
    brief_url = os.getenv("BRIEF_URL") or os.getenv("CORTEX_BRIEF_URL", "")
    if demo:
        brief_col = '<div class="empty">' + LOCK_SVG + 'The daily brief and foresight are private to the full Cortex.</div>'
        toggle_btn = ('<a class="btn viewfull" href="login.html" style="margin-left:auto;text-decoration:none;'
                      'pointer-events:auto;background:linear-gradient(135deg,#ff8a5c,#7c5cff);color:#fff;'
                      'border-color:transparent;font-weight:600;box-shadow:0 4px 14px rgba(124,92,255,.4)">'
                      + LOCK_SVG + 'view full Cortex</a>')
    else:
        _ds = os.getenv("DEMO_PATH_SECRET", "")
        _demo_href = f"/{_ds}/demo.html" if _ds else "demo.html"
        toggle_btn = ('<a class="btn" href="' + _demo_href + '" target="_blank" rel="noopener" '
                      'style="margin-left:auto;text-decoration:none;pointer-events:auto">◇ share view</a>')
        if brief_url:
            brief_embed = brief_url.replace("brief.html", "embed.html")
            fore_embed = brief_url.replace("brief.html", "foresight-embed.html")
            brief_col = (
                '<div class="btabs"><div class="btab on" data-src="' + brief_embed + '">daily brief</div>'
                '<div class="btab" data-src="' + fore_embed + '">foresight</div></div>'
                '<iframe id="briefFrame" src="' + brief_embed + '" title="daily brief"></iframe>'
            )
        else:
            brief_col = '<div class="empty">No brief yet. Set BRIEF_URL to embed the daily brief here.</div>'
    html = (HTML_TEMPLATE
            .replace("/*DATA*/", json.dumps(data, separators=(",", ":")))
            .replace("<!--BRIEF_COL-->", brief_col)
            .replace("<!--TOGGLE_BTN-->", toggle_btn))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(html)
    print(f"wrote {out} ({len(pts)} points, demo={demo}, {Path(out).stat().st_size//1024}KB)", flush=True)


HTML_TEMPLATE = r"""<!doctype html><html><head><meta charset="utf-8"><title>CortexHost</title>
<style>
 :root{--bg:#07070d;--accent:#ff8a5c;--accent2:#7c5cff;--cyan:#5cc8ff;--panel:#0b0b14;--bord:#23233a;--txt:#eef;--mut:#8a8aa2;--mono:ui-monospace,'SF Mono',Menlo,monospace}
 *{box-sizing:border-box} html,body{margin:0;height:100%;background:var(--bg);color:var(--txt);font:13px -apple-system,system-ui,sans-serif;overflow:hidden}
 #c{display:block;position:fixed;inset:0;cursor:grab} #c:active{cursor:grabbing}
 /* HUD corner brackets */
 .corner{position:fixed;width:26px;height:26px;border:2px solid var(--accent);opacity:.35;z-index:4;pointer-events:none;animation:cpulse 4s ease-in-out infinite}
 .corner.tl{top:14px;left:14px;border-right:0;border-bottom:0} .corner.tr{top:14px;right:14px;border-left:0;border-bottom:0}
 .corner.bl{bottom:14px;left:14px;border-right:0;border-top:0} .corner.br{bottom:14px;right:14px;border-left:0;border-top:0}
 @keyframes cpulse{0%,100%{opacity:.18}50%{opacity:.45}}
 /* scanline texture */
 #scan{position:fixed;inset:0;pointer-events:none;z-index:3;opacity:.5;
   background:repeating-linear-gradient(0deg,#ffffff03 0,#ffffff03 1px,transparent 1px,transparent 3px);animation:scanmove 8s linear infinite}
 @keyframes scanmove{from{background-position-y:0}to{background-position-y:60px}}
 #hud{position:fixed;top:0;left:0;right:0;padding:18px 26px;display:flex;gap:16px;align-items:center;z-index:8;
   background:linear-gradient(#07070df0,#07070d00);pointer-events:none;animation:fadeDown .7s ease both}
 @keyframes fadeDown{from{opacity:0;transform:translateY(-10px)}to{opacity:1;transform:none}}
 .brand{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;
   background:linear-gradient(90deg,var(--accent),#ffd0a0,var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent;
   background-size:200% auto;animation:sheen 6s linear infinite}
 @keyframes sheen{to{background-position:200% center}}
 .brainico{width:22px;height:22px;flex:none;color:var(--accent);filter:drop-shadow(0 0 6px #ff8a5c66);animation:brainpulse 4s ease-in-out infinite}
 @keyframes brainpulse{0%,100%{filter:drop-shadow(0 0 4px #ff8a5c44)}50%{filter:drop-shadow(0 0 11px #ff8a5cbb)}}
 #briefPanel{position:fixed;top:64px;left:14px;bottom:14px;width:480px;background:rgba(12,12,22,0.5);border:1px solid rgba(255,255,255,0.1);border-radius:14px;z-index:7;display:flex;flex-direction:column;overflow:hidden;box-shadow:8px 0 50px #0008,0 0 0 1px #7c5cff10,inset -1px 0 0 #ff8a5c22;backdrop-filter:blur(18px) saturate(1.2);-webkit-backdrop-filter:blur(18px) saturate(1.2);animation:fadeLf .7s ease both}
 @keyframes fadeLf{from{opacity:0;transform:translateX(-20px)}to{opacity:1;transform:none}}
 #briefPanel .bhead{padding:14px 16px;font-family:var(--mono);font-size:11px;letter-spacing:.6px;text-transform:uppercase;color:var(--accent);border-bottom:1px solid var(--bord);display:flex;align-items:center;gap:8px;flex:none}
 #briefPanel iframe{flex:1;border:0;width:100%;background:transparent}
 .btabs{display:flex;gap:8px;padding:12px 14px 0;flex:none}
 .btab{flex:1;text-align:center;padding:8px 6px;border-radius:9px 9px 0 0;cursor:pointer;color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px;border-bottom:2px solid transparent;transition:.15s}
 .btab:hover{color:#cfcfe0} .btab.on{color:#fff;border-image:linear-gradient(90deg,var(--accent),var(--accent2)) 1;border-bottom:2px solid;background:#ffffff06}
 #briefResize{position:absolute;top:0;right:-2px;width:9px;height:100%;cursor:ew-resize;z-index:9;transition:background .15s} #briefResize:hover{background:linear-gradient(90deg,transparent,#ff8a5c66)}
 #briefPanel .empty{padding:18px;color:var(--mut);line-height:1.5}
 .chips{display:flex;gap:9px;margin-left:6px}
 .chip{font-family:var(--mono);background:#10101cc0;border:1px solid var(--bord);border-radius:7px;padding:5px 11px;font-size:11px;color:var(--mut);box-shadow:inset 0 0 14px #7c5cff10}
 .chip b{color:var(--txt);font-weight:600} .chip b.a{color:var(--accent)}
 .ctrls{position:fixed;top:14px;left:50%;transform:translateX(-50%);display:flex;gap:9px;z-index:9;pointer-events:auto}
 #search{font-family:var(--mono);background:#10101cd0;border:1px solid var(--bord);color:var(--txt);padding:9px 13px;border-radius:9px;width:240px;outline:none;transition:.2s}
 #search:focus{border-color:var(--accent);box-shadow:0 0 0 3px #ff8a5c22,0 0 20px #ff8a5c22}
 .btn{font-family:var(--mono);font-size:12px;background:#10101cd0;border:1px solid var(--bord);color:#cfcfe0;padding:9px 13px;border-radius:9px;cursor:pointer;white-space:nowrap;user-select:none;transition:.18s}
 .btn:hover{border-color:#4a4a66;color:#fff} .btn.on{border-color:transparent;color:#fff;background:linear-gradient(135deg,#ff8a5c,#7c5cff);box-shadow:0 0 18px #ff8a5c44}
 #legendWrap{position:fixed;bottom:18px;left:510px;right:372px;display:flex;flex-direction:column;align-items:flex-start;gap:9px;z-index:5;animation:fadeUp .8s ease both}
 #legendBar{width:100%;background:rgba(12,12,22,0.5);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:12px 14px;backdrop-filter:blur(18px) saturate(1.2);-webkit-backdrop-filter:blur(18px) saturate(1.2);box-shadow:0 8px 40px #0008,0 0 0 1px #7c5cff10}
 #legend{display:flex;flex-wrap:wrap;gap:8px;max-width:none;max-height:118px;overflow:auto}
 @keyframes fadeUp{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
 #legend .l{display:flex;align-items:center;gap:7px;background:#ffffff0d;border:1px solid #ffffff14;padding:6px 11px;border-radius:20px;font-size:11.5px;cursor:pointer;user-select:none;transition:.15s}
 #legend .l:hover{background:#ffffff1f;border-color:#ffffff2e} #legend .l.off{opacity:.3} #legend .l i{width:9px;height:9px;border-radius:3px;box-shadow:0 0 8px currentColor}
 #legend .l b{color:var(--mut);font-weight:500;font-family:var(--mono)}
 #tip{position:fixed;max-width:330px;background:#0c0c16ee;border:1px solid #34344e;border-radius:10px;padding:11px 13px;font-size:12px;line-height:1.45;pointer-events:none;opacity:0;transition:opacity .1s;z-index:11;box-shadow:0 12px 40px #000c,0 0 0 1px #7c5cff18;backdrop-filter:blur(8px)}
 #tip .s{font-family:var(--mono);color:var(--accent);font-size:10px;text-transform:uppercase;letter-spacing:.6px;font-weight:700} #tip .d{opacity:.45;font-size:10.5px;margin-top:5px;font-family:var(--mono)}
 #labels{position:fixed;inset:0;pointer-events:none;z-index:4}
 #labels div{position:absolute;transform:translate(-50%,-50%);font-size:12px;font-weight:650;letter-spacing:.3px;color:#fff;text-shadow:0 0 10px #000,0 0 4px #000,0 1px 2px #000;white-space:nowrap}
 #panel{position:fixed;top:64px;right:14px;bottom:14px;width:344px;background:rgba(12,12,22,0.5);border:1px solid rgba(255,255,255,0.1);border-radius:14px;
   z-index:7;display:flex;flex-direction:column;transition:transform .26s cubic-bezier(.4,0,.2,1);overflow:hidden;
   box-shadow:-8px 0 50px #0008,0 0 0 1px #7c5cff10,inset 1px 0 0 #ff8a5c22;backdrop-filter:blur(18px) saturate(1.2);-webkit-backdrop-filter:blur(18px) saturate(1.2);animation:fadeRt .7s ease both}
 @keyframes fadeRt{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:none}}
 #panel.hidden{transform:translateX(372px)}
 .tabs{display:flex;padding:14px 14px 0;gap:8px}
 .tab{flex:1;display:flex;align-items:center;justify-content:center;gap:6px;padding:9px;border-radius:9px 9px 0 0;cursor:pointer;color:var(--mut);font-weight:600;font-size:12px;border-bottom:2px solid transparent;transition:.15s}
 .tabico{width:14px;height:14px;flex:none}
 .tab:hover{color:#cfcfe0} .tab.on{color:#fff;border-image:linear-gradient(90deg,var(--accent),var(--accent2)) 1;border-bottom:2px solid;background:#ffffff06}
 .phead{padding:10px 16px;color:var(--mut);font-size:11px;font-family:var(--mono);display:flex;align-items:center;gap:8px;text-transform:uppercase;letter-spacing:.5px}
 .phead .clr{margin-left:auto;color:var(--accent);cursor:pointer;display:none;text-transform:none} .phead .clr.show{display:inline}
 #plist{overflow-y:auto;padding:2px 0 16px} #plist::-webkit-scrollbar{width:7px}#plist::-webkit-scrollbar-thumb{background:#2a2a44;border-radius:4px}
 .fi{padding:12px 16px;border-bottom:1px solid #13131f;cursor:pointer;transition:.12s;position:relative}
 .fi:hover{background:#13131f} .fi:hover::before{content:'';position:absolute;left:0;top:0;bottom:0;width:2px;background:linear-gradient(var(--accent),var(--accent2))}
 .fi .h{display:flex;align-items:center;gap:7px;margin-bottom:5px} .fi .h i{width:8px;height:8px;border-radius:2px;flex:none;box-shadow:0 0 6px currentColor}
 .fi .h .src{font-family:var(--mono);font-size:9.5px;text-transform:uppercase;letter-spacing:.5px;color:var(--mut);font-weight:700}
 .fi .h .dt{margin-left:auto;font-size:10px;opacity:.4;font-family:var(--mono)} .fi .x{font-size:12px;line-height:1.45;color:#d6d6e4}
 .ti{padding:12px 16px;border-bottom:1px solid #13131f;cursor:pointer;display:flex;align-items:center;gap:11px;transition:.12s} .ti:hover{background:#13131f}
 .ti i{width:11px;height:11px;border-radius:3px;flex:none;box-shadow:0 0 8px currentColor} .ti .nm{font-size:12.5px;color:#e2e2f0;text-transform:capitalize} .ti .ct{margin-left:auto;color:var(--mut);font-size:11px;font-family:var(--mono)}
 #card{position:fixed;left:510px;bottom:70px;max-width:360px;background:#0c0c16f5;border:1px solid #34344e;border-radius:13px;padding:16px 18px;z-index:10;
   box-shadow:0 18px 60px #000d,0 0 0 1px #ff8a5c22;display:none;backdrop-filter:blur(10px);animation:pop .2s ease}
 @keyframes pop{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}
 #card .s{font-family:var(--mono);color:var(--accent);font-size:10px;text-transform:uppercase;letter-spacing:.6px;font-weight:700;display:flex;gap:8px;align-items:center}
 #card .s .topic{color:var(--mut);text-transform:none;letter-spacing:0} #card .x{margin:9px 0;font-size:13.5px;line-height:1.55} #card .d{opacity:.45;font-size:11px;font-family:var(--mono)}
 #card .cl{position:absolute;top:11px;right:13px;cursor:pointer;color:var(--mut)} #card .cl:hover{color:#fff}
</style></head><body>
<canvas id="c"></canvas><div id="scan"></div>
<div class="corner tl"></div><div class="corner tr"></div><div class="corner bl"></div><div class="corner br"></div>
<div id="labels"></div>
<div id="hud"><div class="brand"><svg class="brainico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M17.599 6.5a3 3 0 0 0 .399-1.375"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M19.938 10.5a4 4 0 0 1 .585.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M19.967 17.484A4 4 0 0 1 18 18"/></svg>CortexHost</div>
 <div class="chips"><div class="chip"><b class="a" id="cMem">0</b> memories</div><div class="chip"><b id="cSrc">0</b> sources</div><div class="chip"><b id="cTop">0</b> topics</div></div>
 <!--TOGGLE_BTN-->
 <a class="btn" id="howlink" href="how.html" target="_blank" rel="noopener" style="margin-left:8px;text-decoration:none;pointer-events:auto">ⓘ how it works</a>
</div>
<div id="legendWrap"><div class="btn" id="toggle">◐ source</div><div id="legendBar"><div id="legend"></div></div></div>
<div id="briefPanel"><!--BRIEF_COL--><div id="briefResize"></div></div>
<div id="panel"><div class="tabs"><div class="tab on" data-m="recent"><svg class="tabico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Recent</div><div class="tab" data-m="topics"><svg class="tabico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/></svg>Topics</div></div>
 <div class="phead"><span id="phLabel">60 newest</span><span class="clr" id="clr">clear ✕</span></div><div id="plist"></div></div>
<div id="tip"></div>
<div id="card"><span class="cl" id="cardClose">✕</span><div class="s"><span id="cardSrc"></span><span class="topic" id="cardTopic"></span></div><div class="x" id="cardText"></div><div class="d" id="cardDate"></div></div>
<script>
const D=/*DATA*/;const DEMO=D.demo;
const cv=document.getElementById('c'),ctx=cv.getContext('2d'),tip=document.getElementById('tip'),lab=document.getElementById('labels');
const buf=document.createElement('canvas'),bx=buf.getContext('2d');
function genColor(i){const h=(20+i*137.508)%360;const l=56+((i*47)%20);return 'hsl('+(h|0)+',72%,'+l+'%)'}
const DPR=devicePixelRatio||1;
let W,H,view={x:0,y:0,s:1},target={x:0,y:0,s:1},colorBy='source',q='',flash=null,flashT=0,pmode='recent',focusC=null,dirty=true,t0=performance.now();
const hidden=new Set();
const srcColor={};D.sources.forEach((s,i)=>srcColor[s]=genColor(i));
function colorOf(p){if(DEMO&&p[6])return '#34343e';return colorBy==='source'?srcColor[p[2]]:genColor(p[3])}
function vis(p){return !hidden.has(p[2])&&(focusC===null||p[3]===focusC)}
function resize(){W=cv.width=buf.width=innerWidth*DPR;H=cv.height=buf.height=innerHeight*DPR;cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px';dirty=true}
addEventListener('resize',resize);
function tx(x){return x*view.s+view.x} function ty(y){return y*view.s+view.y}
function renderPoints(){
 bx.clearRect(0,0,W,H);const r=Math.max(1,1.25*view.s),ql=q.toLowerCase();
 const groups={};for(const p of D.points){if(!vis(p))continue;(groups[colorOf(p)]=groups[colorOf(p)]||[]).push(p)}
 if(!q){const rr=r*3.6;for(const c in groups){bx.fillStyle=c;bx.globalAlpha=0.045;for(const p of groups[c])bx.fillRect(tx(p[0])-rr,ty(p[1])-rr,rr*2,rr*2)}}
 for(const c in groups){bx.fillStyle=c;bx.globalAlpha=q?0.1:0.85;for(const p of groups[c]){if(q&&p[4].toLowerCase().includes(ql))continue;bx.fillRect(tx(p[0])-r,ty(p[1])-r,r*2,r*2)}}
 if(q){bx.globalAlpha=1;for(const p of D.points){if(!vis(p)||!p[4].toLowerCase().includes(ql))continue;bx.fillStyle=colorOf(p);const rr=r*1.9;bx.fillRect(tx(p[0])-rr,ty(p[1])-rr,rr*2,rr*2)}}
 bx.globalAlpha=1;
}
function frame(ts){
 let moving=false;for(const k of['x','y','s']){const d=target[k]-view[k];if(Math.abs(d)>(k==='s'?1e-4:0.4)){view[k]+=d*0.2;moving=true}else view[k]=target[k]}
 if(moving)dirty=true;if(dirty){renderPoints();dirty=false}
 const g=ctx.createRadialGradient(W*0.5,H*0.42,0,W*0.5,H*0.42,Math.max(W,H)*0.8);
 g.addColorStop(0,'#0d0d1c');g.addColorStop(.55,'#08080f');g.addColorStop(1,'#050508');ctx.fillStyle=g;ctx.fillRect(0,0,W,H);
 const li=Math.min(1,(ts-t0)/1300);ctx.globalAlpha=li;ctx.drawImage(buf,0,0);ctx.globalAlpha=1;
 const sx=((ts/9000)%1.4-0.2)*W,sw=160*DPR;const sg=ctx.createLinearGradient(sx-sw,0,sx+sw,0);
 sg.addColorStop(0,'#ff8a5c00');sg.addColorStop(.5,'#ff8a5c0e');sg.addColorStop(1,'#ff8a5c00');ctx.fillStyle=sg;ctx.fillRect(sx-sw,0,sw*2,H);
 if(flash!=null){const a=Math.max(0,1-(ts-flashT)/1500);if(a>0){const p=D.points[flash];ctx.strokeStyle='rgba(255,220,200,'+a+')';ctx.lineWidth=2*DPR;ctx.beginPath();ctx.arc(tx(p[0]),ty(p[1]),(6+32*(1-a))*DPR,0,7);ctx.stroke()}else flash=null}
 drawLabels();requestAnimationFrame(frame);
}
function clusters(){if(window._cl)return window._cl;const m={};for(let i=0;i<D.points.length;i++){const p=D.points[i],c=p[3];(m[c]=m[c]||{members:[],sx:0,sy:0}).members.push(i);m[c].sx+=p[0];m[c].sy+=p[1]}
 window._cl=Object.entries(m).map(([c,o])=>({c:+c,name:D.clusterNames[c]||('topic '+c),n:o.members.length,cx:o.sx/o.members.length,cy:o.sy/o.members.length,members:o.members})).sort((a,b)=>b.n-a.n);return window._cl}
function drawLabels(){lab.innerHTML='';if(colorBy!=='cluster')return;for(const cl of (focusC!==null?clusters():clusters().slice(0,24))){if(focusC!==null&&cl.c!==focusC)continue;
 const d=document.createElement('div');d.textContent=cl.name;d.style.left=tx(cl.cx)/DPR+'px';d.style.top=ty(cl.cy)/DPR+'px';lab.appendChild(d)}}
let drag=null,moved=false;
cv.addEventListener('mousedown',e=>{drag=[e.clientX,e.clientY,view.x,view.y];moved=false});
addEventListener('mouseup',()=>drag=null);
addEventListener('mousemove',e=>{if(drag){view.x=target.x=drag[2]+(e.clientX-drag[0])*DPR;view.y=target.y=drag[3]+(e.clientY-drag[1])*DPR;moved=true;dirty=true;return}hover(e)});
cv.addEventListener('wheel',e=>{e.preventDefault();const f=e.deltaY<0?1.14:0.88;const mx=e.clientX*DPR,my=e.clientY*DPR;target.x=mx-(mx-target.x)*f;target.y=my-(my-target.y)*f;target.s*=f},{passive:false});
function nearest(e){const mx=e.clientX*DPR,my=e.clientY*DPR;let b=-1,bd=16*16*DPR*DPR;for(let i=0;i<D.points.length;i++){const p=D.points[i];if(!vis(p))continue;const dx=tx(p[0])-mx,dy=ty(p[1])-my,d=dx*dx+dy*dy;if(d<bd){bd=d;b=i}}return b}
function hover(e){const i=nearest(e);if(i>=0){const p=D.points[i];tip.style.opacity=1;tip.style.left=Math.min(e.clientX+15,innerWidth-345)+'px';tip.style.top=(e.clientY+15)+'px';tip.innerHTML='<div class="s" style="color:'+colorOf(p)+'">'+p[2]+'</div>'+esc(p[4])+'<div class="d">'+(p[5]||'')+'</div>'}else tip.style.opacity=0}
cv.addEventListener('click',e=>{if(moved)return;const i=nearest(e);if(i>=0)showCard(i)});
function showCard(i){const p=D.points[i];cardSrc.textContent=p[2];cardSrc.style.color=colorOf(p);cardTopic.textContent='· '+(D.clusterNames[p[3]]||'');cardText.textContent=p[4];cardDate.textContent=p[5]||'';document.getElementById('card').style.display='block';flash=i;flashT=performance.now()}
document.getElementById('cardClose').onclick=()=>document.getElementById('card').style.display='none';
function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function flyTo(x,y,s){target.s=(s||2.4)*DPR;target.x=W/2-x*target.s;target.y=H/2-y*target.s}
function flyToPt(i){const p=D.points[i];flyTo(p[0],p[1],2.8);flash=i;flashT=performance.now()}
document.getElementById('toggle').addEventListener('click',e=>{colorBy=colorBy==='source'?'cluster':'source';e.target.textContent=(colorBy==='source'?'◐ source':'◑ topic');buildLegend();dirty=true});
document.querySelectorAll('.tab').forEach(t=>t.addEventListener('click',()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('on'));t.classList.add('on');pmode=t.dataset.m;focusC=null;renderPanel();dirty=true}));
document.getElementById('clr').addEventListener('click',()=>{focusC=null;renderPanel();dirty=true});
function buildLegend(){const L=document.getElementById('legend');if(colorBy!=='source'){L.innerHTML='<div class="l" style="cursor:default"><b>topics labeled on map · scroll to zoom · drag to pan</b></div>';return}
 L.innerHTML='';for(const s of D.sources){const el=document.createElement('div');el.className='l'+(hidden.has(s)?' off':'');el.innerHTML='<i style="background:'+srcColor[s]+';color:'+srcColor[s]+'"></i>'+s+' <b>'+D.srcCounts[s]+'</b>';
  el.onclick=()=>{hidden.has(s)?hidden.delete(s):hidden.add(s);buildLegend();dirty=true};L.appendChild(el)}}
function fiHTML(i){const p=D.points[i];return '<div class="fi" data-i="'+i+'"><div class="h"><i style="background:'+(srcColor[p[2]]||'#888')+';color:'+(srcColor[p[2]]||'#888')+'"></i><span class="src">'+p[2]+'</span><span class="dt">'+(p[5]||'')+'</span></div><div class="x">'+esc(p[4])+'</div></div>'}
function bindFi(){[...document.querySelectorAll('#plist .fi')].forEach(el=>el.onclick=()=>{const i=+el.dataset.i;flyToPt(i);showCard(i)})}
function renderPanel(){const L=document.getElementById('plist'),ph=document.getElementById('phLabel'),clr=document.getElementById('clr');clr.classList.remove('show');
 if(q){const ql=q.toLowerCase(),hits=[];for(let i=0;i<D.points.length;i++)if(D.points[i][4].toLowerCase().includes(ql)){hits.push(i);if(hits.length>=200)break}
  ph.textContent=hits.length+(hits.length>=200?'+ matches':' matches');L.innerHTML=hits.map(fiHTML).join('');bindFi();return}
 if(pmode==='topics'&&focusC===null){ph.textContent=clusters().length+' topics';
  L.innerHTML=clusters().map(cl=>'<div class="ti" data-c="'+cl.c+'"><i style="background:'+genColor(cl.c)+';color:'+genColor(cl.c)+'"></i><span class="nm">'+esc(cl.name)+'</span><span class="ct">'+cl.n+'</span></div>').join('');
  [...L.querySelectorAll('.ti')].forEach(el=>el.onclick=()=>{focusC=+el.dataset.c;if(colorBy!=='cluster'){colorBy='cluster';document.getElementById('toggle').textContent='◑ topic';buildLegend()}const cl=clusters().find(c=>c.c===focusC);flyTo(cl.cx,cl.cy,1.7);renderPanel();dirty=true});return}
 if(pmode==='topics'&&focusC!==null){const cl=clusters().find(c=>c.c===focusC);ph.textContent=cl.name+' · '+cl.n;clr.classList.add('show');L.innerHTML=cl.members.slice(0,200).map(fiHTML).join('');bindFi();return}
 ph.textContent=D.feed.length+' newest';L.innerHTML=D.feed.map(f=>fiHTML(f.i)).join('');bindFi();
}
cMem.textContent=D.total.toLocaleString();cSrc.textContent=D.sources.length;cTop.textContent=clusters().length;
resize();target.s=view.s=Math.min(innerWidth,innerHeight)/1150*DPR;target.x=view.x=W/2-500*view.s;target.y=view.y=H/2-500*view.s;
buildLegend();renderPanel();requestAnimationFrame(frame);
document.querySelectorAll('#briefPanel .btab').forEach(t=>t.addEventListener('click',()=>{document.querySelectorAll('#briefPanel .btab').forEach(x=>x.classList.remove('on'));t.classList.add('on');const f=document.getElementById('briefFrame');if(f)f.src=t.dataset.src}));
(function(){const bp=document.getElementById('briefPanel');if(!bp)return;const h=document.getElementById('briefResize'),lw=document.getElementById('legendWrap'),cd=document.getElementById('card'),ifr=bp.querySelector('iframe');
 function layout(){const w=bp.offsetWidth,L=(14+w+16)+'px';if(lw)lw.style.left=L;if(cd)cd.style.left=L}
 function aw(w){bp.style.width=Math.max(300,Math.min(innerWidth-420,w))+'px';layout()}
 const sv=+localStorage.getItem('briefW');if(sv)bp.style.width=Math.max(300,Math.min(innerWidth-420,sv))+'px';layout();
 addEventListener('resize',layout);let rs=null;
 h.addEventListener('mousedown',e=>{rs={x:e.clientX,w:bp.offsetWidth};if(ifr)ifr.style.pointerEvents='none';document.body.style.cursor='ew-resize';e.preventDefault()});
 addEventListener('mousemove',e=>{if(rs)aw(rs.w+(e.clientX-rs.x))});
 addEventListener('mouseup',()=>{if(rs){localStorage.setItem('briefW',bp.offsetWidth);rs=null;if(ifr)ifr.style.pointerEvents='';document.body.style.cursor=''}});})();
</script></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "viz" / "brain_map.html"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--topics", type=int, default=50, help="number of topic clusters")
    ap.add_argument("--render-only", action="store_true", help="rebuild HTML from cache (no pull/projection)")
    ap.add_argument("--recluster", action="store_true", help="re-cluster cached projection with --topics (no re-pull)")
    ap.add_argument("--demo", action="store_true", help="redacted demo render (strip text, gray personal sources)")
    args = ap.parse_args()

    if args.render_only or args.recluster:
        c = json.loads(CACHE.read_text())
        norm = np.array(c["norm"])
        if args.recluster:
            print(f"reclustering cached {len(c['texts'])} points into {args.topics} topics...", flush=True)
            labels, k = cluster(norm, args.topics)
            names = label_clusters(c["texts"], labels, k)
            c["labels"] = [int(x) for x in labels]
            c["cluster_names"] = names
            CACHE.write_text(json.dumps(c))
        names = {int(k): v for k, v in c["cluster_names"].items()}
        build_html(norm, c["texts"], c["sources"], c["types"], c["dates"], c["tss"],
                   c["labels"], names, args.out, personal=c.get("personal"), demo=args.demo)
        return

    print("pulling corpus...", flush=True)
    vecs, texts, sources, types, dates, tss, personal = pull(args.limit)
    print(f"pulled {len(texts)} memories", flush=True)
    norm = normalize(project(vecs))
    labels, k = cluster(norm, args.topics)
    names = label_clusters(texts, labels, k)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"norm": norm.tolist(), "texts": texts, "sources": sources,
                                 "types": types, "dates": dates, "tss": tss, "personal": personal,
                                 "labels": [int(x) for x in labels], "cluster_names": names}))
    print(f"cached projection -> {CACHE}", flush=True)
    build_html(norm, texts, sources, types, dates, tss, labels, names, args.out, personal=personal, demo=args.demo)


if __name__ == "__main__":
    main()
