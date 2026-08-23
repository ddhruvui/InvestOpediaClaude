/* Chart primitives — inline SVG so the mark specs hold exactly:
   2px lines, 4px rounded data-ends anchored to the baseline, a 2px surface gap
   between adjacent bars, solid hairline grid one shade off the surface, and a
   hover layer on every plot. One y-axis, always: no dual-scale charts. */
import { useCallback, useMemo, useRef, useState } from 'react';

export const fmtPct = (v, d = 1) =>
  v == null || Number.isNaN(v) ? '—' : `${(v * 100).toFixed(d)}%`;
export const fmtSignedPct = (v, d = 1) =>
  v == null || Number.isNaN(v) ? '—' : `${v >= 0 ? '+' : ''}${(v * 100).toFixed(d)}%`;
export const fmtNum = (v, d = 2) =>
  v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(d);
export const fmtInt = (v) =>
  v == null || Number.isNaN(v) ? '—' : Number(v).toLocaleString();

/** Sign glyph — the redundant channel that carries profit/loss without hue. */
export const arrow = (v) => (v == null ? '' : v >= 0 ? '▲' : '▼');
export const signClass = (v) => (v == null ? '' : v >= 0 ? 'pos' : 'neg');

export function useTooltip() {
  const [tip, setTip] = useState(null);
  const show = useCallback((e, content) => {
    setTip({ x: e.clientX, y: e.clientY, content });
  }, []);
  const hide = useCallback(() => setTip(null), []);
  const node = tip ? (
    <div
      className="tooltip"
      style={{
        left: Math.min(tip.x + 14, window.innerWidth - 270),
        top: Math.max(tip.y - 12, 8),
      }}
    >
      {tip.content}
    </div>
  ) : null;
  return { show, hide, node };
}

/** Rounded data-end only: the baseline end stays square. */
function barPath(x, y, w, h, r, dir) {
  const rr = Math.max(0, Math.min(r, w / 2, h));
  if (h <= 0.01) return '';
  if (dir === 'up') {
    return `M${x},${y + h} L${x},${y + rr} Q${x},${y} ${x + rr},${y} `
      + `L${x + w - rr},${y} Q${x + w},${y} ${x + w},${y + rr} L${x + w},${y + h} Z`;
  }
  if (dir === 'down') {
    return `M${x},${y} L${x},${y + h - rr} Q${x},${y + h} ${x + rr},${y + h} `
      + `L${x + w - rr},${y + h} Q${x + w},${y + h} ${x + w},${y + h - rr} L${x + w},${y} Z`;
  }
  // 'right' — horizontal bar, rounded right end
  const rh = Math.max(0, Math.min(r, h / 2, w));
  return `M${x},${y} L${x + w - rh},${y} Q${x + w},${y} ${x + w},${y + rh} `
    + `L${x + w},${y + h - rh} Q${x + w},${y + h} ${x + w - rh},${y + h} L${x},${y + h} Z`;
}

function niceTicks(min, max, count = 5) {
  if (min === max) return [min];
  const span = max - min;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / mag;
  const step = (norm >= 7.5 ? 10 : norm >= 3.5 ? 5 : norm >= 1.5 ? 2 : 1) * mag;
  const out = [];
  for (let t = Math.ceil(min / step) * step; t <= max + step * 1e-9; t += step) {
    out.push(Number(t.toFixed(10)));
  }
  return out;
}

/* ------------------------------------------------------------- LineChart */
export function LineChart({
  series, height = 260, yFormat = (v) => fmtNum(v, 2),
  xLabels = 6, yTicks = 5, logY = false, zeroLine = false,
}) {
  const ref = useRef(null);
  const [hover, setHover] = useState(null);
  const pad = { t: 12, r: 62, b: 26, l: 52 };
  const W = 900;
  const H = height;

  const flat = series.flatMap((s) => s.points);
  if (!flat.length) return <div className="empty">No data</div>;

  const tx = (v) => (logY ? Math.log10(Math.max(v, 1e-9)) : v);
  const ys = flat.map((p) => tx(p.y)).filter((v) => Number.isFinite(v));
  let yMin = Math.min(...ys);
  let yMax = Math.max(...ys);
  if (zeroLine && !logY) { yMin = Math.min(yMin, 0); yMax = Math.max(yMax, 0); }
  const padY = (yMax - yMin) * 0.08 || 0.1;
  yMin -= padY; yMax += padY;
  const n = Math.max(...series.map((s) => s.points.length));

  const px = (i) => pad.l + (i / Math.max(1, n - 1)) * (W - pad.l - pad.r);
  const py = (v) => pad.t + (1 - (tx(v) - yMin) / (yMax - yMin)) * (H - pad.t - pad.b);

  const ticks = logY
    ? niceTicks(yMin, yMax, yTicks).map((t) => 10 ** t)
    : niceTicks(yMin, yMax, yTicks);
  const labels = series[0].points;
  const xIdx = Array.from({ length: Math.min(xLabels, n) }, (_, k) =>
    Math.round((k / Math.max(1, Math.min(xLabels, n) - 1)) * (n - 1)));

  const onMove = (e) => {
    const r = ref.current.getBoundingClientRect();
    const rel = ((e.clientX - r.left) / r.width) * W;
    const i = Math.round(((rel - pad.l) / (W - pad.l - pad.r)) * (n - 1));
    if (i >= 0 && i < n) setHover({ i, cx: e.clientX, cy: e.clientY });
    else setHover(null);
  };

  return (
    <div style={{ position: 'relative' }}>
      <div className="legend">
        {series.map((s) => (
          <span className="key" key={s.name}>
            <span className="swatch" style={{ background: s.color }} />
            {s.name}
          </span>
        ))}
      </div>
      <svg
        ref={ref} viewBox={`0 0 ${W} ${H}`} width="100%" height={H}
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        role="img" style={{ display: 'block', overflow: 'visible' }}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={pad.l} x2={W - pad.r} y1={py(t)} y2={py(t)}
                  stroke="var(--gridline)" strokeWidth="1" />
            <text x={pad.l - 8} y={py(t) + 4} textAnchor="end"
                  fontSize="11" fill="var(--text-muted)"
                  style={{ fontVariantNumeric: 'tabular-nums' }}>
              {yFormat(t)}
            </text>
          </g>
        ))}
        {zeroLine && !logY && yMin < 0 && yMax > 0 && (
          <line x1={pad.l} x2={W - pad.r} y1={py(0)} y2={py(0)}
                stroke="var(--axis)" strokeWidth="1" />
        )}
        <line x1={pad.l} x2={W - pad.r} y1={H - pad.b} y2={H - pad.b}
              stroke="var(--axis)" strokeWidth="1" />
        {xIdx.map((i) => (
          <text key={i} x={px(i)} y={H - pad.b + 15} textAnchor="middle"
                fontSize="11" fill="var(--text-muted)">
            {labels[i]?.x ?? ''}
          </text>
        ))}

        {series.map((s) => {
          const d = s.points.map((p, i) =>
            `${i === 0 ? 'M' : 'L'}${px(i)},${py(p.y)}`).join(' ');
          return <path key={s.name} d={d} fill="none" stroke={s.color}
                       strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />;
        })}

        {/* direct endpoint labels — identity without relying on the legend */}
        {series.length <= 4 && series.map((s) => {
          const last = s.points[s.points.length - 1];
          if (!last) return null;
          return (
            <text key={`${s.name}-lbl`} x={W - pad.r + 7} y={py(last.y) + 4}
                  fontSize="11.5" fill="var(--text-secondary)" fontWeight="600"
                  style={{ fontVariantNumeric: 'tabular-nums' }}>
              {yFormat(last.y)}
            </text>
          );
        })}

        {hover && (
          <g>
            <line x1={px(hover.i)} x2={px(hover.i)} y1={pad.t} y2={H - pad.b}
                  stroke="var(--axis)" strokeWidth="1" />
            {series.map((s) => {
              const p = s.points[hover.i];
              return p ? (
                <circle key={s.name} cx={px(hover.i)} cy={py(p.y)} r="4.5"
                        fill={s.color} stroke="var(--surface-1)" strokeWidth="2" />
              ) : null;
            })}
          </g>
        )}
      </svg>
      {hover && (
        <div className="tooltip" style={{
          left: Math.min(hover.cx + 14, window.innerWidth - 250),
          top: Math.max(hover.cy - 12, 8),
        }}>
          <div className="t-title">{labels[hover.i]?.x}</div>
          {series.map((s) => (
            <div className="t-row" key={s.name}>
              <span style={{ color: 'var(--text-secondary)' }}>{s.name}</span>
              <span>{s.points[hover.i] ? yFormat(s.points[hover.i].y) : '—'}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* -------------------------------------------------------------- BarChart */
export function BarChart({
  data, height = 240, horizontal = false, bySign = false,
  color = 'var(--series-1)', valueFormat = (v) => fmtNum(v, 3),
  refLine = null, refLabel = '', labelEvery = 1,
}) {
  const { show, hide, node } = useTooltip();
  if (!data.length) return <div className="empty">No data</div>;

  const W = 900;
  const H = height;
  const pad = horizontal
    ? { t: 8, r: 58, b: 24, l: 108 }
    : { t: 14, r: 14, b: 40, l: 54 };
  const vals = data.map((d) => d.value).filter((v) => Number.isFinite(v));
  let vMin = Math.min(0, ...vals);
  let vMax = Math.max(0, ...vals);
  if (refLine != null) { vMin = Math.min(vMin, refLine); vMax = Math.max(vMax, refLine); }
  if (vMin === vMax) vMax = vMin + 1;
  const span = vMax - vMin;
  vMax += span * 0.06;
  if (vMin < 0) vMin -= span * 0.06;

  const fillFor = (v) => (bySign ? (v >= 0 ? 'var(--profit)' : 'var(--loss)') : color);

  if (horizontal) {
    const rowH = (H - pad.t - pad.b) / data.length;
    const barH = Math.max(6, Math.min(22, rowH - 6));   // ≥2px surface gap
    const x0 = pad.l + ((0 - vMin) / (vMax - vMin)) * (W - pad.l - pad.r);
    return (
      <div style={{ position: 'relative' }}>
        <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img"
             style={{ display: 'block', overflow: 'visible' }}>
          {niceTicks(vMin, vMax, 5).map((t) => {
            const x = pad.l + ((t - vMin) / (vMax - vMin)) * (W - pad.l - pad.r);
            return (
              <g key={t}>
                <line x1={x} x2={x} y1={pad.t} y2={H - pad.b}
                      stroke="var(--gridline)" strokeWidth="1" />
                <text x={x} y={H - pad.b + 15} textAnchor="middle" fontSize="11"
                      fill="var(--text-muted)"
                      style={{ fontVariantNumeric: 'tabular-nums' }}>
                  {valueFormat(t)}
                </text>
              </g>
            );
          })}
          <line x1={x0} x2={x0} y1={pad.t} y2={H - pad.b} stroke="var(--axis)" strokeWidth="1" />
          {refLine != null && (() => {
            const x = pad.l + ((refLine - vMin) / (vMax - vMin)) * (W - pad.l - pad.r);
            return (
              <g>
                <line x1={x} x2={x} y1={pad.t} y2={H - pad.b}
                      stroke="var(--warning)" strokeWidth="2" />
                <text x={x + 5} y={pad.t + 10} fontSize="10.5" fill="var(--text-secondary)">
                  {refLabel}
                </text>
              </g>
            );
          })()}
          {data.map((d, i) => {
            const y = pad.t + i * rowH + (rowH - barH) / 2;
            const xv = pad.l + ((d.value - vMin) / (vMax - vMin)) * (W - pad.l - pad.r);
            const x = Math.min(x0, xv);
            const w = Math.abs(xv - x0);
            return (
              <g key={d.label}
                 onMouseMove={(e) => show(e, (
                   <>
                     <div className="t-title">{d.label}</div>
                     <div className="t-row">
                       <span style={{ color: 'var(--text-secondary)' }}>{d.tipLabel || 'value'}</span>
                       <span>{valueFormat(d.value)}</span>
                     </div>
                     {d.tip}
                   </>
                 ))}
                 onMouseLeave={hide}>
                <rect x={pad.l} y={pad.t + i * rowH} width={W - pad.l - pad.r}
                      height={rowH} fill="transparent" />
                <path d={barPath(x, y, Math.max(w, 1.5), barH, 4, 'right')}
                      fill={fillFor(d.value)} />
                <text x={pad.l - 9} y={y + barH / 2 + 4} textAnchor="end" fontSize="11.5"
                      fill="var(--text-secondary)">{d.label}</text>
                <text x={x + w + 7} y={y + barH / 2 + 4} fontSize="11.5"
                      fill="var(--text-secondary)"
                      style={{ fontVariantNumeric: 'tabular-nums' }}>
                  {valueFormat(d.value)}
                </text>
              </g>
            );
          })}
        </svg>
        {node}
      </div>
    );
  }

  const colW = (W - pad.l - pad.r) / data.length;
  const barW = Math.max(3, Math.min(46, colW - 2));      // 2px surface gap
  const y0 = pad.t + (1 - (0 - vMin) / (vMax - vMin)) * (H - pad.t - pad.b);
  return (
    <div style={{ position: 'relative' }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} role="img"
           style={{ display: 'block', overflow: 'visible' }}>
        {niceTicks(vMin, vMax, 5).map((t) => {
          const y = pad.t + (1 - (t - vMin) / (vMax - vMin)) * (H - pad.t - pad.b);
          return (
            <g key={t}>
              <line x1={pad.l} x2={W - pad.r} y1={y} y2={y}
                    stroke="var(--gridline)" strokeWidth="1" />
              <text x={pad.l - 8} y={y + 4} textAnchor="end" fontSize="11"
                    fill="var(--text-muted)"
                    style={{ fontVariantNumeric: 'tabular-nums' }}>
                {valueFormat(t)}
              </text>
            </g>
          );
        })}
        <line x1={pad.l} x2={W - pad.r} y1={y0} y2={y0} stroke="var(--axis)" strokeWidth="1" />
        {data.map((d, i) => {
          const x = pad.l + i * colW + (colW - barW) / 2;
          const yv = pad.t + (1 - (d.value - vMin) / (vMax - vMin)) * (H - pad.t - pad.b);
          const up = d.value >= 0;
          const y = up ? yv : y0;
          const h = Math.abs(yv - y0);
          return (
            <g key={d.label}
               onMouseMove={(e) => show(e, (
                 <>
                   <div className="t-title">{d.label}</div>
                   <div className="t-row">
                     <span style={{ color: 'var(--text-secondary)' }}>{d.tipLabel || 'value'}</span>
                     <span>{valueFormat(d.value)}</span>
                   </div>
                   {d.tip}
                 </>
               ))}
               onMouseLeave={hide}>
              <rect x={pad.l + i * colW} y={pad.t} width={colW} height={H - pad.t - pad.b}
                    fill="transparent" />
              <path d={barPath(x, y, barW, Math.max(h, 1.5), 4, up ? 'up' : 'down')}
                    fill={fillFor(d.value)} />
              {i % labelEvery === 0 && (
                <text x={x + barW / 2} y={H - pad.b + 15} textAnchor="middle"
                      fontSize="11" fill="var(--text-muted)">{d.label}</text>
              )}
            </g>
          );
        })}
      </svg>
      {node}
    </div>
  );
}
