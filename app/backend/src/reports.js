// Report bundle access.
//
// Production: the bundle lives in MongoDB (collection `reports`, one document
// per section, put there by tools/publish_mongo.py after each pipeline run).
// Dev/tests (no MONGO_URI): the same sections read from reports/<bundle>/*.json.
//
// Either way this layer only reads and slices — nothing is recomputed, so the
// API can never disagree with the pipeline that produced the numbers.
import fs from 'node:fs';
import path from 'node:path';
import { getDb, mongoEnabled, BUNDLE, DB_NAME } from './db.js';

export const SECTIONS = ['summary', 'equity', 'suggestions', 'trades_summary',
  'trades_sample', 'manifest', 'calendar', 'config'];

/* ------------------------------------------------- files (local / tests) */

/** Branch of the repo this backend lives in (worktree-aware), or null. */
function currentBranch() {
  try {
    let gitPath = path.resolve(process.cwd(), '../../.git');
    if (fs.statSync(gitPath).isFile()) {          // worktree: .git is a pointer file
      const m = fs.readFileSync(gitPath, 'utf8').match(/^gitdir: (.+)$/m);
      if (m) gitPath = path.resolve(path.dirname(gitPath), m[1].trim());
    }
    const head = fs.readFileSync(path.join(gitPath, 'HEAD'), 'utf8').trim();
    return head.match(/^ref: refs\/heads\/(.+)$/)?.[1] ?? null;
  } catch {
    return null;
  }
}

// main serves the universal whole-market bundle (reports/latest); the top-150
// experiment branch serves its own restricted-universe bundle. Explicit
// REPORTS_DIR / REPORT_BUNDLE always win. Checked once at startup.
const BRANCH_BUNDLE = { top150: 'top150', top200: 'top150' };

function defaultReportsDir() {
  const base = path.resolve(process.cwd(), '../../reports');
  const wanted = process.env.REPORT_BUNDLE || BRANCH_BUNDLE[currentBranch()];
  if (wanted && fs.existsSync(path.join(base, wanted, 'suggestions.json'))) {
    return path.join(base, wanted);
  }
  return path.join(base, 'latest');
}

const REPORTS_DIR = process.env.REPORTS_DIR || defaultReportsDir();
const fileCache = new Map();

function readFile(name) {
  const file = path.join(REPORTS_DIR, name);
  let stat;
  try {
    stat = fs.statSync(file);
  } catch {
    return null;
  }
  const hit = fileCache.get(name);
  if (hit && hit.mtimeMs === stat.mtimeMs) return hit.data;
  try {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    fileCache.set(name, { mtimeMs: stat.mtimeMs, data });
    return data;
  } catch (err) {
    console.error(`reports: cannot parse ${name}:`, err.message);
    return null;
  }
}

/* ---------------------------------------------------------------- mongo */

// A warm function keeps each section for a minute; the bundle changes once a
// day, and the trades sample is ~1.3 MB we would rather not refetch per click.
const TTL_MS = Number(process.env.REPORT_CACHE_MS ?? 60_000);
const mongoCache = new Map();          // section -> { at, doc }

async function mongoDoc(section) {
  const hit = mongoCache.get(section);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.doc;
  const db = await getDb();
  const doc = await db.collection('reports').findOne({ _id: `${BUNDLE}/${section}` });
  mongoCache.set(section, { at: Date.now(), doc });
  return doc;
}

/* ------------------------------------------------------------ public API */

export const source = () => (mongoEnabled() ? 'mongo' : 'files');
export const bundle = () => BUNDLE;
export const reportsDir = () => (mongoEnabled()
  ? `mongodb ${DB_NAME}.reports/${BUNDLE}/*` : REPORTS_DIR);

export async function section(name) {
  if (mongoEnabled()) return (await mongoDoc(name))?.data ?? null;
  return readFile(`${name}.json`);
}

export const summary = () => section('summary');
export const equity = () => section('equity');
export const suggestions = () => section('suggestions');
export const tradesSummary = () => section('trades_summary');
export const tradesSample = () => section('trades_sample');
export const manifest = () => section('manifest');
export const readCalendar = () => section('calendar');

/** When the bundle was built and (Mongo only) when it was published. */
export async function provenance() {
  if (mongoEnabled()) {
    const doc = await mongoDoc('manifest');
    return doc ? { built_utc: doc.built_utc ?? null, published_utc: doc.published_utc ?? null } : null;
  }
  const m = readFile('manifest.json');
  return m ? { built_utc: m.built_utc ?? null, published_utc: null } : null;
}

export async function config() {
  return (await section('config')) || {
    cost: { per_trade_bps: 15, borrow_gc_bps_yr: 50, slippage_bps: 0 },
    barrier: { m: 1.5, h_days: 20 },
    pdt: { limit: 3, window_business_days: 5, equity_floor: 25000 },
    ops: { max_daily_loss_pct: 0.02 },
    slippage_adoption_min_fills: 60,
    decay: { window_sessions: 63, breach_sessions: 126, ratio_of_backtest: 0.5 },
  };
}

/** History of published books (Mongo only): one row per as_of_close, newest first. */
export async function predictions(limit = 90) {
  if (!mongoEnabled()) return { bundle: BUNDLE, source: 'files', rows: [] };
  const db = await getDb();
  const rows = await db.collection('predictions')
    .find({ bundle: BUNDLE }, { projection: { data: 0 } })
    .sort({ as_of_close: -1 })
    .limit(Math.min(Number(limit) || 90, 1000))
    .toArray();
  return { bundle: BUNDLE, source: 'mongo', rows };
}

export async function prediction(asOfClose) {
  if (!mongoEnabled()) return null;
  const db = await getDb();
  const doc = await db.collection('predictions').findOne({ _id: `${BUNDLE}/${asOfClose}` });
  return doc?.data ?? null;
}

const SORTABLE_TRADE_KEYS = new Set(['ticker', 'entry_date', 'exit_date',
  'entry_price', 'exit_price', 'barrier_hit', 'holding_days', 'ensemble_rank',
  'exit_ret_net']);

/** Filter + sort + paginate the trade sample (the full ledger stays on the
 *  volume). Sorting lives here because the client only ever sees one page. */
export async function queryTrades({ limit = 100, offset = 0, exit, ticker, year,
                                    minRank, outcome, sortKey, sortDir } = {}) {
  const bundle = await tradesSample();
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
