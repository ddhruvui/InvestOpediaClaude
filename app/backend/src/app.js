// Report + paper-trading API (the Express app, no listener).
//
// Read side is a thin slice over the report bundle the pipeline published to
// MongoDB (tools/build_reports.py -> tools/publish_mongo.py) — no metric is
// recomputed here, so the API can never disagree with the pipeline. Write side
// is the paper book (M18/BP15), also in MongoDB.
//
// api/index.js exports this app for Vercel; src/server.js listens on a port
// for local use.
import express from 'express';
import cors from 'cors';
import * as reports from './reports.js';
import * as paper from './paper.js';
import { ticket } from './today.js';
import { mongoEnabled, BUNDLE, DB_NAME } from './db.js';

const app = express();

// CORS_ORIGIN: comma-separated list of allowed origins (the Render UI URL),
// or unset for any origin — the API is read-mostly and holds no secrets.
// A single origin is passed as a string so the header is a constant rather
// than an echo of the request's Origin.
const origins = (process.env.CORS_ORIGIN || '*').split(',').map((s) => s.trim()).filter(Boolean);
app.use(cors({ origin: origins.includes('*') ? true : origins.length === 1 ? origins[0] : origins }));
app.use(express.json({ limit: '1mb' }));
app.disable('x-powered-by');

// Never cacheable, never conditional. Vercel stamps function responses with
// `public, max-age=0, must-revalidate` and Express adds an ETag, so a browser
// revalidates and gets a 304 whose merged headers can carry a STALE
// Access-Control-Allow-Origin (seen on Render: the UI's /api/health was blocked
// with the header value of an earlier localhost visit). Everything here is
// small and changes daily, so no-store costs nothing.
app.set('etag', false);
app.use('/api', (_req, res, next) => {
  res.set('Cache-Control', 'no-store');
  next();
});

const HINT = mongoEnabled()
  ? 'publish the bundle: python3 tools/publish_mongo.py --bundle reports/latest'
  : 'build the bundle: python3 tools/build_reports.py --src derived --out reports/latest';

/** Async route -> JSON; null data from a named section is a 503 with a hint. */
const handle = (fn, name) => async (req, res) => {
  try {
    const data = await fn(req);
    if (data == null && name) {
      return res.status(503).json({
        error: `${name} not published yet`,
        hint: HINT,
        source: reports.source(),
        bundle: BUNDLE,
        location: reports.reportsDir(),
      });
    }
    return res.json(data);
  } catch (err) {
    const status = err.status || 500;
    if (status >= 500) console.error(`${req.method} ${req.originalUrl}:`, err.message);
    return res.status(status).json({ error: err.message });
  }
};

// ---------------------------------------------------------------- reports
app.get('/api/health', async (_req, res) => {
  const base = {
    source: reports.source(), bundle: BUNDLE,
    db: mongoEnabled() ? DB_NAME : null, location: reports.reportsDir(),
  };
  try {
    const [s, m, prov] = await Promise.all([
      reports.summary(), reports.manifest(), reports.provenance()]);
    res.json({
      ok: true, ...base,
      bundle_present: Boolean(s),
      generated_utc: s?.generated_utc ?? null,
      built_utc: prov?.built_utc ?? null,
      published_utc: prov?.published_utc ?? null,
      as_of_close: (await reports.suggestions())?.as_of_close ?? null,
      verdict: s?.gates?.verdict ?? null,
      sections: m?.sections ?? null,
    });
  } catch (err) {
    res.status(503).json({ ok: false, ...base, error: err.message });
  }
});

app.get('/api/today', handle(() => ticket()));
app.get('/api/summary', handle(() => reports.summary(), 'summary'));
app.get('/api/equity', handle(() => reports.equity(), 'equity'));
app.get('/api/suggestions', handle(() => reports.suggestions(), 'suggestions'));
app.get('/api/config', handle(() => reports.config()));
app.get('/api/trades/summary', handle(() => reports.tradesSummary(), 'trades'));
app.get('/api/trades', handle((req) => reports.queryTrades(req.query)));
// Published-book history (one row per as_of_close), and one day's full book.
app.get('/api/predictions', handle((req) => reports.predictions(req.query.limit)));
app.get('/api/predictions/:as_of', handle(
  (req) => reports.prediction(req.params.as_of), 'prediction'));

// ----------------------------------------------------------- paper book
app.get('/api/paper', handle(() => paper.state()));
app.post('/api/paper/open', handle((req) => paper.openPosition(req.body || {})));
app.post('/api/paper/:id/fill', handle((req) => paper.recordFill(req.params.id, req.body || {})));
app.post('/api/paper/:id/close', handle((req) => paper.closePosition(req.params.id, req.body || {})));
app.delete('/api/paper/:id', handle((req) => paper.removePosition(req.params.id)));
app.post('/api/paper/settings', handle((req) => paper.settings(req.body || {})));
app.post('/api/paper/reset', handle(() => paper.reset()));

// A bare hit on the deployment URL should say what this is, not "Cannot GET /".
app.get('/', (_req, res) => res.redirect(302, '/api'));
app.get('/api', (_req, res) => res.json({
  name: 'InvestOpediaClaude API', source: reports.source(), bundle: BUNDLE,
  routes: ['/api/health', '/api/today', '/api/summary', '/api/equity', '/api/suggestions',
    '/api/config', '/api/trades/summary', '/api/trades', '/api/predictions',
    '/api/predictions/:as_of', '/api/paper'],
}));
app.use('/api', (_req, res) => res.status(404).json({ error: 'no such route' }));

export default app;
