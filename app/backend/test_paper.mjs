/* Regression tests for the paper book. Run: node test_paper.mjs
   The reset test guards a real bug: a shared `positions` array on the template
   meant push() mutated the template and reset() wrote the stale rows back. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import assert from 'node:assert/strict';

process.env.PAPER_DATA_DIR = fs.mkdtempSync(path.join(os.tmpdir(), 'paperbook-'));
const paper = await import('./src/paper.js');

let pass = 0;
const t = (name, fn) => {
  try { fn(); console.log(`  ok  ${name}`); pass += 1; }
  catch (e) { console.error(`  FAIL ${name}: ${e.message}`); process.exitCode = 1; }
};

console.log('paper book');

t('open queues an MOO order, not a position', () => {
  const p = paper.openPosition({ ticker: 'aapl', target_weight: 0.03, ref_close: 100,
                                 stop_pct: -8, profit_take_pct: 8 });
  assert.equal(p.status, 'ordered');
  assert.equal(p.ticker, 'AAPL');            // normalised
});

t('duplicate open is rejected', () => {
  assert.throws(() => paper.openPosition({ ticker: 'AAPL' }), /already has an open/);
});

t('fill measures slippage against the official open (M18)', () => {
  const id = paper.state().positions[0].id;
  const p = paper.recordFill(id, { fill_price: 100.35, official_open: 100.20 });
  assert.ok(Math.abs(p.slip_bps - 14.97) < 0.01);
  // M5.2: barriers hang off the ACTUAL fill, never the signal-day close
  assert.ok(Math.abs(p.stop_price - 100.35 * 0.92) < 1e-9);
  assert.ok(Math.abs(p.profit_take_price - 100.35 * 1.08) < 1e-9);
});

t('close nets the C-08 round-trip cost', () => {
  const id = paper.state().positions[0].id;
  const p = paper.closePosition(id, { exit_price: 108.4, reason: 'profit_take',
                                      exit_date: '2026-09-04' });
  assert.ok(Math.abs(p.ret_gross - (108.4 / 100.35 - 1)) < 1e-9);
  assert.ok(Math.abs(p.ret_gross - p.ret_net - 0.003) < 1e-9);   // 2 legs x 15bps
  assert.equal(p.day_trade, false);
});

t('reset truly empties the book (shared-array regression)', () => {
  paper.reset();
  assert.deepEqual(paper.state().positions, []);
  paper.openPosition({ ticker: 'MSFT', ref_close: 400 });
  paper.reset();
  assert.deepEqual(paper.state().positions, [],
    'reset must not write back rows from the template');
});

t('a fresh book after reset starts clean', () => {
  const s = paper.state();
  assert.equal(s.positions.length, 0);
  assert.equal(s.stats.counts.open, 0);
  assert.equal(s.stats.slippage.n_fills, 0);
});

t('PDT blocks the 4th same-day round trip under $25k', () => {
  paper.reset();
  paper.settings({ account_equity: 10000 });
  const today = new Date().toISOString().slice(0, 10);
  for (let i = 0; i < 4; i += 1) {
    const p = paper.openPosition({ ticker: `T${i}`, ref_close: 100, target_weight: 0.01 });
    paper.recordFill(p.id, { fill_price: 100, official_open: 100, fill_date: today });
    if (i < 3) {
      paper.closePosition(p.id, { exit_price: 101, reason: 'profit_take', exit_date: today });
    } else {
      assert.throws(
        () => paper.closePosition(p.id, { exit_price: 101, reason: 'stop', exit_date: today }),
        /PDT budget exhausted/);
    }
  }
  assert.equal(paper.state().stats.pdt.used, 3);
});

t('PDT is not enforced at or above $25k', () => {
  paper.reset();
  paper.settings({ account_equity: 50000 });
  assert.equal(paper.state().stats.pdt.enforced, false);
});

console.log(`${pass} passed`);
fs.rmSync(process.env.PAPER_DATA_DIR, { recursive: true, force: true });
