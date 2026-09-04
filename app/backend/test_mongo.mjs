/* Smoke test against the real MongoDB (needs MONGO_URI/DB_PASSWORD in .env).
   Reads the published bundle and round-trips a paper position under a
   throwaway bundle id so the real book is untouched.
   Run: npm run test:mongo */
import assert from 'node:assert/strict';

if (!process.env.MONGO_URI) {
  console.log('MONGO_URI not set — skipping');
  process.exit(0);
}
const REAL_BUNDLE = process.env.REPORT_BUNDLE || 'latest';
process.env.REPORT_BUNDLE = '_smoke_test';          // paper writes go here only

const { getDb, closeDb } = await import('./src/db.js');
const paper = await import('./src/paper.js');

let pass = 0;
const t = async (name, fn) => {
  try { await fn(); console.log(`  ok  ${name}`); pass += 1; }
  catch (e) { console.error(`  FAIL ${name}: ${e.message}`); process.exitCode = 1; }
};

const db = await getDb();
console.log(`mongo: ${db.databaseName}`);

await t(`published bundle "${REAL_BUNDLE}" has summary + suggestions`, async () => {
  const s = await db.collection('reports').findOne({ _id: `${REAL_BUNDLE}/summary` });
  const g = await db.collection('reports').findOne({ _id: `${REAL_BUNDLE}/suggestions` });
  assert.ok(s?.data?.gates?.verdict, 'summary missing or has no verdict');
  assert.ok(g?.data?.as_of_close, 'suggestions missing or has no as_of_close');
  console.log(`      verdict=${s.data.gates.verdict} as_of_close=${g.data.as_of_close} `
    + `published=${g.published_utc}`);
});

await t('paper book round-trips through paper_books', async () => {
  await paper.reset();
  const p = await paper.openPosition({ ticker: 'test', ref_close: 10, target_weight: 0.01 });
  const f = await paper.recordFill(p.id, { fill_price: 10.1, official_open: 10 });
  assert.equal(f.status, 'open');
  const s = await paper.state();
  assert.equal(s.positions.length, 1);
  assert.equal(s.positions[0].ticker, 'TEST');
  assert.ok(Math.abs(s.positions[0].slip_bps - 100) < 1e-6);
});

await db.collection('paper_books').deleteOne({ _id: '_smoke_test' });
await closeDb();
console.log(`${pass} passed`);
