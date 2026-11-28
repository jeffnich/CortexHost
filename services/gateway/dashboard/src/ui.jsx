import { useState } from "react";

export const LANES = [
  { key: "inbound", label: "Visitors", color: "var(--series-1)", hint: "Other agents calling your public door" },
  { key: "triage", label: "Triage", color: "var(--series-2)", hint: "Your triage agent's opinions" },
  { key: "owner", label: "You", color: "var(--series-3)", hint: "Your decisions and edits" },
  { key: "outbound", label: "Outbound", color: "var(--series-4)", hint: "Your agent asking other agents" },
];
export const laneOf = (k) => LANES.find((l) => l.key === k) || { label: k, color: "#888" };

export function Card({ children, className = "" }) {
  return (
    <section
      className={`rounded-xl border border-neutral-200 bg-white p-4 dark:border-neutral-800 dark:bg-neutral-900 ${className}`}
    >
      {children}
    </section>
  );
}

export function SectionTitle({ title, sub, right }) {
  return (
    <div className="mb-3 flex items-end justify-between gap-3">
      <div>
        <h2 className="text-base font-semibold">{title}</h2>
        {sub && <p className="text-sm text-neutral-500 dark:text-neutral-400">{sub}</p>}
      </div>
      {right}
    </div>
  );
}

const TONES = {
  neutral: "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300",
  good: "bg-emerald-50 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300",
  warn: "bg-amber-50 text-amber-800 dark:bg-amber-950 dark:text-amber-300",
  bad: "bg-red-50 text-red-800 dark:bg-red-950 dark:text-red-300",
  info: "bg-sky-50 text-sky-800 dark:bg-sky-950 dark:text-sky-300",
};

export function Badge({ tone = "neutral", children, title }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  );
}

// Status always carries an icon and a word, never color alone.
export function Check({ ok, label, detail }) {
  const tone = ok === null || ok === undefined ? "neutral" : ok ? "good" : "bad";
  const icon = ok === null || ok === undefined ? "–" : ok ? "✓" : "✕";
  return (
    <div className={`flex items-start gap-2 rounded-lg px-3 py-2 text-sm ${TONES[tone]}`}>
      <span aria-hidden className="font-bold">{icon}</span>
      <div>
        <div className="font-medium">{label}</div>
        {detail && <div className="text-xs opacity-80">{detail}</div>}
      </div>
    </div>
  );
}

export function Button({ variant = "default", className = "", ...props }) {
  const v = {
    default: "border border-neutral-300 bg-white hover:bg-neutral-50 dark:border-neutral-700 dark:bg-neutral-900 dark:hover:bg-neutral-800",
    primary: "bg-neutral-900 text-white hover:bg-neutral-700 dark:bg-white dark:text-neutral-900 dark:hover:bg-neutral-200",
    danger: "border border-red-300 text-red-700 hover:bg-red-50 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950",
  }[variant];
  return (
    <button
      {...props}
      className={`min-h-9 rounded-lg px-3 text-sm font-medium transition disabled:opacity-50 ${v} ${className}`}
    />
  );
}

export function Segmented({ options, value, onChange }) {
  return (
    <div className="inline-flex flex-wrap gap-1 rounded-lg bg-neutral-100 p-1 dark:bg-neutral-800">
      {options.map((o) => (
        <button
          key={o.value}
          onClick={() => onChange(o.value)}
          className={`rounded-md px-2.5 py-1 text-sm ${
            value === o.value
              ? "bg-white font-medium shadow-sm dark:bg-neutral-950"
              : "text-neutral-600 hover:text-neutral-900 dark:text-neutral-400 dark:hover:text-neutral-100"
          }`}
        >
          {o.label}
          {o.count !== undefined && <span className="ml-1 text-neutral-400">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function fmtDay(iso) {
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  const today = new Date();
  const y = new Date(today);
  y.setDate(today.getDate() - 1);
  if (d.toDateString() === today.toDateString()) return "Today";
  if (d.toDateString() === y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}

export function timeAgo(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

export function groupByDay(items, key = "ts") {
  const out = [];
  let cur = null;
  for (const it of items) {
    const day = new Date(it[key]).toDateString();
    if (!cur || cur.day !== day) {
      cur = { day, label: fmtDay(it[key]), items: [] };
      out.push(cur);
    }
    cur.items.push(it);
  }
  return out;
}

// Stacked daily bars. 2px surface gap between segments, rounded top only,
// hover tooltip per day, legend above. `series` = [{key,label,color}].
export function StackedBars({ data, series, height = 120 }) {
  const [hover, setHover] = useState(null);
  const totals = data.map((d) => series.reduce((a, s) => a + (d[s.key] || 0), 0));
  const max = Math.max(1, ...totals);
  const w = 100 / data.length;
  return (
    <div className="relative">
      <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-neutral-600 dark:text-neutral-400">
        {series.map((s) => (
          <span key={s.key} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />
            {s.label}
          </span>
        ))}
      </div>
      <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" className="block w-full" style={{ height }}
        role="img" aria-label="Events per day">
        <line x1="0" x2="100" y1={height - 0.5} y2={height - 0.5} stroke="currentColor" strokeOpacity="0.15" vectorEffect="non-scaling-stroke" />
        {data.map((d, i) => {
          let y = height;
          const x = i * w + w * 0.18;
          const bw = w * 0.64;
          const segs = series.filter((s) => d[s.key]);
          return (
            <g key={d.day}>
              {segs.map((s, j) => {
                const h = Math.max(1.5, ((d[s.key] || 0) / max) * (height - 6));
                y -= h;
                const top = j === segs.length - 1;
                return (
                  <rect key={s.key} x={x} y={y + (j ? 0 : 0)} width={bw} height={Math.max(0.5, h - (j < segs.length - 1 ? 1.2 : 0))}
                    fill={s.color} rx={top ? 0.8 : 0} />
                );
              })}
              <rect x={i * w} y="0" width={w} height={height} fill="transparent"
                onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} onClick={() => setHover(i)} />
            </g>
          );
        })}
      </svg>
      <div className="mt-1 flex justify-between text-[11px] text-neutral-500">
        <span>{data[0] && fmtDay(data[0].day)}</span>
        <span>Today</span>
      </div>
      {hover !== null && (
        <div
          className="pointer-events-none absolute top-6 z-10 min-w-36 rounded-lg border border-neutral-200 bg-white p-2 text-xs shadow-lg dark:border-neutral-700 dark:bg-neutral-900"
          style={{ left: `clamp(0px, calc(${(hover + 0.5) * w}% - 72px), calc(100% - 150px))` }}
        >
          <div className="mb-1 font-medium">{fmtDay(data[hover].day)}</div>
          {series.map((s) => (
            <div key={s.key} className="flex items-center justify-between gap-3">
              <span className="inline-flex items-center gap-1.5">
                <span className="h-2 w-2 rounded-sm" style={{ background: s.color }} />
                {s.label}
              </span>
              <span className="tabular-nums">{data[hover][s.key] || 0}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function Empty({ children }) {
  return <div className="rounded-lg border border-dashed border-neutral-300 p-6 text-center text-sm text-neutral-500 dark:border-neutral-700">{children}</div>;
}

export function ErrorNote({ error }) {
  if (!error) return null;
  return <div className="rounded-lg bg-red-50 p-3 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">{String(error.message || error)}</div>;
}

// Markdown hard-wraps paragraphs; join wrapped lines for display, keep blank lines and list items.
export function unwrap(text) {
  return (text || "").replace(/([^\n])\n(?![\n\-*>#]|\d+\.)/g, "$1 ");
}
