const base = '';

async function req(path, opts) {
  const res = await fetch(`${base}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(body.error || res.statusText), { body });
  return body;
}

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
  paper: () => req('/api/paper'),
  paperOpen: (b) => req('/api/paper/open', { method: 'POST', body: JSON.stringify(b) }),
  paperFill: (id, b) => req(`/api/paper/${id}/fill`, { method: 'POST', body: JSON.stringify(b) }),
  paperClose: (id, b) => req(`/api/paper/${id}/close`, { method: 'POST', body: JSON.stringify(b) }),
  paperRemove: (id) => req(`/api/paper/${id}`, { method: 'DELETE' }),
  paperSettings: (b) => req('/api/paper/settings', { method: 'POST', body: JSON.stringify(b) }),
};
