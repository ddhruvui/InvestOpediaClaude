/* The trade ticket: what to do at the NEXT open.
   Answers the two questions the raw suggestion file cannot — which session these
   orders belong to (Friday evening and all weekend both point at Monday), and
   which names are genuinely new versus already held. */
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { fmtNum, fmtPct, fmtInt } from '../components/Chart.jsx';
import { Card, StatTile, Badge, Loading, ErrorBox } from '../components/Bits.jsx';

const DOW = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const pretty = (iso) => {
  if (!iso) return '—';
  const d = new Date(`${iso}T12:00:00Z`);
  return `${DOW[d.getUTCDay()]} ${d.getUTCDate()} ${
    d.toLocaleString('en', { month: 'short', timeZone: 'UTC' })}`;
};

const STATE_TEXT = {
  closed: 'Market closed',
  pre_open: 'Pre-open',
  open: 'Market open',
  post_close: 'After the close',
};

function ActionRow({ r, kind, onAct, busy }) {
  const wide = r.barrier_unreachable;
  return (
    <tr>
      <td><strong>{r.ticker}</strong></td>
      <td>
        <Badge kind={kind === 'BUY' ? 'pass' : kind === 'SELL' ? 'fail' : 'neutral'}
               glyph={kind === 'BUY' ? '▲' : kind === 'SELL' ? '▼' : '='}>
          {kind === 'BUY' ? 'Buy' : kind === 'SELL' ? 'Sell' : 'Hold'}
        </Badge>
      </td>
      <td className="n">{r.target_weight ? fmtPct(r.target_weight, 2) : '—'}</td>
      <td className="n">{r.ensemble_rank != null ? fmtNum(r.ensemble_rank, 3) : '—'}</td>
      <td className="n">{r.last_close != null ? fmtNum(r.last_close, 2)
        : r.fill_price != null ? fmtNum(r.fill_price, 2) : '—'}</td>
      <td className="n neg">{r.stop_pct != null ? `${fmtNum(r.stop_pct, 2)}%` : '—'}</td>
      <td className="n pos">{r.profit_take_pct != null ? `+${fmtNum(r.profit_take_pct, 2)}%` : '—'}</td>
      <td className="small muted" style={{ whiteSpace: 'normal', maxWidth: 260 }}>
        {wide ? <Badge kind="neutral">⏱ time-exit only</Badge> : r.reason}
      </td>
      <td>
        {onAct && (
          <button className="btn sm" disabled={busy === r.ticker} onClick={() => onAct(r)}>
            {busy === r.ticker ? '…' : kind === 'BUY' ? 'Add to paper' : 'Close in paper'}
          </button>
        )}
      </td>
    </tr>
  );
}

export default function Today() {
  const [d, setD] = useState({ loading: true });
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);
  const [showHolds, setShowHolds] = useState(false);

  const load = () => api.today().then((t) => setD({ t })).catch((error) => setD({ error }));
  useEffect(() => { load(); }, []);

  if (d.loading) return <Loading what="today's ticket" />;
  if (d.error) return <ErrorBox error={d.error} hint="Is the API running, and is the report bundle built?" />;

  const t = d.t;
  if (t.error) return <ErrorBox error={t.error} hint="Run the predict job, then rebuild the bundle." />;
  const s = t.session;

  const buy = async (r) => {
    setBusy(r.ticker); setMsg(null);
    try {
      await api.paperOpen({
        ticker: r.ticker, side: 1, target_weight: r.target_weight,
        signal_date: t.signals.as_of_close, ref_close: r.last_close,
        stop_pct: r.stop_pct, profit_take_pct: r.profit_take_pct,
        max_hold_sessions: r.max_hold_sessions, ensemble_rank: r.ensemble_rank,
      });
      setMsg({ ok: true, text: `${r.ticker} queued as a market-on-open order for ${pretty(s.next_open)}.` });
      load();
    } catch (e) { setMsg({ ok: false, text: `${r.ticker}: ${e.message}` }); }
    finally { setBusy(null); }
  };

  return (
    <>
      <Card>
        <div style={{ display: 'flex', justifyContent: 'space-between',
                      alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
          <div>
            <h1>Orders for the {pretty(s.next_open)} open</h1>
            <p className="muted small" style={{ margin: '4px 0 0' }}>
              Signals from the {pretty(t.signals.as_of_close)} close.{' '}
              {s.is_weekend
                ? 'It is the weekend — the next session is Monday.'
                : s.market_state === 'post_close'
                  ? 'Today has closed; these fill at the next session\'s open.'
                  : s.market_state === 'pre_open'
                    ? 'Before the bell — these fill at today\'s open.'
                    : s.market_state === 'open'
                      ? 'The market is open; the opening auction has passed.'
                      : 'Market closed today.'}
            </p>
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <Badge kind="neutral">{STATE_TEXT[s.market_state]}</Badge>
            <Badge kind={t.signals.fresh ? 'pass' : 'fail'}>
              {t.signals.fresh ? 'Signals current' : 'Signals stale'}
            </Badge>
          </div>
        </div>
      </Card>

      {!t.signals.fresh && (
        <div className="verdict bad">
          <div className="mark" aria-hidden="true">✕</div>
          <div>
            <h2>These signals are out of date</h2>
            <p>{t.signals.stale_reason}</p>
          </div>
        </div>
      )}

      {s.open_already_passed && (
        <div className="verdict warn">
          <div className="mark" aria-hidden="true">⚠</div>
          <div>
            <h2>Today's opening auction has already passed</h2>
            <p>This system fills market-on-open. Entering now means a different
              price than the one the backtest assumes, so the measured edge no
              longer applies to these fills.</p>
          </div>
        </div>
      )}

      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="New buys" value={fmtInt(t.counts.buy)}
                  sub="in the target book, not held" tone="pos" />
        <StatTile label="Sells" value={fmtInt(t.counts.sell)}
                  sub="held, no longer wanted" tone={t.counts.sell ? 'neg' : ''} />
        <StatTile label="Holds" value={fmtInt(t.counts.hold)} sub="already positioned" />
        <StatTile label="Time-barrier exits due" value={fmtInt(t.counts.due_exit)}
                  sub="scheduled MOO exits" tone={t.counts.due_exit ? 'neg' : ''} />
        <StatTile label="Currently held" value={fmtInt(t.counts.held_total)}
                  sub="in the paper book" />
      </div>

      <div className="verdict warn">
        <div className="mark" aria-hidden="true">⚠</div>
        <div>
          <h2>Research output — the gates say do not trade this book</h2>
          <p>{t.gate_warning} Holdings come from {t.holdings_source}</p>
        </div>
      </div>

      {msg && (
        <div className="note" style={{
          borderLeft: `3px solid ${msg.ok ? 'var(--profit)' : 'var(--loss)'}`, marginBottom: 16,
        }}>
          <span aria-hidden="true">{msg.ok ? '✓ ' : '✕ '}</span>{msg.text}
        </div>
      )}

      {t.due_exits.length > 0 && (
        <Card title="Exits due at this open (time barrier)"
              subtitle="The vertical barrier is a scheduled market-on-open exit at fill + h sessions. It is due regardless of what the model now says about the name.">
          <div className="table-wrap">
            <table>
              <thead><tr><th>Ticker</th><th>Filled</th><th>Vertical date</th><th>Why</th></tr></thead>
              <tbody>
                {t.due_exits.map((r) => (
                  <tr key={r.position_id}>
                    <td><strong>{r.ticker}</strong></td>
                    <td className="muted">{r.fill_date}</td>
                    <td className="muted">{r.vertical_date}</td>
                    <td className="small muted">{r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {t.sells.length > 0 && (
        <Card title={`Sell — ${t.sells.length}`}
              subtitle="Positions you hold that have dropped out of the target book.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <th>Ticker</th><th>Action</th><th className="n">Target</th>
                <th className="n">Conviction</th><th className="n">Price</th>
                <th className="n">Stop</th><th className="n">Profit-take</th><th>Why</th><th></th>
              </tr></thead>
              <tbody>
                {t.sells.map((r) => (
                  <ActionRow key={r.ticker} r={r} kind="SELL" busy={busy} />
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <Card title={`Buy — ${t.buys.length} new positions`}
            subtitle={`Not currently held. Each fills market-on-open on ${pretty(s.next_open)} and carries its triple-barrier exits from the actual fill price.`}>
        <div className="table-wrap" style={{ maxHeight: 640, overflowY: 'auto' }}>
          <table>
            <thead><tr>
              <th>Ticker</th><th>Action</th><th className="n">Target weight</th>
              <th className="n">Conviction</th><th className="n">Last close</th>
              <th className="n">Stop</th><th className="n">Profit-take</th>
              <th>Why</th><th></th>
            </tr></thead>
            <tbody>
              {t.buys.map((r) => (
                <ActionRow key={r.ticker} r={r} kind="BUY" onAct={buy} busy={busy} />
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      {t.holds.length > 0 && (
        <Card title={`Hold — ${t.holds.length}`}
              subtitle="Already held and still in the target book: no action."
              right={<button className="btn sm" onClick={() => setShowHolds(!showHolds)}>
                {showHolds ? 'Hide' : 'Show'}
              </button>}>
          {showHolds && (
            <div className="table-wrap">
              <table>
                <thead><tr>
                  <th>Ticker</th><th>Action</th><th className="n">Target</th>
                  <th className="n">Conviction</th><th className="n">Fill</th>
                  <th className="n">Stop</th><th className="n">Profit-take</th><th>Status</th><th></th>
                </tr></thead>
                <tbody>
                  {t.holds.map((r) => (
                    <ActionRow key={r.ticker} r={{ ...r, reason: r.status }} kind="HOLD" busy={busy} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
