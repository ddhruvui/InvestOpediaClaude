/* Regression tests for the paper book, run against the file store (no Mongo).
   Run: node test_paper.mjs
   The reset test guards a real bug: a shared `positions` array on the template
   meant push() mutated the template and reset() wrote the stale rows back. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

delete process.env.MONGO_URI;                       // force the file store
process.env.PAPER_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'paperbook-'));
const paper = await import('./src/paper.js');

let pass = 0;
const t = async (name, fn) => {
  try { await fn(); console.log(`  ok  ${name}`); pass += 1; }
  catch (e) { console.error(`  FAIL ${name}: ${e.message}`); process.exitCode = 1; }
};

console.log('paper book');

await t('open queues an MOO order, not a position', async () => {
  const p = await paper.openPosition({ ticker: 'aapl', target_weight: 0.03, ref_close: 100,
                                       stop_pct: -8, profit_take_pct: 8 });
  assert.equal(p.status, 'ordered');
  assert.equal(p.ticker, 'AAPL');            // normalised
});

await t('duplicate open is rejected', async () => {
  await assert.rejects(() => paper.openPosition({ ticker: 'AAPL' }), /already has an open/);
});

await t('fill measures slippage against the official open (M18)', async () => {
  const id = (await paper.state()).positions[0].id;
  // explicit fill_date: an implicit "today" made this a day trade on 2026-09-04
  const p = await paper.recordFill(id, { fill_price: 100.35, official_open: 100.20,
                                         fill_date: '2026-09-01' });
  assert.ok(Math.abs(p.slip_bps - 14.97) < 0.01);
  // M5.2: barriers hang off the ACTUAL fill, never the signal-day close
  assert.ok(Math.abs(p.stop_price - 100.35 * 0.92) < 1e-9);
  assert.ok(Math.abs(p.profit_take_price - 100.35 * 1.08) < 1e-9);
});

await t('close nets the C-08 round-trip cost', async () => {
  const id = (await paper.state()).positions[0].id;
  const p = await paper.closePosition(id, { exit_price: 108.4, reason: 'profit_take',
                                            exit_date: '2026-09-04' });
  assert.ok(Math.abs(p.ret_gross - (108.4 / 100.35 - 1)) < 1e-9);
  assert.ok(Math.abs(p.ret_gross - p.ret_net - 0.003) < 1e-9);   // 2 legs x 15bps
  assert.equal(p.day_trade, false);
  assert.equal(p.holding_days, 3);                                 // Tue 1st -> Fri 4th
});

await t('reset truly empties the book (shared-array regression)', async () => {
  await paper.reset();
  assert.deepEqual((await paper.state()).positions, []);
  await paper.openPosition({ ticker: 'MSFT', ref_close: 400 });
  await paper.reset();
  assert.deepEqual((await paper.state()).positions, [],
    'reset must not write back rows from the template');
});

await t('a fresh book after reset starts clean', async () => {
  const s = await paper.state();
  assert.equal(s.positions.length, 0);
  assert.equal(s.stats.counts.open, 0);
  assert.equal(s.stats.slippage.n_fills, 0);
});

await t('PDT blocks the 4th same-day round trip under $25k', async () => {
  await paper.reset();
  await paper.settings({ account_equity: 10000 });
  const today = new Date().toISOString().slice(0, 10);
  for (let i = 0; i < 4; i += 1) {
    const p = await paper.openPosition({ ticker: `T${i}`, ref_close: 100, target_weight: 0.01 });
    await paper.recordFill(p.id, { fill_price: 100, official_open: 100, fill_date: today });
    if (i < 3) {
      await paper.closePosition(p.id, { exit_price: 101, reason: 'profit_take', exit_date: today });
    } else {
      await assert.rejects(
        () => paper.closePosition(p.id, { exit_price: 101, reason: 'stop', exit_date: today }),
        /PDT budget exhausted/);
    }
  }
  assert.equal((await paper.state()).stats.pdt.used, 3);
});

await t('PDT is not enforced at or above $25k', async () => {
  await paper.reset();
  await paper.settings({ account_equity: 50000 });
  assert.equal((await paper.state()).stats.pdt.enforced, false);
});

console.log(`${pass} passed`);
fs.rmSync(process.env.PAPER_DATA_DIR, { recursive: true, force: true });

/* --- session logic: the ticket must point at the right open --- */
const { sessionContext } = await import('./src/today.js');
const reports = await import('./src/reports.js');
const sessions = (await reports.readCalendar())?.sessions;
console.log('session context');
if (!sessions) {
  console.log('  (skipped — no calendar.json in the local report bundle)');
} else {
  const cases = [
    // [UTC instant, expected next_open, label]
    ['2026-08-23T16:00:00Z', '2026-08-24', 'Sunday afternoon -> Monday open'],
    ['2026-08-21T21:30:00Z', '2026-08-24', 'Friday after the close -> Monday open'],
    ['2026-08-21T12:00:00Z', '2026-08-21', 'Friday pre-open -> today\'s open'],
    ['2026-08-25T21:30:00Z', '2026-08-26', 'Tuesday after the close -> Wednesday'],
  ];
  for (const [iso, expected, label] of cases) {
    await t(label, () => {
      const c = sessionContext(new Date(iso), sessions);
      assert.equal(c.next_open, expected, `got ${c.next_open}`);
    });
  }
  await t('weekend is flagged as a non-trading day', () => {
    const c = sessionContext(new Date('2026-08-23T16:00:00Z'), sessions);
    assert.equal(c.is_weekend, true);
    assert.equal(c.is_trading_day, false);
    assert.equal(c.last_completed_close, '2026-08-21');
  });
}
console.log(`${pass} passed total`);
