"""
Deterministic 2D physics for one Escape Cup match.

Two balls start in the centre of N nested, rotating rings, each with one gap.
When a ball's whole body clears the innermost live ring through its gap, that
ring SHATTERS and the ball scores it. Both balls are then inside the next ring.
Whoever breaks the final (outermost) ring wins the match.

Nothing is scripted: the same seed replays the same match exactly. The director
only chooses WHICH seed to publish (see director.py).

Pure Python on purpose (no pymunk): the only shapes are circles and arcs, and
owning the maths keeps the physics identical on Windows and CI.
"""
import math
import random
from dataclasses import dataclass, field

# Arena geometry (pixels, in the 1080x1920 frame)
CX, CY = 540.0, 905.0
RING_RADII = [130, 188, 246, 304, 362, 420]
RING_THICK = 9.0
BALL_R = 32.0
GAP_ARC = 5.2 * BALL_R            # gap length along the ring (same at every radius)
FINAL_GAP_ARC = 4.2 * BALL_R      # the last ring is the hardest to get through

GRAVITY = 820.0
RESTITUTION = 1.0
MIN_BOUNCE_SPEED = 470.0          # keeps balls lively; wall hits never go below this
MAX_SPEED = 1350.0
FPS = 60
SUBSTEPS = 6
DT = 1.0 / (FPS * SUBSTEPS)
MAX_SECONDS = 75.0
N_RINGS = len(RING_RADII)


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


@dataclass
class Match:
    seed: int
    rings: list                                      # initial ring setup
    frames: list = field(default_factory=list)      # per frame: (angles, alive, [(x, y)...])
    events: list = field(default_factory=list)
    winner: int = -1
    duration: float = 0.0
    breaks: list = field(default_factory=lambda: [0, 0])


def _wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


def _make_rings(rng):
    rings = []
    direction = rng.choice([-1, 1])
    for i, r in enumerate(RING_RADII):
        arc = FINAL_GAP_ARC if i == N_RINGS - 1 else GAP_ARC
        omega = direction * rng.uniform(0.6, 1.3)
        rings.append(Ring(r, (arc / r) / 2, rng.uniform(-math.pi, math.pi), omega))
        direction = -direction
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


def simulate(seed, record=True):
    rng = random.Random(seed)
    rings = _make_rings(rng)
    balls = []
    for i in range(2):
        ang = rng.uniform(0, 2 * math.pi)
        spd = rng.uniform(520, 700)
        side = -1 if i == 0 else 1
        balls.append(Ball(CX + side * 36, CY - 20 + rng.uniform(-8, 8),
                          math.cos(ang) * spd, math.sin(ang) * spd))

    m = Match(seed=seed, rings=[Ring(r.radius, r.gap_half, r.angle, r.omega) for r in rings])
    t = 0.0
    reach = BALL_R + RING_THICK / 2
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
                for k in range(current, N_RINGS):
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
                            m.events.append(Event(t, "bounce", i, k, qx, qy))
                spd = math.hypot(b.vx, b.vy)
                if spd > MAX_SPEED:
                    b.vx *= MAX_SPEED / spd
                    b.vy *= MAX_SPEED / spd

            a, c = balls
            dx, dy = c.x - a.x, c.y - a.y
            d = math.hypot(dx, dy)
            if 1e-9 < d < 2 * BALL_R:
                nx, ny = dx / d, dy / d
                push = (2 * BALL_R - d) / 2
                a.x -= nx * push; a.y -= ny * push
                c.x += nx * push; c.y += ny * push
                rel = (a.vx - c.vx) * nx + (a.vy - c.vy) * ny
                if rel > 0:
                    a.vx -= rel * nx; a.vy -= rel * ny
                    c.vx += rel * nx; c.vy += rel * ny
                    m.events.append(Event(t, "clash", -1, -1, (a.x + c.x) / 2, (a.y + c.y) / 2))

            # A ring breaks once a ball's centre is well past it (the next ring
            # is closer than a ball's diameter, so "whole body outside" is
            # geometrically impossible for the inner rings).
            ring = rings[current]
            for i, b in enumerate(balls):
                if math.hypot(b.x - CX, b.y - CY) > ring.radius + BALL_R * 0.55:
                    ring.alive = False
                    m.breaks[i] += 1
                    m.events.append(Event(t, "break", i, current, b.x, b.y))
                    if current == N_RINGS - 1:
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


def drama(m):
    """Summarise a match for the director."""
    breakers = [e.ball for e in m.events if e.kind == "break"]
    swaps = sum(1 for a, b in zip(breakers, breakers[1:]) if a != b)
    # score line after the second-to-last ring: close = both on similar counts
    pre = [breakers[:-1].count(0), breakers[:-1].count(1)] if breakers else [0, 0]
    gaps = [e.t for e in m.events if e.kind == "break"]
    longest_wait = max((b - a for a, b in zip([0.0] + gaps, gaps)), default=0.0)
    return {"winner": m.winner, "duration": round(m.duration, 2), "breaks": m.breaks,
            "swaps": swaps, "pre_final": pre, "longest_wait": round(longest_wait, 1)}


if __name__ == "__main__":
    import statistics
    ds = [drama(simulate(s, record=False)) for s in range(40)]
    dur = sorted(d["duration"] for d in ds)
    print("timeouts", sum(d["winner"] < 0 for d in ds), "median", statistics.median(dur))
    print(dur)
    for d in ds[:8]:
        print(d)
