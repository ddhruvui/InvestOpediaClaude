# Verification & Re-Verification Report — Implementation Blueprint v1.0 → v1.0.1
**Subject:** `stock_prediction_implementation_blueprint_v1_0.md` audited against `stock_prediction_algorithms_v2_1.md` (the verified research spec) and external primary sources.
**Date:** 2026-07-17 · **Outcome:** zero correctness errors found in spec values or formulas; 21 completeness/traceability findings, all resolved in **v1.0.1**.

## 1. Method
Four independent verification methods were run in sequence (this environment has no parallel sub-agents; independence comes from method diversity, not from separate processes — stated plainly so the audit trail is honest):

- **Pass A — programmatic cross-extraction.** Scripts extracted every numeric/named requirement from the spec and grep-verified presence in the blueprint; extracted every `[IMPL]` tag and reconciled against the §10 register; resolved every rule/quantity ID reference against its definition.
- **Pass A′ — manual line-by-line read.** The full 147-line spec was re-read end-to-end and mapped section-by-section (TL;DR, What Changed, KF1–7, §A–§K, BP1–16, GNG, Scope Notes, CAV) to blueprint anchors.
- **Pass B — independent recomputation.** Every formula the blueprint asserts was re-derived numerically from scratch (§3 below).
- **Pass C — external primary-source checks.** Live re-verification of the drift-prone anchors (§4 below).

## 2. Pass A/A′ — coverage & internal consistency
- **ID integrity:** 141 rule/quantity IDs referenced, 141 defined, 0 dangling (re-confirmed on v1.0.1).
- **Spec coverage:** every requirement in §A–§K, BP1–16, GNG, Scope Notes, and CAV has a blueprint home. Items found present that initial regexes flagged as missing (regex artifacts, no action): optional 120–250d head, short-bottom-decile option, GGR ~11%/1962–2002/post-1989/post-2002 decay + one-day-delay note, JT 6/6 & 12/3 ~1%/month grid, tercile-vs-regression choice, Kakushadze 0.6–6.4d holds, Lopez-Lira caveat, missing-H/L render rule, MA-window=image-length, weekly-Sharpe-7 warning, dual-vendor check.
- **Genuine gaps at v1.0** (all completeness/traceability, none numeric): see §5 findings — each fixed in v1.0.1.
- **Structure:** markdown tables parse (no short rows), code fences balanced, mermaid block closed; §10 register now contiguous rows 1–30.

## 3. Pass B — recomputation results (all PASS)
| Check | Result |
|---|---|
| EWMA span↔λ: λ = 1 − 2/(span+1) | span 32 → λ = 0.9394 ≈ spec's 0.94; 0.94 → span 32.33 ✓ |
| CPCV N=6, k=2 | C(6,2)=15 splits; each group in exactly 5 test instances; 30 group-instances = 6×5 → 5 assembled paths ✓ (T-15's exact assertions) |
| DSR formula behavior | Best-of-100 null trials (T=1260 daily): DSR 0.33 (correctly unimpressed). True ann. SR 1.5, same ledger: DSR 0.82 — **harsh by design**, prompting the new §16.2 calibration note ✓ |
| bb_pos vs canonical %B | %B = bb_pos/2 + 0.5 exactly; Spearman = 1.000000 → rank-identical after Q-017 ✓ |
| Barrier width m=1.5, σ=2%/d, h=20 | ±13.42% — sane swing-trade envelope ✓ |
| mom_12_1 window | close₍t−21₎/close₍t−252₎ spans 231 sessions ≈ months −12…−1, 21-session skip ✓ |
| JKX widths | 3 px/day × {5,20,60} = {15,60,180} ✓ (heights: §4) |
| Wilder RSI smoothing | recursion w_t = w_{t−1} + (x_t − w_{t−1})/14 ≡ EMA α=1/14, canonical ✓ |

## 4. Pass C — external primary-source checks
| Anchor | Source checked | Result |
|---|---|---|
| qlib Alpha158/CSI300 table | live `microsoft/qlib` main README (raw.githubusercontent) | **Full match to spec**: LGBM 0.0399/0.4065/0.0482/0.5101/AR 0.1284/IR 1.5650/MDD −0.0635; CatBoost 0.0345/0.5977; Linear 0.0332/0.1723/−0.4876; MLP 0.0229/0.0602. **Spec's "anomalous row" identified:** DoubleEnsemble's AR/IR/MDD are byte-identical to Linear's — table artifact; row unusable. Bonus context: qlib's benchmark backtest is close-dealt TopkDropout (topk 50, n_drop 5) — not this system's lagged open-to-open convention → §16.4 caveat added. |
| qlib label convention | qlib issue #171 + workflow YAML | `Ref($close,−2)/Ref($close,−1) − 1` corroborated ✓ (Q-016 pattern) |
| DSR formula | Bailey–López de Prado paper PDF (davidhbailey.com); two concordant independent implementations (ml4trading.io, marti.ai) | Blueprint's formula matches: z = (SR−SR\*)·√(T−1)/√(1−γ₃SR+((γ₄−1)/4)SR²), SR\* = √V·((1−γ)Φ⁻¹(1−1/N)+γΦ⁻¹(1−1/(N·e))). Conventions pinned in §16.2: **unannualized per-period SR; T = obs count; γ₄ raw kurtosis (normal=3)**. One circulating Medium implementation confuses Φ⁻¹ with 1/Z — flagged here so it is never copied. |
| JKX paper identity & claims | Wiley/JF, SSRN, Yale, Semantic Scholar | JF 2023, **78:3193–3249** — exact match to spec citation; context-independence + international-transfer claims corroborated. |
| JKX pixel heights (32/64/96) | attempted implementation-repo README fetch (empty); paper PDF located but not parsed this pass | **Rests on the spec's v2 verbatim primary-source audit** + width-side arithmetic (Pass B). Residual reliance disclosed; T-13's golden-hash test makes any error self-catching at implementation time. |

## 5. Findings register (all resolved in v1.0.1; none alter a spec value)
F-01 FINRA 6%-of-total-trades nuance + conservative-simplification tag; cash-account-not-a-workaround (→G-14). F-02 economic-priors screen absent (→G-09, §16.3). F-03 scope-note concrete examples (→G-13). F-04 GC borrow 0.3–1%/yr band (→§3 cost row, M18). F-05 BP14's trailing 20–60d vol-estimator alternative unexposed (→§3 `size.vol_estimator`). F-06 qlib anchors incomplete; anomaly unlocated; close-dealt caveat absent (→§16.4 rewrite). F-07 DSR conventions (unannualized, raw kurtosis) + calibration note absent (→§16.2). F-08 JKX monthly 2.35/2.16, VW ~0.5, 30–35% comparator, 26-market transfer context (→M8-02/M8-04). F-09 GKX documented-results context incl. JKMP 30-bps provenance of the cost grid's upper point (→M9.1). F-10 Sharpe-loss results context 88 futures / 2–3 bps (→M9.2). F-11 long-horizon-forecaster note (PatchTST/N-BEATS/N-HiTS/TFT; neuralforecast [MAY]) (→M9.4). F-12 %B-equivalence note (→F5). F-13 WorldQuant attribution (→F10). F-14 turnover-penalty phrasing (→M14 step 7). F-15–F-20 register completeness: rows 26–30 added (universe operationalization; meta-threshold range; portfolio engineering defaults; data-layer engineering; CNN membership + Stage-2 gate), rows 23/25 amended (indicator encodings; GRU/M6 engineering). F-21 §10 statement updated to reference this audit; changelog block added.

## 6. Explicitly not independently re-verified this pass (carried with flags)
FK/GKX paper decimals beyond the spec's v2 primary-source audit (stable published values); the community LGBM/Alpha158 reproduction (single-source, spec-flagged); Chronos/TimesFM R² decimals, JKX 0.3→0.7 international uplift, ~175%/month turnover (spec-flagged single-source — all remain flagged in §10 CAV); JKX pixel heights (see §4). Nothing in this list is load-bearing for a build decision; each is a context anchor.

## 7. Verdict
v1.0 contained **no incorrect spec values and no incorrect formulas**; its "zero deviations" claim survives re-audit. The 21 findings were completeness and traceability improvements, now incorporated. **v1.0.1 is the authoritative blueprint.** Standing residual risks, by design not removable: qlib decimals will keep drifting (ordering-only use is codified); DSR is intentionally punitive at realistic Sharpes and sample lengths (keep the trials ledger small); published-results anchors are systematically optimistic (spec CAV — carried verbatim).
