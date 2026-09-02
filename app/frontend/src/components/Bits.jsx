/* Small shared pieces.
   Delta is the profit/loss primitive: hue is green/red per market convention,
   but the SIGN and the ▲/▼ glyph carry the meaning on their own — green vs red
   measures ΔE 4.1 under deuteranopia, so color is never the only channel. */
import { useMemo, useState } from 'react';
import { arrow, signClass, fmtSignedPct, fmtNum } from './Chart.jsx';

/* ------------------------------------------------------- sortable tables */

/** Client-side table sorting. Click cycle per column: desc → asc → original
 *  order. Nulls sort last in both directions so gaps never bury the data. */
export function useSort(rows) {
  const [sort, setSort] = useState({ key: null, dir: null });
  const sorted = useMemo(() => {
    if (!sort.key || !sort.dir) return rows;
    const { key, dir } = sort;
    return [...rows].sort((x, y) => {
      const a = x[key];
      const b = y[key];
      if (a == null && b == null) return 0;
      if (a == null) return 1;
      if (b == null) return -1;
      const c = typeof a === 'number' && typeof b === 'number'
        ? a - b : String(a).localeCompare(String(b));
      return dir === 'asc' ? c : -c;
    });
  }, [rows, sort]);
  const onSort = (key) => setSort((s) => (s.key !== key
    ? { key, dir: 'desc' }
    : s.dir === 'desc' ? { key, dir: 'asc' } : { key: null, dir: null }));
  return { rows: sorted, sort, onSort };
}

/** Sortable header cell. `k` is the row field to sort by; omit it for
 *  non-sortable columns (action buttons, free text). */
export function Th({ k, sort, onSort, num, children }) {
  if (!k) return <th className={num ? 'n' : ''}>{children}</th>;
  const active = sort?.key === k;
  return (
    <th className={num ? 'n' : ''}
        onClick={() => onSort(k)}
        title="Click to sort"
        aria-sort={active ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
        style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' }}>
      {children}
      <span aria-hidden="true" style={{ opacity: active ? 0.9 : 0.3, marginLeft: 4 }}>
        {active ? (sort.dir === 'asc' ? '▲' : '▼') : '↕'}
      </span>
    </th>
  );
}

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
    upper: ['pass', '▲', 'Hit profit target'],
    lower: ['fail', '▼', 'Hit stop-loss'],
    vertical: ['neutral', '⏱', 'Time limit'],
    censored: ['neutral', '⋯', 'Still open at end'],
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
