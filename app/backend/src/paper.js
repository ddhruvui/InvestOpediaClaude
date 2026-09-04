// Paper-trading book — the BP15 stage ("paper-trade 3–6 months measuring
// open-print slippage before real capital") made operable, with the M18 ops
// rules it prescribes:
//
//   slippage    slip_bps = side × (fill − official_open)/official_open × 1e4,
//               rolling median adopted into the cost model once ≥ 60 fills (M15-03)
//   barriers    stop / profit-take / vertical are % vs the ACTUAL fill (M5.2),
//               never vs the signal-day close
//   PDT         same-session round trips, rolling 5 business days, ≤ 3 under $25k
//   kill switch realized day loss ≤ −ops.max_daily_loss_pct × NAV halts new orders
//   decay       rolling 63-session paper Sharpe vs the backtest expectation;
//               < ½ for 2+ quarters ⇒ retrain-or-retire (G-11/G-16)
//
// State is one small document (MongoDB in production, a JSON file in dev);
// see store.js. Every write is load → mutate → save, so the book stays small
// and the last writer wins — fine for a single operator's paper book.
import { config } from './reports.js';
import { emptyBook, loadBook, saveBook, bookLocation } from './store.js';

export { bookLocation };

const isoDate = (d) => new Date(d).toISOString().slice(0, 10);

const fail = (message, status) => Object.assign(new Error(message), { status });

/** Business days between two ISO dates (weekends only; holidays are the
 *  calendar's job — this is the conservative direction for a PDT budget). */
function businessDaysBetween(a, b) {
  const start = new Date(`${a}T00:00:00Z`);
  const end = new Date(`${b}T00:00:00Z`);
  let n = 0;
  for (const d = new Date(start); d < end; d.setUTCDate(d.getUTCDate() + 1)) {
    const wd = d.getUTCDay();
    if (wd !== 0 && wd !== 6) n += 1;
  }
  return n;
}

function roundTripCostFrac(side, holdingDays, cfg) {
  const legs = 2 * (cfg.cost.per_trade_bps + cfg.cost.slippage_bps) / 1e4;
  const borrow = side < 0
    ? (cfg.cost.borrow_gc_bps_yr / 1e4 / 252) * Math.max(0, holdingDays) : 0;
  return legs + borrow;
}

// ------------------------------------------------------------------ reads
export async function state() {
  const [s, cfg] = await Promise.all([loadBook(), config()]);
  return { ...s, stats: stats(s, cfg), cfg, location: bookLocation() };
}

/** Same-session round trips inside the rolling window ending `today`. */
function pdtCount(s, cfg, today) {
  const win = cfg.pdt.window_business_days;
  return s.positions.filter((p) =>
    p.status === 'closed' && p.day_trade
    && p.exit_date && businessDaysBetween(p.exit_date, today) < win).length;
}

/** Daily REALIZED return series, attributed on exit date (a paper book has no
 *  intraday marks; realized-only is the honest series to judge decay on). */
function dailyReturns(s) {
  const byDate = new Map();
  for (const p of s.positions) {
    if (p.status !== 'closed' || p.ret_net == null) continue;
    const w = p.target_weight ?? 0;
    byDate.set(p.exit_date, (byDate.get(p.exit_date) ?? 0) + p.ret_net * w);
  }
  return [...byDate.entries()].sort((a, b) => a[0].localeCompare(b[0]))
    .map(([date, ret]) => ({ date, ret }));
}

function sharpe(rets) {
  if (rets.length < 3) return null;
  const mean = rets.reduce((a, b) => a + b, 0) / rets.length;
  const varr = rets.reduce((a, b) => a + (b - mean) ** 2, 0) / (rets.length - 1);
  const sd = Math.sqrt(varr);
  return sd > 0 ? Math.sqrt(252) * mean / sd : null;
}

function median(xs) {
  if (!xs.length) return null;
  const v = [...xs].sort((a, b) => a - b);
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

/** Pure: derives every ops number from a book + config. */
export function stats(s, cfg) {
  const open = s.positions.filter((p) => p.status === 'open');
  const ordered = s.positions.filter((p) => p.status === 'ordered');
  const closed = s.positions.filter((p) => p.status === 'closed');

  const slips = s.positions.filter((p) => p.slip_bps != null).map((p) => p.slip_bps);
  const med = median(slips);
  const mad = slips.length ? median(slips.map((x) => Math.abs(x - med))) : null;

  const daily = dailyReturns(s);
  const rets = daily.map((d) => d.ret);
  const equity = [];
  let cum = 1;
  for (const d of daily) { cum *= 1 + d.ret; equity.push({ date: d.date, equity: cum }); }

  const win = cfg.decay.window_sessions;
  const rollSharpe = rets.length >= win ? sharpe(rets.slice(-win)) : null;

  const today = isoDate(Date.now());
  const todayPnl = daily.filter((d) => d.date === today)
    .reduce((a, b) => a + b.ret, 0);

  const wins = closed.filter((p) => p.ret_net > 0).length;
  return {
    counts: { ordered: ordered.length, open: open.length, closed: closed.length },
    realized: {
      n: closed.length,
      win_rate: closed.length ? wins / closed.length : null,
      avg_ret: closed.length
        ? closed.reduce((a, p) => a + (p.ret_net ?? 0), 0) / closed.length : null,
      sharpe_all: sharpe(rets),
      equity,
    },
    slippage: {
      n_fills: slips.length,
      median_bps: med,
      mad_bps: mad,
      adoption_min_fills: cfg.slippage_adoption_min_fills,
      adopted: slips.length >= cfg.slippage_adoption_min_fills,
      note: `M15-03: the backtest cost model adopts the rolling median once `
            + `≥ ${cfg.slippage_adoption_min_fills} fills exist `
            + `(currently ${slips.length}).`,
    },
    pdt: {
      enforced: s.account_equity != null && s.account_equity < cfg.pdt.equity_floor,
      used: pdtCount(s, cfg, today),
      limit: cfg.pdt.limit,
      window_business_days: cfg.pdt.window_business_days,
      note: 'Flat ≤3 budget ignores FINRA\'s 6%-of-total-trades carve-out '
            + '(strictly conservative).',
    },
    kill_switch: {
      threshold_pct: cfg.ops.max_daily_loss_pct,
      today_realized_pct: todayPnl,
      tripped: todayPnl <= -cfg.ops.max_daily_loss_pct,
    },
    decay: {
      window_sessions: win,
      rolling_sharpe: rollSharpe,
      sessions_recorded: rets.length,
      note: `G-11: retrain-or-retire when rolling ${win}-session Sharpe stays `
            + `below ½ the backtest expectation for 2+ quarters `
            + `(${cfg.decay.breach_sessions} sessions).`,
    },
  };
}

// ----------------------------------------------------------------- writes
export async function openPosition(input) {
  const [s, cfg] = await Promise.all([loadBook(), config()]);
  const {
    ticker, side = 1, target_weight = 0, signal_date, ref_close,
    stop_pct, profit_take_pct, max_hold_sessions = cfg.barrier.h_days,
    ensemble_rank = null,
  } = input;

  if (!ticker) throw fail('ticker is required', 400);
  if (![1, -1].includes(Number(side))) throw fail('side must be 1 or -1', 400);
  const sym = String(ticker).toUpperCase();
  if (s.positions.some((p) => p.ticker === sym && p.status !== 'closed')) {
    throw fail(`${sym} already has an open/ordered position`, 409);
  }
  if (stats(s, cfg).kill_switch.tripped) {
    throw fail('kill switch tripped — new orders halted (BP16)', 423);
  }

  s.seq = (s.seq ?? 0) + 1;
  const pos = {
    id: `p${String(s.seq).padStart(5, '0')}`,
    ticker: sym,
    side: Number(side),
    target_weight: Number(target_weight) || 0,
    signal_date: signal_date ? isoDate(signal_date) : isoDate(Date.now()),
    ref_close: ref_close == null ? null : Number(ref_close),
    stop_pct: stop_pct == null ? null : Number(stop_pct),
    profit_take_pct: profit_take_pct == null ? null : Number(profit_take_pct),
    max_hold_sessions: Number(max_hold_sessions),
    ensemble_rank,
    status: 'ordered',       // MOO at next open (G-02) — not yet filled
    created_utc: new Date().toISOString(),
  };
  s.positions.push(pos);
  await saveBook(s);
  return pos;
}

export async function recordFill(id, { fill_price, official_open, fill_date }) {
  const s = await loadBook();
  const p = s.positions.find((x) => x.id === id);
  if (!p) throw fail('position not found', 404);
  if (p.status !== 'ordered') throw fail(`position is ${p.status}, not ordered`, 409);
  const fill = Number(fill_price);
  if (!(fill > 0)) throw fail('fill_price must be > 0', 400);
  const open = official_open == null ? null : Number(official_open);

  p.fill_price = fill;
  p.official_open = open;
  p.fill_date = fill_date ? isoDate(fill_date) : isoDate(Date.now());
  // M18 step 5 — the measurement the whole paper stage exists to collect
  p.slip_bps = open > 0 ? p.side * ((fill - open) / open) * 1e4 : null;
  // M5.2: barriers hang off the ACTUAL fill
  if (p.stop_pct != null) p.stop_price = fill * (1 + p.stop_pct / 100);
  if (p.profit_take_pct != null) p.profit_take_price = fill * (1 + p.profit_take_pct / 100);
  p.status = 'open';
  await saveBook(s);
  return p;
}

export async function closePosition(id, { exit_price, exit_date, reason = 'manual' }) {
  const [s, cfg] = await Promise.all([loadBook(), config()]);
  const p = s.positions.find((x) => x.id === id);
  if (!p) throw fail('position not found', 404);
  if (p.status !== 'open') throw fail(`position is ${p.status}, not open`, 409);
  const px = Number(exit_price);
  if (!(px > 0)) throw fail('exit_price must be > 0', 400);
  const date = exit_date ? isoDate(exit_date) : isoDate(Date.now());
  const holding = Math.max(0, businessDaysBetween(p.fill_date, date));
  const dayTrade = date === p.fill_date;

  if (dayTrade) {
    const st = stats(s, cfg);
    if (st.pdt.enforced && st.pdt.used >= cfg.pdt.limit) {
      throw fail(
        `PDT budget exhausted (${st.pdt.used}/${cfg.pdt.limit} in `
        + `${cfg.pdt.window_business_days} business days) — defer this exit to the `
        + 'next open (pdt.mode_under_25k)', 423);
    }
  }

  p.exit_price = px;
  p.exit_date = date;
  p.exit_reason = reason;
  p.holding_days = holding;
  p.day_trade = dayTrade;
  p.ret_gross = p.side * (px / p.fill_price - 1);
  p.ret_net = p.ret_gross - roundTripCostFrac(p.side, holding, cfg);
  p.status = 'closed';
  await saveBook(s);
  return p;
}

export async function removePosition(id) {
  const s = await loadBook();
  const before = s.positions.length;
  s.positions = s.positions.filter((x) => x.id !== id);
  if (s.positions.length === before) throw fail('position not found', 404);
  await saveBook(s);
  return { removed: id };
}

export async function settings({ nav, account_equity }) {
  const s = await loadBook();
  if (nav != null) s.nav = Number(nav);
  if (account_equity !== undefined) {
    s.account_equity = account_equity == null ? null : Number(account_equity);
  }
  await saveBook(s);
  return { nav: s.nav, account_equity: s.account_equity };
}

export async function reset() {
  await saveBook({ ...emptyBook(), created_utc: new Date().toISOString() });
  return { ok: true };
}
