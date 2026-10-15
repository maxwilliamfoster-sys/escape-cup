"""growth_stats — shared A/B judging for the content bots (stdlib only).

Vendored as a single file into chess-puzzle-automation, youtube-automation
(BuriedCasefiles) and escape-cup. The master copy lives in
Projects/Content-Bots/growth-lab; edit it there and re-copy.

Rules (growth-lab plan, 2026-10-15):
- Judge every post at a fixed age (72h by default) via value_at_age(), never on
  its latest snapshot.
- Compare arms with a bootstrap 90% CI of the treatment/control ratio of the
  median (or of a pooled rate such as stay rate = sum(engaged)/sum(views)),
  plus a Mann-Whitney p-value as a second opinion.
- Verdicts: keep running (below min n) -> winner / loser (CI clear of 1.0)
  -> inconclusive at max n (keep the simpler arm). A winner that makes a
  guardrail metric clearly worse is BLOCKED.
"""
import math
import random
import statistics

KEEP_RUNNING = "keep running"
WINNER = "winner"
LOSER = "loser"
INCONCLUSIVE = "inconclusive at max n"
BLOCKED = "winner blocked by guardrail"


# ---------------------------------------------------------------- snapshots
def value_at_age(snapshots, hours, zero_at_birth=False, max_gap=48):
    """Value of a cumulative metric at `hours` of age, linearly interpolated.

    snapshots: iterable of (age_hours, value). Returns None when the post is
    too young (no snapshot at/after `hours`) or when nothing brackets the age.
    zero_at_birth adds (0, 0), right for cumulative counts like views.
    max_gap: refuse to interpolate between points further apart than this many
    hours (a video first snapshotted at 3000h has no known 72h value).
    """
    pts = sorted((float(a), float(v)) for a, v in snapshots if a is not None and v is not None)
    if zero_at_birth:
        pts = [(0.0, 0.0)] + [p for p in pts if p[0] > 0]
    if not pts or pts[-1][0] < hours:
        return None
    prev = None
    for age, val in pts:
        if age == hours:
            return val
        if age > hours:
            if prev is None:
                return None
            a0, v0 = prev
            if max_gap is not None and age - a0 > max_gap:
                return None
            return v0 + (val - v0) * (hours - a0) / (age - a0)
        prev = (age, val)
    return None


def escalation(snapshots, early=24, late=72):
    """views@late / views@early — did the platform push it past the first pool?"""
    e = value_at_age(snapshots, early, zero_at_birth=True)
    l = value_at_age(snapshots, late, zero_at_birth=True)
    if not e or l is None:
        return None
    return l / e


# ---------------------------------------------------------------- statistics
def pooled_rate(items):
    """sum(num)/sum(den) over (num, den) pairs — e.g. stay rate across videos."""
    den = sum(d for _, d in items)
    return sum(n for n, _ in items) / den if den else 0.0


def _rate(item):
    n, d = item
    return n / d if d else 0.0


def mann_whitney_p(a, b):
    """Two-sided Mann-Whitney U p-value (normal approx, tie + continuity corrected)."""
    n1, n2 = len(a), len(b)
    if not n1 or not n2:
        return 1.0
    allv = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks = [0.0] * len(allv)
    tie_term = 0.0
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        r = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = r
        t = j - i + 1
        tie_term += t ** 3 - t
        i = j + 1
    r1 = sum(r for r, (_, g) in zip(ranks, allv) if g == 0)
    u = r1 - n1 * (n1 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - tie_term / (n * (n - 1))) if n > 1 else 0
    if var <= 0:
        return 1.0
    z = (abs(u - n1 * n2 / 2) - 0.5) / math.sqrt(var)
    return min(1.0, 2 * (1 - statistics.NormalDist().cdf(max(z, 0.0))))


def bootstrap_ratio_ci(control, treatment, stat=statistics.median, iters=4000,
                       level=0.90, seed=0):
    """(lo, point, hi) for stat(treatment)/stat(control); None if control stat is 0."""
    base = stat(control) if control else 0
    if not control or not treatment or not base:
        return None
    point = stat(treatment) / base
    rng = random.Random(seed)
    ratios = []
    for _ in range(iters):
        c = stat([rng.choice(control) for _ in control])
        if not c:
            continue
        t = stat([rng.choice(treatment) for _ in treatment])
        ratios.append(t / c)
    if not ratios:
        return None
    ratios.sort()
    tail = (1 - level) / 2
    lo = ratios[int(tail * (len(ratios) - 1))]
    hi = ratios[int((1 - tail) * (len(ratios) - 1))]
    return lo, point, hi


def detectable_lift(n_per_arm, sigma=0.6, alpha=0.10, power=0.80):
    """Smallest multiplicative lift detectable on a log-normal metric.

    sigma = SD of log(metric) between videos (~0.5-0.8 for Shorts views).
    """
    z = statistics.NormalDist()
    d = (z.inv_cdf(1 - alpha / 2) + z.inv_cdf(power)) * sigma * math.sqrt(2 / n_per_arm)
    return math.exp(d)


# ---------------------------------------------------------------- verdicts
def _clean(vals):
    return [v for v in vals if v is not None]


def _side(ci, higher_is_better):
    """+1 clearly better, -1 clearly worse, 0 unclear."""
    if ci is None:
        return 0
    lo, _, hi = ci
    if lo > 1:
        return 1 if higher_is_better else -1
    if hi < 1:
        return -1 if higher_is_better else 1
    return 0


def _fragile(control, treatment, stat, ci):
    """True if dropping any single video flips the ratio to the other side of 1.0."""
    up = ci[0] > 1                       # the CI says treatment > control
    for arm, other, is_t in ((control, treatment, False), (treatment, control, True)):
        for i in range(len(arm)):
            rest = arm[:i] + arm[i + 1:]
            if not rest:
                return True
            c, t = (stat(other), stat(rest)) if is_t else (stat(rest), stat(other))
            if not c or (t / c > 1) != up:
                return True
    return False


def judge(control, treatment, min_n, max_n, higher_is_better=True, stat=None,
          guardrails=None, level=0.90, interim_level=0.98, seed=0):
    """Judge one experiment arm against control.

    control/treatment: per-post values at the judging age (None = too young,
    dropped), or (num, den) pairs when stat=pooled_rate. Prefer per-video rates
    with the default median: a pooled rate is dominated by whichever arm got a
    breakout video (testing research, 2026-10-15).
    The reported CI is always at `level`; a verdict before max n needs the
    `interim_level` CI to clear 1.0.
    guardrails: {name: (control_vals, treatment_vals, higher_is_better)} —
    judged with the median; any clearly-worse guardrail blocks a winner.
    """
    control, treatment = _clean(control), _clean(treatment)
    if stat is None:
        stat = pooled_rate if (control and isinstance(control[0], tuple)) else statistics.median
    key = _rate if stat is pooled_rate else (lambda x: x)
    res = {
        "n_control": len(control), "n_treatment": len(treatment),
        "min_n": min_n, "max_n": max_n,
        "control": stat(control) if control else None,
        "treatment": stat(treatment) if treatment else None,
        "ratio": None, "ci": None, "p": None, "guardrails_worse": [], "fragile": False,
    }
    n = min(len(control), len(treatment))
    ci = bootstrap_ratio_ci(control, treatment, stat=stat, level=level, seed=seed) if n else None
    if ci:
        res["ratio"], res["ci"] = ci[1], (ci[0], ci[2])
    if n:
        res["p"] = mann_whitney_p([key(v) for v in control], [key(v) for v in treatment])
    for name, (gc, gt, ghib) in (guardrails or {}).items():
        gc, gt = _clean(gc), _clean(gt)
        if gc and gt and _side(bootstrap_ratio_ci(gc, gt, level=level, seed=seed), ghib) < 0:
            res["guardrails_worse"].append(name)

    if n < min_n:
        res["verdict"] = KEEP_RUNNING
        return res
    # Weekly looks between min n and max n inflate false alarms (stop-when-
    # significant took 10% to ~26-30% in published simulations), so an early
    # stop needs the stricter interim_level; the final look at max n uses level.
    if n < max_n and interim_level > level:
        ci = bootstrap_ratio_ci(control, treatment, stat=stat, level=interim_level, seed=seed)
    side = _side(ci, higher_is_better)
    if side and _fragile(control, treatment, stat, ci):
        # one video decides it (e.g. a single breakout): not evidence yet
        res["fragile"] = True
        side = 0
    if side > 0:
        res["verdict"] = BLOCKED if res["guardrails_worse"] else WINNER
    elif side < 0:
        res["verdict"] = LOSER
    elif n >= max_n:
        res["verdict"] = INCONCLUSIVE
    else:
        res["verdict"] = KEEP_RUNNING
    return res


def _fmt(v):
    if v is None:
        return "-"
    return f"{v:.3g}" if abs(v) < 1000 else f"{v:,.0f}"


def format_result(experiment, metric, r):
    """One markdown line for a report / Telegram digest."""
    head = f"**{experiment}** · {metric}: {r['verdict'].upper()}"
    n = f"n={r['n_treatment']} vs {r['n_control']} (min {r['min_n']}, max {r['max_n']})"
    if r["ratio"] is None:
        return f"{head} — {n}"
    lo, hi = r["ci"]
    p = "" if r["p"] is None else f", p={r['p']:.3f}"
    line = (f"{head} — treatment {r['ratio']:.2f}x control "
            f"({_fmt(r['treatment'])} vs {_fmt(r['control'])}; 90% CI {lo:.2f}–{hi:.2f}{p}), {n}")
    if r["guardrails_worse"]:
        line += f" · guardrail worse: {', '.join(r['guardrails_worse'])}"
    return line
