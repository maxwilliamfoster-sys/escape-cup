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
- 2026-10-23 amendment (growth-lab audit 2026-10-21, MASTER_PLAN #5): posting went from 2/day to 10/day on 2026-10-21, partway through A1, and to 7/day (08:00-17:00 UK) on 2026-10-23. Both arms are affected equally (slot alternation is unchanged), so the verdict rule is unchanged and still uses every post since 10-13. `analytics.py --compare` now also prints A1 split into posts before 10-21 and from 10-21 (info only), so a cadence effect can be told apart from a format effect. If the two halves disagree, judge the format on posts from 10-21 onward.
- 2026-10-23 amendment (MASTER_PLAN #11): `CARD_SECONDS` 2.8 → 1.5 s (cup) and `ROYALE_CARD_SECONDS` 3.4 → 2.0 s (royale), both arms on the same day, so videos are ~1.3-1.4 s shorter. The last 0.25 s of audio now fades out (the win chime outlasts the shorter card). Same day, royale arm only: the royale caption's first line now starts with the countries ("England, Wales, Germany or Italy? 16 countries, one way out · heat 12"; was "Escape Royale heat N: …"), and royale themes come from the whole log so consecutive royales never share a look. Cup captions are unchanged.
- Result: —

## Cadence test: 7/day, 2026-10-23 to 2026-10-30 (pre-registered)
- Change: 10/day → 7/day, the 18:30/20:00/21:30 UK slots dropped (medians by hour, old data: 12:00 = 816, 16:30 = 748, 19:30 = 320, 20:00 = 414 views@72h). Config: `posting.json`.
- Baseline at 2/day (A1 posts 2026-10-13 to 10-18, from `analytics.json` on 2026-10-23): median views@72h per post **724** (n=12 scored), total views@72h per day **~1,261** (6 full days, 2 posts each), followers **+1** from 10-15 to 10-20 (18 → 19). Followers then rose 19 → 27 on 10-20 to 10-22, the first two 10/day days.
- Judge on 10-30: total views@72h per day and followers gained per day vs the baseline; median views@72h per post as a guardrail.
- Kill switch (4/day, 11:00-16:30 UK): set `"kill_switch": true` in `posting.json` if the median views@72h of posts since 10-21 falls below **375** (`analytics.py --compare` prints the check), or if TikTok Studio marks any video "not eligible for For You". Max adds 2 rows a week to `studio_manual.csv` (completion %, For You %, followers gained).

## Queue (research 2026-10-15: growth-lab/research/reports/TikTok growth and testing methods.md)
1. **Pacing:** the first ring breaks within 1s, and no stall lasts over 5s (viewers leave at about 6s, right where the 10-13 sheet showed a 5s gap).
2. A stake-first hook: deadline/number text plus a visible counter of rings or countries left.
3. Fewer, bigger flags (a 3-flag rivalry) vs the 16-flag heat, if royale wins on stake but loses on readability.
4. **Owner's call (sound):** the big accounts sync each break to a recognisable tune ("which song/anthem?" drives their comments). That only goes ahead as a *soft-sound* option that passes the existing audio gates (no sharp/high sounds; background music was rejected before).
5. A loop ending that cuts straight into the next match's first frame.
