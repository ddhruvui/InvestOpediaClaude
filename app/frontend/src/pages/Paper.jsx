/* Paper-trading console — the BP15 stage the blueprint requires before real
   capital ("paper-trade 3–6 months measuring open-print slippage"), with the
   M18 ops instruments: slippage measurement, PDT budget, kill switch, and the
   G-11 decay monitor. */
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LineChart, fmtNum, fmtPct, fmtInt, fmtSignedPct } from '../components/Chart.jsx';
import { Card, StatTile, Badge, Delta, Loading, ErrorBox } from '../components/Bits.jsx';

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
      <input style={{ width: 96 }} placeholder="fill px" value={fill}
             onChange={(e) => setFill(e.target.value)} />
      <input style={{ width: 110 }} placeholder="official open" value={open}
             onChange={(e) => setOpen(e.target.value)} />
      <button className="btn sm primary" disabled={!fill} onClick={submit}>Record fill</button>
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
      <input style={{ width: 96 }} placeholder="exit px" value={px}
             onChange={(e) => setPx(e.target.value)} />
      <select value={reason} onChange={(e) => setReason(e.target.value)}>
        <option value="profit_take">Profit-take</option>
        <option value="stop">Stop</option>
        <option value="vertical">Time exit</option>
        <option value="manual">Manual</option>
      </select>
      <button className="btn sm" disabled={!px} onClick={submit}>Close</button>
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

  if (d.loading) return <Loading what="paper book" />;
  if (d.error) return <ErrorBox error={d.error} hint="Is the API running? npm start in app/backend." />;

  const { positions, stats: st, cfg, nav, account_equity: equity } = d.p;
  const ordered = positions.filter((p) => p.status === 'ordered');
  const open = positions.filter((p) => p.status === 'open');
  const closed = positions.filter((p) => p.status === 'closed')
    .sort((a, b) => (b.exit_date || '').localeCompare(a.exit_date || ''));

  const slipPct = Math.min(100,
    (st.slippage.n_fills / st.slippage.adoption_min_fills) * 100);
  const decayFloor = backtestSharpe != null ? backtestSharpe * cfg.decay.ratio_of_backtest : null;
  const decayBreached = st.decay.rolling_sharpe != null && decayFloor != null
    && st.decay.rolling_sharpe < decayFloor;

  const eqSeries = st.realized.equity.length > 1 ? [{
    name: 'Paper book (realized)',
    color: 'var(--series-1)',
    points: st.realized.equity.map((p) => ({ x: p.date, y: p.equity })),
  }] : null;

  return (
    <>
      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="Ordered" value={fmtInt(st.counts.ordered)} sub="awaiting the next open" />
        <StatTile label="Open" value={fmtInt(st.counts.open)} sub="live paper positions" />
        <StatTile label="Closed" value={fmtInt(st.counts.closed)}
                  sub={st.realized.win_rate != null ? `win rate ${fmtPct(st.realized.win_rate)}` : '—'} />
        <StatTile label="Avg realized"
                  value={st.realized.avg_ret != null ? fmtSignedPct(st.realized.avg_ret, 2) : '—'}
                  sub="net of the C-08 cost model"
                  tone={st.realized.avg_ret == null ? ''
                    : st.realized.avg_ret >= 0 ? 'pos' : 'neg'} />
        <StatTile label="Paper Sharpe" value={fmtNum(st.realized.sharpe_all, 2)}
                  sub={backtestSharpe != null ? `backtest ${fmtNum(backtestSharpe, 2)}` : ''} />
      </div>

      <div className="grid cols-3">
        <Card title="Open-print slippage (M18 → M15-03)"
              subtitle="The measurement this whole stage exists to collect.">
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
              ? <><span aria-hidden="true">✓ </span>Adopted into the cost model.</>
              : `${st.slippage.n_fills} / ${st.slippage.adoption_min_fills} fills before adoption`}
          </div>
        </Card>

        <Card title="Compliance" subtitle="PDT budget and the daily kill switch.">
          <div style={{ display: 'grid', gap: 10 }}>
            <div>
              <div className="small muted">Same-day round trips (rolling {st.pdt.window_business_days} business days)</div>
              <div style={{ fontSize: 22, fontWeight: 620 }} className="num">
                {st.pdt.used} / {st.pdt.limit}{' '}
                <Badge kind={st.pdt.enforced ? (st.pdt.used >= st.pdt.limit ? 'fail' : 'neutral') : 'neutral'}>
                  {st.pdt.enforced ? 'enforced (< $25k)' : 'not enforced'}
                </Badge>
              </div>
            </div>
            <div>
              <div className="small muted">Kill switch (day loss ≥ {fmtPct(st.kill_switch.threshold_pct, 0)})</div>
              <div style={{ fontSize: 18, fontWeight: 600 }}>
                <Badge kind={st.kill_switch.tripped ? 'fail' : 'pass'}>
                  {st.kill_switch.tripped ? 'TRIPPED — new orders halted' : 'Armed'}
                </Badge>
              </div>
            </div>
          </div>
        </Card>

        <Card title="Decay monitor (G-11 / G-16)"
              subtitle={`Rolling ${st.decay.window_sessions}-session paper Sharpe against the backtest.`}>
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
                  {decayBreached ? 'Below ½ backtest — watch' : 'Healthy'}
                </Badge>}
          </div>
        </Card>
      </div>

      {eqSeries && (
        <Card title="Realized paper equity"
              subtitle="Growth of 1 on closed trades only — a paper book has no intraday marks, so realized P&L is the honest series to judge decay on.">
          <LineChart series={eqSeries} height={230} yFormat={(v) => `${fmtNum(v, 3)}×`} />
        </Card>
      )}

      {ordered.length > 0 && (
        <Card title="Awaiting fill"
              subtitle="Orders sit here until the next open. Record the fill and the official open print — the difference is the slippage measurement.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th>Ticker</th><th>Signal date</th><th className="n">Weight</th>
                <th className="n">Ref close</th><th className="n">Stop</th>
                <th className="n">Profit-take</th><th>Record fill</th><th></th>
              </tr></thead>
              <tbody>
                {ordered.map((p) => (
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
        <Card title="Open positions"
              subtitle="Barrier levels are computed from the actual fill, exactly as the backtest does it (M5.2).">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th>Ticker</th><th>Filled</th><th className="n">Fill</th>
                <th className="n">Slippage</th><th className="n">Stop level</th>
                <th className="n">Profit-take level</th><th className="n">Weight</th><th>Close</th>
              </tr></thead>
              <tbody>
                {open.map((p) => (
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
            Slippage sign convention: negative means the fill beat the official open
            (▲ favourable); positive means it lagged (▼ costly).
          </div>
        </Card>
      )}

      <Card title="Closed paper trades"
            subtitle="Realized outcomes, net of the same cost model the backtest uses."
            right={<button className="btn sm" onClick={load}>Refresh</button>}>
        <div className="table-wrap">
          <table>
            <thead><tr>
              <th>Ticker</th><th>Filled</th><th>Exited</th><th>Reason</th>
              <th className="n">Fill</th><th className="n">Exit</th>
              <th className="n">Held</th><th className="n">Net return</th>
            </tr></thead>
            <tbody>
              {closed.map((p) => (
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
                  No closed paper trades yet. Push a suggestion into the book from the
                  Suggestions tab, record its fill, then close it when a barrier is touched.
                </td></tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>

      <Card title="Book settings">
        <div className="controls">
          <label className="small muted">NAV
            <input style={{ width: 130, marginLeft: 6 }} defaultValue={nav}
                   onBlur={(e) => api.paperSettings({ nav: Number(e.target.value) }).then(load)} />
          </label>
          <label className="small muted">Account equity (PDT)
            <input style={{ width: 130, marginLeft: 6 }} defaultValue={equity ?? ''}
                   placeholder="unset"
                   onBlur={(e) => api.paperSettings({
                     account_equity: e.target.value === '' ? null : Number(e.target.value),
                   }).then(load)} />
          </label>
          <span className="muted small">
            Under ${fmtInt(cfg.pdt.equity_floor)} the PDT budget is enforced (≤{cfg.pdt.limit} same-day
            round trips per {cfg.pdt.window_business_days} business days).
          </span>
        </div>
      </Card>
    </>
  );
}
