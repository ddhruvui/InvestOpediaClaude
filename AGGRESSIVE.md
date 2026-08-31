# Aggressive cash books — branch `aggressive-short-horizon`

Research answer to: *"can this system make materially more than the baseline's
13%/yr, with shorter holds, without borrowing money?"* Built entirely on
existing data (cached Stage-2 scores on the experiment volume); no model was
retrained. Main branch and the production volume `8qik4zxpxq` are untouched —
the source volume was only ever **read**.

## The two adopted books (cash only, gross ≤ 1.0×)

| | `system_aggressive.yaml` | `system_fast.yaml` |
|---|---|---|
| Selection | top-**5** by ensemble rank | top-**10**, h5+h20 members only |
| Vertical barrier | 40 sessions (~34d avg hold) | 20 sessions (~17d avg hold) |
| Vol target | 20% (protective de-risking) | 30% |
| Gross | hard 1.0× cap, exact recycling | hard 1.0× cap, exact recycling |
| 19y walk-forward | **18.8% CAGR · SR 0.92 · MDD −51%** | **15.5% CAGR · SR 0.82 · MDD −55%** |
| At 30bps/leg (2× costs) | 17.7% CAGR (h60 twin) | ~10-12% CAGR — fragile |

Baseline for reference: 13.4% CAGR, SR 0.98, MDD −24%, ~51-session holds.
The full-period 40%/yr target is **not reachable without leverage**; the
levered twin (2× margin, 6%/yr financing) reached 23% full-period / 46%
last-3y and was explicitly declined (no borrowed money).

## What moved the needle (rounds A–F, 70 variants, DSR-ledgered)

1. **Concentration** — top-of-ranking entries earn measurably more per trade
   (top-5 > top-10 > top-20 ≫ decile). Monotone in every round.
2. **MOO cost netting** (`net_moo_costs`) — a persistent top-N name no longer
   pays a round trip to re-enter itself at the same opening print.
3. **Exact cash recycling** (`gross_cap` + `gross_cap_exact`) — sizes entries
   against realized live gross, releasing capital on actual exit dates;
   ~0.95× invested vs ~0.80× under the conservative projection.
4. **Protective vol target** — under a 1.0× cap the vol target can only
   de-risk; vt20 cut MDD by ~6pp *and* improved CAGR/Sharpe.
5. **Horizon-matched members** — the h20 book improves ~2pp CAGR using only
   h5+h20 model heads.
6. **What did nothing**: earnings-skip, FinBERT news gate, tighter regime
   thresholds, barrier width caps, asymmetric/tight barriers, m=2.0.
7. **What failed**: every 3–10 day cycle. 30bps round trips × 25-50 turns/yr
   is a 8–15%/yr headwind no gate fixed. h20 is the shortest viable cycle.
   Rank-triggered exits (round G: sell once a holding falls past rank
   10/15/25) collapse holds to 4–7 days — top-of-book rank is noise while the
   position's alpha persists — and crush CAGR to 1.7–7.9%. Barriers + the
   vertical remain the exit rule.

## Run it

```sh
# one-time: copy inputs from the production volume (read-only) to crimtr8kbf
scripts/launch_sync_volume.sh

# reproduce any experiment round
scripts/launch_exp_aggressive.sh roundF scripts/variants/roundF.json

# daily suggestions for a book (EXPERIMENT VOLUME ONLY — the config hash
# differs from production, so predict will full-refit its own champion store)
RUNPOD_VOLUME_ID_OVERRIDE=crimtr8kbf SYSTEM_CONFIG=configs/system_aggressive.yaml \
  scripts/launch_predict.sh predict
```

Engine levers added on this branch (all default-off, symmetric path
bit-identical, 25/25 tests pass): `m_up`/`m_dn`, `net_moo_costs`,
`gross_cap`(+`_exact`); harness levers `top_n`, `tranches`, `name_cap`,
`cost_bps`, `sent_gate`, `fin_bps_yr`, `vt`/`vt_cap`, `gc_exact`, plus
CAGR/subperiod/per-year/gross metrics per variant.

## From-scratch validation (2026-08-31, branch `aggressive-adoption`)

Volume cleared; ONLY raw vendor trees copied from the production volume (read-
only); every stage rerun under `SYSTEM_CONFIG=configs/system_aggressive.yaml`:
validate → m1 (**byte-identical to production**) → m1x → stage1 (RankIC 0.027)
→ stage2 (RankIC 0.030, CNN excluded as always) → stage3 → predict.

Fresh book vs research: **Sharpe 0.917 vs 0.916** (active window), CAGR 16.9%
vs 18.8% (fresh models on a newer snapshot; documented ±0.1-SR band), MDD
−43.8% vs −50.9% (shallower), last-3y 29.9%/yr at SR 1.45. CPCV median 1.04;
DSR 1.00 at N=610. Meta gate again not adopted. predict emits top-5 cash
suggestions, gross 0.99, no hedge leg (`port.hedge: none`).

Caveats: stage3_report's headline sharpe (0.786) is diluted by pre-2007 flat
days — recompute on the active window; single-name cap is per-ticker, not
per-issuer (GOOGN+GOOGM held together ≈30%).

## Before real money

Blueprint's own path applies, doubly at 5-name size: paper-trade 3–6 months
measuring open-print slippage (the console records it); expect snapshot drift
(EODHD back-revisions move headline Sharpe ~0.1 between pulls); remember the
full-period numbers contain the 2007–09 crisis at −51% peak-to-trough — the
recent-era 30%+ CAGRs are not a floor. Research tooling under its own gates — not financial advice.
