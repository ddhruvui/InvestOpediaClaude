/* Paper-trading console — the BP15 stage the blueprint requires before real
   capital ("paper-trade 3–6 months measuring open-print slippage"), with the
   M18 ops instruments: slippage measurement, PDT budget, kill switch, and the
   G-11 decay monitor. */
import { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { LineChart, fmtNum, fmtPct, fmtInt, fmtSignedPct } from '../components/Chart.jsx';
import { Card, StatTile, Badge, Delta, Loading, ErrorBox, useSort, Th } from '../components/Bits.jsx';

const EMPTY = [];

function FillForm({ pos, onDone }) {
  const [fill, setFill] = useState('');
  const [open, setOpen] = useState('');
  const [err, setErr] = useState(null);
  const submit = async () => {
    try {
      await api.paperFill(pos.id, {
        fill_price: Number(fill),
        official_open: open === '' ? null : Number(open),
      });
      onDone();
    } catch (e) { setErr(e.message); }
  };
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      <input style={{ width: 100 }} placeholder="price you paid" value={fill}
             onChange={(e) => setFill(e.target.value)} />
      <input style={{ width: 110 }} placeholder="official open" value={open}
             onChange={(e) => setOpen(e.target.value)} />
      <button className="btn sm primary" disabled={!fill} onClick={submit}>Record it</button>
      {err && <span className="neg small">✕ {err}</span>}
    </div>
  );
}

function CloseForm({ pos, onDone }) {
  const [px, setPx] = useState('');
  const [reason, setReason] = useState('manual');
  const [err, setErr] = useState(null);
  const submit = async () => {
    try {
      await api.paperClose(pos.id, { exit_price: Number(px), reason });
      onDone();
    } catch (e) { setErr(e.message); }
  };
  return (
    <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
      <input style={{ width: 100 }} placeholder="price you sold at" value={px}
             onChange={(e) => setPx(e.target.value)} />
      <select value={reason} onChange={(e) => setReason(e.target.value)}>
        <option value="profit_take">Hit profit target</option>
        <option value="stop">Hit stop-loss</option>
        <option value="vertical">Time limit reached</option>
        <option value="manual">My own call</option>
      </select>
      <button className="btn sm" disabled={!px} onClick={submit}>Sold it</button>
      {err && <span className="neg small">✕ {err}</span>}
    </div>
  );
}

export default function Paper() {
  const [d, setD] = useState({ loading: true });
  const [backtestSharpe, setBacktestSharpe] = useState(null);

  const load = () => api.paper().then((p) => setD({ p })).catch((error) => setD({ error }));
  useEffect(() => {
    load();
    api.summary().then((s) => setBacktestSharpe(s.book?.sharpe_net)).catch(() => {});
  }, []);

  const positions = d.p?.positions ?? EMPTY;
  const ordered = useMemo(() => positions.filter((p) => p.status === 'ordered'), [positions]);
  const open = useMemo(() => positions.filter((p) => p.status === 'open'), [positions]);
  const closed = useMemo(() => positions.filter((p) => p.status === 'closed')
    .sort((a, b) => (b.exit_date || '').localeCompare(a.exit_date || '')), [positions]);
  const orderedSort = useSort(ordered);
  const openSort = useSort(open);
  const closedSort = useSort(closed);

  if (d.loading) return <Loading what="paper book" />;
  if (d.error) return <ErrorBox error={d.error} hint="Is the API reachable? Check VITE_API_BASE and the backend's /api/health." />;

  const { stats: st, cfg, nav, account_equity: equity } = d.p;

  const slipPct = Math.min(100,
    (st.slippage.n_fills / st.slippage.adoption_min_fills) * 100);
  const decayFloor = backtestSharpe != null ? backtestSharpe * cfg.decay.ratio_of_backtest : null;
  const decayBreached = st.decay.rolling_sharpe != null && decayFloor != null
    && st.decay.rolling_sharpe < decayFloor;

  const eqSeries = st.realized.equity.length > 1 ? [{
    name: 'Paper book (realized)',
    color: 'var(--series-1)',
    points: st.realized.equity.map((p) => ({ x: p.date, y: p.equity - 1 })),
  }] : null;

  return (
    <>
      <Card>
        <h1>Your practice book</h1>
        <p className="muted small" style={{ margin: '4px 0 0' }}>
          Trades you chose to track, run with pretend money. When one "fills" at the
          open, record the price here — comparing it with the official opening print
          is the whole point of this practice stage.
        </p>
      </Card>

      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="Waiting to fill" value={fmtInt(st.counts.ordered)}
                  sub="orders for the next open" />
        <StatTile label="Open" value={fmtInt(st.counts.open)} sub="trades you're in right now" />
        <StatTile label="Finished" value={fmtInt(st.counts.closed)}
                  sub={st.realized.win_rate != null
                    ? `${fmtPct(st.realized.win_rate)} made money` : '—'} />
        <StatTile label="Avg profit per trade"
                  value={st.realized.avg_ret != null ? fmtSignedPct(st.realized.avg_ret, 2) : '—'}
                  sub="after trading costs"
                  tone={st.realized.avg_ret == null ? ''
                    : st.realized.avg_ret >= 0 ? 'pos' : 'neg'} />
        <StatTile label="Risk-adjusted score" value={fmtNum(st.realized.sharpe_all, 2)}
                  sub={backtestSharpe != null
                    ? `Sharpe · the backtest scored ${fmtNum(backtestSharpe, 2)}` : 'Sharpe'} />
      </div>

      <div className="grid cols-3">
        <Card title="Do you get the price you expect?"
              subtitle="The gap between the official opening price and the price you actually got (slippage) — the measurement this whole practice stage exists to collect. 100 bps = 1%.">
          <div style={{ fontSize: 26, fontWeight: 620 }} className="num">
            {st.slippage.median_bps == null ? '—' : `${fmtNum(st.slippage.median_bps, 1)} bps`}
          </div>
          <div className="small muted">
            median of {fmtInt(st.slippage.n_fills)} fills
            {st.slippage.mad_bps != null && ` · MAD ${fmtNum(st.slippage.mad_bps, 1)} bps`}
          </div>
          <div style={{
            height: 6, borderRadius: 3, background: 'var(--gridline)', marginTop: 10,
          }}>
            <div style={{
              width: `${slipPct}%`, height: '100%', borderRadius: 3,
              background: st.slippage.adopted ? 'var(--profit)' : 'var(--series-1)',
            }} />
          </div>
          <div className="small muted" style={{ marginTop: 6 }}>
            {st.slippage.adopted
              ? <><span aria-hidden="true">✓ </span>Enough data — now baked into the cost model.</>
              : `${st.slippage.n_fills} of the ${st.slippage.adoption_min_fills} recorded fills needed before it counts`}
          </div>
        </Card>

        <Card title="Safety rules"
              subtitle="The day-trading limit and the daily loss cut-off, enforced automatically.">
          <div style={{ display: 'grid', gap: 10 }}>
            <div>
              <div className="small muted">Bought &amp; sold the same day (last {st.pdt.window_business_days} business days) — US rules allow max 3 under $25k</div>
              <div style={{ fontSize: 22, fontWeight: 620 }} className="num">
                {st.pdt.used} / {st.pdt.limit}{' '}
                <Badge kind={st.pdt.enforced ? (st.pdt.used >= st.pdt.limit ? 'fail' : 'neutral') : 'neutral'}>
                  {st.pdt.enforced ? 'enforced (< $25k)' : 'not enforced'}
                </Badge>
              </div>
            </div>
            <div>
              <div className="small muted">Bad-day brake: lose more than {fmtPct(st.kill_switch.threshold_pct, 0)} in one day and no new trades are allowed</div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>
                <Badge kind={st.kill_switch.tripped ? 'fail' : 'pass'}>
                  {st.kill_switch.tripped ? 'TRIPPED — no new trades today' : 'Not tripped'}
                </Badge>
              </div>
            </div>
          </div>
        </Card>

        <Card title="Is the strategy fading?"
              subtitle={`Your last ${st.decay.window_sessions} sessions of practice results, scored against what the backtest promised. If it stays below half the promise for too long, the strategy gets retrained or retired.`}>
          <div style={{ fontSize: 26, fontWeight: 620 }} className="num">
            {st.decay.rolling_sharpe == null ? '—' : fmtNum(st.decay.rolling_sharpe, 2)}
          </div>
          <div className="small muted">
            {st.decay.sessions_recorded} / {st.decay.window_sessions} sessions recorded
            {decayFloor != null && ` · retire floor ${fmtNum(decayFloor, 2)}`}
          </div>
          <div style={{ marginTop: 8 }}>
            {st.decay.rolling_sharpe == null
              ? <Badge kind="neutral">Not enough history yet</Badge>
              : <Badge kind={decayBreached ? 'fail' : 'pass'}>
                  {decayBreached ? 'Doing worse than promised — watch it' : 'Healthy'}
                </Badge>}
          </div>
        </Card>
      </div>

      {eqSeries && (
        <Card title="How your practice money has done"
              subtitle="% change since you started, counting finished trades only — open ones aren't counted until they close, so this is the honest number.">
          <LineChart series={eqSeries} height={230} zeroLine
                     yFormat={(v) => fmtSignedPct(v, 2)} />
        </Card>
      )}

      {ordered.length > 0 && (
        <Card title="Waiting to fill"
              subtitle="These sit here until the market next opens. Once you buy, enter the price you paid and the day's official opening price — the gap between them is the slippage measurement.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <Th k="ticker" sort={orderedSort.sort} onSort={orderedSort.onSort}>Stock</Th>
                <Th k="signal_date" sort={orderedSort.sort} onSort={orderedSort.onSort}>Picked on</Th>
                <Th k="target_weight" num sort={orderedSort.sort} onSort={orderedSort.onSort}>Slice of pot</Th>
                <Th k="ref_close" num sort={orderedSort.sort} onSort={orderedSort.onSort}>Close that day</Th>
                <Th k="stop_pct" num sort={orderedSort.sort} onSort={orderedSort.onSort}>Stop</Th>
                <Th k="profit_take_pct" num sort={orderedSort.sort} onSort={orderedSort.onSort}>Profit-take</Th>
                <th>What did you pay?</th><th></th>
              </tr></thead>
              <tbody>
                {orderedSort.rows.map((p) => (
                  <tr key={p.id}>
                    <td><strong>{p.ticker}</strong></td>
                    <td className="muted">{p.signal_date}</td>
                    <td className="n">{fmtPct(p.target_weight, 2)}</td>
                    <td className="n">{fmtNum(p.ref_close, 2)}</td>
                    <td className="n neg">{fmtNum(p.stop_pct, 2)}%</td>
                    <td className="n pos">+{fmtNum(p.profit_take_pct, 2)}%</td>
                    <td><FillForm pos={p} onDone={load} /></td>
                    <td>
                      <button className="btn sm" onClick={() => api.paperRemove(p.id).then(load)}>
                        Cancel
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {open.length > 0 && (
        <Card title="Trades you're in"
              subtitle="The sell triggers below come from the price you actually paid, exactly as the backtest computes them.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <Th k="ticker" sort={openSort.sort} onSort={openSort.onSort}>Stock</Th>
                <Th k="fill_date" sort={openSort.sort} onSort={openSort.onSort}>Bought</Th>
                <Th k="fill_price" num sort={openSort.sort} onSort={openSort.onSort}>You paid</Th>
                <Th k="slip_bps" num sort={openSort.sort} onSort={openSort.onSort}>Slippage</Th>
                <Th k="stop_price" num sort={openSort.sort} onSort={openSort.onSort}>Sell if it drops to</Th>
                <Th k="profit_take_price" num sort={openSort.sort} onSort={openSort.onSort}>Sell if it climbs to</Th>
                <Th k="target_weight" num sort={openSort.sort} onSort={openSort.onSort}>Slice of pot</Th>
                <th>Close it out</th>
              </tr></thead>
              <tbody>
                {openSort.rows.map((p) => (
                  <tr key={p.id}>
                    <td><strong>{p.ticker}</strong></td>
                    <td className="muted">{p.fill_date}</td>
                    <td className="n">{fmtNum(p.fill_price, 2)}</td>
                    <td className="n">
                      {p.slip_bps == null ? <span className="muted">—</span>
                        : <span className={p.slip_bps <= 0 ? 'pos' : 'neg'}>
                            <span aria-hidden="true">{p.slip_bps <= 0 ? '▲' : '▼'}</span>{' '}
                            {fmtNum(p.slip_bps, 1)} bps
                          </span>}
                    </td>
                    <td className="n neg">{fmtNum(p.stop_price, 2)}</td>
                    <td className="n pos">{fmtNum(p.profit_take_price, 2)}</td>
                    <td className="n">{fmtPct(p.target_weight, 2)}</td>
                    <td><CloseForm pos={p} onDone={load} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="note">
            Slippage: negative means you got a better price than the official open
            (▲ good for you); positive means you paid up (▼ costly). 100 bps = 1%.
          </div>
        </Card>
      )}

      <Card title="Finished trades"
            subtitle="What each practice trade made or lost, with the same trading costs the backtest charges."
            right={<button className="btn sm" onClick={load}>Refresh</button>}>
        <div className="table-wrap">
          <table>
            <thead><tr>
              <Th k="ticker" sort={closedSort.sort} onSort={closedSort.onSort}>Stock</Th>
              <Th k="fill_date" sort={closedSort.sort} onSort={closedSort.onSort}>Bought</Th>
              <Th k="exit_date" sort={closedSort.sort} onSort={closedSort.onSort}>Sold</Th>
              <Th k="exit_reason" sort={closedSort.sort} onSort={closedSort.onSort}>Why sold</Th>
              <Th k="fill_price" num sort={closedSort.sort} onSort={closedSort.onSort}>Paid</Th>
              <Th k="exit_price" num sort={closedSort.sort} onSort={closedSort.onSort}>Got</Th>
              <Th k="holding_days" num sort={closedSort.sort} onSort={closedSort.onSort}>Days held</Th>
              <Th k="ret_net" num sort={closedSort.sort} onSort={closedSort.onSort}>Profit / loss</Th>
            </tr></thead>
            <tbody>
              {closedSort.rows.map((p) => (
                <tr key={p.id}>
                  <td><strong>{p.ticker}</strong></td>
                  <td className="muted">{p.fill_date}</td>
                  <td className="muted">{p.exit_date}</td>
                  <td>{p.exit_reason?.replace('_', ' ')}{p.day_trade && ' · day trade'}</td>
                  <td className="n">{fmtNum(p.fill_price, 2)}</td>
                  <td className="n">{fmtNum(p.exit_price, 2)}</td>
                  <td className="n">{p.holding_days}</td>
                  <td className="n"><Delta value={p.ret_net} /></td>
                </tr>
              ))}
              {!closed.length && (
                <tr><td colSpan="8" className="empty">
                  No finished trades yet. Press "Track this trade" on the Today page,
                  record what you paid when it fills, then close it here when one of its
                  sell triggers is hit.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Settings"
            subtitle="The practice pot sizes the share counts on the Today page. Set your real account size too if you want the day-trading limit enforced realistically.">
        <div className="controls">
          <label className="small muted">Practice pot ($)
            <input style={{ width: 130, marginLeft: 6 }} defaultValue={nav}
                   onBlur={(e) => api.paperSettings({ nav: Number(e.target.value) }).then(load)} />
          </label>
          <label className="small muted">Real account size ($)
            <input style={{ width: 130, marginLeft: 6 }} defaultValue={equity ?? ''}
                   placeholder="unset"
                   onBlur={(e) => api.paperSettings({
                     account_equity: e.target.value === '' ? null : Number(e.target.value),
                   }).then(load)} />
          </label>
          <span className="muted small">
            US brokers limit accounts under ${fmtInt(cfg.pdt.equity_floor)} to{' '}
            {cfg.pdt.limit} same-day buy-and-sells per {cfg.pdt.window_business_days}{' '}
            business days — the book enforces that when your real account is below the line.
          </span>
        </div>
      </Card>
    </>
  );
}
