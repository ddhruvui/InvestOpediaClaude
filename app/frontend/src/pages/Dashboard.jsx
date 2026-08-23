import { useEffect, useState } from 'react';
import { api } from '../api.js';
import { LineChart, BarChart, fmtNum, fmtPct, fmtInt } from '../components/Chart.jsx';
import { Card, StatTile, Badge, Delta, Loading, ErrorBox } from '../components/Bits.jsx';

const VERDICT = {
  ADVANCE: ['good', '✓', 'Advance to paper trading',
    'Every G-11 ship criterion is met. The blueprint\'s next step is 3–6 months of '
    + 'paper trading measuring open-print slippage before any real capital.'],
  ITERATE: ['warn', '⚠', 'Iterate — not ready to trade',
    'The kill floor is cleared, so the signal is real, but the book has not reached '
    + 'the advance bar. Per the blueprint you iterate on features and members — '
    + 'never on the thresholds.'],
  KILL: ['bad', '✕', 'Kill — do not trade',
    'Rank IC or net Sharpe is below the G-11 minimum. This configuration does not ship.'],
  'LEAKAGE-AUDIT': ['bad', '⚠', 'Sanity ceiling breached — leakage audit required',
    'Results are too good to be true (Sharpe > 2.0 or drawdown < 5%). The blueprint '
    + 'routes this to the leakage checklist instead of the results deck.'],
};

export default function Dashboard() {
  const [d, setD] = useState({ loading: true });
  useEffect(() => {
    Promise.all([api.summary(), api.equity().catch(() => null)])
      .then(([summary, equity]) => setD({ summary, equity }))
      .catch((error) => setD({ error }));
  }, []);

  if (d.loading) return <Loading what="report" />;
  if (d.error) {
    return (
      <ErrorBox error={d.error} hint={
        <>Build the bundle from the pod artifacts first:{' '}
          <code>python3 tools/build_reports.py --src derived --out reports/latest</code></>
      } />
    );
  }

  const { summary, equity } = d;
  const g = summary.gates;
  const book = summary.book || {};
  const bl = summary.baselines || {};
  const [tone, mark, title, blurb] = VERDICT[g.verdict] || VERDICT.ITERATE;

  const equitySeries = equity && [{
    name: 'Deployed book (barrier exits)',
    color: 'var(--series-1)',
    points: equity.series.map((p) => ({ x: p.date.slice(0, 7), y: p.equity })),
  }];

  const memberBars = (summary.members || [])
    .filter((m) => m.RankIC != null)
    .map((m) => ({
      label: m.name, value: m.RankIC, tipLabel: 'Rank IC',
      tip: <div className="t-row"><span>Rank ICIR</span><span>{fmtNum(m.RankICIR, 3)}</span></div>,
    }));

  const cpcvBars = (summary.cpcv?.paths || []).map((p) => ({
    label: `Path ${p.path + 1}`, value: p.sharpe, tipLabel: 'Sharpe',
    tip: <div className="t-row"><span>Max drawdown</span><span>{fmtPct(p.mdd)}</span></div>,
  }));

  const sens = Object.entries(summary.sensitivity || {})
    .map(([bps, v]) => ({ label: `${bps} bps`, value: v.sharpe_net, tipLabel: 'Sharpe' }))
    .sort((a, b) => parseInt(a.label, 10) - parseInt(b.label, 10));

  return (
    <>
      <div className={`verdict ${tone}`}>
        <div className="mark" aria-hidden="true">{mark}</div>
        <div>
          <h2>{g.verdict} — {title}</h2>
          <p>{blurb}</p>
        </div>
      </div>

      <div className="tiles" style={{ marginBottom: 16 }}>
        <StatTile label="Net Sharpe" value={fmtNum(book.sharpe_net, 3)}
                  sub={`kill < 0.50 · advance ≥ 0.80`}
                  tone={book.sharpe_net == null ? '' : book.sharpe_net >= 0.5 ? 'pos' : 'neg'} />
        <StatTile label="Max drawdown" value={fmtPct(book.mdd)}
                  sub="advance needs > −15%" tone="neg" />
        <StatTile label="Annual return" value={fmtPct(book.ann_return)}
                  sub={`vol ${fmtPct(book.ann_vol)}`}
                  tone={book.ann_return == null ? '' : book.ann_return >= 0 ? 'pos' : 'neg'} />
        <StatTile label="Ensemble Rank IC" value={fmtNum(summary.ic?.RankIC, 4)}
                  sub={`floor 0.02 · ICIR ${fmtNum(summary.ic?.RankICIR, 2)}`}
                  tone={summary.ic?.RankIC == null ? '' : summary.ic.RankIC >= 0.02 ? 'pos' : 'neg'} />
        <StatTile label="Deflated Sharpe" value={fmtNum(book.dsr?.DSR, 3)}
                  sub={`N = ${fmtInt(book.dsr?.N)} trials in the ledger`} />
        <StatTile label="Barrier trades" value={fmtInt(book.n_trades)}
                  sub={`avg hold ${fmtNum(book.avg_hold_sessions, 1)} sessions`} />
      </div>

      {equitySeries && (
        <Card title="Growth of 1 — deployed book"
              subtitle={`${equity.start} to ${equity.end}, net of costs, open-to-open. `
                + `Final ${fmtNum(equity.final_equity, 2)}× · worst drawdown ${fmtPct(equity.max_drawdown)}.`}>
          <LineChart series={equitySeries} height={280} yFormat={(v) => `${fmtNum(v, 2)}×`} />
        </Card>
      )}

      <Card title="G-11 go/no-go gates"
            subtitle="The blueprint's ship criteria, evaluated against the deployed book.">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Criterion</th><th className="n">Value</th>
                <th className="n">Threshold</th><th>Status</th><th>Note</th>
              </tr>
            </thead>
            <tbody>
              {g.checks.map((c) => (
                <tr key={c.id}>
                  <td>{c.id}</td>
                  <td className="n">{fmtNum(c.value, 4)}</td>
                  <td className="n">{c.op} {fmtNum(c.threshold, 4)}</td>
                  <td><Badge kind={c.pass ? 'pass' : 'fail'}>{c.pass ? 'Pass' : 'Fail'}</Badge></td>
                  <td className="muted small">{c.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>

      <div className="grid cols-2">
        <Card title="Member Rank IC" subtitle="Out-of-sample, stitched across walk-forward folds. A member joins the ensemble only above the 0.02 floor (M10-03).">
          <BarChart data={memberBars} horizontal height={Math.max(180, memberBars.length * 30 + 40)}
                    refLine={summary.member_admission_floor} refLabel="0.02 floor"
                    valueFormat={(v) => fmtNum(v, 3)} />
        </Card>

        <Card title="Baselines it must beat (G-08)"
              subtitle="A model that cannot beat both free baselines does not ship.">
          <div className="table-wrap">
            <table>
              <thead><tr><th>Book</th><th className="n">Sharpe</th><th className="n">Ann. return</th><th className="n">Max DD</th></tr></thead>
              <tbody>
                <tr>
                  <td><span className="legend"><span className="swatch sq" style={{ background: 'var(--series-1)' }} /> Deployed book</span></td>
                  <td className="n"><strong>{fmtNum(book.sharpe_net, 3)}</strong></td>
                  <td className="n">{fmtPct(book.ann_return)}</td>
                  <td className="n">{fmtPct(book.mdd)}</td>
                </tr>
                <tr>
                  <td><span className="legend"><span className="swatch sq" style={{ background: 'var(--series-2)' }} /> SPY buy &amp; hold</span></td>
                  <td className="n">{fmtNum(bl.spy_bh?.sharpe_net, 3)}</td>
                  <td className="n">{fmtPct(bl.spy_bh?.ann_return)}</td>
                  <td className="n">{fmtPct(bl.spy_bh?.mdd)}</td>
                </tr>
                <tr>
                  <td><span className="legend"><span className="swatch sq" style={{ background: 'var(--series-3)' }} /> 12-1 momentum L/S</span></td>
                  <td className="n">{fmtNum(bl.mom_12_1?.sharpe_net, 3)}</td>
                  <td className="n">{fmtPct(bl.mom_12_1?.ann_return)}</td>
                  <td className="n">{fmtPct(bl.mom_12_1?.mdd)}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div className="note">
            Alpha vs SPY <Delta value={bl.alpha_beta_vs_spy?.alpha_ann} /> annualised,
            beta {fmtNum(bl.alpha_beta_vs_spy?.beta, 3)}.
          </div>
        </Card>
      </div>

      <div className="grid cols-2">
        {cpcvBars.length > 0 && (
          <Card title="CPCV path Sharpes"
                subtitle={`Combinatorial purged CV (N=6, k=2): 15 splits assembled into 5 backtest paths. Median ${fmtNum(summary.cpcv.sharpe_median, 2)}, worst-path drawdown ${fmtPct(summary.cpcv.mdd_worst)}. A stability estimate — it trains across eras, so it is not a live-replicable number.`}>
            <BarChart data={cpcvBars} height={210} valueFormat={(v) => fmtNum(v, 2)} />
          </Card>
        )}
        {sens.length > 0 && (
          <Card title="Cost sensitivity"
                subtitle="Net Sharpe of the Stage-1 fast path across the mandated 5/15/30 bps grid — how much of the edge survives friction.">
            <BarChart data={sens} height={210} bySign valueFormat={(v) => fmtNum(v, 2)} />
          </Card>
        )}
      </div>

      <Card title="Provenance">
        <div className="small muted" style={{ display: 'grid', gap: 3 }}>
          <div>Universe: {fmtInt(summary.universe?.names)} names ever in-scope, {fmtNum(summary.universe?.avg_daily, 0)} per day (survivorship-free).</div>
          <div>Config hash <code>{String(summary.stamp?.config_hash || '').slice(0, 12)}</code> · data snapshot <code>{summary.stamp?.data_snapshot_id}</code> · git <code>{String(summary.stamp?.git_sha || '').slice(0, 8)}</code> · seed {summary.stamp?.seed}</div>
          <div>{summary.caveat}</div>
        </div>
      </Card>
    </>
  );
}
