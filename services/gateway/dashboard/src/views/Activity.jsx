import { useEffect, useMemo, useState } from "react";
import { api } from "../api.js";
import { Badge, Card, Empty, ErrorNote, LANES, SectionTitle, StackedBars, fmtTime, groupByDay, laneOf } from "../ui.jsx";

const OUTCOME_TONE = { ok: "good", denied: "warn", rate_limited: "warn", error: "bad", sending: "info" };

export default function Activity({ daily }) {
  const [events, setEvents] = useState(null);
  const [error, setError] = useState(null);
  const [lanes, setLanes] = useState(() => new Set(LANES.map((l) => l.key)));
  const [reads, setReads] = useState(false);
  const [problemsOnly, setProblemsOnly] = useState(false);

  useEffect(() => {
    api.activity(reads).then((r) => { setEvents(r.events); setError(null); }).catch(setError);
  }, [reads]);

  const shown = useMemo(
    () => (events || []).filter((e) => lanes.has(e.lane) && (!problemsOnly || e.outcome !== "ok")),
    [events, lanes, problemsOnly],
  );
  const toggle = (k) => {
    const n = new Set(lanes);
    n.has(k) ? n.delete(k) : n.add(k);
    setLanes(n.size ? n : new Set(LANES.map((l) => l.key)));
  };

  return (
    <div className="space-y-4">
      {daily && (
        <Card>
          <SectionTitle title="Last 14 days" sub="Everything that crossed the gateway, by who started it" />
          <StackedBars data={daily} series={LANES} />
        </Card>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {LANES.map((l) => (
          <button key={l.key} onClick={() => toggle(l.key)} title={l.hint}
            className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm ${lanes.has(l.key) ? "border-neutral-400 dark:border-neutral-500" : "border-neutral-200 text-neutral-400 dark:border-neutral-800"}`}>
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: lanes.has(l.key) ? l.color : "transparent", border: `1.5px solid ${l.color}` }} />
            {l.label}
          </button>
        ))}
        <label className="ml-auto inline-flex items-center gap-1.5 text-sm text-neutral-600 dark:text-neutral-400">
          <input type="checkbox" checked={problemsOnly} onChange={(e) => setProblemsOnly(e.target.checked)} /> Problems only
        </label>
        <label className="inline-flex items-center gap-1.5 text-sm text-neutral-600 dark:text-neutral-400">
          <input type="checkbox" checked={reads} onChange={(e) => setReads(e.target.checked)} /> Include reads
        </label>
      </div>

      <ErrorNote error={error} />
      {events && shown.length === 0 && <Empty>Nothing here yet.</Empty>}
      {groupByDay(shown).map((g) => (
        <div key={g.day}>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-neutral-500">{g.label}</h3>
          <Card className="divide-y divide-neutral-100 !p-0 dark:divide-neutral-800">
            {g.items.map((e, i) => <Row key={`${e.trace_id}-${i}`} e={e} />)}
          </Card>
        </div>
      ))}
    </div>
  );
}

function Row({ e }) {
  const [open, setOpen] = useState(false);
  const lane = laneOf(e.lane);
  return (
    <button onClick={() => setOpen(!open)} className="block w-full px-4 py-2.5 text-left hover:bg-neutral-50 dark:hover:bg-neutral-800/50">
      <div className="flex items-start gap-3">
        <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full" style={{ background: lane.color }} aria-hidden />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-xs font-medium text-neutral-500">{lane.label}</span>
            <span className="text-xs text-neutral-400">{e.client}</span>
          </div>
          <div className={`text-sm ${open ? "whitespace-pre-wrap break-words" : "truncate"}`}>{e.summary}</div>
          {open && (
            <div className="mt-1 space-y-0.5 text-xs text-neutral-500">
              {e.detail && <div>Detail: {e.detail}</div>}
              {e.card_signature && <div>Their agent card: {e.card_signature}{e.key_changed ? " (signing key changed since last call)" : ""}</div>}
              {e.latency_ms != null && <div>Took {e.latency_ms} ms</div>}
              <div className="mono">trace {e.trace_id}{e.chained ? " · hash-chained" : " · before chaining"}</div>
            </div>
          )}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span className="text-xs tabular-nums text-neutral-500">{fmtTime(e.ts)}</span>
          {e.outcome !== "ok" && <Badge tone={OUTCOME_TONE[e.outcome] || "neutral"}>{e.outcome.replace("_", " ")}</Badge>}
          {e.key_changed && <Badge tone="bad">key changed</Badge>}
        </div>
      </div>
    </button>
  );
}
