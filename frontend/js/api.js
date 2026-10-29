// Обёртки над HTTP API агента.

async function request(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch { /* не JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

export const api = {
  health: () => request("/api/health"),
  start: (query) => request("/api/session", { method: "POST", body: { query } }),
  confirm: (sid, kind, approved, data = {}) =>
    request(`/api/session/${sid}/confirm`, { method: "POST", body: { kind, approved, data } }),
  seller: (sid, text) => request(`/api/session/${sid}/seller`, { method: "POST", body: { text } }),
  message: (sid, text, marks = {}) => request(`/api/session/${sid}/message`, { method: "POST", body: { text, ...marks } }),
  product: (sid, product, amount) => request(`/api/session/${sid}/product`, { method: "POST", body: { product, amount } }),
  economics: (params) => request(`/api/economics?${new URLSearchParams(params)}`),
  stream: (sid, after = 0) => new EventSource(`/api/chat/${sid}/stream?after=${after}`),
  chats: () => request("/api/chats"),
  chatMessage: (sid, text, marks = {}) => request(`/api/chat/${sid}/message`, { method: "POST", body: { text, ...marks } }),
  me: () => request("/api/me"),
  history: () => request("/api/me/history"),
  register: (data) => request("/api/auth/register", { method: "POST", body: data }),
  login: (data) => request("/api/auth/login", { method: "POST", body: data }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  sberId: () => request("/api/auth/sberid"),
  consent: () => request("/api/consent", { method: "POST" }),
  security: (listingId, sid) => request(`/api/listing/${encodeURIComponent(listingId)}/security${sid ? `?sid=${sid}` : ""}`),
};
