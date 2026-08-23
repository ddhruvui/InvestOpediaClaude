// Report + paper-trading API.
//
// Read side is a thin slice over the report bundle built from RunPod artifacts
// (tools/build_reports.py) — no metric is recomputed here, so the API can never
// disagree with the pipeline. Write side is the paper book (M18/BP15).
import express from 'express';
import cors from 'cors';
import path from 'node:path';
import fs from 'node:fs';
import * as reports from './reports.js';
import * as paper from './paper.js';

const app = express();
const PORT = process.env.PORT || 8787;

app.use(cors());
app.use(express.json({ limit: '1mb' }));

const send = (res, data, name) => (data
  ? res.json(data)
  : res.status(503).json({
      error: `${name} not built yet`,
      hint: 'run: python3 tools/build_reports.py --src derived --out reports/latest',
      reports_dir: reports.reportsDir(),
    }));

// ---------------------------------------------------------------- reports
app.get('/api/health', (_req, res) => {
  const s = reports.summary();
  res.json({
    ok: true,
    reports_dir: reports.reportsDir(),
    bundle_present: Boolean(s),
    generated_utc: s?.generated_utc ?? null,
    sections: reports.manifest()?.sections ?? null,
  });
});

app.get('/api/summary', (_req, res) => send(res, reports.summary(), 'summary'));
app.get('/api/equity', (_req, res) => send(res, reports.equity(), 'equity'));
app.get('/api/suggestions', (_req, res) => send(res, reports.suggestions(), 'suggestions'));
app.get('/api/config', (_req, res) => res.json(reports.config()));
app.get('/api/trades/summary', (_req, res) => send(res, reports.tradesSummary(), 'trades'));
app.get('/api/trades', (req, res) => res.json(reports.queryTrades(req.query)));

// ----------------------------------------------------------- paper book
app.get('/api/paper', (_req, res) => res.json(paper.state()));

const guard = (res, fn) => {
  try {
    res.json(fn());
  } catch (err) {
    res.status(err.status || 500).json({ error: err.message });
  }
};

app.post('/api/paper/open', (req, res) => guard(res, () => paper.openPosition(req.body)));
app.post('/api/paper/:id/fill', (req, res) =>
  guard(res, () => paper.recordFill(req.params.id, req.body)));
app.post('/api/paper/:id/close', (req, res) =>
  guard(res, () => paper.closePosition(req.params.id, req.body)));
app.delete('/api/paper/:id', (req, res) =>
  guard(res, () => paper.removePosition(req.params.id)));
app.post('/api/paper/settings', (req, res) => guard(res, () => paper.settings(req.body)));
app.post('/api/paper/reset', (_req, res) => guard(res, () => paper.reset()));

// ------------------------------------------------- static frontend (prod)
const dist = path.resolve(process.cwd(), '../frontend/dist');
if (fs.existsSync(dist)) {
  app.use(express.static(dist));
  app.get('*', (_req, res) => res.sendFile(path.join(dist, 'index.html')));
}

app.listen(PORT, () => {
  const s = reports.summary();
  console.log(`API on http://localhost:${PORT}`);
  console.log(`  reports: ${reports.reportsDir()} ` +
    (s ? `(built ${s.generated_utc}, verdict ${s.gates?.verdict})` : '(NOT BUILT YET)'));
  if (fs.existsSync(dist)) console.log(`  serving frontend from ${dist}`);
});
