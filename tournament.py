"""
Escape Cup bracket: 32 nations, single elimination, one match per video.

The single source of truth is state["log"]: every match that has been scheduled,
in posting order. The bracket (who plays whom, who advanced, titles, which cup
we are on) is ALWAYS rebuilt by replaying the log from Escape Cup 1, so:
  - winners go into the next round exactly as the videos showed,
  - after a Final the next cup starts with a fresh draw,
  - if a scheduled video never makes it to TikTok (deleted in Buffer as a veto,
    or rejected), main.reconcile() drops that entry and every later unpublished
    one, and the rebuilt bracket replays the match again - so the published
    story never skips a match or shows a team that didn't earn its place.

Log entry: {"edition", "r", "m", "a", "b", "winner", "seed", "theme",
            "due" (iso UTC), "buffer_id", "status": scheduled|published}
Other state: recent_captions, seen_errors, excluded_seeds {"e-r-m": [seeds]}.
"""
import json
import os
import random

import teams

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "tournament_state.json")
ROUND_NAMES = {16: "Round of 32", 8: "Round of 16", 4: "Quarter-final", 2: "Semi-final", 1: "The Final"}
KEEP_KEYS = ("log", "recent_captions", "seen_errors", "excluded_seeds")


def _empty_match(a=None, b=None):
    return {"a": a, "b": b, "winner": None, "seed": None, "melody": None,
            "posted": None, "buffer_id": None}


def new_edition(edition, titles=None, posts=0):
    """A fresh draw. Deterministic per edition number, different every cup."""
    rng = random.Random(f"escape-cup-{edition}")
    codes = [c for c, _, _ in teams.TEAMS]
    rng.shuffle(codes)
    first = [_empty_match(codes[i], codes[i + 1]) for i in range(0, 32, 2)]
    rounds = [first] + [[_empty_match() for _ in range(n)] for n in (8, 4, 2, 1)]
    return {"edition": edition, "rounds": rounds, "titles": dict(titles or {}), "posts": posts}


def next_match(state):
    """(round_index, match_index) of the next unplayed match, or None if the cup is over."""
    for r, rnd in enumerate(state["rounds"]):
        for m, match in enumerate(rnd):
            if match["winner"] is None and match["a"] and match["b"]:
                return r, m
    return None


def describe(state, r, m):
    size = len(state["rounds"][r])
    name = ROUND_NAMES[size]
    if size == 1:
        header = f"ESCAPE CUP #{state['edition']}  ·  THE FINAL"
    else:
        header = f"ESCAPE CUP #{state['edition']}  ·  {name.upper()}  ·  MATCH {m + 1} OF {size}"
    return {"round_name": name, "size": size, "header": header}


def _apply(state, r, m, winner, seed, melody):
    match = state["rounds"][r][m]
    if winner not in (match["a"], match["b"]):
        raise ValueError(f"winner {winner} not in {match['a']} v {match['b']}")
    match["winner"], match["seed"], match["melody"] = winner, seed, melody
    state["posts"] = state.get("posts", 0) + 1
    if r + 1 < len(state["rounds"]):
        state["rounds"][r + 1][m // 2]["a" if m % 2 == 0 else "b"] = winner
    else:
        state["titles"][winner] = state["titles"].get(winner, 0) + 1


def roll_over_if_done(state):
    """After the Final, start the next cup (keeping titles and the log)."""
    if next_match(state) is None:
        nxt = new_edition(state["edition"] + 1, state["titles"], state.get("posts", 0))
        for k in KEEP_KEYS:
            if k in state:
                nxt[k] = state[k]
        return nxt
    return state


def rebuild(log, extra=None):
    """Replay the log from Escape Cup 1. Raises if the log is inconsistent."""
    state = new_edition(1)
    for i, e in enumerate(log):
        state = roll_over_if_done(state)
        if state["edition"] != e["edition"] or next_match(state) != (e["r"], e["m"]):
            raise ValueError(f"log entry {i} ({e['edition']}-{e['r']}-{e['m']}) is out of order; "
                             f"expected {state['edition']}-{next_match(state)}")
        match = state["rounds"][e["r"]][e["m"]]
        if (match["a"], match["b"]) != (e["a"], e["b"]):
            raise ValueError(f"log entry {i}: teams {e['a']} v {e['b']} but bracket has "
                             f"{match['a']} v {match['b']}")
        _apply(state, e["r"], e["m"], e["winner"], e["seed"], e.get("theme"))
        match["posted"], match["buffer_id"] = e.get("due"), e.get("buffer_id")
    state = roll_over_if_done(state)
    state["log"] = list(log)
    for k, v in (extra or {}).items():
        state[k] = v
    return state


def append(state, entry):
    """Record a newly scheduled match and return the rebuilt state."""
    extra = {k: state[k] for k in KEEP_KEYS if k in state and k != "log"}
    return rebuild(state.get("log", []) + [entry], extra)


def truncate(state, keep):
    """Drop log entries from index `keep` onward and rebuild."""
    extra = {k: state[k] for k in KEEP_KEYS if k in state and k != "log"}
    return rebuild(state["log"][:keep], extra)


def _migrate(old):
    """Pre-log state files: turn recorded matches into a log, oldest first."""
    entries = []
    for r, rnd in enumerate(old["rounds"]):
        for m, match in enumerate(rnd):
            if match.get("winner"):
                entries.append({"edition": old["edition"], "r": r, "m": m, "a": match["a"], "b": match["b"],
                                "winner": match["winner"], "seed": match["seed"], "theme": match.get("melody"),
                                "due": match.get("posted"), "buffer_id": match.get("buffer_id"),
                                "status": "scheduled"})
    entries.sort(key=lambda e: e["due"] or "")
    extra = {k: old[k] for k in KEEP_KEYS if k in old and k != "log"}
    return rebuild(entries, extra)


def load():
    if not os.path.exists(STATE_PATH):
        return rebuild([])
    with open(STATE_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    if "log" not in raw:
        return _migrate(raw)
    extra = {k: raw[k] for k in KEEP_KEYS if k in raw and k != "log"}
    return rebuild(raw["log"], extra)


def save(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)


def preview_after(state, r, m, winner):
    """What the end card teases, computed as if `winner` had just been recorded."""
    probe = json.loads(json.dumps(state))
    _apply(probe, r, m, winner, 0, "")
    nm = next_match(probe)
    if nm is None:
        return None
    nr, nmi = nm
    match = probe["rounds"][nr][nmi]
    return {"a": match["a"], "b": match["b"], "round_name": ROUND_NAMES[len(probe["rounds"][nr])]}
