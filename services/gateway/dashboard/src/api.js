// All calls are relative: the page lives at /<owner-secret>/, so ./api/... stays behind it.
async function request(path, opts = {}) {
  const res = await fetch(`./api/${path}`, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    credentials: "same-origin",
    referrerPolicy: "no-referrer",
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

export const api = {
  overview: () => request("overview"),
  requests: (status) => request(`requests${status ? `?status=${status}` : ""}`),
  resolve: (id, decision, reply) =>
    request(`requests/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify({ decision, reply }) }),
  activity: (reads) => request(`activity?limit=400${reads ? "&reads=1" : ""}`),
  publicView: () => request("public"),
  saveDoc: (name, markdown) => request(`public/${name}`, { method: "PUT", body: JSON.stringify({ markdown }) }),
  preview: (question) => request("public/preview", { method: "POST", body: JSON.stringify({ question }) }),
};

// The memory access feed lives on the Cortex MCP service. Its URL is kept in this
// browser only, so the public Gateway never learns where the memory service is.
const FEED_KEY = "cortex.accessFeedUrl";
export const feedUrl = {
  get() {
    try { return localStorage.getItem(FEED_KEY) || ""; } catch { return ""; }
  },
  set(v) {
    try { v ? localStorage.setItem(FEED_KEY, v) : localStorage.removeItem(FEED_KEY); } catch { /* private mode */ }
  },
};

export async function fetchAccessFeed(url) {
  const res = await fetch(url, { referrerPolicy: "no-referrer", credentials: "omit" });
  if (!res.ok) throw new Error(`feed answered HTTP ${res.status}`);
  return res.json();
}
