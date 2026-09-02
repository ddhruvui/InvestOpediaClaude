/* The current target book: what the model proposes for the next open, with the
   M5.2 barrier levels each entry would carry, and a one-click push into the
   paper book (BP15). Signals are from the last close; fills happen at the NEXT
   open — the one-day lag is structural (G-02). */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { BarChart, fmtNum, fmtPct, fmtInt } from '../components/Chart.jsx';
import { Card, StatTile, Loading, ErrorBox, Badge, useSort, Th } from '../components/Bits.jsx';

const EMPTY = [];

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
  const pickSort = useSort(rows);
  const exitSort = useSort(d.s?.sells_or_exits ?? EMPTY);

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
      setMsg({ ok: true, text: `${r.ticker} is now tracked in your practice book — it "fills" at the next open.` });
      refreshPaper();
    } catch (err) {
      setMsg({ ok: false, text: `${r.ticker}: ${err.message}` });
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <Card>
        <h1>The model's full pick list</h1>
        <p className="muted small" style={{ margin: '4px 0 0' }}>
          This is the raw output — every stock the model wants and how it splits the
          money. For exact share counts and what to actually do at the open, use the
          Today page; this page is for digging into the picks themselves.
        </p>
      </Card>

      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="Picked after" value={s.as_of_close} sub="that day's market close" />
        <StatTile label="Stocks picked" value={fmtInt(book.n_names)}
                  sub="the model's top-decile names" />
        <StatTile label="Money invested" value={fmtPct(book.gross_long, 0)}
                  sub="of the pot (rest stays cash)" />
        <StatTile label="Short hedge" value={fmtNum(book.spy_hedge_weight, 2)}
                  sub="0 = no bet against the market" />
      </div>

      <div className="verdict warn">
        <div className="mark" aria-hidden="true">⚠</div>
        <div>
          <h2>Practice money only</h2>
          <p>{s.disclaimer} Timing rule: {s.execute_at}</p>
        </div>
      </div>

      <Card title="Biggest slices of the pot"
            subtitle="How the money is split. Steadier stocks get bigger slices; jumpier ones get smaller slices, and no single stock may exceed the cap.">
        <BarChart data={topWeights} horizontal height={Math.max(200, topWeights.length * 26 + 40)}
                  valueFormat={(v) => fmtPct(v, 2)} />
      </Card>

      {wideCount > 0 && (
        <div className="note" style={{ marginBottom: 16, borderLeft: '3px solid var(--warning)' }}>
          <span aria-hidden="true">⚠ </span>
          <strong>{wideCount} of {(s.buys_or_increases || []).length} picks have sell
          triggers wider than ±{WIDE_BARRIER_PCT}%</strong> (widest ±{fmtNum(widest, 0)}%).
          Trigger width scales with how jumpy a stock is, and for the jumpiest names it
          lands further than the price could plausibly move before the time limit — so
          their stop and profit-take will never fire, and those positions simply get
          sold at the time limit. Don't count on those triggers as protection.
        </div>
      )}

      <Card title="Every pick"
            subtitle={`Stop and profit-take are % moves from the price you actually pay at the open; the time limit is ${s.exit_rules?.h_sessions} trading days. Conviction is the model's score — higher means it likes the stock more.`}
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
                <Th k="ticker" sort={pickSort.sort} onSort={pickSort.onSort}>Stock</Th>
                <th>Action</th>
                <Th k="target_weight" num sort={pickSort.sort} onSort={pickSort.onSort}>Slice of pot</Th>
                <Th k="ensemble_rank" num sort={pickSort.sort} onSort={pickSort.onSort}>Conviction</Th>
                <Th k="last_close" num sort={pickSort.sort} onSort={pickSort.onSort}>Latest close</Th>
                <Th k="stop_pct" num sort={pickSort.sort} onSort={pickSort.onSort}>Stop</Th>
                <Th k="profit_take_pct" num sort={pickSort.sort} onSort={pickSort.onSort}>Profit-take</Th>
                <Th k="max_hold_sessions" num sort={pickSort.sort} onSort={pickSort.onSort}>Time limit</Th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {pickSort.rows.map((r) => {
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
                        ? <span title={`The ±${fmtNum(Math.abs(r.stop_pct), 0)}% triggers are `
                            + 'further than this stock could plausibly move in time, so the '
                            + 'only realistic exit is selling when the time limit is reached.'}>
                            <Badge kind="neutral">⏱ {r.max_hold_sessions} days — only exit</Badge>
                          </span>
                        : `${r.max_hold_sessions} days`}
                    </td>
                    <td>
                      <button className="btn sm" disabled={inPaper || busy === r.ticker}
                              onClick={() => addToPaper(r)}>
                        {inPaper ? 'Already tracked' : busy === r.ticker ? 'Adding…' : 'Track this trade'}
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
        <Card title="Get out of these" subtitle="Stocks the model held before and no longer wants.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <Th k="ticker" sort={exitSort.sort} onSort={exitSort.onSort}>Stock</Th>
                <th>Action</th>
                <Th k="last_close" num sort={exitSort.sort} onSort={exitSort.onSort}>Latest close</Th>
                <Th k="current_weight" num sort={exitSort.sort} onSort={exitSort.onSort}>Slice held</Th>
              </tr></thead>
              <tbody>
                {exitSort.rows.map((r) => (
                  <tr key={r.ticker}>
                    <td><strong>{r.ticker}</strong></td>
                    <td><Badge kind="fail">Sell</Badge></td>
                    <td className="n">{r.last_close != null ? fmtNum(r.last_close, 2) : '—'}</td>
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
