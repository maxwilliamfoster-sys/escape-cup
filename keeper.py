"""
Beat The Keeper - the club league format for the Escape Cup account (sample 2026-11-26).

Two club balls drop into a peg board and bounce down towards a goal guarded by a
sliding keeper. A ball that gets past the keeper into the net scores; it is put
back at the top and the race goes on. First to 3 goals wins the match (3 points).

Why this format (research 2026-11-26, reports/Premier League ball sim formats.md):
"Beat The Keeper" club marble videos get 0.4-5.6M views, but only as 13-50 minute
YouTube videos; nobody runs it as a 20-30 s Short. It is football-native, clearly
not the rings-escape look, and a 20-club league table gives every post a stake.

IP (same report): club NAMES and KIT COLOURS only. No crests (copyright + trade mark,
Premier League v Panini 2003), no "Premier League" name, lion, trophy or font. The
competition is our own "Escape League".

Deterministic like sim.py: same seed, same match. The director only picks which seed.
"""
import math
import random
from dataclasses import dataclass, field

from sim import Event

# --- clubs (2026/27 top flight: Coventry, Ipswich, Hull promoted; West Ham, Burnley,
# Wolves relegated). code, display name, primary, secondary, pattern ("solid"/"stripes")
CLUBS = [
    ("ARS", "Arsenal", "#E8132A", "#FFFFFF", "solid"),
    ("AVL", "Aston Villa", "#7A1E45", "#95BFE5", "solid"),
    ("BOU", "Bournemouth", "#DA291C", "#111111", "stripes"),
    ("BRE", "Brentford", "#E30613", "#FFFFFF", "stripes"),
    ("BHA", "Brighton", "#0057B8", "#FFFFFF", "stripes"),
    ("CHE", "Chelsea", "#1E54B7", "#FFFFFF", "solid"),
    ("COV", "Coventry", "#59CBE8", "#FFFFFF", "solid"),
    ("CRY", "Crystal Palace", "#1B458F", "#D2122E", "stripes"),
    ("EVE", "Everton", "#1440A6", "#FFFFFF", "solid"),
    ("FUL", "Fulham", "#F4F4F4", "#111111", "solid"),
    ("HUL", "Hull City", "#F5A12D", "#111111", "stripes"),
    ("IPS", "Ipswich", "#3A64A3", "#FFFFFF", "solid"),
    ("LEE", "Leeds", "#F4F4F4", "#FFCD00", "solid"),
    ("LIV", "Liverpool", "#C8102E", "#00B2A9", "solid"),
    ("MCI", "Man City", "#6CABDD", "#FFFFFF", "solid"),
    ("MUN", "Man Utd", "#DA291C", "#FBE122", "solid"),
    ("NEW", "Newcastle", "#111111", "#F4F4F4", "stripes"),
    ("NFO", "Nott'm Forest", "#DD0000", "#F4F4F4", "solid"),
    ("SUN", "Sunderland", "#EB172B", "#F4F4F4", "stripes"),
    ("TOT", "Tottenham", "#F4F4F4", "#132257", "solid"),
]
BY_CODE = {c[0]: c for c in CLUBS}
LEAGUE_NAME = "ESCAPE LEAGUE"
GOALS_TO_WIN = 3


def name(code):
    return BY_CODE[code][1]


def kit(code):
    """(primary, secondary, pattern)."""
    return BY_CODE[code][2:]


def fixtures(codes=None, seed=0):
    """Double round robin (circle method): 38 matchdays of 10 matches, home and away."""
    teams = list(codes or [c[0] for c in CLUBS])
    random.Random(seed).shuffle(teams)
    n = len(teams)
    rounds = []
    for r in range(n - 1):
        pairs = [(teams[i], teams[n - 1 - i]) for i in range(n // 2)]
        rounds.append([(a, b) if r % 2 == 0 else (b, a) for a, b in pairs])
        teams = [teams[0]] + [teams[-1]] + teams[1:-1]
    return rounds + [[(b, a) for a, b in md] for md in rounds]


def table(results):
    """results: [(home, away, hg, ag)] -> rows sorted by points, goal difference, goals for."""
    t = {}
    for h, a, hg, ag in results:
        for c, gf, ga in ((h, hg, ag), (a, ag, hg)):
            row = t.setdefault(c, {"code": c, "p": 0, "w": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0})
            row["p"] += 1
            row["gf"] += gf
            row["ga"] += ga
            if gf > ga:
                row["w"] += 1
                row["pts"] += 3
            else:
                row["l"] += 1
    return sorted(t.values(), key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"], r["code"]))


# --- arena (1080x1920 frame; TikTok covers y<170 and y>1480) -----------------------
W, H = 1080, 1920
CX = 540.0
WALL_L, WALL_R = 120.0, 960.0
TOP = 520.0
RAMP_Y = 1318.0                    # where the floor ramps meet the side walls
GOAL_Y = 1392.0                    # goal line (the posts sit on it)
MOUTH = 190.0                      # half the goal mouth
POST_L, POST_R = CX - MOUTH, CX + MOUTH
POST_R_RAD = 10.0
NET_BOTTOM = 1468.0
BALL_R = 32.0
PEG_R = 11.0
KEEPER_HALF = 62.0                 # half the keeper's width (a capsule)
KEEPER_RAD = 14.0
KEEPER_Y = GOAL_Y - 30.0
KEEPER_AMP = MOUTH - KEEPER_HALF + 14.0

# tuned 2026-11-26 on 40 seeds: median 17 s to 3 goals, first goal ~4 s, 33/40 end 3-2.
# Lower gravity or springier bounces kept balls floating back to the top (first goal 8-10 s).
GRAVITY = 1600.0
RESTITUTION = 0.78
MIN_BOUNCE = 280.0
MAX_SPEED = 1400.0
FPS = 60
SUBSTEPS = 6
DT = 1.0 / (FPS * SUBSTEPS)
MAX_SECONDS = 60.0
NET_SECONDS = 0.8                  # a scorer sits in the net this long, then drops in again
POST_ROLL = 0.6                    # frames kept after the winning goal (net ripple, celebration)


def _pegs():
    out = []
    rows, y0, dy, dx = 5, 690.0, 115.0, 120.0
    for r in range(rows):
        off = 0.0 if r % 2 == 0 else dx / 2
        x = WALL_L + 60 + off
        while x <= WALL_R - 60:
            out.append((x, y0 + r * dy))
            x += dx
    return out


PEGS = _pegs()
# static segments (ax, ay, bx, by): walls, roof, the two floor ramps into the goal
SEGS = [
    (WALL_L, TOP, WALL_R, TOP),
    (WALL_L, TOP, WALL_L, RAMP_Y),
    (WALL_R, TOP, WALL_R, RAMP_Y),
    (WALL_L, RAMP_Y, POST_L, GOAL_Y),
    (POST_R, GOAL_Y, WALL_R, RAMP_Y),
]
POSTS = [(POST_L, GOAL_Y), (POST_R, GOAL_Y)]


@dataclass
class Ball:
    x: float
    y: float
    vx: float
    vy: float
    net_until: float = -1.0
    last_note: float = -1.0


@dataclass
class KMatch:
    seed: int
    home: str
    away: str
    frames: list = field(default_factory=list)   # (keeper_x, [(x, y, in_net)...])
    events: list = field(default_factory=list)   # sim.Event: bounce/clash/break(=goal)/save/win
    winner: int = -1
    duration: float = 0.0
    goals: list = field(default_factory=lambda: [0, 0])
    n_balls: int = 2


def _hit(b, ax, ay, bx, by, rad, vx=0.0, vy=0.0):
    """Collide ball b with a capsule from (ax, ay) to (bx, by) of radius rad moving at (vx, vy).
    Returns (qx, qy, impact speed) on a bounce, else None."""
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    s = 0.0 if l2 == 0 else max(0.0, min(1.0, ((b.x - ax) * dx + (b.y - ay) * dy) / l2))
    qx, qy = ax + s * dx, ay + s * dy
    nx, ny = b.x - qx, b.y - qy
    d = math.hypot(nx, ny)
    reach = BALL_R + rad
    if d >= reach or d < 1e-9:
        return None
    nx, ny = nx / d, ny / d
    b.x += nx * (reach - d)
    b.y += ny * (reach - d)
    rvx, rvy = b.vx - vx, b.vy - vy
    vn = rvx * nx + rvy * ny
    if vn >= 0:
        return None
    rvx -= (1 + RESTITUTION) * vn * nx
    rvy -= (1 + RESTITUTION) * vn * ny
    b.vx, b.vy = rvx + vx, rvy + vy
    spd = math.hypot(b.vx, b.vy)
    if spd < MIN_BOUNCE:
        b.vx *= MIN_BOUNCE / spd
        b.vy *= MIN_BOUNCE / spd
    return qx, qy, -vn


def _drop(rng, b):
    """Put a ball back in at the top."""
    b.x = CX + rng.uniform(-260, 260)
    b.y = TOP + BALL_R + 14
    b.vx = rng.uniform(-260, 260)
    b.vy = rng.uniform(0, 120)


def simulate(seed, home="ARS", away="TOT", record=True):
    rng = random.Random(seed)
    m = KMatch(seed=seed, home=home, away=away)
    balls = []
    for i in range(2):
        side = -1 if i == 0 else 1
        balls.append(Ball(CX + side * rng.uniform(120, 260), TOP + BALL_R + 14,
                          side * rng.uniform(40, 220), rng.uniform(0, 90)))
    phase = rng.uniform(0, 2 * math.pi)
    w0 = rng.uniform(1.9, 2.5)
    wob, wob_p = rng.uniform(0.25, 0.45), rng.uniform(0, 2 * math.pi)
    t, end_t, kx = 0.0, None, CX
    while t < MAX_SECONDS:
        for _ in range(SUBSTEPS):
            t += DT
            omega = w0 * (1 + wob * math.sin(0.8 * t + wob_p))
            phase += omega * DT
            kx = CX + KEEPER_AMP * math.sin(phase)
            kvx = KEEPER_AMP * math.cos(phase) * omega
            for i, b in enumerate(balls):
                if b.net_until > 0:                       # sitting in the net
                    if t >= b.net_until:
                        b.net_until = -1.0
                        _drop(rng, b)
                    else:
                        b.vy += GRAVITY * DT
                        b.vx *= 0.995
                        b.x += b.vx * DT
                        b.y += b.vy * DT
                        if b.y > NET_BOTTOM - BALL_R:
                            b.y, b.vy = NET_BOTTOM - BALL_R, -abs(b.vy) * 0.35
                        lo, hi = POST_L + BALL_R, POST_R - BALL_R
                        if not lo <= b.x <= hi:
                            b.x = min(max(b.x, lo), hi)
                            b.vx = -b.vx * 0.5
                    continue
                b.vy += GRAVITY * DT
                b.x += b.vx * DT
                b.y += b.vy * DT
                hits = []
                for k, (px, py) in enumerate(PEGS):
                    if abs(b.y - py) < BALL_R + PEG_R and abs(b.x - px) < BALL_R + PEG_R:
                        h = _hit(b, px, py, px, py, PEG_R)
                        if h:
                            hits.append((k, h))
                for seg in SEGS:
                    h = _hit(b, *seg, 0.0)
                    if h:
                        hits.append((-1, h))
                for px, py in POSTS:
                    h = _hit(b, px, py, px, py, POST_R_RAD)
                    if h:
                        hits.append((-2, h))
                h = _hit(b, kx - KEEPER_HALF + KEEPER_RAD, KEEPER_Y, kx + KEEPER_HALF - KEEPER_RAD,
                         KEEPER_Y, KEEPER_RAD, kvx, 0.0)
                if h:
                    hits.append((-3, h))
                    if h[2] > 150 and b.vy < 0:           # bounced back up off the keeper
                        m.events.append(Event(t, "save", i, -3, h[0], h[1], h[2]))
                        m.events.append(Event(t, "clash", -1, -3, h[0], h[1], h[2]))
                for k, (qx, qy, v) in hits:
                    if k != -3 and t - b.last_note > 0.07:
                        b.last_note = t
                        m.events.append(Event(t, "bounce", i, k, qx, qy, v))
                spd = math.hypot(b.vx, b.vy)
                if spd > MAX_SPEED:
                    b.vx *= MAX_SPEED / spd
                    b.vy *= MAX_SPEED / spd
                # GOAL: through the mouth and over the line
                if end_t is None and b.y > GOAL_Y + BALL_R * 0.6 and POST_L < b.x < POST_R:
                    m.goals[i] += 1
                    m.events.append(Event(t, "break", i, sum(m.goals) - 1, b.x, b.y))
                    b.net_until = t + NET_SECONDS
                    b.vy *= 0.35
                    if m.goals[i] >= GOALS_TO_WIN:
                        m.winner, m.duration = i, t
                        m.events.append(Event(t, "win", i, -1, b.x, b.y))
                        b.net_until = 1e9                 # the winner stays in the net
                        end_t = t + POST_ROLL
            a, c = balls
            if a.net_until < 0 and c.net_until < 0:
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
                        m.events.append(Event(t, "clash", -1, -1, (a.x + c.x) / 2, (a.y + c.y) / 2, rel))
        if record:
            m.frames.append((kx, [(b.x, b.y, b.net_until > 0) for b in balls]))
        if end_t is not None and t >= end_t:
            break
    if m.winner < 0:
        m.duration = t
    return m


def drama(m):
    goals = [e for e in m.events if e.kind == "break"]
    times = [e.t for e in goals]
    gaps = [b - a for a, b in zip([0.0] + times, times)]
    score, lead_changes, level_after_1, leader = [0, 0], 0, 0, None
    for e in goals:
        score[e.ball] += 1
        if score[0] == score[1]:
            level_after_1 += 1
        top = 0 if score[0] > score[1] else 1 if score[1] > score[0] else None
        if top is not None and leader is not None and top != leader:
            lead_changes += 1
        if top is not None:
            leader = top
    return {"winner": m.winner, "duration": round(m.duration, 2), "goals": list(m.goals),
            "first_goal": round(times[0], 2) if times else 99.0,
            "longest_gap": round(max(gaps), 1) if gaps else 99.0,
            "saves": sum(1 for e in m.events if e.kind == "save"),
            "levels": level_after_1, "lead_changes": lead_changes}


def acceptable(d):
    return (d["winner"] >= 0 and 14.0 <= d["duration"] <= 30.0 and d["first_goal"] <= 4.5
            and d["longest_gap"] <= 8.0 and d["saves"] >= 2 and min(d["goals"]) >= 1)


def pick(base_seed, home, away, tries=300):
    """Seed with the closest scoreline (3-2 beats 3-1), then the most comebacks and saves."""
    best = None
    for k in range(tries):
        seed = base_seed * 1000 + k
        d = drama(simulate(seed, home, away, record=False))
        if not acceptable(d):
            continue
        score = (min(d["goals"]), d["levels"] + d["lead_changes"], min(d["saves"], 5))
        if best is None or score > best[0]:
            best = (score, seed, d)
        if score[0] == GOALS_TO_WIN - 1 and score[1] >= 2:
            break
    if best is None:
        raise RuntimeError(f"no acceptable keeper match in {tries} seeds from {base_seed}")
    return best[1], best[2]


if __name__ == "__main__":
    import statistics
    ds = [drama(simulate(s, record=False)) for s in range(60)]
    ok = [d for d in ds if acceptable(d)]
    print("acceptable", len(ok), "of", len(ds), "timeouts", sum(d["winner"] < 0 for d in ds))
    print("median duration", statistics.median(d["duration"] for d in ds))
    print("median first goal", statistics.median(d["first_goal"] for d in ds),
          "median saves", statistics.median(d["saves"] for d in ds))
    for d in ds[:6]:
        print(d)
