import { useEffect, useMemo, useState } from 'react';
import { api } from '../api.js';
import { LineChart, BarChart, fmtNum, fmtPct, fmtInt, fmtSignedPct } from '../components/Chart.jsx';
import { Card, StatTile, Badge, Delta, Loading, ErrorBox, useSort, Th } from '../components/Bits.jsx';

const EMPTY = [];

const VERDICT = {
  ADVANCE: ['good', '✓', 'Ready for practice trading',
    'Every quality check passed. The next step is 3–6 months of practice (paper) '
    + 'trading to see how real fills behave — still no real money yet.'],
  ITERATE: ['warn', '⚠', 'Not ready to trade — keep improving',
    'The strategy shows a real signal, but it is not strong enough to pass the '
    + 'ship criteria. The rules say: improve the model, never loosen the bar.'],
  KILL: ['bad', '✕', 'Do not trade — no real edge',
    'Prediction accuracy or the risk-adjusted score is below the minimum. This '
    + 'version of the strategy does not ship.'],
  'LEAKAGE-AUDIT': ['bad', '⚠', 'Too good to be true — check for errors',
    'Results this strong (Sharpe > 2.0 or drawdown < 5%) usually mean the model '
    + 'accidentally peeked at the future. It goes to an error audit, not to trading.'],
};

export default function Dashboard() {
  const [d, setD] = useState({ loading: true });
  useEffect(() => {
    Promise.all([api.summary(), api.equity().catch(() => null)])
      .then(([summary, equity]) => setD({ summary, equity }))
      .catch((error) => setD({ error }));
  }, []);

  const checkSort = useSort(d.summary?.gates?.checks ?? EMPTY);
  const baselineRows = useMemo(() => {
    const b = d.summary?.book || {};
    const bl0 = d.summary?.baselines || {};
    return [
      { name: 'This strategy', color: 'var(--series-1)', strong: true,
        sharpe: b.sharpe_net, ann: b.ann_return, mdd: b.mdd },
      { name: 'SPY buy & hold', color: 'var(--series-2)',
        sharpe: bl0.spy_bh?.sharpe_net, ann: bl0.spy_bh?.ann_return, mdd: bl0.spy_bh?.mdd },
      { name: 'Basic momentum recipe', color: 'var(--series-3)',
        sharpe: bl0.mom_12_1?.sharpe_net, ann: bl0.mom_12_1?.ann_return, mdd: bl0.mom_12_1?.mdd },
    ];
  }, [d.summary]);
  const blSort = useSort(baselineRows);

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

  const eqYears = equity
    && (new Date(equity.end) - new Date(equity.start)) / (365.25 * 86400e3);
  const cagr = equity && eqYears > 0 ? equity.final_equity ** (1 / eqYears) - 1 : null;

  const equitySeries = equity && [{
    name: 'This strategy, after costs',
    color: 'var(--series-1)',
    points: equity.series.map((p) => ({ x: p.date.slice(0, 7), y: p.equity - 1 })),
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
        <StatTile label="Risk-adjusted score" value={fmtNum(book.sharpe_net, 3)}
                  sub="Sharpe · below 0.50 kills it, 0.80 advances"
                  tone={book.sharpe_net == null ? '' : book.sharpe_net >= 0.5 ? 'pos' : 'neg'} />
        <StatTile label="Worst losing stretch" value={fmtPct(book.mdd)}
                  sub="max drawdown · must stay above −15%" tone="neg" />
        <StatTile label="Return per year" value={fmtPct(book.ann_return)}
                  sub={`typical yearly swing ±${fmtPct(book.ann_vol)}`}
                  tone={book.ann_return == null ? '' : book.ann_return >= 0 ? 'pos' : 'neg'} />
        <StatTile label="Growth per year" value={fmtSignedPct(cagr, 1)}
                  sub={eqYears ? `compound, over ${fmtNum(eqYears, 1)} years` : ''}
                  tone={cagr == null ? '' : cagr >= 0 ? 'pos' : 'neg'} />
        <StatTile label="Prediction accuracy" value={fmtNum(summary.ic?.RankIC, 4)}
                  sub={`rank IC · floor 0.02 · ICIR ${fmtNum(summary.ic?.RankICIR, 2)}`}
                  tone={summary.ic?.RankIC == null ? '' : summary.ic.RankIC >= 0.02 ? 'pos' : 'neg'} />
        <StatTile label="Luck-adjusted score" value={fmtNum(book.dsr?.DSR, 3)}
                  sub={`deflated Sharpe over ${fmtInt(book.dsr?.N)} tries`} />
        <StatTile label="Trades tested" value={fmtInt(book.n_trades)}
                  sub={`held ${fmtNum(book.avg_hold_sessions, 1)} trading days on average`} />
      </div>

      {equitySeries && (
        <Card title="How money in this strategy would have grown"
              subtitle={`Simulated from ${equity.start} to ${equity.end}, after trading costs. `
                + `Total ${fmtSignedPct(equity.final_equity - 1, 1)} · average ${fmtSignedPct(cagr, 1)} per year `
                + `· worst losing stretch ${fmtPct(equity.max_drawdown)}.`}>
          <LineChart series={equitySeries} height={280} zeroLine
                     yFormat={(v) => fmtSignedPct(v, 1)} />
        </Card>
      )}

      <Card title="Quality checks (the G-11 gates)"
            subtitle="Every rule the strategy must pass before it counts as tradeable. One fail and it stays in research.">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <Th k="id" sort={checkSort.sort} onSort={checkSort.onSort}>Check</Th>
                <Th k="value" num sort={checkSort.sort} onSort={checkSort.onSort}>This strategy</Th>
                <Th k="threshold" num sort={checkSort.sort} onSort={checkSort.onSort}>Needs to be</Th>
                <Th k="pass" sort={checkSort.sort} onSort={checkSort.onSort}>Result</Th>
                <th>What it means</th>
              </tr>
            </thead>
            <tbody>
              {checkSort.rows.map((c) => (
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
        <Card title="How accurate is each model?"
              subtitle="The strategy blends several models. Each bar is one model's prediction accuracy (rank IC) on data it never saw; a model gets a vote only above the 0.02 floor.">
          <BarChart data={memberBars} horizontal height={Math.max(180, memberBars.length * 30 + 40)}
                    refLine={summary.member_admission_floor} refLabel="0.02 floor"
                    valueFormat={(v) => fmtNum(v, 3)} />
        </Card>

        <Card title="Does it beat doing the simple thing?"
              subtitle="If the strategy can't beat just buying the index (SPY) and a basic momentum recipe, it doesn't ship.">
          <div className="table-wrap">
            <table>
              <thead><tr>
                <Th k="name" sort={blSort.sort} onSort={blSort.onSort}>Approach</Th>
                <Th k="sharpe" num sort={blSort.sort} onSort={blSort.onSort}>Risk-adjusted score</Th>
                <Th k="ann" num sort={blSort.sort} onSort={blSort.onSort}>Return per year</Th>
                <Th k="mdd" num sort={blSort.sort} onSort={blSort.onSort}>Worst stretch</Th>
              </tr></thead>
              <tbody>
                {blSort.rows.map((r) => (
                  <tr key={r.name}>
                    <td><span className="legend"><span className="swatch sq" style={{ background: r.color }} /> {r.name}</span></td>
                    <td className="n">{r.strong ? <strong>{fmtNum(r.sharpe, 3)}</strong> : fmtNum(r.sharpe, 3)}</td>
                    <td className="n">{fmtPct(r.ann)}</td>
                    <td className="n">{fmtPct(r.mdd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="note">
            Extra return beyond what the market itself gave (alpha):{' '}
            <Delta value={bl.alpha_beta_vs_spy?.alpha_ann} /> per year · moves{' '}
            {fmtNum(bl.alpha_beta_vs_spy?.beta, 2)}× as much as the market (beta).
          </div>
        </Card>
      </div>

      <div className="grid cols-2">
        {cpcvBars.length > 0 && (
          <Card title="Stability check"
                subtitle={`The same strategy re-scored on 5 alternate re-shuffles of history (CPCV). Similar bars = the result isn't a fluke of one particular period. Median score ${fmtNum(summary.cpcv.sharpe_median, 2)}, worst losing stretch across paths ${fmtPct(summary.cpcv.mdd_worst)}. A robustness estimate, not a live-tradeable number.`}>
            <BarChart data={cpcvBars} height={210} valueFormat={(v) => fmtNum(v, 2)} />
          </Card>
        )}
        {sens.length > 0 && (
          <Card title="What if trading got more expensive?"
                subtitle="The risk-adjusted score as per-trade costs rise (5, 15, 30 hundredths of a percent). Shows how much of the edge survives real-world fees and slippage.">
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
