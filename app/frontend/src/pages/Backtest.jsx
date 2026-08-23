/* "What was suggested, and what happened to it."
   Every row here is one barrier trade the book actually proposed in the
   walk-forward: entry at the next open after the signal, exit by the M5.2
   triple barrier (profit-take / stop / 20-session time exit). */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { BarChart, LineChart, fmtNum, fmtPct, fmtInt, fmtSignedPct } from '../components/Chart.jsx';
import { Card, StatTile, Loading, ErrorBox, OutcomeBadge, Delta } from '../components/Bits.jsx';

const EXIT_LABEL = { upper: 'Profit-take', lower: 'Stop', vertical: 'Time exit', censored: 'Censored' };

export default function Backtest() {
  const [d, setD] = useState({ loading: true });
  const [filters, setFilters] = useState({ exit: '', ticker: '', outcome: '', limit: 50, offset: 0 });
  const [table, setTable] = useState(null);

  useEffect(() => {
    api.tradesSummary().then((s) => setD({ s })).catch((error) => setD({ error }));
  }, []);
  useEffect(() => { api.trades(filters).then(setTable).catch(() => setTable(null)); }, [filters]);

  const conviction = useMemo(() => (d.s?.by_conviction_decile || []).map((r) => ({
    label: `D${r.decile}`, value: r.avg_ret, tipLabel: 'avg net return',
    tip: (<>
      <div className="t-row"><span>Win rate</span><span>{fmtPct(r.win_rate)}</span></div>
      <div className="t-row"><span>Trades</span><span>{fmtInt(r.n)}</span></div>
    </>),
  })), [d.s]);

  if (d.loading) return <Loading what="backtest trades" />;
  if (d.error) {
    return <ErrorBox error={d.error} hint={
      <>The trade ledger comes from Stage 3. Run it on a pod, then rebuild:{' '}
        <code>scripts/launch_predict.sh stage3</code> →{' '}
        <code>python3 tools/build_reports.py</code></>} />;
  }

  const s = d.s;
  const exitMix = (s.by_exit || []).map((r) => ({
    label: EXIT_LABEL[r.exit] || r.exit,
    value: r.n,
    tipLabel: 'trades',
    tip: (<>
      <div className="t-row"><span>Avg net return</span><span>{fmtSignedPct(r.avg_ret, 2)}</span></div>
      <div className="t-row"><span>Avg hold</span><span>{fmtNum(r.avg_hold, 1)} sessions</span></div>
    </>),
  }));

  const byYearWin = [{
    name: 'Win rate',
    color: 'var(--series-1)',
    points: (s.by_year || []).map((r) => ({ x: String(r.year), y: r.win_rate })),
  }];
  const byYearRet = (s.by_year || []).map((r) => ({
    label: String(r.year), value: r.avg_ret, tipLabel: 'avg net return',
    tip: <div className="t-row"><span>Trades</span><span>{fmtInt(r.n)}</span></div>,
  }));
  const dist = (s.return_distribution || []).map((b) => ({
    label: `${(b.lo * 100).toFixed(0)}%`,
    value: b.n,
    signedBy: b.lo + (b.hi - b.lo) / 2,
    tipLabel: 'trades',
  }));
  const holds = (s.by_holding_bucket || []).map((r) => ({
    label: r.bucket, value: r.avg_ret, tipLabel: 'avg net return',
    tip: (<>
      <div className="t-row"><span>Trades</span><span>{fmtInt(r.n)}</span></div>
      <div className="t-row"><span>Win rate</span><span>{fmtPct(r.win_rate)}</span></div>
    </>),
  }));

  return (
    <>
      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="Trades proposed" value={fmtInt(s.n_trades)}
                  sub={`${s.date_range[0]} → ${s.date_range[1]}`} />
        <StatTile label="Win rate" value={fmtPct(s.win_rate)}
                  sub="net of costs" tone={s.win_rate == null ? '' : s.win_rate > 0.5 ? 'pos' : 'neg'} />
        <StatTile label="Avg net return" value={fmtSignedPct(s.avg_ret, 2)}
                  sub={`median ${fmtSignedPct(s.median_ret, 2)} per trade`}
                  tone={s.avg_ret == null ? '' : s.avg_ret >= 0 ? 'pos' : 'neg'} />
        <StatTile label="Avg hold" value={`${fmtNum(s.avg_hold, 1)}`}
                  sub="sessions (20-session vertical barrier)" />
        <StatTile label="Hit profit-take" value={fmtInt(s.total_pt)}
                  sub={`${fmtPct(s.total_pt / s.n_trades)} of trades`} tone="pos" />
        <StatTile label="Hit stop" value={fmtInt(s.total_stop)}
                  sub={`${fmtPct(s.total_stop / s.n_trades)} of trades`} tone="neg" />
      </div>

      <Card title="Did conviction pay, inside the book?"
            subtitle="Average net return by ensemble-rank decile AMONG THE TRADES TAKEN. The book only enters decile-10 names, so D1..D10 here slice that already-narrow top band — not the whole universe. A flat or noisy profile is the expected result; it says the ranking earns its keep at the selection step, not by fine-grading winners inside the book.">
        <BarChart data={conviction} height={250} bySign
                  valueFormat={(v) => fmtSignedPct(v, 2)} />
        <div className="note">
          Bars above the zero line are profitable on average (green, ▲ in the tooltip);
          below it, loss-making (red, ▼). Sign and position carry the meaning — the
          colour is only reinforcement.
        </div>
      </Card>

      <div className="grid cols-2">
        <Card title="How trades ended"
              subtitle="The triple barrier decides every exit: profit-take, stop, or the 20-session time barrier.">
          <BarChart data={exitMix} horizontal height={170}
                    valueFormat={(v) => fmtInt(v)} />
        </Card>
        <Card title="Average net return by holding length"
              subtitle="Short holds are cost-hostile by construction (G-17): two legs of friction against a small move.">
          <BarChart data={holds} height={210} bySign valueFormat={(v) => fmtSignedPct(v, 2)} />
        </Card>
      </div>

      <div className="grid cols-2">
        <Card title="Win rate by entry year"
              subtitle="Winning cross-sectionally looks like 52–55% — not 70% (G-12).">
          <LineChart series={byYearWin} height={220} zeroLine
                     yFormat={(v) => fmtPct(v, 0)} xLabels={8} />
        </Card>
        <Card title="Average net return by entry year">
          <BarChart data={byYearRet} height={220} bySign
                    valueFormat={(v) => fmtSignedPct(v, 1)} labelEvery={2} />
        </Card>
      </div>

      <Card title="Distribution of trade outcomes"
            subtitle="Net return per trade, clipped at ±50%. The mass sits near zero — cross-sectional edges are thin by nature.">
        <BarChart data={dist} height={210} labelEvery={5}
                  color="var(--series-1)" valueFormat={(v) => fmtInt(v)} />
      </Card>

      <Card title="Trade ledger"
            subtitle={table?.note || 'Individual proposals and their outcomes.'}>
        <div className="controls">
          <select value={filters.exit}
                  onChange={(e) => setFilters({ ...filters, exit: e.target.value, offset: 0 })}>
            <option value="">All exits</option>
            <option value="upper">Profit-take</option>
            <option value="lower">Stop</option>
            <option value="vertical">Time exit</option>
          </select>
          <select value={filters.outcome}
                  onChange={(e) => setFilters({ ...filters, outcome: e.target.value, offset: 0 })}>
            <option value="">Wins and losses</option>
            <option value="win">Winners only</option>
            <option value="loss">Losers only</option>
          </select>
          <input placeholder="Filter ticker…" value={filters.ticker}
                 onChange={(e) => setFilters({ ...filters, ticker: e.target.value, offset: 0 })} />
          <span className="muted small">
            {table ? `${fmtInt(table.total)} matching` : ''}
          </span>
          <div style={{ flex: 1 }} />
          <button className="btn sm" disabled={!filters.offset}
                  onClick={() => setFilters({ ...filters, offset: Math.max(0, filters.offset - filters.limit) })}>
            ← Prev
          </button>
          <button className="btn sm"
                  disabled={!table || filters.offset + filters.limit >= table.total}
                  onClick={() => setFilters({ ...filters, offset: filters.offset + filters.limit })}>
            Next →
          </button>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ticker</th><th>Signal</th><th className="n">Entry</th>
                <th>Exit date</th><th className="n">Exit</th><th>Outcome</th>
                <th className="n">Held</th><th className="n">Conviction</th>
                <th className="n">Net return</th>
              </tr>
            </thead>
            <tbody>
              {(table?.rows || []).map((r, i) => (
                <tr key={`${r.ticker}-${r.entry_date}-${i}`}>
                  <td><strong>{r.ticker}</strong></td>
                  <td className="muted">{r.entry_date}</td>
                  <td className="n">{fmtNum(r.entry_price, 2)}</td>
                  <td className="muted">{r.exit_date}</td>
                  <td className="n">{fmtNum(r.exit_price, 2)}</td>
                  <td><OutcomeBadge hit={r.barrier_hit} /></td>
                  <td className="n">{r.holding_days}</td>
                  <td className="n">{fmtNum(r.ensemble_rank, 3)}</td>
                  <td className="n"><Delta value={r.exit_ret_net} /></td>
                </tr>
              ))}
              {!table?.rows?.length && (
                <tr><td colSpan="9" className="empty">No trades match these filters.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}
