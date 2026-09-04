// API client. VITE_API_BASE points at the deployed backend (the Vercel URL);
// leave it unset for local dev, where Vite proxies /api to localhost:8787 and
// the backend serves the built UI itself.
const base = (import.meta.env.VITE_API_BASE || '').replace(/\/+$/, '');

async function req(path, opts) {
  let res;
  try {
    res = await fetch(`${base}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
  } catch (err) {
    throw new Error(`API unreachable at ${base || window.location.origin}: ${err.message}`);
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(body.error || res.statusText), { body });
  return body;
}

export const apiBase = base;

export const api = {
  health: () => req('/api/health'),
  today: () => req('/api/today'),
  summary: () => req('/api/summary'),
  equity: () => req('/api/equity'),
  suggestions: () => req('/api/suggestions'),
  config: () => req('/api/config'),
  tradesSummary: () => req('/api/trades/summary'),
  trades: (q = {}) => {
    const params = new URLSearchParams(
      Object.entries(q).filter(([, v]) => v !== '' && v != null));
    return req(`/api/trades?${params}`);
  },
  predictions: (limit) => req(`/api/predictions${limit ? `?limit=${limit}` : ''}`),
  prediction: (asOf) => req(`/api/predictions/${asOf}`),
  paper: () => req('/api/paper'),
  paperOpen: (b) => req('/api/paper/open', { method: 'POST', body: JSON.stringify(b) }),
  paperFill: (id, b) => req(`/api/paper/${id}/fill`, { method: 'POST', body: JSON.stringify(b) }),
  paperClose: (id, b) => req(`/api/paper/${id}/close`, { method: 'POST', body: JSON.stringify(b) }),
  paperRemove: (id) => req(`/api/paper/${id}`, { method: 'DELETE' }),
  paperSettings: (b) => req('/api/paper/settings', { method: 'POST', body: JSON.stringify(b) }),
};
