// Report bundle access. The bundle is built by tools/build_reports.py from pod
// artifacts; this layer only reads and slices it — no computation, so the API
// can never disagree with the pipeline that produced the numbers.
import fs from 'node:fs';
import path from 'node:path';

const REPORTS_DIR = process.env.REPORTS_DIR
  || path.resolve(process.cwd(), '../../reports/latest');

const cache = new Map();

function readJson(name) {
  const file = path.join(REPORTS_DIR, name);
  let stat;
  try {
    stat = fs.statSync(file);
  } catch {
    return null;
  }
  const hit = cache.get(name);
  if (hit && hit.mtimeMs === stat.mtimeMs) return hit.data;
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    cache.set(name, { mtimeMs: stat.mtimeMs, data });
    return data;
  } catch (err) {
    console.error(`reports: cannot parse ${name}:`, err.message);
    return null;
  }
}

export const reportsDir = () => REPORTS_DIR;
export const summary = () => readJson('summary.json');
export const equity = () => readJson('equity.json');
export const suggestions = () => readJson('suggestions.json');
export const tradesSummary = () => readJson('trades_summary.json');
export const tradesSample = () => readJson('trades_sample.json');
export const manifest = () => readJson('manifest.json');
export const readCalendar = () => readJson('calendar.json');

export function config() {
  return readJson('config.json') || {
    cost: { per_trade_bps: 15, borrow_gc_bps_yr: 50, slippage_bps: 0 },
    barrier: { m: 1.5, h_days: 20 },
    pdt: { limit: 3, window_business_days: 5, equity_floor: 25000 },
    ops: { max_daily_loss_pct: 0.02 },
    slippage_adoption_min_fills: 60,
    decay: { window_sessions: 63, breach_sessions: 126, ratio_of_backtest: 0.5 },
  };
}

const SORTABLE_TRADE_KEYS = new Set(['ticker', 'entry_date', 'exit_date',
  'entry_price', 'exit_price', 'barrier_hit', 'holding_days', 'ensemble_rank',
  'exit_ret_net']);

/** Filter + sort + paginate the trade sample (the full ledger stays on the
 *  volume). Sorting lives here because the client only ever sees one page. */
export function queryTrades({ limit = 100, offset = 0, exit, ticker, year,
                              minRank, outcome, sortKey, sortDir } = {}) {
  const bundle = tradesSample();
  if (!bundle) return { rows: [], total: 0, n_ledger: 0 };
  let rows = bundle.rows;
  if (exit) rows = rows.filter((r) => r.barrier_hit === exit);
  if (ticker) {
    const t = String(ticker).toUpperCase();
    rows = rows.filter((r) => String(r.ticker).toUpperCase().includes(t));
  }
  if (year) rows = rows.filter((r) => String(r.entry_date).startsWith(String(year)));
  if (minRank != null && minRank !== '') {
    rows = rows.filter((r) => r.ensemble_rank != null && r.ensemble_rank >= Number(minRank));
  }
  if (outcome === 'win') rows = rows.filter((r) => r.exit_ret_net > 0);
  if (outcome === 'loss') rows = rows.filter((r) => r.exit_ret_net <= 0);
  if (sortKey && SORTABLE_TRADE_KEYS.has(sortKey)) {
    const dir = sortDir === 'asc' ? 1 : -1;
    rows = [...rows].sort((x, y) => {
      const a = x[sortKey];
      const b = y[sortKey];
      if (a == null && b == null) return 0;
      if (a == null) return 1;          // nulls last regardless of direction
      if (b == null) return -1;
      const c = typeof a === 'number' && typeof b === 'number'
        ? a - b : String(a).localeCompare(String(b));
      return dir * c;
    });
  }
  const total = rows.length;
  const page = rows.slice(Number(offset), Number(offset) + Number(limit));
  return { rows: page, total, n_ledger: bundle.n_total, note: bundle.note };
}
