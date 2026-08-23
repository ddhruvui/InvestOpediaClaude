/* The current target book: what the model proposes for the next open, with the
   M5.2 barrier levels each entry would carry, and a one-click push into the
   paper book (BP15). Signals are from the last close; fills happen at the NEXT
   open — the one-day lag is structural (G-02). */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { BarChart, fmtNum, fmtPct, fmtInt } from '../components/Chart.jsx';
import { Card, StatTile, Loading, ErrorBox, Badge } from '../components/Bits.jsx';

/* A barrier is only a barrier if price can plausibly reach it inside the hold.
   thr = m·σ·√h with m=1.5, h=20 is ±6.7σ, so a name at ~13% daily vol prices a
   ±90% barrier — unreachable, i.e. the trade can only ever end at the vertical
   exit. That is a faithful consequence of the spec, not a bug, but it must not
   be presented as a working stop. */
const WIDE_BARRIER_PCT = 40;

export default function Suggestions() {
  const [d, setD] = useState({ loading: true });
  const [q, setQ] = useState('');
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);
  const [paperTickers, setPaperTickers] = useState(new Set());

  const refreshPaper = () => api.paper()
    .then((p) => setPaperTickers(new Set(
      p.positions.filter((x) => x.status !== 'closed').map((x) => x.ticker))))
    .catch(() => {});

  useEffect(() => {
    api.suggestions().then((s) => setD({ s })).catch((error) => setD({ error }));
    refreshPaper();
  }, []);

  const rows = useMemo(() => {
    const all = d.s?.buys_or_increases || [];
    const t = q.trim().toUpperCase();
    return t ? all.filter((r) => r.ticker.toUpperCase().includes(t)) : all;
  }, [d.s, q]);

  if (d.loading) return <Loading what="suggestions" />;
  if (d.error) return <ErrorBox error={d.error} hint="Run the predict job on a pod, then rebuild the report bundle." />;

  const s = d.s;
  const book = s.portfolio || {};
  const wideRows = (s.buys_or_increases || [])
    .filter((r) => Math.abs(r.stop_pct || 0) > WIDE_BARRIER_PCT);
  const wideCount = wideRows.length;
  const widest = wideRows.reduce((a, r) => Math.max(a, Math.abs(r.stop_pct || 0)), 0);
  const topWeights = (s.buys_or_increases || []).slice(0, 18).map((r) => ({
    label: r.ticker, value: r.target_weight, tipLabel: 'target weight',
    tip: <div className="t-row"><span>Conviction</span><span>{fmtNum(r.ensemble_rank, 3)}</span></div>,
  }));

  const addToPaper = async (r) => {
    setBusy(r.ticker);
    setMsg(null);
    try {
      await api.paperOpen({
        ticker: r.ticker, side: 1, target_weight: r.target_weight,
        signal_date: s.as_of_close, ref_close: r.last_close,
        stop_pct: r.stop_pct, profit_take_pct: r.profit_take_pct,
        max_hold_sessions: r.max_hold_sessions, ensemble_rank: r.ensemble_rank,
      });
      setMsg({ ok: true, text: `${r.ticker} queued as an MOO order in the paper book.` });
      refreshPaper();
    } catch (err) {
      setMsg({ ok: false, text: `${r.ticker}: ${err.message}` });
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="Signals as of" value={s.as_of_close} sub="last close used" />
        <StatTile label="Names in book" value={fmtInt(book.n_names)} sub="decile-10 selections" />
        <StatTile label="Gross long" value={fmtNum(book.gross_long, 2)} sub="fraction of NAV" />
        <StatTile label="SPY hedge" value={fmtNum(book.spy_hedge_weight, 2)}
                  sub="beta-matched short leg" />
      </div>

      <div className="verdict warn">
        <div className="mark" aria-hidden="true">⚠</div>
        <div>
          <h2>Research output — the gates say do not trade this book</h2>
          <p>{s.disclaimer} Execution convention: {s.execute_at}</p>
        </div>
      </div>

      <Card title="Largest positions"
            subtitle="Inverse-volatility weights inside the entering tranche, capped at the single-name limit.">
        <BarChart data={topWeights} horizontal height={Math.max(200, topWeights.length * 26 + 40)}
                  valueFormat={(v) => fmtPct(v, 2)} />
      </Card>

      {wideCount > 0 && (
        <div className="note" style={{ marginBottom: 16, borderLeft: '3px solid var(--warning)' }}>
          <span aria-hidden="true">⚠ </span>
          <strong>{wideCount} of {(s.buys_or_increases || []).length} names price a barrier
          wider than ±{WIDE_BARRIER_PCT}%</strong> (widest ±{fmtNum(widest, 0)}%). The barrier
          width is m·σ·√h = ±6.7σ, so at very high volatility it lands beyond any
          plausible 20-session move and the stop and profit-take can never trigger —
          those positions run to the time barrier. Treat their stop/profit-take
          columns as nominal, not as risk control.
        </div>
      )}

      <Card title="Target book"
            subtitle={`Each entry carries its triple-barrier exits: stop and profit-take are percentages against the ACTUAL fill, and the vertical barrier is a market-on-open exit ${s.exit_rules?.h_sessions} sessions later.`}
            right={<input placeholder="Filter ticker…" value={q} onChange={(e) => setQ(e.target.value)} />}>
        {msg && (
          <div className="note" style={{
            borderLeft: `3px solid ${msg.ok ? 'var(--profit)' : 'var(--loss)'}`,
          }}>
            <span aria-hidden="true">{msg.ok ? '✓ ' : '✕ '}</span>{msg.text}
          </div>
        )}
        <div className="table-wrap" style={{ maxHeight: 620, overflowY: 'auto' }}>
          <table>
            <thead>
              <tr>
                <th>Ticker</th><th>Action</th><th className="n">Target weight</th>
                <th className="n">Conviction</th><th className="n">Last close</th>
                <th className="n">Stop</th><th className="n">Profit-take</th>
                <th className="n">Max hold</th><th></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => {
                const inPaper = paperTickers.has(r.ticker);
                return (
                  <tr key={r.ticker}>
                    <td><strong>{r.ticker}</strong></td>
                    <td><Badge kind="pass">Buy / add</Badge></td>
                    <td className="n">{fmtPct(r.target_weight, 2)}</td>
                    <td className="n">{fmtNum(r.ensemble_rank, 3)}</td>
                    <td className="n">{fmtNum(r.last_close, 2)}</td>
                    <td className="n neg">{fmtNum(r.stop_pct, 2)}%</td>
                    <td className="n pos">+{fmtNum(r.profit_take_pct, 2)}%</td>
                    <td className="n">
                      {Math.abs(r.stop_pct) > WIDE_BARRIER_PCT
                        ? <span title={`±${fmtNum(Math.abs(r.stop_pct), 0)}% barrier is `
                            + 'far beyond a plausible 20-session move, so this position '
                            + 'can realistically only exit at the time barrier.'}>
                            <Badge kind="neutral">⏱ time-exit only</Badge>
                          </span>
                        : r.max_hold_sessions}
                    </td>
                    <td>
                      <button className="btn sm" disabled={inPaper || busy === r.ticker}
                              onClick={() => addToPaper(r)}>
                        {inPaper ? 'In paper book' : busy === r.ticker ? 'Adding…' : 'Paper trade'}
                      </button>
                    </td>
                  </tr>
                );
              })}
              {!rows.length && <tr><td colSpan="9" className="empty">No matches.</td></tr>}
            </tbody>
          </table>
        </div>
      </Card>

      {s.sells_or_exits?.length > 0 && (
        <Card title="Exits" subtitle="Positions the model would close.">
          <div className="table-wrap">
            <table>
              <thead><tr><th>Ticker</th><th>Action</th><th className="n">Current weight</th></tr></thead>
              <tbody>
                {s.sells_or_exits.map((r) => (
                  <tr key={r.ticker}>
                    <td><strong>{r.ticker}</strong></td>
                    <td><Badge kind="fail">Exit</Badge></td>
                    <td className="n">{fmtPct(r.current_weight, 2)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </>
  );
}
