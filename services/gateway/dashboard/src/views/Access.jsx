import { useEffect, useMemo, useState } from "react";
import { feedUrl, fetchAccessFeed } from "../api.js";
import { Badge, Button, Card, Check, Empty, ErrorNote, SectionTitle, fmtTime, groupByDay } from "../ui.jsx";

const KINDS = [
  { key: "read", label: "Reads", color: "var(--series-1)" },
  { key: "write", label: "Writes", color: "var(--series-2)" },
];

export default function Access() {
  const [url, setUrl] = useState(feedUrl.get);
  const [draft, setDraft] = useState(url);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [client, setClient] = useState("");

  useEffect(() => {
    if (!url) return;
    setData(null);
    fetchAccessFeed(url).then((d) => { setData(d); setError(null); }).catch(setError);
  }, [url]);

  const byClient = useMemo(() => {
    const m = {};
    for (const e of data?.entries || []) {
      const c = (m[e.client] ||= { client: e.client, read: 0, write: 0, memories: new Set(), last: e.ts });
      c[e.kind] = (c[e.kind] || 0) + 1;
      e.memory_ids.forEach((id) => c.memories.add(id));
    }
    return Object.values(m).sort((a, b) => b.read + b.write - (a.read + a.write));
  }, [data]);

  if (!url) {
    return (
      <Card>
        <SectionTitle title="Connect the memory access feed"
          sub="Cortex MCP records which app read or wrote which memory. Paste its feed URL once." />
        <p className="mb-3 text-sm text-neutral-600 dark:text-neutral-400">
          It's your Cortex MCP connector URL with <span className="mono">/mcp</span> swapped for{" "}
          <span className="mono">/access.json</span>. It stays in this browser only; the public gateway never sees it.
        </p>
        <FeedForm draft={draft} setDraft={setDraft} onSave={() => { feedUrl.set(draft.trim()); setUrl(draft.trim()); }} />
      </Card>
    );
  }

  const max = Math.max(1, ...byClient.map((c) => c.read + c.write));
  const entries = (data?.entries || []).filter((e) => !client || e.client === client);

  return (
    <div className="space-y-4">
      <ErrorNote error={error && new Error(`Couldn't read the access feed: ${error.message}`)} />
      {data && (
        <div className="grid gap-4 md:grid-cols-[1fr_2fr]">
          <div className="space-y-2">
            <Check ok={data.chain.intact} label={data.chain.intact ? "Access log untampered" : "Access log chain broken"}
              detail={data.chain.intact ? `${data.chain.checked} entries verified` : `First bad entry #${data.chain.broken_at}`} />
            <Card className="!p-3 text-xs text-neutral-500">
              Every read and write through Cortex MCP is recorded with the app that made it. Showing the latest {data.entries.length}.
            </Card>
          </div>
          <Card>
            <SectionTitle title="Who touched your memory" sub="Tool calls per app, and how many distinct memories each saw" />
            <div className="mb-2 flex gap-4 text-xs text-neutral-600 dark:text-neutral-400">
              {KINDS.map((k) => (
                <span key={k.key} className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: k.color }} />{k.label}</span>
              ))}
            </div>
            {byClient.length === 0 && <Empty>No access recorded yet.</Empty>}
            <ul className="space-y-2">
              {byClient.map((c) => (
                <li key={c.client}>
                  <button onClick={() => setClient(client === c.client ? "" : c.client)}
                    className={`w-full rounded-md p-1 text-left ${client === c.client ? "bg-neutral-100 dark:bg-neutral-800" : ""}`}
                    title={`${c.read} reads, ${c.write} writes, ${c.memories.size} memories`}>
                    <div className="mb-1 flex justify-between text-sm">
                      <span className="font-medium">{c.client}</span>
                      <span className="tabular-nums text-neutral-500">{c.read} reads · {c.write} writes · {c.memories.size} memories</span>
                    </div>
                    <div className="flex h-2.5 gap-[2px]">
                      {KINDS.map((k) => c[k.key] > 0 && (
                        <div key={k.key} className="h-full first:rounded-l last:rounded-r" style={{ width: `${(c[k.key] / max) * 100}%`, background: k.color }} />
                      ))}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      )}

      {data && (
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">{client ? `Only ${client}` : "Every access"}</h3>
          {client && <Button onClick={() => setClient("")}>Show all apps</Button>}
        </div>
      )}
      {groupByDay(entries).map((g) => (
        <div key={g.day}>
          <h3 className="mb-1 text-xs font-medium uppercase tracking-wide text-neutral-500">{g.label}</h3>
          <Card className="divide-y divide-neutral-100 !p-0 dark:divide-neutral-800">
            {g.items.map((e) => <AccessRow key={e.id} e={e} />)}
          </Card>
        </div>
      ))}

      <details className="text-sm">
        <summary className="cursor-pointer text-neutral-500">Feed settings</summary>
        <div className="mt-2">
          <FeedForm draft={draft} setDraft={setDraft} onSave={() => { feedUrl.set(draft.trim()); setUrl(draft.trim()); }}
            onClear={() => { feedUrl.set(""); setUrl(""); setDraft(""); }} />
        </div>
      </details>
    </div>
  );
}

function AccessRow({ e }) {
  const [open, setOpen] = useState(false);
  return (
    <button onClick={() => setOpen(!open)} className="block w-full px-4 py-2.5 text-left hover:bg-neutral-50 dark:hover:bg-neutral-800/50">
      <div className="flex items-start gap-3">
        <Badge tone={e.kind === "write" ? "warn" : "info"}>{e.kind}</Badge>
        <div className="min-w-0 flex-1">
          <div className="text-sm"><span className="font-medium">{e.client}</span> <span className="mono text-xs text-neutral-500">{e.tool}</span></div>
          <div className={`text-xs text-neutral-500 ${open ? "break-words" : "truncate"}`}>
            {e.memory_ids.length ? `${e.memory_ids.length} ${e.memory_ids.length === 1 ? "memory" : "memories"}` : "no memories returned"}
            {e.detail ? ` · ${e.detail}` : ""}
          </div>
          {open && e.memory_ids.length > 0 && (
            <div className="mono mt-1 break-all text-[11px] text-neutral-400">{e.memory_ids.join(" ")}</div>
          )}
        </div>
        <span className="shrink-0 text-xs tabular-nums text-neutral-500">{fmtTime(e.ts)}</span>
      </div>
    </button>
  );
}

function FeedForm({ draft, setDraft, onSave, onClear }) {
  const valid = /^https:\/\/.+\/access\.json$/.test(draft.trim()) || /^http:\/\/localhost[:/]/.test(draft.trim());
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (valid) onSave(); }} className="flex flex-wrap gap-2">
      <input value={draft} onChange={(e) => setDraft(e.target.value)} type="url" autoComplete="off"
        placeholder="https://…/<secret>/access.json"
        className="mono min-w-0 flex-1 rounded-lg border border-neutral-300 bg-white px-3 py-2 text-xs outline-none focus:border-neutral-500 dark:border-neutral-700 dark:bg-neutral-950" />
      <Button variant="primary" type="submit" disabled={!valid}>Save</Button>
      {onClear && <Button type="button" onClick={onClear}>Forget</Button>}
    </form>
  );
}
