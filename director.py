"""Pick which simulated match gets published.

The physics is never altered - the director just runs candidate seeds and
keeps the first one that makes a watchable video: long enough to build
tension, short enough to finish, no dead stretches, and a lead that changes
hands. Winner is whatever the physics says; it is never chosen.
"""
import sim

# v2 (2026-10-01): viewers left at 0:01 and averaged 6-8s on 24-35s videos, so
# matches are shorter, start breaking rings immediately and never stall.
MIN_SECONDS = 14.0
MAX_SECONDS = 28.0
MAX_FIRST_BREAK = 2.0    # a ring must pop inside the first 2 seconds
MAX_WAIT = 6.0           # longest stretch with no ring breaking (before the final ring)
MAX_FINAL_WAIT = 8.0     # the final ring may build tension, but not drag


def acceptable(d):
    return (d["winner"] >= 0 and MIN_SECONDS <= d["duration"] <= MAX_SECONDS
            and d["first_break"] <= MAX_FIRST_BREAK and d["longest_wait"] <= MAX_WAIT
            and d["final_wait"] <= MAX_FINAL_WAIT and d["swaps"] >= 2
            and min(d["breaks"]) >= 3)


ROYALE_RULES = dict(min_s=20.0, max_s=36.0, first_break=1.5, wait=5.5, final_wait=8.0, swaps=2)


def acceptable_royale(d):
    r = ROYALE_RULES
    return (d["winner"] >= 0 and r["min_s"] <= d["duration"] <= r["max_s"]
            and d["first_break"] <= r["first_break"] and d["longest_wait"] <= r["wait"]
            and d["final_wait"] <= r["final_wait"] and d["swaps"] >= r["swaps"]
            and sorted(d["breaks"])[-2] >= 3)          # a real race: runner-up broke 3+ rings


def pick_royale(base_seed, tries=300, exclude=()):
    """Same idea as pick(), for the 16-country heat."""
    best = None
    for k in range(tries):
        seed = base_seed * 1000 + k
        if seed in exclude:
            continue
        d = sim.drama(sim.simulate(seed, record=False, **sim.ROYALE))
        if acceptable_royale(d):
            score = (-abs(d["pre_final"][0] - d["pre_final"][1]), d["swaps"])
            if best is None or score > best[0]:
                best = (score, seed, d)
            if score[0] >= -1 and d["swaps"] >= 3:
                break
            if k > 40 and best:
                break
    if best is None:
        raise RuntimeError(f"no acceptable royale in {tries} seeds from {base_seed}")
    return best[1], best[2]


def pick(base_seed, tries=400, exclude=()):
    best = None
    for k in range(tries):
        seed = base_seed * 1000 + k
        if seed in exclude:
            continue
        m = sim.simulate(seed, record=False)
        d = sim.drama(m)
        if acceptable(d):
            # prefer close scorelines going into the final ring
            closeness = -abs(d["pre_final"][0] - d["pre_final"][1])
            score = (closeness, d["swaps"])
            if best is None or score > best[0]:
                best = (score, seed, d)
            if closeness == 0 and d["swaps"] >= 3:
                break
            if k > 60 and best:
                break
    if best is None:
        raise RuntimeError(f"no acceptable match in {tries} seeds from {base_seed}")
    return best[1], best[2]
