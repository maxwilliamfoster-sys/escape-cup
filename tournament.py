"""
Escape Cup bracket: 32 nations, single elimination, one match per video.

State lives in tournament_state.json (committed back by CI):
  edition      - cup number, shown on screen ("ESCAPE CUP #3")
  rounds       - list of rounds; each round a list of matches
                 {"a": code|None, "b": code|None, "winner": code|None, "seed": int|None,
                  "melody": str|None, "posted": iso|None, "buffer_id": str|None}
  titles       - {code: cups won}, for the champion card
  posts        - total videos made (drives melody rotation)
"""
import json
import os
import random

import teams

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "tournament_state.json")
ROUND_NAMES = {16: "Round of 32", 8: "Round of 16", 4: "Quarter-final", 2: "Semi-final", 1: "The Final"}
NEXT_STAGE = {16: "the Round of 16", 8: "the quarter-finals", 4: "the semi-finals", 2: "the Final"}


def _empty_match(a=None, b=None):
    return {"a": a, "b": b, "winner": None, "seed": None, "melody": None,
            "posted": None, "buffer_id": None}


def new_edition(edition, titles=None, posts=0):
    rng = random.Random(f"escape-cup-{edition}")
    codes = [c for c, _, _ in teams.TEAMS]
    rng.shuffle(codes)
    first = [_empty_match(codes[i], codes[i + 1]) for i in range(0, 32, 2)]
    rounds = [first] + [[_empty_match() for _ in range(n)] for n in (8, 4, 2, 1)]
    return {"edition": edition, "rounds": rounds, "titles": titles or {}, "posts": posts}


def load():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return new_edition(1)


def save(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, ensure_ascii=False)


def next_match(state):
    """(round_index, match_index) of the next unplayed match, or None if the cup is over."""
    for r, rnd in enumerate(state["rounds"]):
        for m, match in enumerate(rnd):
            if match["winner"] is None and match["a"] and match["b"]:
                return r, m
    return None


def describe(state, r, m):
    rnd = state["rounds"][r]
    size = len(rnd)
    name = ROUND_NAMES[size]
    if size == 1:
        header = f"ESCAPE CUP #{state['edition']}  ·  THE FINAL"
    else:
        header = f"ESCAPE CUP #{state['edition']}  ·  {name.upper()}  ·  MATCH {m + 1} OF {size}"
    return {"round_name": name, "size": size, "header": header}


def record(state, r, m, winner, seed, melody):
    match = state["rounds"][r][m]
    match["winner"], match["seed"], match["melody"] = winner, seed, melody
    state["posts"] = state.get("posts", 0) + 1
    if r + 1 < len(state["rounds"]):
        nxt = state["rounds"][r + 1][m // 2]
        nxt["a" if m % 2 == 0 else "b"] = winner
    else:
        state["titles"][winner] = state["titles"].get(winner, 0) + 1


def roll_over_if_done(state):
    """After the Final, start the next cup. Returns the (possibly new) state."""
    if next_match(state) is None:
        return new_edition(state["edition"] + 1, state["titles"], state.get("posts", 0))
    return state


def preview_after(state, r, m, winner):
    """What the end card teases, computed as if `winner` had just been recorded."""
    probe = json.loads(json.dumps(state))
    record(probe, r, m, winner, 0, "")
    nm = next_match(probe)
    if nm is None:
        return None
    nr, nmi = nm
    match = probe["rounds"][nr][nmi]
    return {"a": match["a"], "b": match["b"], "round_name": ROUND_NAMES[len(probe["rounds"][nr])]}
