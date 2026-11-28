import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Badge, Button, Card, Empty, ErrorNote, Segmented, timeAgo } from "../ui.jsx";

const VERDICT = {
  escalate: { tone: "bad", label: "Escalate" },
  digest: { tone: "info", label: "Digest" },
  suggest_decline: { tone: "warn", label: "Suggest decline" },
  noise: { tone: "neutral", label: "Noise" },
};
const STATUSES = ["pending", "approved", "declined", "done", ""];

// Escalations first, then by priority, then newest; untriaged sits after escalations.
function rank(r) {
  const t = r.triage;
  if (!t) return [1, 2];
  const v = { escalate: 0, digest: 2, suggest_decline: 3, noise: 4 }[t.verdict] ?? 2;
  return [v, t.priority || 3];
}

export default function Inbox({ onChange }) {
  const [status, setStatus] = useState("pending");
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);

  const load = () => api.requests(status).then((r) => { setRows(r.requests); setError(null); }).catch(setError);
  useEffect(() => { setRows(null); load(); }, [status]); // eslint-disable-line react-hooks/exhaustive-deps

  const sorted = rows && [...rows].sort((a, b) => {
    const [a1, a2] = rank(a), [b1, b2] = rank(b);
    return a1 - b1 || a2 - b2 || b.created_at.localeCompare(a.created_at);
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Segmented value={status} onChange={setStatus}
          options={STATUSES.map((s) => ({ value: s, label: s ? s[0].toUpperCase() + s.slice(1) : "All" }))} />
        <p className="text-xs text-neutral-500">Nothing is sent or booked until you decide here.</p>
      </div>
      <ErrorNote error={error} />
      {sorted && sorted.length === 0 && <Empty>No {status || ""} requests.</Empty>}
      {sorted?.map((r) => (
        <RequestCard key={r.request_id} r={r} onDone={() => { load(); onChange?.(); }} />
      ))}
    </div>
  );
}

function RequestCard({ r, onDone }) {
  const u = r.untrusted || {};
  const t = r.triage;
  const [reply, setReply] = useState(r.reply ?? t?.draft_reply ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(r.status === "pending");
  const pending = r.status === "pending";

  const decide = async (decision) => {
    setBusy(true);
    try { await api.resolve(r.request_id, decision, reply.trim() || null); onDone(); }
    catch (e) { setError(e); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <button className="flex w-full items-start justify-between gap-3 text-left" onClick={() => setOpen(!open)}>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge>{r.kind === "meeting" ? "Meeting request" : "Message"}</Badge>
            {t && VERDICT[t.verdict] && <Badge tone={VERDICT[t.verdict].tone}>{VERDICT[t.verdict].label} · P{t.priority}</Badge>}
            {!t && pending && <Badge tone="neutral">Not triaged</Badge>}
            {!pending && <Badge tone={r.status === "declined" ? "warn" : "good"}>{r.status}</Badge>}
          </div>
          <div className="mt-1.5 truncate font-medium">{u.subject}</div>
          <div className="text-xs text-neutral-500">
            from {u.from_name} · via {r.client} · {timeAgo(r.created_at)}
          </div>
        </div>
        <span className="text-neutral-400" aria-hidden>{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="mt-3 space-y-3">
          {t && (
            <div className="rounded-lg bg-neutral-50 p-3 text-sm dark:bg-neutral-950">
              <div className="text-xs font-medium uppercase tracking-wide text-neutral-500">Your triage agent</div>
              <p className="mt-1">{t.summary}</p>
              {t.rationale && <p className="mt-1 text-xs text-neutral-500">Why: {t.rationale}</p>}
            </div>
          )}

          {/* Visitor-written text: shown as plain text, never rendered as markup. */}
          <div className="border-l-2 border-amber-400 pl-3">
            <div className="text-xs font-medium text-amber-700 dark:text-amber-400">Written by the visitor, unverified</div>
            <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-sm">
              <dt className="text-neutral-500">Name</dt><dd className="break-words">{u.from_name}</dd>
              <dt className="text-neutral-500">Contact</dt><dd className="break-all">{u.from_contact}</dd>
              {u.proposed_times?.length > 0 && <><dt className="text-neutral-500">Times</dt><dd>{[].concat(u.proposed_times).join(", ")}</dd></>}
              {u.duration_minutes && <><dt className="text-neutral-500">Length</dt><dd>{u.duration_minutes} min</dd></>}
            </dl>
            {u.body && <p className="mt-2 whitespace-pre-wrap break-words text-sm">{u.body}</p>}
          </div>

          <div>
            <label className="text-xs font-medium text-neutral-500" htmlFor={`reply-${r.request_id}`}>
              Reply the requester sees {t?.draft_reply && pending ? "(drafted by triage, edit freely)" : ""}
            </label>
            <textarea id={`reply-${r.request_id}`} value={reply} onChange={(e) => setReply(e.target.value)} rows={3}
              disabled={!pending}
              className="mt-1 w-full rounded-lg border border-neutral-300 bg-white p-2 text-sm outline-none focus:border-neutral-500 disabled:opacity-70 dark:border-neutral-700 dark:bg-neutral-950" />
          </div>
          <ErrorNote error={error} />
          {pending && (
            <div className="flex flex-wrap gap-2">
              <Button variant="primary" disabled={busy} onClick={() => decide("approved")}>Approve</Button>
              <Button variant="danger" disabled={busy} onClick={() => decide("declined")}>Decline</Button>
              <Button disabled={busy} onClick={() => decide("done")}>Mark done</Button>
              <span className="self-center text-xs text-neutral-500">Approving records it. Put meetings on your calendar yourself.</span>
            </div>
          )}
          <div className="mono text-[11px] text-neutral-400">{r.request_id}</div>
        </div>
      )}
    </Card>
  );
}
