// "What do I do at the next open?" — the session-aware trade ticket.
//
// Two things the raw suggestion file cannot answer on its own:
//
//   1. WHICH SESSION do these orders belong to? An EOD system signals after a
//      close and fills at the NEXT open (G-02). On a Friday evening or all
//      weekend that next open is Monday; on a weekday morning it is today's.
//      Resolved against the D-11 NYSE calendar, so holidays are handled.
//   2. WHAT DO I ALREADY HOLD? The suggestion file is a TARGET book, not a
//      diff. Every name reads as a "buy" until it is compared with the current
//      book — so a name already held shows as HOLD, and a held name that has
//      dropped out of the target shows as SELL.
import * as reports from './reports.js';
import { state as paperState } from './paper.js';

const ET = 'America/New_York';

/** Wall-clock date + minutes-since-midnight in US market time. */
function nowInET(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: ET, year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).formatToParts(now).reduce((a, p) => ({ ...a, [p.type]: p.value }), {});
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    minutes: Number(parts.hour) * 60 + Number(parts.minute),
  };
}

const CLOSE_MIN = 16 * 60;      // 16:00 ET
const OPEN_MIN = 9 * 60 + 30;   // 09:30 ET

export function sessionContext(now = new Date()) {
  const cal = reports.readCalendar();
  const sessions = cal?.sessions ?? [];
  const { date: todayET, minutes } = nowInET(now);
  const isSessionToday = sessions.includes(todayET);
  const todayClosed = isSessionToday && minutes >= CLOSE_MIN;

  // The most recent close whose data a signal could already reflect.
  const past = sessions.filter((s) => s < todayET || (s === todayET && todayClosed));
  const lastClose = past.length ? past[past.length - 1] : null;
  const nextOpen = sessions.find((s) => (lastClose ? s > lastClose : s >= todayET)) ?? null;

  const weekday = new Date(`${todayET}T12:00:00Z`).getUTCDay();
  return {
    now_et: todayET,
    minutes_et: minutes,
    is_trading_day: isSessionToday,
    is_weekend: weekday === 0 || weekday === 6,
    market_state: !isSessionToday ? 'closed'
      : minutes < OPEN_MIN ? 'pre_open'
        : minutes < CLOSE_MIN ? 'open' : 'post_close',
    last_completed_close: lastClose,
    next_open: nextOpen,
    open_already_passed: Boolean(nextOpen === todayET && minutes >= OPEN_MIN),
  };
}

export function ticket(now = new Date()) {
  const ctx = sessionContext(now);
  const sug = reports.suggestions();
  const cfg = reports.config();
  if (!sug) return { session: ctx, error: 'no suggestions in the report bundle' };

  const held = new Map();
  for (const p of paperState().positions) {
    if (p.status === 'open' || p.status === 'ordered') held.set(p.ticker, p);
  }

  const target = new Map();
  for (const r of sug.buys_or_increases || []) target.set(r.ticker, r);

  const buys = [];
  const holds = [];
  for (const [ticker, r] of target) {
    const pos = held.get(ticker);
    const row = {
      ticker,
      target_weight: r.target_weight,
      ensemble_rank: r.ensemble_rank,
      last_close: r.last_close,
      stop_pct: r.stop_pct,
      profit_take_pct: r.profit_take_pct,
      max_hold_sessions: r.max_hold_sessions,
      barrier_unreachable: Math.abs(r.stop_pct ?? 0) > 40,
    };
    if (!pos) buys.push({ ...row, action: 'BUY', reason: 'new — not held' });
    else holds.push({ ...row, action: 'HOLD', position_id: pos.id, status: pos.status,
                      fill_price: pos.fill_price ?? null });
  }

  // Held but no longer wanted, plus anything the source file explicitly exits.
  const sells = [];
  for (const [ticker, pos] of held) {
    if (!target.has(ticker)) {
      sells.push({
        ticker, action: 'SELL', position_id: pos.id, status: pos.status,
        fill_price: pos.fill_price ?? null, target_weight: 0,
        reason: 'dropped out of the target book',
      });
    }
  }
  for (const r of sug.sells_or_exits || []) {
    if (!sells.some((x) => x.ticker === r.ticker)) {
      sells.push({ ticker: r.ticker, action: 'SELL', target_weight: 0,
                   current_weight: r.current_weight,
                   reason: 'model flagged an exit' });
    }
  }

  // Barrier obligations on open positions: the vertical exit is a scheduled MOO
  // at fill + h sessions (M5.2) — it is due regardless of what the model says.
  const cal = reports.readCalendar()?.sessions ?? [];
  const dueExits = [];
  for (const pos of held.values()) {
    if (pos.status !== 'open' || !pos.fill_date) continue;
    const i = cal.indexOf(pos.fill_date);
    const h = pos.max_hold_sessions ?? cfg.barrier.h_days;
    const verticalDate = i >= 0 ? cal[i + h + 1] ?? null : null;
    if (verticalDate && ctx.next_open && verticalDate <= ctx.next_open) {
      dueExits.push({
        ticker: pos.ticker, position_id: pos.id, action: 'EXIT (time barrier)',
        fill_date: pos.fill_date, vertical_date: verticalDate,
        reason: `${h}-session vertical barrier reached — market-on-open exit`,
      });
    }
  }

  const stale = Boolean(ctx.last_completed_close
    && sug.as_of_close && sug.as_of_close < ctx.last_completed_close);

  return {
    session: ctx,
    signals: {
      as_of_close: sug.as_of_close,
      fresh: !stale,
      stale_reason: stale
        ? `Signals are from the ${sug.as_of_close} close, but ${ctx.last_completed_close} `
          + 'has since closed. Re-run the predict job before acting on these.'
        : null,
      execute_at: sug.execute_at,
    },
    counts: { buy: buys.length, sell: sells.length, hold: holds.length,
              due_exit: dueExits.length, held_total: held.size },
    buys, sells, holds, due_exits: dueExits,
    gate_warning: 'The G-11 gates on this book return ITERATE — research output, '
      + 'not a recommendation to trade.',
    holdings_source: 'the paper book (app/backend/data/paper_book.json). Positions '
      + 'you hold elsewhere are invisible to this diff until they are recorded here.',
  };
}
