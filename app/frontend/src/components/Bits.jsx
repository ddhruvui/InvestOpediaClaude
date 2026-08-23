/* Small shared pieces.
   Delta is the profit/loss primitive: hue is green/red per market convention,
   but the SIGN and the ▲/▼ glyph carry the meaning on their own — green vs red
   measures ΔE 4.1 under deuteranopia, so color is never the only channel. */
import { arrow, signClass, fmtSignedPct, fmtNum } from './Chart.jsx';

export function Delta({ value, pct = true, digits = 2, suffix = '', title }) {
  if (value == null || Number.isNaN(value)) return <span className="muted">—</span>;
  const text = pct ? fmtSignedPct(value, digits)
    : `${value >= 0 ? '+' : ''}${fmtNum(value, digits)}${suffix}`;
  return (
    <span className={`${signClass(value)} num`} title={title}>
      <span aria-hidden="true">{arrow(value)}</span> {text}
    </span>
  );
}

export function StatTile({ label, value, sub, tone }) {
  return (
    <div className="tile">
      <div className="label">{label}</div>
      <div className={`value num ${tone || ''}`}>{value}</div>
      {sub && <div className="sub">{sub}</div>}
    </div>
  );
}

export function Badge({ kind = 'neutral', glyph, children }) {
  const g = glyph ?? (kind === 'pass' ? '✓' : kind === 'fail' ? '✕' : '•');
  return (
    <span className={`badge ${kind}`}>
      <span aria-hidden="true">{g}</span>{children}
    </span>
  );
}

/** Barrier outcome — word + glyph, so the class is never colour-only. */
export function OutcomeBadge({ hit }) {
  const map = {
    upper: ['pass', '▲', 'Profit-take'],
    lower: ['fail', '▼', 'Stop'],
    vertical: ['neutral', '⏱', 'Time exit'],
    censored: ['neutral', '⋯', 'Censored'],
  };
  const [kind, glyph, label] = map[hit] || ['neutral', '•', hit || '—'];
  return <span className={`badge ${kind}`}><span aria-hidden="true">{glyph}</span>{label}</span>;
}

export function Card({ title, subtitle, right, children }) {
  return (
    <section className="card">
      {(title || right) && (
        <header style={{ display: 'flex', justifyContent: 'space-between', gap: 12 }}>
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {right}
        </header>
      )}
      {children}
    </section>
  );
}

export function Loading({ what = 'data' }) {
  return <div className="empty">Loading {what}…</div>;
}

export function ErrorBox({ error, hint }) {
  return (
    <div className="err">
      <strong>Could not load.</strong>
      <div className="small muted" style={{ marginTop: 4 }}>{String(error?.message || error)}</div>
      {hint && <div className="note" style={{ marginTop: 10 }}>{hint}</div>}
    </div>
  );
}
