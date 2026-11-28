import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Empty, ErrorNote, SectionTitle, Segmented, timeAgo, unwrap } from "../ui.jsx";

const TOOL_TEXT = {
  ask_about_owner: "Answers from your profile, word for word",
  get_owner_take: "Returns a take you approved, word for word",
  check_availability: "Free/busy blocks only, never event details",
  request_meeting: "Files a request in your inbox",
  leave_message: "Files a message in your inbox",
  check_request: "Requester checks their own request, token-gated",
};

// Fixed by design, not configuration: the gateway process holds no credentials for these.
const NEVER = [
  "Your memory corpus (no Qdrant or embedding keys in the gateway)",
  "Private notes, decisions, to-dos and commitments",
  "Calendar event titles, attendees or locations",
  "Any LLM key: the gateway never runs a model",
  "Anything said yes to on your behalf: every commitment waits for you",
];

export default function PublicView({ onChange }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const load = () => api.publicView().then((d) => { setData(d); setError(null); }).catch(setError);
  useEffect(() => { load(); }, []);

  if (error) return <ErrorNote error={error} />;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <SectionTitle title="What visiting agents can get" sub="The entire public surface. Nothing else exists to call." />
          <ul className="space-y-2">
            {data.tools.map((t) => (
              <li key={t.name} className="flex items-start justify-between gap-3 text-sm">
                <div>
                  <div className="mono text-xs">{t.name}</div>
                  <div className="text-neutral-600 dark:text-neutral-400">
                    {TOOL_TEXT[t.name] || ""}
                    {t.name === "check_availability" && !data.availability_connected && " (calendar not connected)"}
                    {t.name === "get_owner_take" && data.takes.sections.length === 0 && " (no takes published)"}
                  </div>
                </div>
                <Badge tone={t.permission === "auto" ? "info" : "warn"} title={t.permission === "auto" ? "Answers without you" : "Waits for you"}>
                  {t.permission === "auto" ? "answers now" : "waits for you"}
                </Badge>
              </li>
            ))}
          </ul>
        </Card>
        <Card>
          <SectionTitle title="What never crosses" sub="Not hidden by a prompt. The gateway can't reach it." />
          <ul className="space-y-2 text-sm">
            {NEVER.map((n) => (
              <li key={n} className="flex gap-2"><span aria-hidden className="text-emerald-600">✓</span>{n}</li>
            ))}
          </ul>
        </Card>
      </div>

      <AskAsStranger owner={data.owner} />

      <DocEditor name="profile" title="Public profile" doc={data.profile}
        sub="ask_about_owner answers with whole sections of this. Each ## heading is a section."
        onSaved={(d) => { setData({ ...data, profile: d }); onChange?.(); }} />
      <DocEditor name="takes" title="Your takes" doc={data.takes}
        sub="get_owner_take returns these word for word. No match means no answer, never an improvised one."
        onSaved={(d) => { setData({ ...data, takes: d }); onChange?.(); }} />
    </div>
  );
}

function AskAsStranger({ owner }) {
  const [q, setQ] = useState("");
  const [res, setRes] = useState(null);
  const [error, setError] = useState(null);
  const run = (e) => {
    e.preventDefault();
    if (!q.trim()) return;
    api.preview(q).then((r) => { setRes(r); setError(null); }).catch(setError);
  };
  return (
    <Card>
      <SectionTitle title="Ask as a stranger" sub={`Type what another agent might ask. You'll see exactly what it would get back about ${owner}. Not logged as visitor traffic.`} />
      <form onSubmit={run} className="flex gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="What is he working on right now?"
          className="min-w-0 flex-1 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-sm outline-none focus:border-neutral-500 dark:border-neutral-700 dark:bg-neutral-950" />
        <Button variant="primary" type="submit">Preview</Button>
      </form>
      <ErrorNote error={error} />
      {res && (
        <div className="mt-3 grid gap-3 md:grid-cols-2">
          <Answer label="ask_about_owner returns" items={res.ask_about_owner} empty="Nothing (no profile published)" />
          <Answer label="get_owner_take returns" items={res.get_owner_take} empty="No approved take matches, so nothing. The agent is told to leave a message." />
        </div>
      )}
    </Card>
  );
}

function Answer({ label, items, empty }) {
  return (
    <div>
      <div className="mb-1 text-xs font-medium text-neutral-500">{label}</div>
      {items.length === 0 ? <p className="text-sm text-neutral-500">{empty}</p> : items.map((s) => (
        <div key={s.title} className="mb-2 rounded-lg bg-neutral-50 p-3 text-sm dark:bg-neutral-950">
          <div className="font-medium">{s.title}</div>
          <p className="mt-1 whitespace-pre-wrap text-neutral-700 dark:text-neutral-300">{unwrap(s.text)}</p>
        </div>
      ))}
    </div>
  );
}

function DocEditor({ name, title, sub, doc, onSaved }) {
  const [mode, setMode] = useState("view");
  const [md, setMd] = useState(doc.markdown);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const dirty = md !== doc.markdown;

  const save = async () => {
    setBusy(true);
    try { const d = await api.saveDoc(name, md); onSaved(d); setMode("view"); setError(null); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  };
  const source = doc.source === "env" ? "from the deploy's env var" : doc.source === "none" ? "not set" : `saved here (${doc.source.replace("dashboard ", "")})`;

  return (
    <Card>
      <SectionTitle title={title} sub={sub}
        right={<Segmented value={mode} onChange={setMode} options={[{ value: "view", label: "As agents see it" }, { value: "edit", label: "Edit" }]} />} />
      <p className="mb-3 text-xs text-neutral-500">Live version: {source}. Every save is kept; nothing is overwritten.</p>
      {mode === "view" ? (
        doc.sections.length === 0 ? <Empty>Nothing published.</Empty> : (
          <div className="grid gap-2 md:grid-cols-2">
            {doc.sections.map((s) => (
              <div key={s.title} className="rounded-lg border border-neutral-200 p-3 text-sm dark:border-neutral-800">
                <div className="font-medium">{s.title}</div>
                <p className="mt-1 line-clamp-6 whitespace-pre-wrap text-neutral-600 dark:text-neutral-400">{unwrap(s.text)}</p>
              </div>
            ))}
          </div>
        )
      ) : (
        <div className="space-y-2">
          <textarea value={md} onChange={(e) => setMd(e.target.value)} rows={16} spellCheck
            className="mono w-full rounded-lg border border-neutral-300 bg-white p-3 text-[13px] leading-relaxed outline-none focus:border-neutral-500 dark:border-neutral-700 dark:bg-neutral-950" />
          <p className="text-xs text-neutral-500">{"<!-- comments -->"} stay private. Saving goes live immediately for every visiting agent.</p>
          <ErrorNote error={error} />
          <div className="flex gap-2">
            <Button variant="primary" disabled={!dirty || busy} onClick={save}>Publish</Button>
            <Button disabled={!dirty || busy} onClick={() => setMd(doc.markdown)}>Discard</Button>
          </div>
        </div>
      )}
      {doc.history.length > 0 && (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-neutral-500">Version history ({doc.history.length})</summary>
          <ul className="mt-2 space-y-1 text-xs text-neutral-500">
            {doc.history.map((h) => <li key={h.id}>v{h.id} · {timeAgo(h.saved_at)} · {h.chars.toLocaleString()} chars · by {h.saved_by}</li>)}
          </ul>
        </details>
      )}
    </Card>
  );
}
