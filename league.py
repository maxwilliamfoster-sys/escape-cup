"""Escape League: the weekly points league for Beat The Keeper (Max, 2026-11-26).

Max: "a point system league ... last about a week so theres multiple matches a day then its
announced which team wins at the end of the week with the most points", posted alternately
with the country cup.

How it works:
  - A season is MATCHDAYS (10) matchdays: the first 10 rounds of a seeded single round
    robin of the 20 clubs, so every club plays 10 different opponents once.
  - Every league post is ONE matchday. All 10 fixtures are simulated with the real physics
    (keeper.simulate, first to 3); one of them, the featured match (the biggest clash on
    the table), is rendered and the other 9 results go into the table shown after it.
  - Win = 3 points (first to 3 always has a winner), table order: points, goal
    difference, goals scored.
  - At 3 posts a day alternating with the cup that is ~1.5 league posts a day, so a season
    lasts about a week. Matchday 10 ends with the CHAMPIONS card; then season 2 starts.

State lives in the shared posting log (entries with series "league", in order), so the
reconcile rewind (a deleted/rejected post re-queues it and everything after) replays the
league exactly like the cup bracket. Pure functions; nothing here touches the network.
"""
import hashlib
import random

import keeper as K

SERIES = "league"
MATCHDAYS = 10
BIG_CLUBS = ("ARS", "CHE", "LIV", "MCI", "MUN", "TOT", "NEW")     # biggest TikTok fanbases
SIM_TRIES = 25                     # seeds per off-screen fixture before giving up (timeouts are rare)


def entries(log):
    return [e for e in log if e.get("series") == SERIES]


def position(log):
    """(season, matchday) of the NEXT league post."""
    n = len(entries(log))
    return 1 + n // MATCHDAYS, 1 + n % MATCHDAYS


def season_fixtures(season):
    """MATCHDAYS rounds of 10 (home, away) pairs; nobody meets the same club twice."""
    return K.fixtures(seed=f"escape-league-{season}")[:MATCHDAYS]


def season_results(log, season, extra=()):
    """[(home, away, hg, ag)] of every match played so far this season (+ extra)."""
    out = []
    for e in entries(log):
        if e.get("season") == season:
            out += [tuple(r) for r in e.get("results", [])]
    return out + [tuple(r) for r in extra]


def standings(results):
    """All 20 clubs (clubs yet to play show zeros), sorted like keeper.table."""
    rows = {r["code"]: r for r in K.table(results)}
    for code, *_ in K.CLUBS:
        rows.setdefault(code, {"code": code, "p": 0, "w": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0})
    return sorted(rows.values(), key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"], r["code"]))


def featured(season, md, results):
    """The matchday's headline fixture: most combined points, then big clubs, then a seeded
    coin toss (deterministic, so the previous post's NEXT MATCH tease is always right)."""
    pts = {r["code"]: r["pts"] for r in standings(results)}
    rng = random.Random(f"league-feature-{season}-{md}")
    pairs = season_fixtures(season)[md - 1]
    keyed = [(pts[h] + pts[a], (h in BIG_CLUBS) + (a in BIG_CLUBS), rng.random(), (h, a)) for h, a in pairs]
    return max(keyed)[3]


def _seed(*parts):
    return int(hashlib.sha1("-".join(map(str, parts)).encode()).hexdigest()[:7], 16)


def base_seed(season, md):
    """keeper.pick base for the featured match (its seeds are base*1000+k)."""
    return _seed("league", season, md)


def play_rest(season, md, skip):
    """Real (unrendered) results of the matchday's other fixtures: [(h, a, hg, ag)]."""
    out = []
    for h, a in season_fixtures(season)[md - 1]:
        if (h, a) == tuple(skip):
            continue
        for k in range(SIM_TRIES):
            m = K.simulate(_seed("league-fx", season, md, h, a, k), h, a, record=False)
            if m.winner >= 0:
                out.append((h, a, m.goals[0], m.goals[1]))
                break
        else:
            raise RuntimeError(f"league: {h} v {a} timed out {SIM_TRIES} times")
    return out


def matchday(log, featured_result):
    """Everything a league post needs once its featured match is simulated.
    featured_result: (home, away, hg, ag). Returns dict with season, md, results (all 10,
    featured first), table (after this matchday), champion (code on the last matchday,
    else None) and next_fixture (the next post's featured pair: next matchday, or the new
    season's first)."""
    season, md = position(log)
    h, a = featured_result[:2]
    results = [tuple(featured_result)] + play_rest(season, md, (h, a))
    played = season_results(log, season, results)
    table = standings(played)
    last = md == MATCHDAYS
    if last:
        nxt = featured(season + 1, 1, [])
    else:
        nxt = featured(season, md + 1, played)
    return {"season": season, "md": md, "results": [list(r) for r in results], "table": table,
            "champion": table[0]["code"] if last else None, "next_fixture": nxt}


def champions(log):
    """[(season, club code, points)] of every finished season."""
    out = []
    by_season = {}
    for e in entries(log):
        by_season.setdefault(e["season"], []).append(e)
    for season, es in sorted(by_season.items()):
        if len(es) == MATCHDAYS:
            top = standings(season_results(log, season))[0]
            out.append((season, top["code"], top["pts"]))
    return out


def hashtag(code):
    return "#" + "".join(ch for ch in K.name(code).lower() if ch.isalnum())
