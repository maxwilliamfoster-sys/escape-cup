# Experiments: Escape Cup (@5lmvill, TikTok)

These rules come from `Projects/Content-Bots/growth-lab` (README, EXPERIMENT_TEMPLATE, growth_stats.py).
- `python analytics.py --compare` scores every post at 72h. For A1 the primary metric is views@72h, with avg % watched and likes per 1k as guardrails (amended 2026-10-19, see A1).
- Each arm needs at least 10 and at most 20 posts. At 15 per arm, watch % can detect a lift of about 1.19x; views would need about 2.1x.
- Nothing live changes without the owner's yes.

## A1: cup (1v1) vs Escape Royale (16 countries)           status: running
- Hypothesis: Giving viewers a stake (many countries at once, a league table, "comment your country") raises avg % watched compared with 1v1 matches between two random countries (growth-report-2026-10-08).
- Variable: the format. Arms swap time slots daily (`main.arm_for`), so slot time is balanced.
- Primary metric: median avg % watched at 72h. Guardrails: views@72h, likes per 1k.
- Min/max n per arm: 10 / 20. This was raised from the original 6 on 2026-10-15, because the measured spread made 6 too few to tell noise from a real effect.
- Start: 2026-10-13 (first royale 10-14). Earliest judge date: about 2026-10-27.
- Amended 2026-10-19 before min n (1 v 1): primary = views@72h, hold % = guardrail. Reason: % watched penalises the longer royale (~29 s vs ~21 s), the same reason length levers use views72. (`levers.json` format lever `"primary": "views72"`; `analytics.py --compare` judges the same way.)
- 2026-10-19 amendment: the royale caption's first line now names the heat ("Escape Royale heat N: …") and the second line starts "Featuring …". This fixes a deadlock: all 5 old first lines had become "recent", so the repeat rule blocked every royale render. Captions are now checked before rendering.
- Result: —

## Queue (research 2026-10-15: growth-lab/research/reports/TikTok growth and testing methods.md)
1. **Pacing:** the first ring breaks within 1s, and no stall lasts over 5s (viewers leave at about 6s, right where the 10-13 sheet showed a 5s gap).
2. A stake-first hook: deadline/number text plus a visible counter of rings or countries left.
3. Fewer, bigger flags (a 3-flag rivalry) vs the 16-flag heat, if royale wins on stake but loses on readability.
4. **Owner's call (sound):** the big accounts sync each break to a recognisable tune ("which song/anthem?" drives their comments). That only goes ahead as a *soft-sound* option that passes the existing audio gates (no sharp/high sounds; background music was rejected before).
5. A loop ending that cuts straight into the next match's first frame.
