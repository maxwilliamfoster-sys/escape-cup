"""Pick which simulated match gets published.

The physics is never altered - the director just runs candidate seeds and
keeps the first one that makes a watchable video: long enough to build
tension, short enough to finish, no dead stretches, and a lead that changes
hands. Winner is whatever the physics says; it is never chosen.
"""
import sim

MIN_SECONDS = 20.0
MAX_SECONDS = 42.0
MAX_WAIT = 11.0          # longest stretch with no ring breaking


def acceptable(d):
    return (d["winner"] >= 0 and MIN_SECONDS <= d["duration"] <= MAX_SECONDS
            and d["longest_wait"] <= MAX_WAIT and d["swaps"] >= 2
            and min(d["breaks"]) >= 1)


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
