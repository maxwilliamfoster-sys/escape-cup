"""
Escape Cup - one physics match per TikTok video, posted via Buffer.

Normal run (CI, once a day, early UTC): fills the posting slots of the next
~36 hours that Buffer doesn't already hold. Each video must pass quality.py
(technical, visual, audio, content and caption checks) or a different match
is rendered; a video that fails is never posted. Scheduling ahead also gives
the owner a veto window: every scheduled video is sent to Telegram hours
before it goes out and can be deleted in Buffer.

  python main.py                     # schedule
  python main.py --sample            # render + gate the next match, Telegram only
  python main.py --sample --local    # same, no network at all
  python main.py --reset-unpublished # cancel our pending Buffer posts; redraw if nothing published

Posting plan (research 2026-09-29):
  - first RAMP_DAYS days: 1 video/day at 19:30 UK (new accounts get scrutiny;
    start slow), then 2/day.
  - 2026-10-21: 10/day, 08:00-21:30 UK every 90 min (DAY_SLOTS) - still under
    TikTok's 15/day API cap; more posts = more chances of a heavy-tail hit.
  - 2026-10-23: 7/day, 08:00-17:00 UK (growth-lab audit 2026-10-21, MASTER_PLAN #5):
    the evening slots were the weak ones. Slots and the 4/day kill switch live in
    posting.json (see its _doc).
"""
import argparse
import copy
import hashlib
import html
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import director
import app_events
import notify
import publish
import quality
import render
import sim
import upload_gate
import teams
import tournament

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
WORK = os.path.join(ROOT, "work")
LONDON = ZoneInfo("Europe/London")

RAMP_DAYS = 7
RAMP_SLOTS = [(19, 30)]
# 10 a day from 2026-10-21 (Max: more posts = more chances of a breakout), 90 min apart.
# 2026-10-23: 18:30/20:00/21:30 UK dropped (medians 320-414 views@72h vs 748-816 at midday;
# growth-lab audit-2026-10-21 MASTER_PLAN #5) -> 7 a day, 08:00-17:00 UK. The live list is
# read from posting.json, which also holds the 4/day kill switch; this is the fallback.
DEFAULT_DAY_SLOTS = [(8, 0), (9, 30), (11, 0), (12, 30), (14, 0), (15, 30), (17, 0)]
POSTING_CONFIG = os.path.join(ROOT, "posting.json")


def _parse_slots(items):
    out = []
    for s in items:
        h, m = (int(x) for x in str(s).split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(f"bad slot {s}")
        out.append((h, m))
    if not out:
        raise ValueError("no slots")
    return sorted(out)


def load_day_slots(path=POSTING_CONFIG):
    """Daily posting slots (UK) from posting.json: day_slots_uk, or kill_switch_slots_uk
    when kill_switch is true. Any problem with the file -> DEFAULT_DAY_SLOTS (never stops
    posting)."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        key = "kill_switch_slots_uk" if cfg.get("kill_switch") is True else "day_slots_uk"
        slots = _parse_slots(cfg[key])
        if key == "kill_switch_slots_uk":
            print(f"[main] KILL SWITCH on (posting.json): {len(slots)} slots a day")
        return slots
    except Exception as e:
        print(f"[main] posting.json unusable ({e}) - using the default {len(DEFAULT_DAY_SLOTS)} slots")
        return list(DEFAULT_DAY_SLOTS)


DAY_SLOTS = load_day_slots()
# How far ahead to schedule (= your veto window). Under 24h so a run never asks Buffer to hold
# more than one day's posts: the free plan's queue cap is 10 per channel.
HORIZON = timedelta(hours=20)
MIN_LEAD = timedelta(minutes=25)     # never schedule a slot closer than this
# A rejected render never skips a slot: new seeds are tried until one passes the
# quality gate AND the upload gate, while the run's time budget lasts (job timeout
# 60 min). MAX_ATTEMPTS is only a per-slot safety cap. If a slot still has nothing,
# the run stops there and the next 3-hourly run retries it (HORIZON = 20h ahead).
MAX_ATTEMPTS = 12
# When the director finds no acceptable seed in its usual 400/300 tries, it searches
# this many once before the slot is given up (x3 the defaults).
WIDE_TRIES_CUP = 1200
WIDE_TRIES_ROYALE = 900
# Real AI reviews per UTC day for this bot. The free Groq quota is shared by 3 bots
# (growth-lab PLAN_2026-10-19). Was 14 (10 posts a day at ~1.1 reviews each). 2026-10-23
# (audit MASTER_PLAN #6): 3 = the first cup and first royale of each UTC day plus one spare.
# All 15 real Escape reviews passed at 9-10/10 (~4,070 tokens each), so this frees ~45k
# tokens a day for BCF, whose required review does catch problems. Renders past the cap
# still post on the deterministic gates ("AI cap reached" is a warning, not a block).
AI_DAILY_CAP = 3
SYSTEMIC_STOP = "same failure 3 times in a row - stopping (not a content problem; check the log)"
# Renders finish by 35 min + 15 min hosting/Telegram per slot + ~3 min setup < the
# 60-min job timeout. Checked before EVERY render (also a later slot's first one),
# against the slowest render so far in this run.
RUN_BUDGET_S = 35 * 60
_RUN_START = time.monotonic()
_SLOWEST = 0.0

RESULT_LINE = {16: "advances to the Round of 16", 8: "reaches the quarter-finals",
               4: "reaches the semi-finals", 2: "reaches the Final"}

# Caption variety: TikTok lists automation that "sends repetitive content" as
# spam. First line always names both countries + a niche keyword (TikTok search
# indexes captions); no spoilers, no like/follow asks.
HEADS = [
    "{A} vs {B} - which country escapes the rings first?",
    "Ball escape simulation: {A} vs {B}",
    "{A} or {B}? Physics decides which country breaks out first",
    "Country ball escape - {A} vs {B}",
    "{A} vs {B} in the escape rings. Who gets out first?",
    "{A} vs {B}: 16 rings, one way out. Physics simulation",
    "Which country escapes first? {A} vs {B} ball escape",
]
FINAL_HEADS = [
    "THE FINAL 🏆 {A} vs {B} - which country escapes with the cup?",
    "Escape Cup final: {A} vs {B}. Physics simulation decides it 🏆",
]
# never "#1" in a caption: TikTok turns it into a hashtag (the gate caught this)
ROUND_LINES = ["{round} · Escape Cup {ed}", "Escape Cup {ed}, {round}", "{round} of Escape Cup {ed} 🏆"]
TEASES = ["Next up: {C} vs {D} - who's your pick? 👇", "Coming next: {C} vs {D}. Who takes it? 👇",
          "{C} vs {D} is next - who are you backing? 👇"]
NICHE_TAGS = ["#ballescape", "#physicssimulation"]
ROTATING_TAG = ["#satisfying", "#oddlysatisfying", "#simulation"]


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# ------------------------------------------------------------------ timing --

def first_post_day(state, fallback):
    """Day of the account's first post, across every cup (the ramp must not restart per cup)."""
    days = [datetime.fromisoformat(e["due"]).astimezone(LONDON).date()
            for e in state.get("log", []) if e.get("due")]
    return min(days) if days else fallback


def slots_for_day(day, first_day):
    if (day - first_day).days < RAMP_DAYS:
        return RAMP_SLOTS
    return DAY_SLOTS


def upcoming_slots(now, taken, state):
    """Slots (UTC) in (now+MIN_LEAD, now+HORIZON] strictly AFTER everything already
    scheduled. Matches are consumed in bracket order, so filling an earlier gap
    with a later match would publish the story out of order - never do that."""
    local_today = now.astimezone(LONDON).date()
    first = first_post_day(state, local_today)
    after = max(taken + [now + MIN_LEAD - timedelta(seconds=1)])
    out = []
    for offset in range(3):
        day = local_today + timedelta(days=offset)
        for h, mnt in slots_for_day(day, first):
            slot = datetime(day.year, day.month, day.day, h, mnt, tzinfo=LONDON).astimezone(timezone.utc)
            if slot <= now + HORIZON and (slot - after).total_seconds() > (1800 if taken else 0):
                out.append(slot)
    return out


# --------------------------------------------------------------- building --

def make_caption(state, a, b, d, preview, rng):
    fa = f"{teams.name(a)} {teams.emoji(a)}"
    fb = f"{teams.name(b)} {teams.emoji(b)}"
    if d["size"] == 1:
        head = rng.choice(FINAL_HEADS).format(A=fa, B=fb)
        second = f"Escape Cup {state['edition']}"
    else:
        head = rng.choice(HEADS).format(A=fa, B=fb)
        second = rng.choice(ROUND_LINES).format(round=d["round_name"], ed=state["edition"])
    if preview:
        tease = rng.choice(TEASES).format(C=teams.name(preview["a"]), D=teams.name(preview["b"]))
    else:
        tease = f"Escape Cup {state['edition'] + 1} starts with a new draw - which country is yours? 👇"
    tags = NICHE_TAGS + [rng.choice(ROTATING_TAG), teams.hashtag(a), teams.hashtag(b)]
    return f"{head}\n{second}\n{tease}\n{' '.join(tags)}"


CAPTION_TRIES = 6


def _pick_caption(build, seed, check):
    """Captions are checked BEFORE rendering: a words-only failure (e.g. a repeated
    first line) used to throw away a finished render and exclude a good seed.
    Tries the seed's own wording first (unchanged from before), then re-rolls the
    words only. Returns (caption, failures of the last try)."""
    caption, fails = "", []
    for k in range(CAPTION_TRIES):
        caption = build(random.Random(seed if k == 0 else f"{seed}-cap{k}"))
        fails = check(caption)
        if not fails:
            break
    return caption, fails


def _caption_failures(caption, state, a, b):
    """Our caption rules plus the upload gate's text layer (same recent headlines)."""
    return (quality.check_caption(caption, a, b, state.get("recent_captions", []))
            + upload_gate.check_text("tiktok", caption=caption, recent_texts=_recent_heads(state))[0])


def _caption_only(caption, fails, **fields):
    """Build result for a caption that failed every re-roll: nothing was rendered."""
    return {"path": None, "caption": caption, "report": {}, "gate": None, "caption_only": True,
            "failures": [f"caption: {f}" for f in fails], **fields}


def build_one(state, exclude_seeds=(), tries=None):
    """Render + gate the next match. Returns dict with path, caption, report, failures..."""
    r, m = tournament.next_match(state)
    match = state["rounds"][r][m]
    a, b = match["a"], match["b"]
    d = tournament.describe(state, r, m)
    base = int(hashlib.sha1(f"{state['edition']}-{r}-{m}".encode()).hexdigest()[:7], 16)
    seed, drama = director.pick(base, exclude=exclude_seeds, **({"tries": tries} if tries else {}))
    phys = sim.simulate(seed)
    winner = [a, b][phys.winner]
    is_final = d["size"] == 1

    if is_final:
        titles = state["titles"].get(winner, 0) + 1
        result = f"wins Escape Cup #{state['edition']}" + (f" - {_ordinal(titles)} title" if titles > 1 else "")
    else:
        result = RESULT_LINE[d["size"]]
    preview = tournament.preview_after(state, r, m, winner)
    if preview:
        next_label = "NEXT MATCH" if preview["round_name"] == d["round_name"] else preview["round_name"].upper()
        next_line = f"{teams.name(preview['a']).upper()} vs {teams.name(preview['b']).upper()}"
        cta = "Who wins? Comment your pick"
    else:
        next_label = f"ESCAPE CUP #{state['edition'] + 1}"
        next_line = "NEW DRAW NEXT"
        cta = "Which country is yours?"

    # consecutive videos never share a theme; the Final gets its own gold look
    themes = [t for t in render.THEMES if t != render.FINAL_THEME]
    theme = render.FINAL_THEME if is_final else themes[state.get("posts", 0) % len(themes)]

    info = dict(a=a, b=b, header=d["header"], result_line=result, theme=theme,
                next_label=next_label, next_line=next_line, cta=cta, final=is_final,
                hook=hook_for_match(a, b))
    fname = f"ec{state['edition']:02d}_r{r}_m{m:02d}_{seed}.mp4"
    caption, cap_fail = _pick_caption(
        lambda g: make_caption(state, a, b, d, preview, g), seed,
        lambda c: _caption_failures(c, state, a, b))
    if cap_fail:
        return _caption_only(caption, cap_fail, seed=seed, file=fname, winner=winner, r=r, m=m,
                             round=d["round_name"], a=a, b=b, theme=theme, drama=drama)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(OUT, fname)
    render.render_match(phys, info, path, WORK)

    report, failures = quality.check_video(path, len(phys.frames) / sim.FPS)
    failures += quality.check_content(a, b, winner, phys.breaks)
    verdict = gate(path, caption, state, quality.DURATION_RANGE) if not failures else None
    if verdict:
        failures += verdict["failures"]
    return {"path": path, "caption": caption, "file": fname, "seed": seed, "winner": winner,
            "r": r, "m": m, "round": d["round_name"], "a": a, "b": b, "theme": theme,
            "drama": drama, "report": report, "failures": failures, "gate": verdict}


# Upload gate (growth-lab upload_gate.py): TikTok's limits and guidelines, technical
# quality and, when GROQ_API_KEY is set, an AI review of the frames. Runs after
# quality.py; publish.host_video refuses any file it did not approve.
GATE_LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upload_gate_log.json")
GATE_CONTEXT = ("Escape Cup: physics simulation. Country flag balls bounce inside rotating rings; a "
                "ring shatters when a ball escapes through its gap; first to break out wins. Hook box "
                "at the top; the end card names the winner and teases the NEXT match or heat "
                "(different countries by design). No narration, soft sound effects. The physics is "
                "real; results are not rigged.")


def gate(path, caption, state, seconds):
    return upload_gate.check(path, "tiktok", profile={
        "fps": 60, "seconds": seconds, "min_video_kbps": quality.MIN_VIDEO_KBPS,
        "lufs": quality.LUFS_RANGE, "ai_review": "if_available", "ai_daily_cap": AI_DAILY_CAP,
        "ai_context": GATE_CONTEXT,
        "ledger": GATE_LEDGER}, caption=caption, ai_label=False,
        recent_texts=_recent_heads(state))


def _recent_heads(state):
    """First caption lines the gate counts as recent: our last 40 captions plus the
    headlines it approved before (upload_gate.check adds those from its ledger)."""
    heads = [c.split("\n")[0] for c in state.get("recent_captions", [])]
    if GATE_LEDGER:
        heads += upload_gate.recent_heads(GATE_LEDGER, "tiktok")
    return heads


def _gate_record(v):
    g = v.get("gate") or {}
    return {"ok": bool(g.get("ok")), "sha256": g.get("sha256"), "checked_at": g.get("checked_at")}


def build_passing(state):
    """Try up to MAX_ATTEMPTS different matches; return the first that passes, else None + reasons."""
    r, m = tournament.next_match(state)
    # seeds whose videos were vetoed (deleted in Buffer) or rejected are never reused
    key = f"{state['edition']}-{r}-{m}"
    return _build_with_retries(state, key, build_one)


def prune_excluded(state):
    """Drop excluded_seeds of matches/heats already published: they are never
    built again, so their lists only grew the state file."""
    done = set()
    for e in state.get("log", []):
        if e.get("status") != "published":
            continue
        done.add(f"royale-{e['heat']}" if e.get("series") == "royale"
                 else f"{e.get('edition')}-{e.get('r')}-{e.get('m')}")
    ex = state.get("excluded_seeds", {})
    for k in [k for k in ex if k in done]:
        del ex[k]


def _build(build, state, tried, tries):
    if tries:
        return build(state, exclude_seeds=tried, tries=tries)
    return build(state, exclude_seeds=tried)


def _build_with_retries(state, key, build, wide_tries=WIDE_TRIES_CUP):
    """New seeds until one passes (up to MAX_ATTEMPTS, while RUN_BUDGET_S lasts).
    Failing seeds are saved in state["excluded_seeds"] so the next run tries NEW
    seeds: the base seed is deterministic, so without this every 3-hourly run
    retried the same failing seeds forever. If the director runs out of
    acceptable seeds, it searches wider (wide_tries) once before giving up."""
    tried = list(state.get("excluded_seeds", {}).get(key, []))
    global _SLOWEST
    reasons, keys = [], []
    tries = None
    for attempt in range(MAX_ATTEMPTS):
        # only start a render that can finish inside the run's budget
        if time.monotonic() - _RUN_START + _SLOWEST > RUN_BUDGET_S:
            reasons.append("out of time for this run - the next run retries this slot")
            break
        t0 = time.monotonic()
        v, err = None, None
        for t in ([tries] if tries else [None, wide_tries]):
            try:
                v, tries = _build(build, state, tried, t), t
                break
            except RuntimeError as exc:          # director found no acceptable seed
                err = exc
                if t is None:
                    print(f"[quality] {exc} - searching wider ({wide_tries} seeds)")
        if v is None:
            reasons.append(f"no usable simulation: {err}")
            print("[quality] FAILED", reasons[-1])
            break
        _SLOWEST = max(_SLOWEST, time.monotonic() - t0)
        if not v["failures"]:
            return v, reasons
        if v.get("caption_only"):
            # The words failed every re-roll; a new seed wouldn't change them, and the
            # seed itself is fine, so it stays usable.
            reasons.append("; ".join(v["failures"]))
            print("[quality] FAILED", reasons[-1])
            break
        tried.append(v["seed"])
        state.setdefault("excluded_seeds", {}).setdefault(key, []).append(v["seed"])
        reasons.append(f"seed {v['seed']}: " + "; ".join(v["failures"]))
        print("[quality] FAILED", reasons[-1])
        # A broken tool or secret fails every seed the same way: stop burning minutes.
        keys.append(upload_gate.failure_key(v["failures"]))
        if upload_gate.systemic(keys):
            reasons.append(SYSTEMIC_STOP)
            print("[quality]", SYSTEMIC_STOP)
            break
        if attempt + 1 < MAX_ATTEMPTS:
            print(f"[retry] render {attempt + 2} with a new seed")
    return None, reasons


# ----------------------------------------------------------- A/B: royale --
# Approved 2026-10-08 ("build it and A/B test"). 19 one-v-one videos all sat at
# ~25% average watch (6s) and ~0.3% likes, and the v2 ring redesign did not move it.
# Diagnosis: two random countries give a 91%-UK audience no stake. The closest
# growing competitor shows 8+ flags with "COMMENT YOUR COUNTRY". Each day one slot
# is a cup match and the other an Escape Royale heat (16 countries, England plus a
# home nation in every heat); the arms swap time slots daily so neither owns the
# better hour. Judge on avg % watched, then likes and comments per view, after 6+
# posts per arm (analytics.py --compare).
ARMS = ("cup", "royale")
ROYALE_SIZE = 16
ROYALE_POINTS = (3, 2, 1)                 # podium points for the running league table

# The middle of the royale first line: "<countries>? <head> · heat N". Kept to 50
# chars or fewer so the longest country list still fits in 110 (2026-10-23).
ROYALE_HEADS = [
    "16 countries, one way out",
    "Country ball escape: 16 flags, one exit",
    "Ball escape simulation, only one breaks out first",
    "16 country balls, 28 rings. Find your country",
    "Physics decides which flag escapes the rings first",
]
ROYALE_TEASES = ["Is your country in this one? 👇", "Which flag were you backing? 👇",
                 "Where did your country finish? 👇"]


def _slot_parity(slot, state):
    """0 or 1 for a posting slot (UTC); flips between a day's slots and swaps daily."""
    local = slot.astimezone(LONDON)
    day = local.date()
    slots = slots_for_day(day, first_post_day(state, day))
    idx = slots.index((local.hour, local.minute)) if (local.hour, local.minute) in slots else 0
    return (day.toordinal() + idx) % 2


def arm_for(slot, state, loop_vals=None):
    """'cup' or 'royale' for a posting slot (UTC). Once the experiment loop has
    judged the format test, its "format" value decides every slot."""
    fmt = (loop_vals or {}).get("format")
    if fmt in ARMS:
        return fmt
    return ARMS[_slot_parity(slot, state)]


# ------------------------------------------------- nightly experiment loop --
# growth-lab, 2026-10-16. The loop (loop_nightly.py, in analytics.yml) judges
# the running test and promotes/reverts it in loop_config.json. Arms follow
# the same slot alternation as cup vs royale (control = parity 0), so neither
# arm owns the better hour. Allowed levers and values: levers.json only.
LOOP_DEFAULTS = {"hook": render.HOOK_TEXT, "max_s": director.MAX_SECONDS,
                 "first_break": director.MAX_FIRST_BREAK, "wait": director.MAX_WAIT,
                 "royale": dict(director.ROYALE_RULES)}
PACING_FAST = {"first_break": 1.2, "wait": 4.5, "royale_first_break": 1.0, "royale_wait": 4.5}
LENGTH_SHORT = {"max_s": 20.0, "royale_max_s": 26.0}


def loop_for(slot, state):
    """(lever values, {experiment_id: arm}) for a posting slot. Never raises."""
    try:
        import experiment_loop
        led, _, cfg = experiment_loop.load_repo(ROOT)
        arm = ("control", "variant")[_slot_parity(slot, state)]
        return experiment_loop.values_for_post(led, cfg, force_arm=arm)
    except Exception as e:
        print(f"[loop] off ({e})")
        return {}, {}


def apply_loop(vals):
    """Set the render/director knobs for one video, starting from the defaults
    every time (control and variant alternate within one run)."""
    d = LOOP_DEFAULTS
    royale = dict(d["royale"])
    render.HOOK_TEXT = vals.get("hook_text") or d["hook"]
    director.MAX_SECONDS, director.MAX_FIRST_BREAK, director.MAX_WAIT = d["max_s"], d["first_break"], d["wait"]
    if vals.get("pacing") == "fast":
        director.MAX_FIRST_BREAK, director.MAX_WAIT = PACING_FAST["first_break"], PACING_FAST["wait"]
        royale.update(first_break=PACING_FAST["royale_first_break"], wait=PACING_FAST["royale_wait"])
    if vals.get("length") == "short":
        director.MAX_SECONDS = LENGTH_SHORT["max_s"]
        royale["max_s"] = LENGTH_SHORT["royale_max_s"]
    director.ROYALE_RULES = royale


def royale_heat(state):
    return 1 + sum(1 for e in state.get("log", []) if e.get("series") == "royale")


def royale_field(heat):
    """England + one home nation + 14 others (big fanbases weighted x3), seeded per heat."""
    rng = random.Random(f"royale-{heat}")
    picks = ["gb-eng", rng.choice(teams.HOME_NATIONS)]
    pool = [c for c, _, _ in teams.ALL_TEAMS
            if c not in picks and c not in teams.HOME_NATIONS and c not in quality.EXCLUDED_CODES]
    weights = [3 if c in teams.BIG_FANBASES else 1 for c in pool]
    while len(picks) < ROYALE_SIZE:
        c = rng.choices(pool, weights)[0]
        if c not in picks:
            picks.append(c)
    rng.shuffle(picks)
    return picks


def royale_league(state, extra_podium=()):
    """Top three of the running league (3/2/1 points per heat podium)."""
    pts = {}
    podiums = [e.get("podium", []) for e in state.get("log", []) if e.get("series") == "royale"]
    for podium in podiums + [list(extra_podium)]:
        for code, p in zip(podium, ROYALE_POINTS):
            pts[code] = pts.get(code, 0) + p
    top = sorted(pts.items(), key=lambda kv: -kv[1])[:3]
    return "LEAGUE: " + "  ·  ".join(f"{teams.name(c).upper()} {p}" for c, p in top)


def royale_theme(state):
    """Theme for the next royale. Was themes[state["posts"] + 3]: "posts" counts cup
    matches only, so royales with no cup between them (the day boundary at 10 slots,
    or every slot once A1 picks royale) all got the same look - royale008/009/010
    were all "candy" (audit 2026-10-21, MASTER_PLAN #11). Now from len(state["log"])
    (every post), and never the theme of the previous royale or the post just before."""
    themes = [t for t in render.THEMES if t != render.FINAL_THEME]
    log = state.get("log", [])
    avoid = {log[-1].get("theme")} if log else set()
    avoid.add(next((e.get("theme") for e in reversed(log) if e.get("series") == "royale"), None))
    start = len(log) + len(themes) // 2          # offset from the cup's rotation
    for k in range(len(themes)):
        t = themes[(start + k) % len(themes)]
        if t not in avoid:
            return t
    return themes[start % len(themes)]


# Per-match hook (audit 2026-10-21, plan-escape #4): a hook_text value may name the
# countries, e.g. "{a} vs {b}: who escapes first?" (royale: "{home}"). Built but OFF: it
# goes through the experiment loop only after the length lever, never random rotation.
# To switch on: set True AND add the template as a hook_text variant in levers.json.
HOOK_TEMPLATE_LEVER = False


def match_hook(template, a=None, b=None, home=None):
    """Fill {a}/{b}/{home} with country names. A template whose countries aren't
    known is returned unchanged (never raises)."""
    names = {k: teams.name(v) for k, v in (("a", a), ("b", b), ("home", home)) if v}
    try:
        return template.format(**names)
    except (KeyError, IndexError, ValueError):
        return template


def hook_for_match(a=None, b=None, home=None):
    """The on-screen hook for one video: the loop's hook_text, filled in only when
    HOOK_TEMPLATE_LEVER is on."""
    if not HOOK_TEMPLATE_LEVER:
        return render.HOOK_TEXT
    return match_hook(render.HOOK_TEXT, a, b, home)


def _list_names(codes):
    names = [teams.name(c) for c in codes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def make_royale_caption(codes, heat, rng, recent_heads=()):
    """First line: the countries first (search keywords + the stake), then a phrasing,
    then "· heat N" at the end, so it still differs from every earlier heat's (with only
    the 5 bare ROYALE_HEADS all were "recent" by 2026-10-19 and the repeat rule blocked
    every royale render). Countries-first since 2026-10-23 (audit MASTER_PLAN #11; was
    "Escape Royale heat N: ..."). A re-render of the same heat (after a Buffer veto)
    takes a phrasing that isn't recent yet."""
    home = next(c for c in codes if c in teams.HOME_NATIONS)
    named = ["gb-eng", home] + [c for c in codes if c in teams.BIG_FANBASES and c != "gb-eng"][:2]
    others = [c for c in codes if c not in named][:2]
    second = ("Also in it: " + ", ".join(teams.name(c) for c in others)
              + f" and {len(codes) - len(named) - len(others)} more")
    heads = list(ROYALE_HEADS)
    rng.shuffle(heads)
    recent = {upload_gate._norm_space(h) for h in recent_heads if h}
    lead = _list_names(named) + "?"
    firsts = [f"{lead} {h} · heat {heat}" for h in heads]
    firsts = [f for f in firsts if len(f) <= 110] or [f"{lead} 16 countries, one way out · heat {heat}"]
    first = next((f for f in firsts if upload_gate._norm_space(f) not in recent), firsts[0])
    tags = NICHE_TAGS + [rng.choice(ROTATING_TAG), "#countryballs"]
    return f"{first}\n{second}\n{rng.choice(ROYALE_TEASES)}\n{' '.join(tags)}"


def build_royale(state, exclude_seeds=(), tries=None):
    heat = royale_heat(state)
    codes = royale_field(heat)
    base = int(hashlib.sha1(f"royale-{heat}".encode()).hexdigest()[:7], 16)
    seed, drama = director.pick_royale(base, exclude=exclude_seeds, **({"tries": tries} if tries else {}))
    phys = sim.simulate(seed, **sim.ROYALE)
    winner = codes[phys.winner]
    podium = [codes[i] for i in sim.standings(phys)[:3]]
    theme = royale_theme(state)
    info = dict(codes=codes, header=f"ESCAPE ROYALE  ·  HEAT {heat}", theme=theme,
                hook=hook_for_match(home=next(c for c in codes if c in teams.HOME_NATIONS)),
                league_line=royale_league(state, podium), next_line="NEW HEAT, 16 NEW COUNTRIES NEXT")
    fname = f"royale{heat:03d}_{seed}.mp4"
    heads = _recent_heads(state)
    caption, cap_fail = _pick_caption(
        lambda g: make_royale_caption(codes, heat, g, heads), seed,
        lambda c: _caption_failures(c, state, None, None))
    if cap_fail:
        return _caption_only(caption, cap_fail, seed=seed, file=fname, winner=winner,
                             series="royale", heat=heat, codes=codes, podium=podium,
                             round=f"Escape Royale heat {heat}", theme=theme, drama=drama)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    path = os.path.join(OUT, fname)
    render.render_royale(phys, info, path, WORK)

    report, failures = quality.check_video(path, len(phys.frames) / sim.FPS,
                                           quality.ROYALE_DURATION_RANGE)
    failures += quality.check_royale(codes, winner, phys.breaks)
    verdict = gate(path, caption, state, quality.ROYALE_DURATION_RANGE) if not failures else None
    if verdict:
        failures += verdict["failures"]
    return {"path": path, "caption": caption, "file": fname, "seed": seed, "winner": winner,
            "series": "royale", "heat": heat, "codes": codes, "podium": podium,
            "round": f"Escape Royale heat {heat}", "theme": theme, "drama": drama,
            "report": report, "failures": failures, "gate": verdict}


def build_passing_royale(state):
    return _build_with_retries(state, f"royale-{royale_heat(state)}", build_royale,
                               wide_tries=WIDE_TRIES_ROYALE)


def _app(kind, severity, title, body="", **data):
    """Also tell Bot HQ (the phone app). Telegram alerts are unchanged."""
    try:
        short = " ".join(str(title).split())
        app_events.emit("escape", kind, severity, short if len(short) <= 80 else short[:79] + "…",
                        body, data=data or None)
    except Exception as e:
        print(f"[BotHQ] emit failed: {e}")


def label(e):
    """Human name of a log entry, for alerts."""
    if e.get("series") == "royale":
        return f"Escape Royale heat {e['heat']}"
    return f"{teams.name(e['a'])} v {teams.name(e['b'])}"


def qc_line(rep):
    return (f"{rep['res']} {rep['fps']:.0f}fps · {rep['seconds']}s · {rep['mbps']} Mbps · "
            f"{rep['lufs']} LUFS · {rep['hf_pct']}% >2kHz · no black/frozen frames · hook ✓ · card ✓")


# ------------------------------------------------------------------- runs --

NOT_FOUND = ("not found", "notfound", "does not exist", "doesn't exist", "no post", "deleted")


def _buffer_status(post_id, pending_ids):
    """'scheduled' | 'sending' | 'sent' | 'error' | 'missing' for one of our Buffer posts."""
    if post_id in pending_ids:
        return "scheduled", None
    try:
        post = publish.get_post(post_id)
    except publish.PublishError as e:
        if any(s in str(e).lower() for s in NOT_FOUND):
            return "missing", None
        raise                                    # unknown API trouble: never roll back on a guess
    if not post:
        return "missing", None
    return post["status"], post


def reconcile(state, org, channel):
    """Make the log match what really happened in Buffer/TikTok.

    - published posts are marked published (checked once, then never again),
    - if a scheduled video will never publish (you deleted it in Buffer, or
      TikTok rejected it), that match and every LATER unpublished match are
      pulled out of Buffer and out of the log; the rebuilt bracket then replays
      them in order, so the published story never skips a match. The vetoed or
      rejected match gets a different simulation next time.
    Returns the (possibly rebuilt) state.
    """
    log = state.get("log", [])
    pending = publish.pending_posts(org, channel)
    pending_ids = {p["id"] for p in pending}
    # A scheduled Buffer post the log doesn't know means a run was killed between
    # scheduling and saving. Alert once; never delete it automatically.
    known = {e.get("buffer_id") for e in log}
    alerted = state.setdefault("orphans_alerted", [])
    for p in pending:
        if p["id"] not in known and p["id"] not in alerted:
            due = (p.get("dueAt") or "?")[:16].replace("T", " ")
            _app("orphan_post", "action", "Escape Cup: a Buffer post isn't in the bracket log",
                 f"Due {due} UTC. A run was probably killed mid-post. Delete it in Buffer if it "
                 "duplicates a match.", buffer_id=p["id"])
            notify.send("⚠️ <b>Escape Cup: a scheduled Buffer post isn't in the bracket log</b> "
                        f"(due {html.escape(due)} UTC). A run was probably killed mid-post. "
                        "Delete it in Buffer if it duplicates a match.")
            alerted.append(p["id"])
    # First learn the status of EVERY entry (a later match may already be live,
    # which decides whether rewinding is allowed), then act on the earliest drop.
    drop_at = None
    for i, e in enumerate(log):
        if e.get("status") in ("published", "lost") or not e.get("buffer_id"):
            continue
        status, post = _buffer_status(e["buffer_id"], pending_ids)
        if status == "sent":
            e["status"] = "published"
            e["link"] = (post or {}).get("externalLink")
        elif status in ("error", "missing"):
            e["drop_reason"] = "deleted in Buffer" if status == "missing" else \
                ((post or {}).get("error") or {}).get("message", "TikTok/Buffer error")
            if drop_at is None:
                drop_at = i
    if drop_at is None:
        return state

    dropped = log[drop_at]
    later_published = [e for e in log[drop_at + 1:] if e.get("status") == "published"]
    title = label(dropped)
    if later_published:
        # A later match already went out, so the bracket can't be rewound. Keep
        # the result; mark it so it is only alerted once.
        dropped["status"] = "lost"
        _app("post_lost", "action", f"Escape Cup: {title} never reached TikTok",
             f"Reason: {dropped['drop_reason']}. Later matches were already published, so the "
             "bracket keeps its result. Check Buffer/TikTok for what went wrong.",
             buffer_id=dropped.get("buffer_id"))
        notify.send(f"⚠️ <b>Escape Cup: {html.escape(title)} never reached TikTok</b> "
                    f"({html.escape(dropped['drop_reason'])}), and later matches were already "
                    f"published, so the bracket keeps its result.")
        return state

    # cancel every later still-scheduled post so the replay stays in order
    cancelled = []
    for e in log[drop_at + 1:]:
        if e.get("buffer_id") in pending_ids:
            publish.delete_post(e["buffer_id"])
            cancelled.append(label(e))
    key = (f"royale-{dropped['heat']}" if dropped.get("series") == "royale"
           else f"{dropped['edition']}-{dropped['r']}-{dropped['m']}")
    state.setdefault("excluded_seeds", {}).setdefault(key, []).append(dropped["seed"])
    state = tournament.truncate(state, drop_at)
    _app("post_rewound", "warn", f"Escape Cup: {title} was not published",
         f"Reason: {dropped['drop_reason']}. It will be re-simulated and scheduled again in order"
         + (f"; also re-queued: {', '.join(cancelled)}." if cancelled else ". Nothing to do."))
    notify.send(f"↩️ <b>Escape Cup: {html.escape(title)} was not published</b> "
                f"({html.escape(dropped['drop_reason'])}).\nIt will be re-simulated and scheduled again "
                f"in order" + (f"; also re-queued: {html.escape(', '.join(cancelled))}." if cancelled else "."))
    return state


def requeue_unpublished():
    """After a format change: cancel every still-scheduled post and re-render those
    matches with the current code, in the same bracket order. Published matches
    are untouched; seeds are NOT excluded (nothing was wrong with those matches)."""
    state = tournament.load()
    org, channel = publish.tiktok_channel()
    state = reconcile(state, org, channel)
    log = state.get("log", [])
    keep = 0
    while keep < len(log) and log[keep].get("status") in ("published", "lost"):
        keep += 1
    pending_ids = {p["id"] for p in publish.pending_posts(org, channel)}
    cancelled = []
    for e in log[keep:]:
        if e.get("buffer_id") in pending_ids:
            publish.delete_post(e["buffer_id"])
            cancelled.append(label(e))
    state = tournament.truncate(state, keep)
    tournament.save(state)
    print(f"[requeue] cancelled {cancelled}; log kept {keep} published entries")
    run()
    if cancelled:
        notify.send(f"🔁 <b>Escape Cup requeued</b> {len(cancelled)} unpublished video(s) in the new format: "
                    f"{html.escape(', '.join(cancelled))}")


def reset_unpublished():
    """Cancel our still-scheduled Buffer posts; if nothing has been published yet,
    redraw the cup from scratch (used when the team list changed before launch)."""
    state = tournament.load()
    org, channel = publish.tiktok_channel()
    ours = {e["buffer_id"] for e in state.get("log", []) if e.get("buffer_id")}
    pending = publish.pending_posts(org, channel)
    cancelled = [p["id"] for p in pending if p["id"] in ours]
    for pid in cancelled:
        publish.delete_post(pid)
    now = datetime.now(timezone.utc)
    published = [e for e in state.get("log", [])
                 if e.get("status") == "published" or (e.get("due") and datetime.fromisoformat(e["due"]) <= now)]
    if published:
        raise RuntimeError(f"{len(published)} match(es) already published - not redrawing")
    tournament.save(tournament.rebuild([]))
    notify.send(f"♻️ <b>Escape Cup reset</b>: cancelled {len(cancelled)} scheduled post(s) in Buffer "
                f"and redrew Escape Cup #1. Nothing had been published.")
    print(f"[reset] cancelled {cancelled}, redrew edition 1")


def post_now():
    """Publish our earliest pending Buffer post immediately (same checked video and
    caption) and wait for TikTok to confirm it - an end-to-end test of auto-posting."""
    state = tournament.load()
    org, channel = publish.tiktok_channel()
    pending = {p["id"]: p for p in publish.pending_posts(org, channel)}
    idx = next((i for i, e in enumerate(state.get("log", [])) if e.get("buffer_id") in pending), None)
    if idx is None:
        raise RuntimeError("no pending Escape Cup post in Buffer to publish")
    e = state["log"][idx]
    if not (e.get("gate") or {}).get("ok"):
        raise RuntimeError("that post has no upload-gate approval on record - not publishing it early")
    target = pending[e["buffer_id"]]
    fname = e.get("file") or f"ec{e['edition']:02d}_r{e['r']}_m{e['m']:02d}_{e['seed']}.mp4"
    url = f"{publish.pages_base()}/v/{fname}"
    publish.delete_post(target["id"])
    post = publish.schedule_tiktok(channel, url, target["text"], datetime.now(timezone.utc))
    e["due"], e["buffer_id"] = datetime.now(timezone.utc).isoformat(), post["id"]
    tournament.save(state)
    final = publish.wait_until_published(post["id"])
    title = label(e)
    if final["status"] == "sent":
        e["status"], e["link"] = "published", final.get("externalLink")
        tournament.save(state)
        notify.send(f"✅ <b>Escape Cup post is LIVE on TikTok</b>\n{html.escape(title)}\n"
                    f"{final.get('externalLink') or '(no link returned)'}\n"
                    f"Posted automatically by Buffer - no action needed.")
    else:
        err = (final.get("error") or {})
        notify.send(f"⚠️ <b>Escape Cup post-now: {final['status']}</b>\n{html.escape(title)}\n"
                    f"{html.escape(str(err.get('message') or ''))}\n"
                    f"{html.escape(str(err.get('rawError') or ''))[:500]}")
        raise RuntimeError(f"post ended as {final['status']}: {err}")


def run(sample=False, local=False, royale=False):
    global GATE_LEDGER
    state = tournament.load()
    if sample:
        GATE_LEDGER = None             # samples are never posted; keep them out of the audit log
        # LOOP_FORCE_VALUES (JSON): preview a queued lever value before the loop uses it
        apply_loop(json.loads(os.environ.get("LOOP_FORCE_VALUES") or "{}"))
        work = copy.deepcopy(state)
        v = build_royale(work) if royale else build_one(work)
        print(json.dumps({k: v.get(k) for k in ("file", "round", "a", "b", "codes", "winner", "theme",
                                                "drama", "report", "failures")}, indent=1))
        print(v["caption"])
        if not local:
            verdict = ("✅ passed quality gate\n" + qc_line(v["report"]) + "\n"
                       + upload_gate.summary(v["gate"])) if not v["failures"] else \
                ("❌ FAILED quality gate / upload gate:\n" + html.escape("; ".join(v["failures"]))[:800])
            notify.send_video(v["path"], f"🧪 <b>Escape Cup sample</b> (not posted)\n{verdict}\n\n"
                                         + html.escape(v["caption"]))
        return

    org, channel = publish.tiktok_channel()
    state = reconcile(state, org, channel)
    prune_excluded(state)
    tournament.save(state)
    pending = publish.pending_posts(org, channel)
    taken = [datetime.fromisoformat(p["dueAt"].replace("Z", "+00:00")) for p in pending if p.get("dueAt")]
    slots = upcoming_slots(datetime.now(timezone.utc), taken, state)
    if not slots:
        print(f"[main] nothing to schedule ({len(pending)} already pending)")
        return

    for slot in slots:
        loop_vals, loop_arms = loop_for(slot, state)
        apply_loop(loop_vals)
        if loop_vals or loop_arms:
            print(f"[loop] arms {loop_arms} values {loop_vals}")
        arm = arm_for(slot, state, loop_vals)
        v, reasons = build_passing_royale(state) if arm == "royale" else build_passing(state)
        when = slot.astimezone(LONDON).strftime("%a %d %b %H:%M UK")
        if v is None:
            # Stop here: filling a LATER slot with this match's successor would
            # break the order. The next run retries this match first, with new
            # seeds (failed ones are saved). (A failed royale heat stops too, so
            # the arms never drift out of their slots.)
            tournament.save(state)
            _app("slot_missed", "warn", f"Escape Cup: {when} slot not filled yet",
                 f"The next run (within 3h) retries it with new seeds. {len(reasons)} render(s) failed "
                 "the quality gate / upload gate:\n" + "\n".join(reasons)[:3000])
            notify.send(f"🛑 <b>Escape Cup: {when} not filled yet</b> - the next run "
                        f"(within 3h) retries it with new seeds.\n"
                        f"{len(reasons)} render(s) failed the quality gate / upload gate:\n"
                        + html.escape("\n".join(reasons))[:3000])
            break
        protect = {e.get("file") for e in state.get("log", []) if e.get("status") == "scheduled"}
        url = publish.host_video(v["path"], v["file"], protect=protect)
        post = publish.schedule_tiktok(channel, url, v["caption"], slot)
        try:
            state["recent_captions"] = (state.get("recent_captions", []) + [v["caption"]])[-40:]
            entry = {"edition": state["edition"], "winner": v["winner"], "seed": v["seed"],
                     "theme": v["theme"], "due": slot.isoformat(), "buffer_id": post["id"],
                     "status": "scheduled", "file": v["file"],
                     # length was never stored before 2026-10-13, so avg % watched was
                     # blank for every post after the hand-typed backfill
                     "length": v["report"].get("seconds"),
                     "loop": loop_arms, "gate": _gate_record(v)}
            if v.get("series") == "royale":
                entry.update(series="royale", heat=v["heat"], codes=v["codes"], podium=v["podium"])
            else:
                entry.update(r=v["r"], m=v["m"], a=v["a"], b=v["b"])
            state = tournament.append(state, entry)
            tournament.save(state)
        except Exception:
            # A Buffer post the log doesn't know about would be posted twice later.
            publish.delete_post(post["id"])
            raise
        notify.send_video(v["path"], f"🎾 <b>Escape Cup scheduled</b> for {when}\n"
                                     f"{html.escape(v['round'])}: winner {html.escape(teams.name(v['winner']))} "
                                     f"(don't spoil it 🤫)\n✅ {qc_line(v['report'])}\n"
                                     f"{upload_gate.summary(v['gate'])}\n"
                                     f"Don't like it? Delete it in Buffer before then - the bracket "
                                     f"rewinds and it gets re-simulated.\n\n{html.escape(v['caption'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="render the next match, don't post")
    ap.add_argument("--local", action="store_true", help="with --sample: no Telegram")
    ap.add_argument("--royale", action="store_true", help="with --sample: an Escape Royale heat")
    ap.add_argument("--post-now", action="store_true",
                    help="publish the earliest pending post immediately and wait for TikTok")
    ap.add_argument("--requeue", action="store_true",
                    help="re-render every unpublished scheduled match with the current code")
    ap.add_argument("--reset-unpublished", action="store_true",
                    help="cancel our pending Buffer posts and redraw if nothing is published")
    args = ap.parse_args()
    try:
        if args.reset_unpublished:
            reset_unpublished()
        elif args.post_now:
            post_now()
        elif args.requeue:
            requeue_unpublished()
        else:
            run(sample=args.sample, local=args.local, royale=args.royale)
    except Exception as e:
        traceback.print_exc()
        if not args.local:
            notify.send(f"❌ <b>Escape Cup failed</b>\n<code>{html.escape(str(e))[:900]}</code>\n"
                        f"Check GitHub Actions for the log.")
        sys.exit(1)


if __name__ == "__main__":
    main()
