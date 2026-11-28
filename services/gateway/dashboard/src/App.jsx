import { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";
import Inbox from "./views/Inbox.jsx";
import Activity from "./views/Activity.jsx";
import PublicView from "./views/PublicView.jsx";
import Access from "./views/Access.jsx";

const TABS = [
  { key: "inbox", label: "Inbox", icon: "M4 13h4l2 3h4l2-3h4M4 13l2-8h12l2 8v6H4z" },
  { key: "activity", label: "Activity", icon: "M3 12h4l3-8 4 16 3-8h4" },
  { key: "public", label: "Public view", icon: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zm10 3a3 3 0 100-6 3 3 0 000 6z" },
  { key: "access", label: "Access log", icon: "M12 3l8 4v5c0 5-3.5 8-8 9-4.5-1-8-4-8-9V7z" },
];

function readTab() {
  const h = window.location.hash.replace("#", "");
  return TABS.some((t) => t.key === h) ? h : "inbox";
}

export default function App() {
  const [tab, setTab] = useState(readTab);
  const [overview, setOverview] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(() => {
    api.overview().then((o) => { setOverview(o); setError(null); }).catch(setError);
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30000);
    const onHash = () => setTab(readTab());
    window.addEventListener("hashchange", onHash);
    return () => { clearInterval(t); window.removeEventListener("hashchange", onHash); };
  }, [refresh]);

  const go = (k) => { window.location.hash = k; setTab(k); };

  return (
    <div className="mx-auto min-h-screen max-w-5xl px-4 pb-24 pt-4 md:pb-10">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">Cortex</h1>
          <p className="text-xs text-neutral-500">
            {overview ? `${overview.owner}'s gateway, owner view` : "Owner view"}
          </p>
        </div>
        <nav className="hidden gap-1 md:flex">
          {TABS.map((t) => (
            <button key={t.key} onClick={() => go(t.key)}
              className={`rounded-lg px-3 py-1.5 text-sm ${tab === t.key ? "bg-neutral-900 text-white dark:bg-white dark:text-neutral-900" : "text-neutral-600 hover:bg-neutral-100 dark:text-neutral-400 dark:hover:bg-neutral-800"}`}>
              {t.label}
              {t.key === "inbox" && overview?.pending > 0 && <span className="ml-1.5 tabular-nums opacity-70">{overview.pending}</span>}
            </button>
          ))}
        </nav>
      </header>

      {error && !overview && (
        <div className="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">
          Could not reach the gateway: {error.message}
        </div>
      )}

      {overview && <IntegrityStrip o={overview} />}

      <main className="mt-4">
        {tab === "inbox" && <Inbox onChange={refresh} />}
        {tab === "activity" && <Activity daily={overview?.daily} />}
        {tab === "public" && <PublicView onChange={refresh} />}
        {tab === "access" && <Access />}
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-20 grid grid-cols-4 border-t border-neutral-200 bg-white/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden dark:border-neutral-800 dark:bg-neutral-950/95">
        {TABS.map((t) => (
          <button key={t.key} onClick={() => go(t.key)}
            className={`relative flex flex-col items-center gap-0.5 py-2 text-[11px] ${tab === t.key ? "text-neutral-900 dark:text-white" : "text-neutral-500"}`}>
            <svg viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round"><path d={t.icon} /></svg>
            {t.label}
            {t.key === "inbox" && overview?.pending > 0 && (
              <span className="absolute right-[22%] top-1 rounded-full bg-neutral-900 px-1.5 text-[10px] text-white dark:bg-white dark:text-neutral-900">{overview.pending}</span>
            )}
          </button>
        ))}
      </nav>
    </div>
  );
}

// The trust summary: every claim here is computed from the logs on each load.
function IntegrityStrip({ o }) {
  const a = o.audit_chain;
  const items = [
    {
      ok: a.intact,
      label: a.intact ? "Audit log untampered" : "Audit log chain broken",
      detail: a.intact
        ? `${a.checked} chained entries verified${a.legacy_rows ? `, ${a.legacy_rows} older unchained` : ""}`
        : `First bad entry #${a.broken_at}`,
    },
    {
      ok: o.outbound_chain_intact,
      label: o.outbound_chain_intact ? "Outbound log untampered" : "Outbound log chain broken",
      detail: o.outbound_enabled ? "Every call your agent made, hash-chained" : "Outbound is off",
    },
    {
      ok: o.signed ? true : null,
      label: o.signed ? "Agent card signed" : "Agent card unsigned",
      detail: o.signed ? "Visitors can verify it's really you" : "Set GATEWAY_SIGNING_KEY to sign",
    },
    {
      ok: o.pending === 0 ? true : o.untriaged === 0 ? null : false,
      label: o.pending === 0 ? "Inbox clear" : `${o.pending} waiting on you`,
      detail: o.triage_enabled ? `${o.untriaged} not yet triaged` : "Triage agent not set up",
    },
  ];
  return (
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
      {items.map((i) => <CheckTile key={i.label} {...i} />)}
    </div>
  );
}

function CheckTile({ ok, label, detail }) {
  const tone = ok === null ? "border-neutral-200 dark:border-neutral-800" : ok ? "border-emerald-200 dark:border-emerald-900" : "border-amber-300 dark:border-amber-800";
  const icon = ok === null ? "•" : ok ? "✓" : "!";
  const iconTone = ok === null ? "bg-neutral-200 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300" : ok ? "bg-emerald-600 text-white" : "bg-amber-500 text-white";
  return (
    <div className={`flex items-start gap-2 rounded-xl border bg-white p-2.5 sm:p-3 dark:bg-neutral-900 ${tone}`}>
      <span aria-hidden className={`mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded-full text-xs font-bold ${iconTone}`}>{icon}</span>
      <div className="min-w-0">
        <div className="text-[13px] font-medium leading-tight sm:text-sm">{label}</div>
        <div className="mt-0.5 hidden break-words text-xs text-neutral-500 sm:block">{detail}</div>
      </div>
    </div>
  );
}
