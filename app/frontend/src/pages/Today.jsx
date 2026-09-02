/* The trade ticket: what to do at the NEXT open, in plain terms.
   Answers the two questions the raw suggestion file cannot — which session these
   orders belong to (Friday evening and all weekend both point at Monday), and
   which names are genuinely new versus already held.
   Default view speaks broker-ticket language (shares, dollars, trigger prices);
   the quant columns (weight, conviction, barrier %s) live behind a toggle. */
import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { fmtNum, fmtPct, fmtInt } from '../components/Chart.jsx';
import { Card, StatTile, Badge, Loading, ErrorBox, useSort, Th } from '../components/Bits.jsx';

const EMPTY = [];

const DOW = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const pretty = (iso) => {
  if (!iso) return '—';
  const d = new Date(`${iso}T12:00:00Z`);
  return `${DOW[d.getUTCDay()]} ${d.getUTCDate()} ${
    d.toLocaleString('en', { month: 'short', timeZone: 'UTC' })}`;
};

const fmtMoney = (v) => (v == null || Number.isNaN(v)
  ? '—' : `$${Math.round(v).toLocaleString('en-US')}`);
const fmtPrice = (v) => (v == null || Number.isNaN(v)
  ? '—' : `$${Number(v).toFixed(2)}`);

const STATE_TEXT = {
  closed: 'Market closed',
  pre_open: 'Pre-open',
  open: 'Market open',
  post_close: 'After the close',
};

/* ------------------------------------------------- simple (default) rows */

function BuyRowSimple({ r, onAct, busy }) {
  const noTriggers = r.barrier_unreachable;
  const fractional = r.shares === 0;
  return (
    <tr>
      <td><strong>{r.ticker}</strong></td>
      <td>
        <strong>{fractional ? `Buy ${fmtMoney(r.est_cost)} worth`
          : r.shares != null ? `Buy ${fmtInt(r.shares)} shares` : 'Buy'}</strong>
        <div className="small muted">
          {fractional
            ? 'less than one whole share — needs fractional shares, or skip it'
            : 'at the open'}
        </div>
      </td>
      <td className="n">{fmtPrice(r.last_close)}</td>
      <td className="n">
        {fmtMoney(r.est_cost)}
        <div className="small muted">{fmtPct(r.target_weight, 1)} of your money</div>
      </td>
      {noTriggers ? (
        <td colSpan="2" className="small muted">
          no price triggers — just sell by the date →
        </td>
      ) : (
        <>
          <td className="n neg">{fmtPrice(r.stop_price)}</td>
          <td className="n pos">{fmtPrice(r.profit_take_price)}</td>
        </>
      )}
      <td>
        {r.sell_by_date ? pretty(r.sell_by_date) : `${r.max_hold_sessions} trading days`}
        {r.sell_by_date && (
          <div className="small muted">{r.max_hold_sessions} trading days</div>
        )}
      </td>
      <td>
        {onAct && (
          <button className="btn sm" disabled={busy === r.ticker} onClick={() => onAct(r)}>
            {busy === r.ticker ? '…' : 'Track this trade'}
          </button>
        )}
      </td>
    </tr>
  );
}

function HoldRowSimple({ r }) {
  return (
    <tr>
      <td><strong>{r.ticker}</strong></td>
      <td>
        <strong>Do nothing</strong>
        <div className="small muted">
          {r.status === 'ordered' ? 'order already placed, waiting to fill' : 'keep holding it'}
        </div>
      </td>
      <td className="n">{r.fill_price != null ? fmtPrice(r.fill_price) : '—'}</td>
      {r.barrier_unreachable ? (
        <td colSpan="2" className="small muted">
          no price triggers — just sell by the date →
        </td>
      ) : (
        <>
          <td className="n neg">{fmtPrice(r.stop_price)}</td>
          <td className="n pos">{fmtPrice(r.profit_take_price)}</td>
        </>
      )}
      <td>{r.sell_by_date ? pretty(r.sell_by_date) : '—'}</td>
    </tr>
  );
}

/* --------------------------------------------------- detailed-view row */

function ActionRow({ r, kind }) {
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
    </tr>
  );
}

function DetailHead({ sort, onSort }) {
  const p = { sort, onSort };
  return (
    <tr>
      <Th k="ticker" {...p}>Ticker</Th><th>Action</th>
      <Th k="target_weight" num {...p}>Target weight</Th>
      <Th k="ensemble_rank" num {...p}>Conviction</Th>
      <Th k="last_close" num {...p}>Price</Th>
      <Th k="stop_pct" num {...p}>Stop</Th>
      <Th k="profit_take_pct" num {...p}>Profit-take</Th><th>Why</th>
    </tr>
  );
}

/* ---------------------------------------------------------------- page */

const SELL_REASON = {
  'dropped out of the target book': 'the model no longer ranks it worth holding',
  'model flagged an exit': 'the model says it is time to get out',
};

export default function Today() {
  const [d, setD] = useState({ loading: true });
  const [busy, setBusy] = useState(null);
  const [msg, setMsg] = useState(null);
  const [showHolds, setShowHolds] = useState(false);
  const [detail, setDetail] = useState(false);
  const [showHelp, setShowHelp] = useState(false);

  const load = () => api.today().then((t) => setD({ t })).catch((error) => setD({ error }));
  useEffect(() => { load(); }, []);

  const t = d.t ?? {};
  const buySort = useSort(t.buys ?? EMPTY);
  const sellSort = useSort(t.sells ?? EMPTY);
  const holdSort = useSort(t.holds ?? EMPTY);
  const dueSort = useSort(t.due_exits ?? EMPTY);

  if (d.loading) return <Loading what="today's plan" />;
  if (d.error) return <ErrorBox error={d.error} hint="Is the API running, and is the report bundle built?" />;
  if (t.error) return <ErrorBox error={t.error} hint="Run the predict job, then rebuild the bundle." />;
  const s = t.session;
  const nav = t.plan?.nav;

  const buy = async (r) => {
    setBusy(r.ticker); setMsg(null);
    try {
      await api.paperOpen({
        ticker: r.ticker, side: 1, target_weight: r.target_weight,
        signal_date: t.signals.as_of_close, ref_close: r.last_close,
        stop_pct: r.stop_pct, profit_take_pct: r.profit_take_pct,
        max_hold_sessions: r.max_hold_sessions, ensemble_rank: r.ensemble_rank,
      });
      setMsg({ ok: true, text: `${r.ticker} is now tracked in your practice book — it "fills" at the ${pretty(s.next_open)} open.` });
      load();
    } catch (e) { setMsg({ ok: false, text: `${r.ticker}: ${e.message}` }); }
    finally { setBusy(null); }
  };

  // The one-sentence version of the whole page.
  const planBits = [];
  if (t.counts.due_exit) planBits.push(`sell ${t.counts.due_exit} whose time is up`);
  if (t.counts.sell) planBits.push(`sell ${t.counts.sell} the model dropped`);
  if (t.counts.buy) {
    planBits.push(`buy ${t.counts.buy} stock${t.counts.buy === 1 ? '' : 's'} for about ${
      fmtMoney(t.plan?.invest_total)}`);
  }
  if (t.counts.hold) planBits.push(`leave ${t.counts.hold} alone`);
  const planText = planBits.length
    ? `${planBits.join(', then ')}.` : 'nothing — there are no orders for this open.';

  return (
    <>
      <Card>
        <div style={{ display: 'flex', justifyContent: 'space-between',
                      alignItems: 'flex-start', gap: 16, flexWrap: 'wrap' }}>
          <div>
            <h1>Your plan for the {pretty(s.next_open)} open</h1>
            <p style={{ margin: '6px 0 0', fontSize: 15 }}>
              In short: <strong>{planText}</strong>
            </p>
            <p className="muted small" style={{ margin: '4px 0 0' }}>
              Based on the {pretty(t.signals.as_of_close)} close and your{' '}
              {fmtMoney(nav)} practice account.{' '}
              {s.is_weekend
                ? 'It is the weekend — the next trading day is Monday.'
                : s.market_state === 'post_close'
                  ? 'Today has closed; do these when the market next opens.'
                  : s.market_state === 'pre_open'
                    ? 'Before the bell — do these at today\'s open.'
                    : s.market_state === 'open'
                      ? 'The market is already open; the opening price has passed.'
                      : 'Market closed today.'}
            </p>
          </div>
          <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
            <Badge kind="neutral">{STATE_TEXT[s.market_state]}</Badge>
            <Badge kind={t.signals.fresh ? 'pass' : 'fail'}>
              {t.signals.fresh ? 'List is current' : 'List is out of date'}
            </Badge>
            <button className="btn sm" onClick={() => setDetail(!detail)}>
              {detail ? 'Simple view' : 'Detailed view'}
            </button>
            <button className="btn sm" onClick={() => setShowHelp(!showHelp)}>
              {showHelp ? 'Hide help' : 'How do I use this?'}
            </button>
          </div>
        </div>
      </Card>

      {showHelp && (
        <Card title="How to use this page">
          <div style={{ maxWidth: 720 }}>
            <p>
              After every market close, the model re-scores the market and picks the
              stocks it likes best. This page turns that into a shopping list for the
              next time the market opens.
            </p>
            <p>
              <strong>Every buy comes with its exit plan already decided.</strong> You
              sell when the <em>first</em> of three things happens:
            </p>
            <ul style={{ margin: '8px 0', paddingLeft: 20, lineHeight: 1.7 }}>
              <li><span className="neg">The price falls to the "sell if it drops to" level</span> — cut the loss.</li>
              <li><span className="pos">The price climbs to the "sell if it climbs to" level</span> — take the profit.</li>
              <li>The <strong>"sell by" date</strong> arrives — sell at that morning's open no matter what.</li>
            </ul>
            <p>
              Share counts and dollar amounts are sized to your practice account
              ({fmtMoney(nav)} — change it on the Paper page). The trigger prices are
              estimates from the last close; the exact levels come from the price you
              actually pay. "Track this trade" records the buy in your practice book so
              the page can tell you when to sell it — no real money moves anywhere.
            </p>
          </div>
        </Card>
      )}

      {!t.signals.fresh && (
        <div className="verdict bad">
          <div className="mark" aria-hidden="true">✕</div>
          <div>
            <h2>This list is out of date — don't trade from it</h2>
            <p>{t.signals.stale_reason}</p>
          </div>
        </div>
      )}

      {s.open_already_passed && (
        <div className="verdict warn">
          <div className="mark" aria-hidden="true">⚠</div>
          <div>
            <h2>Today's opening price has already happened</h2>
            <p>These orders are meant for the moment the market opens. Buying later in
              the day means paying a different price than the plan assumes, so the
              numbers on this page no longer quite apply. Waiting for the next open is
              the safe choice.</p>
          </div>
        </div>
      )}

      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="Buy" value={fmtInt(t.counts.buy)}
                  sub="new stocks to purchase" tone="pos" />
        <StatTile label="Sell" value={fmtInt(t.counts.sell)}
                  sub="you own, model dropped them" tone={t.counts.sell ? 'neg' : ''} />
        <StatTile label="Sell — time is up" value={fmtInt(t.counts.due_exit)}
                  sub="reached their sell-by date" tone={t.counts.due_exit ? 'neg' : ''} />
        <StatTile label="Do nothing" value={fmtInt(t.counts.hold)}
                  sub="you own, still on the list" />
        <StatTile label="You own" value={fmtInt(t.counts.held_total)}
                  sub="stocks in the practice book" />
      </div>

      <div className="verdict warn">
        <div className="mark" aria-hidden="true">⚠</div>
        <div>
          <h2>Practice money only</h2>
          <p>The system's own quality checks say this strategy is not ready for real
            money yet ({t.gate_warning}) Use it with the practice (paper) book, not
            your broker. Also: this page only knows about stocks recorded in
            {' '}{t.holdings_source}</p>
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
        <Card title={`Sell first — time is up (${t.due_exits.length})`}
              subtitle="These reached their sell-by date. Sell them at the open no matter what the price is — the deadline is part of the strategy, even if the model still likes the stock.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <Th k="ticker" sort={dueSort.sort} onSort={dueSort.onSort}>Stock</Th>
                <th>What to do</th>
                <Th k="fill_date" sort={dueSort.sort} onSort={dueSort.onSort}>Bought</Th>
                <Th k="vertical_date" sort={dueSort.sort} onSort={dueSort.onSort}>Sell-by date</Th>
              </tr></thead>
              <tbody>
                {dueSort.rows.map((r) => (
                  <tr key={r.position_id}>
                    <td><strong>{r.ticker}</strong></td>
                    <td><strong>Sell all your shares at the open</strong></td>
                    <td className="muted">{pretty(r.fill_date)}</td>
                    <td className="muted">{pretty(r.vertical_date)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {t.sells.length > 0 && (
        <Card title={`Sell (${t.sells.length})`}
              subtitle="Stocks you own that the model no longer wants. Sell all your shares at the open.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <Th k="ticker" sort={sellSort.sort} onSort={sellSort.onSort}>Stock</Th>
                <th>What to do</th>
                <Th k="last_close" num sort={sellSort.sort} onSort={sellSort.onSort}>Latest close</Th>
                <Th k="fill_price" num sort={sellSort.sort} onSort={sellSort.onSort}>You paid</Th>
                <th>Why</th>
              </tr></thead>
              <tbody>
                {sellSort.rows.map((r) => (
                  <tr key={r.ticker}>
                    <td><strong>{r.ticker}</strong></td>
                    <td><strong>Sell all your shares at the open</strong></td>
                    <td className="n">{fmtPrice(r.last_close)}</td>
                    <td className="n">{r.fill_price != null ? fmtPrice(r.fill_price) : '—'}</td>
                    <td className="small muted">{SELL_REASON[r.reason] || r.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <Card title={`Buy (${t.buys.length})`}
            subtitle={detail
              ? `Quant view. Each fills market-on-open on ${pretty(s.next_open)} and carries its triple-barrier exits from the actual fill price.`
              : `Place each as a market order for when the market opens on ${pretty(s.next_open)}. The two sell prices and the sell-by date are your exit plan — whichever happens first wins.`}>
        <div className="table-wrap" style={{ maxHeight: 640, overflowY: 'auto' }}>
          <table>
            <thead>
              {detail ? <DetailHead sort={buySort.sort} onSort={buySort.onSort} /> : (
                <tr>
                  <Th k="ticker" sort={buySort.sort} onSort={buySort.onSort}>Stock</Th>
                  <Th k="shares" sort={buySort.sort} onSort={buySort.onSort}>What to do</Th>
                  <Th k="last_close" num sort={buySort.sort} onSort={buySort.onSort}>Latest close</Th>
                  <Th k="est_cost" num sort={buySort.sort} onSort={buySort.onSort}>Costs about</Th>
                  <Th k="stop_price" num sort={buySort.sort} onSort={buySort.onSort}>Sell if it drops to</Th>
                  <Th k="profit_take_price" num sort={buySort.sort} onSort={buySort.onSort}>Sell if it climbs to</Th>
                  <Th k="sell_by_date" sort={buySort.sort} onSort={buySort.onSort}>Sell by (latest)</Th>
                  <th></th>
                </tr>
              )}
            </thead>
            <tbody>
              {buySort.rows.map((r) => (detail
                ? <ActionRow key={r.ticker} r={r} kind="BUY" />
                : <BuyRowSimple key={r.ticker} r={r} onAct={buy} busy={busy} />))}
            </tbody>
          </table>
        </div>
        {!detail && t.counts.buy > 0 && (
          <div className="note">
            All {t.counts.buy} buys together come to about{' '}
            <strong>{fmtMoney(t.plan?.invest_total)}</strong> of your {fmtMoney(nav)}.
          </div>
        )}
      </Card>

      {t.holds.length > 0 && (
        <Card title={`Do nothing (${t.holds.length})`}
              subtitle="You already own these and the model still likes them. No action — just keep an eye on their sell prices and dates."
              right={<button className="btn sm" onClick={() => setShowHolds(!showHolds)}>
                {showHolds ? 'Hide' : 'Show'}
              </button>}>
          {showHolds && (
            <div className="table-wrap">
              <table>
                <thead>
                  {detail ? <DetailHead sort={holdSort.sort} onSort={holdSort.onSort} /> : (
                    <tr>
                      <Th k="ticker" sort={holdSort.sort} onSort={holdSort.onSort}>Stock</Th>
                      <Th k="status" sort={holdSort.sort} onSort={holdSort.onSort}>What to do</Th>
                      <Th k="fill_price" num sort={holdSort.sort} onSort={holdSort.onSort}>You paid</Th>
                      <Th k="stop_price" num sort={holdSort.sort} onSort={holdSort.onSort}>Sell if it drops to</Th>
                      <Th k="profit_take_price" num sort={holdSort.sort} onSort={holdSort.onSort}>Sell if it climbs to</Th>
                      <Th k="sell_by_date" sort={holdSort.sort} onSort={holdSort.onSort}>Sell by (latest)</Th>
                    </tr>
                  )}
                </thead>
                <tbody>
                  {holdSort.rows.map((r) => (detail
                    ? <ActionRow key={r.ticker} r={{ ...r, reason: r.status }} kind="HOLD" />
                    : <HoldRowSimple key={r.ticker} r={r} />))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      )}
    </>
  );
}
