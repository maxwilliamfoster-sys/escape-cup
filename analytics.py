"""
Performance data for every published Escape Cup video, pulled from Buffer's
post metrics (a personal API key carries the insights scope). Read-only.

Buffer returns for TikTok (checked 2026-10-08): views, reactions (= likes), comments,
shares, reach, engagementRate, averageTimeWatched (s), totalTimeWatched. No saves or follows.
TikTok's "watched full video" % and the retention curve are NOT exposed - for
those, read TikTok Studio. avg_pct = averageTimeWatched / video length is the
closest automatic proxy for retention.

analytics.json holds one record per post plus dated snapshots, so growth over
time (day-0 test batch vs later pushes) can be compared:
  {"posts": {post_id: {match facts..., "snapshots": [{"at", metrics...}]}}}

  python analytics.py            # collect (CI: analytics.yml, daily)
  python analytics.py --report   # print a table from analytics.json
"""
import json
import os
import sys
from datetime import datetime, timezone

import publish
import tournament

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "analytics.json")

# video lengths of posts made before lengths were recorded (seconds, from TikTok)
BACKFILL_LEN = {
    "gb-sct-ch": 35, "pl-uy": 30, "ph-ng": 24, "ma-ua": 25, "au-vn": 22, "us-hr": 21,
    "mx-nl": 25, "co-pt": 27, "id-tr": 24, "kr-jp": 18, "es-br": 18, "sn-it": 20,
}
V2_FROM = "2026-10-02"          # first dense-ring (v2) post

QUERY = """query($i: PostInput!) { post(input: $i) {
  id status sentAt externalLink text metricsUpdatedAt metrics { type value unit } } }"""


def load():
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"posts": {}}


def save(data):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def collect():
    state = tournament.load()
    data = load()
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    for e in state.get("log", []):
        if e.get("status") != "published" or not e.get("buffer_id"):
            continue
        post = publish._gql(QUERY, {"i": {"id": e["buffer_id"]}})["post"]
        if not post:
            continue
        m = {x["type"]: x["value"] for x in (post.get("metrics") or [])}
        if "likes" not in m and "reactions" in m:      # Buffer reports TikTok likes as reactions
            m["likes"] = m["reactions"]
        key = f"{e['a']}-{e['b']}"
        rec = data["posts"].setdefault(post["id"], {})
        rec.update({
            "match": f"{e['a']} v {e['b']}", "edition": e["edition"], "round": e["r"],
            "due": e.get("due"), "theme": e.get("theme"), "seed": e.get("seed"),
            "format": "v2" if (e.get("due") or "") >= V2_FROM else "v1",
            "length": e.get("length") or rec.get("length") or BACKFILL_LEN.get(key),
            "link": post.get("externalLink") or e.get("link"),
            "caption": (post.get("text") or "").split("\n")[0][:120],
        })
        snap = {"at": now, "metrics_updated": post.get("metricsUpdatedAt"), **m}
        snaps = rec.setdefault("snapshots", [])
        if not snaps or snaps[-1].get("metrics_updated") != snap["metrics_updated"] or snaps[-1].get("views") != m.get("views"):
            snaps.append(snap)
    save(data)
    return data


def rows(data):
    out = []
    for pid, r in data["posts"].items():
        s = r["snapshots"][-1] if r.get("snapshots") else {}
        views = s.get("views") or 0
        avg = s.get("averageTimeWatched")
        L = r.get("length")
        out.append({
            "due": (r.get("due") or "")[:16], "match": r["match"], "fmt": r["format"], "len": L,
            "views": views, "likes": s.get("likes", 0), "comments": s.get("comments", 0),
            "shares": s.get("shares", 0), "saves": s.get("saves", 0), "follows": s.get("follows", 0),
            "avg_watch": avg, "avg_pct": round(100 * avg / L, 1) if avg and L else None,
            "like_rate": round(100 * s.get("likes", 0) / views, 2) if views else None,
        })
    return sorted(out, key=lambda x: x["due"])


def report(data):
    print(f"{'posted (UTC)':16} {'match':12} fmt len views likes com shr sav fol avg_s avg%  like%")
    for x in rows(data):
        print(f"{x['due']:16} {x['match']:12} {x['fmt']:3} {x['len'] or '':>3} {x['views']:>5.0f} "
              f"{x['likes']:>5.0f} {x['comments']:>3.0f} {x['shares']:>3.0f} {x['saves']:>3.0f} "
              f"{x['follows']:>3.0f} {x['avg_watch'] if x['avg_watch'] is not None else '':>5} "
              f"{x['avg_pct'] if x['avg_pct'] is not None else '':>5} {x['like_rate'] if x['like_rate'] is not None else '':>5}")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report(load())
    else:
        d = collect()
        report(d)
        # also dump the raw metric names once, so new fields are visible in the log
        last = next(iter(d["posts"].values()), {}).get("snapshots", [{}])[-1]
        print("metric fields:", sorted(k for k in last if k not in ("at", "metrics_updated")))
