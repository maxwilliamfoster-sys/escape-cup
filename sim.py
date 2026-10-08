"""
Deterministic 2D physics for one Escape Cup match.

Two balls start in the centre of N densely nested, rotating rings, each with one
gap. When a ball pokes through the innermost live ring's gap, that ring SHATTERS
and the ball scores it. Whoever breaks the final (outermost) ring wins.

v2 (2026-10-01): 6 thick rings with tiny balls lost viewers at 0:01 (avg watch
6-8s, <2% completion). The format that wins on TikTok is many thin rings
filling the screen with a ring popping every second or so - so 16 rings, a
ring breaks the instant a ball enters its gap (needed anyway: with dense rings
a ball can never fully clear one before touching the next).

Nothing is scripted: the same seed replays the same match exactly. The director
only chooses WHICH seed to publish (see director.py).

Pure Python on purpose (no pymunk): the only shapes are circles and arcs, and
owning the maths keeps the physics identical on Windows and CI.
"""
import math
import random
from dataclasses import dataclass, field

# Arena geometry (pixels, in the 1080x1920 frame)
CX, CY = 540.0, 930.0
N_RINGS = 16
INNER_R, OUTER_R = 96.0, 470.0
RING_RADII = [INNER_R + (OUTER_R - INNER_R) * i / (N_RINGS - 1) for i in range(N_RINGS)]
RING_THICK = 6.0
BALL_R = 22.0
GAP_DEG = 40.0                    # every ring's gap is the same ANGLE: a wedge, like the
FINAL_GAP_SCALE = 0.75            # top ball-escape videos; the last ring's gap is narrower
TWIST = (0.10, 0.30)              # per-ring phase offset (rad) -> a spiral of gaps
BREAK_DEPTH = 0.35 * BALL_R       # how far into the gap a ball must poke to break the ring

GRAVITY = 820.0
RESTITUTION = 1.0
MIN_BOUNCE_SPEED = 470.0          # keeps balls lively; wall hits never go below this
MAX_SPEED = 1350.0
FPS = 60
SUBSTEPS = 6
DT = 1.0 / (FPS * SUBSTEPS)
MAX_SECONDS = 60.0


@dataclass
class Ring:
    radius: float
    gap_half: float        # half the gap's angular width (rad)
    angle: float           # gap centre (rad)
    omega: float           # rad/s
    alive: bool = True


@dataclass
class Ball:
    x: float
    y: float
    vx: float
    vy: float
    last_note: float = -1.0


@dataclass
class Event:
    t: float
    kind: str              # "bounce" | "break" | "clash" | "win"
    ball: int
    ring: int = -1
    x: float = 0.0
    y: float = 0.0
    v: float = 0.0         # impact speed (px/s) - drives how hard the sound hits


@dataclass
class Match:
    seed: int
    rings: list                                      # initial ring setup
    frames: list = field(default_factory=list)      # per frame: (angles, alive, [(x, y)...])
    events: list = field(default_factory=list)
    winner: int = -1
    duration: float = 0.0
    breaks: list = field(default_factory=lambda: [0, 0])
    ball_r: float = BALL_R
    n_balls: int = 2


# Escape Royale (variant B, 2026-10-08): many countries in one arena. More balls
# find the gaps faster, so the arena has more rings and smaller balls.
# Tuned 2026-10-08: <20 deg the innermost gap is narrower than a ball (nothing escapes);
# rings closer than ~reach apart need the deeper break rule below; r18 = readable flags.
ROYALE = {"n_balls": 16, "ball_r": 18.0, "n_rings": 28, "inner_r": 120.0, "outer_r": 470.0,
          "gap_deg": 20.0}


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _make_rings(rng, radii=None, gap_deg=None):
    """All rings spin together with a per-ring phase twist, so the gaps form a
    rotating spiral wedge. A ball that finds the wedge can break several rings
    in a row - the combo moments that make this genre satisfying."""
    radii = radii or RING_RADII
    gap_deg = gap_deg or GAP_DEG
    rings = []
    direction = rng.choice([-1, 1])
    omega = direction * rng.uniform(0.7, 1.2)
    twist = rng.choice([-1, 1]) * rng.uniform(*TWIST)
    start = rng.uniform(-math.pi, math.pi)
    for i, r in enumerate(radii):
        gap = math.radians(gap_deg) * (FINAL_GAP_SCALE if i == len(radii) - 1 else 1.0)
        rings.append(Ring(r, gap / 2, _wrap(start + i * twist), omega))
    return rings


def _closest_on_ring(ring, bx, by):
    """Closest point on the ring's solid arc to (bx, by)."""
    dx, dy = bx - CX, by - CY
    d = math.hypot(dx, dy) or 1e-9
    rel = _wrap(math.atan2(dy, dx) - ring.angle)
    if abs(rel) >= ring.gap_half:           # facing solid wall
        return CX + dx / d * ring.radius, CY + dy / d * ring.radius
    edge = ring.angle + (ring.gap_half if rel > 0 else -ring.gap_half)
    return CX + math.cos(edge) * ring.radius, CY + math.sin(edge) * ring.radius


def simulate(seed, record=True, n_balls=2, ball_r=None, n_rings=None, inner_r=None,
             outer_r=None, gap_deg=None):
    """One match. Defaults are the 1v1 Escape Cup; pass **ROYALE for the many-country heat."""
    rng = random.Random(seed)
    ball_r = ball_r or BALL_R
    n_rings = n_rings or N_RINGS
    inner_r = inner_r or INNER_R
    outer_r = outer_r or OUTER_R
    radii = [inner_r + (outer_r - inner_r) * i / (n_rings - 1) for i in range(n_rings)]
    spacing = radii[1] - radii[0]
    rings = _make_rings(rng, radii, gap_deg)
    balls = []
    if n_balls == 2:
        for i in range(2):
            ang = rng.uniform(0, 2 * math.pi)
            spd = rng.uniform(520, 700)
            side = -1 if i == 0 else 1
            balls.append(Ball(CX + side * 30, CY - 15 + rng.uniform(-6, 6),
                              math.cos(ang) * spd, math.sin(ang) * spd))
    else:                                     # a loose rosette inside the innermost ring
        for i in range(n_balls):
            layer, idx = (0, i) if i < 6 else (1, i - 6)
            count = 6 if layer == 0 else n_balls - 6
            rad = (ball_r * 2.3) if layer == 0 else (ball_r * 4.6)
            a0 = 2 * math.pi * idx / count + layer * 0.3
            ang = rng.uniform(0, 2 * math.pi)
            spd = rng.uniform(480, 680)
            balls.append(Ball(CX + math.cos(a0) * rad, CY + math.sin(a0) * rad,
                              math.cos(ang) * spd, math.sin(ang) * spd))

    m = Match(seed=seed, rings=[Ring(r.radius, r.gap_half, r.angle, r.omega) for r in rings],
              breaks=[0] * n_balls, ball_r=ball_r, n_balls=n_balls)
    t = 0.0
    reach = ball_r + RING_THICK / 2
    # a ring breaks once a ball is far enough into its gap; with dense rings the next ring
    # out would push the ball back before 0.35*r, so never require more than it can reach
    break_depth = max(0.35 * ball_r, reach - spacing + 2.0)
    last = n_rings - 1
    current = 0                               # index of the innermost live ring
    while t < MAX_SECONDS and m.winner < 0:
        for _ in range(SUBSTEPS):
            t += DT
            for ring in rings:
                ring.angle = _wrap(ring.angle + ring.omega * DT)
            for i, b in enumerate(balls):
                b.vy += GRAVITY * DT
                b.x += b.vx * DT
                b.y += b.vy * DT
                # only rings within `reach` of the ball's radius can touch it (exact cull)
                d0 = math.hypot(b.x - CX, b.y - CY)
                lo = max(current, int((d0 - reach - inner_r) // spacing))
                hi = min(last, int((d0 + reach - inner_r) // spacing) + 1)
                for k in range(lo, hi + 1):
                    ring = rings[k]
                    qx, qy = _closest_on_ring(ring, b.x, b.y)
                    nx, ny = b.x - qx, b.y - qy
                    dist = math.hypot(nx, ny)
                    if dist >= reach or dist < 1e-9:
                        continue
                    nx, ny = nx / dist, ny / dist
                    b.x += nx * (reach - dist)
                    b.y += ny * (reach - dist)
                    wx, wy = -ring.omega * (qy - CY), ring.omega * (qx - CX)
                    rvx, rvy = b.vx - wx, b.vy - wy
                    vn = rvx * nx + rvy * ny
                    if vn < 0:
                        rvx -= (1 + RESTITUTION) * vn * nx
                        rvy -= (1 + RESTITUTION) * vn * ny
                        b.vx, b.vy = rvx + wx, rvy + wy
                        spd = math.hypot(b.vx, b.vy)
                        if spd < MIN_BOUNCE_SPEED:
                            b.vx *= MIN_BOUNCE_SPEED / spd
                            b.vy *= MIN_BOUNCE_SPEED / spd
                        if t - b.last_note > 0.07:
                            b.last_note = t
                            m.events.append(Event(t, "bounce", i, k, qx, qy, -vn))
                spd = math.hypot(b.vx, b.vy)
                if spd > MAX_SPEED:
                    b.vx *= MAX_SPEED / spd
                    b.vy *= MAX_SPEED / spd

            two_r = 2 * ball_r
            for p_ in range(n_balls):
                a = balls[p_]
                for q in range(p_ + 1, n_balls):
                    c = balls[q]
                    dx, dy = c.x - a.x, c.y - a.y
                    if abs(dx) >= two_r or abs(dy) >= two_r:
                        continue
                    d = math.hypot(dx, dy)
                    if 1e-9 < d < two_r:
                        nx, ny = dx / d, dy / d
                        push = (two_r - d) / 2
                        a.x -= nx * push; a.y -= ny * push
                        c.x += nx * push; c.y += ny * push
                        rel = (a.vx - c.vx) * nx + (a.vy - c.vy) * ny
                        if rel > 0:
                            a.vx -= rel * nx; a.vy -= rel * ny
                            c.vx += rel * nx; c.vy += rel * ny
                            m.events.append(Event(t, "clash", -1, -1, (a.x + c.x) / 2,
                                                  (a.y + c.y) / 2, rel))

            # A ring breaks the moment a ball pokes into its gap.
            ring = rings[current]
            for i, b in enumerate(balls):
                dx, dy = b.x - CX, b.y - CY
                in_gap = abs(_wrap(math.atan2(dy, dx) - ring.angle)) < ring.gap_half
                dist = math.hypot(dx, dy)
                if (in_gap and dist > ring.radius - break_depth) or dist > ring.radius + ball_r:
                    ring.alive = False
                    m.breaks[i] += 1
                    m.events.append(Event(t, "break", i, current, b.x, b.y))
                    if current == last:
                        m.winner = i
                        m.duration = t
                        m.events.append(Event(t, "win", i, current, b.x, b.y))
                    current += 1
                    break
            if m.winner >= 0:
                break
        if record:
            m.frames.append(([r.angle for r in rings], [r.alive for r in rings],
                             [(b.x, b.y) for b in balls]))
    if m.winner < 0:
        m.duration = t
    return m


def standings(m):
    """Podium order: the winner first, then by rings broken (tie: broke a ring most recently)."""
    last_t = {}
    for e in m.events:
        if e.kind == "break":
            last_t[e.ball] = e.t
    return sorted(range(m.n_balls), key=lambda i: (i != m.winner, -m.breaks[i], -last_t.get(i, -1)))


def drama(m):
    """Summarise a match for the director."""
    breakers = [e.ball for e in m.events if e.kind == "break"]
    if m.n_balls == 2:
        swaps = sum(1 for a, b in zip(breakers, breakers[1:]) if a != b)
        # score line after the second-to-last ring: close = both on similar counts
        pre = [breakers[:-1].count(0), breakers[:-1].count(1)] if breakers else [0, 0]
    else:
        # lead changes in the running ring count; "pre" = top two counts before the last ring
        counts, leader, swaps = [0] * m.n_balls, None, 0
        for b_ in breakers[:-1]:
            counts[b_] += 1
            top = max(range(m.n_balls), key=lambda i: counts[i])
            if counts[top] > sorted(counts)[-2] and top != leader:
                swaps += leader is not None
                leader = top
        pre = sorted(counts, reverse=True)[:2] if breakers else [0, 0]
    times = [e.t for e in m.events if e.kind == "break"]
    waits = [b - a for a, b in zip([0.0] + times, times)]
    longest_wait = max(waits[:-1], default=0.0)          # excluding the final ring
    final_wait = waits[-1] if waits else 0.0             # tension on the last ring
    return {"winner": m.winner, "duration": round(m.duration, 2), "breaks": m.breaks,
            "swaps": swaps, "pre_final": pre, "longest_wait": round(longest_wait, 1),
            "final_wait": round(final_wait, 1), "first_break": round(times[0], 2) if times else 99.0}


if __name__ == "__main__":
    import statistics
    ds = [drama(simulate(s, record=False)) for s in range(40)]
    dur = sorted(d["duration"] for d in ds)
    print("timeouts", sum(d["winner"] < 0 for d in ds), "median", statistics.median(dur))
    print(dur)
    for d in ds[:8]:
        print(d)
