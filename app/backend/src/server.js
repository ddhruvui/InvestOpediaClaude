// Local runner: the same app Vercel serves, listening on a port. Also serves
// the built UI (../frontend/dist) if present, so one process is enough locally.
import path from 'node:path';
import fs from 'node:fs';
import express from 'express';
import app from './app.js';
import * as reports from './reports.js';

const PORT = process.env.PORT || 8787;

const dist = path.resolve(process.cwd(), '../frontend/dist');
if (fs.existsSync(dist)) {
  app.use(express.static(dist));
  app.get('*', (_req, res) => res.sendFile(path.join(dist, 'index.html')));
}

app.listen(PORT, async () => {
  console.log(`API on http://localhost:${PORT}`);
  console.log(`  reports: ${reports.source()} -> ${reports.reportsDir()}`);
  try {
    const s = await reports.summary();
    console.log(s ? `  bundle built ${s.generated_utc}, verdict ${s.gates?.verdict}`
                  : '  bundle NOT available yet');
  } catch (err) {
    console.log(`  bundle unreachable: ${err.message}`);
  }
  if (fs.existsSync(dist)) console.log(`  serving frontend from ${dist}`);
});
