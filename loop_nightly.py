"""Nightly experiment loop for Escape Cup (growth-lab, 2026-10-16).

Runs in analytics.yml straight after the Buffer collect:

  1. Score every post at 72h (analytics.at_age: avg % watched, views) and map it
     to its arms: the "loop" field of each post, and for the cup-vs-royale test
     (started by hand on 10-13) the post's own "variant".
  2. experiment_loop.evaluate(): judge the running test (growth_stats, min 10 /
     max 20 per arm), PROMOTE winners / REVERT losers into loop_config.json, start
     at most one new test from levers.json.
  3. Write loop_summary.json; the combined morning Telegram digest is sent from
     the chess repo, which reads this file and the ledger from GitHub.

Writes ONLY experiments.json, loop_config.json and loop_summary.json.

    python loop_nightly.py              # real run (CI)
    python loop_nightly.py --dry-run    # decide and print; write nothing
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import analytics
import experiment_loop as el

HERE = os.path.dirname(os.path.abspath(__file__))


def collect(ledger, now=None):
    """(posts, summary). Raises when there is no analytics data at all."""
    now = now or datetime.now(timezone.utc)
    data = analytics.load()
    recs = data.get("posts") or {}
    if not recs:
        raise RuntimeError("analytics.json has no posts")
    fmt_tests = [e for e in ledger.get("experiments", [])
                 if isinstance(e, dict) and e.get("lever") == "format"]
    posts = []
    cutoff = (now - timedelta(days=7)).isoformat()
    week_views, week_watch, week_posts = 0, [], 0
    for pid, r in recs.items():
        due = r.get("due") or ""
        arms = dict(r.get("loop") or {})
        for e in fmt_tests:                      # the rendered truth for cup vs royale
            if due[:10] >= str(e.get("start_date", ""))[:10] and r.get("variant") in ("cup", "royale"):
                arms[e["id"]] = "variant" if r["variant"] == e["variant"] else "control"
        s = analytics.at_age(r)
        posts.append({"id": pid, "arms": arms, "hold": s["watch"], "views72": s["views"]})
        if due >= cutoff:
            last = (r.get("snapshots") or [{}])[-1]
            week_posts += 1
            week_views += last.get("views") or 0
            if last.get("averageTimeWatched") and r.get("length"):
                week_watch.append(last["averageTimeWatched"] / r["length"])
    week_watch.sort()
    summary = {"views": week_views,
               "retention": week_watch[len(week_watch) // 2] if week_watch else None,
               "swipe_away": None, "posts": week_posts,
               "note": "views = latest totals of posts from the last 7 days; retention = median "
                       "avg % watched; TikTok gives no swipe-away via Buffer"}
    return posts, summary


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="decide and print; write nothing")
    args = ap.parse_args(argv)
    today = datetime.now(timezone.utc).date().isoformat()

    led, lev, cfg = el.load_repo(HERE)
    try:
        posts, summary = collect(led)
        data_ok, err = True, None
    except Exception as exc:                          # no data -> no decisions
        posts, summary, data_ok, err = [], None, False, f"analytics error: {str(exc)[:200]}"
    led, cfg, actions = el.evaluate(led, lev, cfg, posts, today, data_ok=data_ok)
    if err:
        actions.insert(0, err)
    for a in actions:
        print("[loop]", a)
    print(el.digest_section("🎾 Escape Cup", led, lev, cfg, summary, actions))
    if args.dry_run:
        print("[DRY RUN - nothing written]")
        return 0
    el.save_json(os.path.join(HERE, "experiments.json"), led)
    el.save_json(os.path.join(HERE, "loop_config.json"), cfg)
    el.save_json(os.path.join(HERE, "loop_summary.json"),
                 {"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                  "summary": summary, "actions": actions})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:          # never fail the analytics job over the loop
        print(f"[loop] fatal: {exc}")
        sys.exit(0)
