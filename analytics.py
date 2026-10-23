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

import buffer_client
import growth_stats
import publish
import tournament

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, "analytics.json")

# video lengths of posts made before lengths were recorded (seconds, from TikTok)
BACKFILL_LEN = {
    "gb-sct-ch": 35, "pl-uy": 30, "ph-ng": 24, "ma-ua": 25, "au-vn": 22, "us-hr": 21,
    "mx-nl": 25, "co-pt": 27, "id-tr": 24, "kr-jp": 18, "es-br": 18, "sn-it": 20,
    # 10-13 cup posts (A/B start), before length was stored: frames of sim.simulate(seed)
    # / FPS + render.CARD_SECONDS (2.8 s then), a formula that reproduces 10-14..10-16 exactly.
    "es-it": 28.9, "ca-fr": 23.7,
}
V2_FROM = "2026-10-02"          # first dense-ring (v2) post

# Buffer reads go through growth-lab buffer_client (S7; growth-lab audit 2026-10-21, MASTER_PLAN #4):
# ONE paginated sent-posts query (100 a page, newest first) instead of one call per published
# post, so the cost stays flat (orgs + channels + 1 page = 3 calls) however many videos go out.
# One page = 100 posts, about 14 days at 7 a day, more than the 72 h metrics need.
# The Free plan allows 100 / 15 min, 250 / 24 h and 3,000 / 30 days per API key, shared
# with BuriedCasefiles. A 429 raises BufferRateLimited and nothing is written.
SENT_PAGES = 1
BUFFER_CACHE = os.path.join(ROOT, "work", "buffer_ids.json")   # org/channel ids (gitignored)


def load():
    if os.path.exists(PATH):
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"posts": {}}


def save(data):
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)


def buffer():
    os.makedirs(os.path.dirname(BUFFER_CACHE), exist_ok=True)
    return buffer_client.BufferClient(cache_path=BUFFER_CACHE)


def _quota_alert(windows, limits):
    """Bot HQ warning when a Buffer window is under 15% (the key is shared with BCF)."""
    try:
        import app_events
        app_events.emit("escape", "buffer_quota", "warning",
                        f"Buffer API quota low: {', '.join(windows)}",
                        buffer_client.format_limits(limits), data={"limits": limits},
                        dedupe_key=f"buffer_quota:{datetime.now(timezone.utc):%Y-%m-%d}")
    except Exception as e:  # noqa: BLE001 - an alert must never stop the collection
        print(f"[BotHQ] emit failed: {e}")


def _log_quota(client):
    lim = client.remaining()
    left = "/".join(str(lim.get(w, {}).get("remaining", "?")) for w in ("24h", "30d"))
    print(f"buffer calls: {client.calls}, remaining 24h/30d: {left}")
    low = client.quota_low()
    if low:
        print(f"WARNING Buffer quota below {int(client.low_fraction * 100)}% in {', '.join(low)}")
        _quota_alert(low, lim)


def collect(client=None):
    client = client or buffer()
    state = tournament.load()
    data = load()
    now = datetime.now(timezone.utc).isoformat(timespec="minutes")
    org, channel = client.find_channel(service="tiktok", channel_id=os.environ.get("BUFFER_CHANNEL_ID") or None,
                                       exclude_handles=publish.OTHER_PROJECT_HANDLES)
    try:
        sent = {p["id"]: p for p in client.sent_posts(org, channel, first=100, max_pages=SENT_PAGES)}
    finally:
        _log_quota(client)
    for e in state.get("log", []):
        if e.get("status") != "published" or not e.get("buffer_id"):
            continue
        post = sent.get(e["buffer_id"])
        if not post:
            continue
        m = dict(post.get("metrics") or {})
        if "likes" not in m and "reactions" in m:      # Buffer reports TikTok likes as reactions
            m["likes"] = m["reactions"]
        royale = e.get("series") == "royale"
        key = "" if royale else f"{e['a']}-{e['b']}"
        rec = data["posts"].setdefault(post["id"], {})
        rec.update({
            "match": f"royale {e['heat']}" if royale else f"{e['a']} v {e['b']}",
            "variant": "royale" if royale else "cup",
            "edition": e["edition"], "round": None if royale else e["r"],
            "due": e.get("due"), "theme": e.get("theme"), "seed": e.get("seed"),
            "format": "v2" if (e.get("due") or "") >= V2_FROM else "v1",
            "length": e.get("length") or rec.get("length") or BACKFILL_LEN.get(key),
            "link": post.get("externalLink") or e.get("link"),
            "caption": (post.get("text") or "").split("\n")[0][:120],
            "loop": e.get("loop") or rec.get("loop") or {},   # experiment-loop arms
        })
        snap = {"at": now, "metrics_updated": post.get("metricsUpdatedAt"), **m}
        snaps = rec.setdefault("snapshots", [])
        if not snaps or snaps[-1].get("metrics_updated") != snap["metrics_updated"] or snaps[-1].get("views") != m.get("views"):
            snaps.append(snap)
    snap_account(data)
    save(data)
    return data


def snap_account(data):
    """Daily follower count from the public profile (growth-lab, 2026-10-15): followers
    per 1k views is the conversion metric, and Buffer doesn't report follows."""
    import re
    import requests
    try:
        r = requests.get("https://www.tiktok.com/@5lmvill", timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/141.0 Safari/537.36",
            "Accept-Language": "en-GB,en;q=0.9"})
        m = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
                      r.text, re.S)
        st = json.loads(m.group(1))["__DEFAULT_SCOPE__"]["webapp.user-detail"]["userInfo"]["stats"]
    except Exception as e:
        print(f"account snapshot skipped ({str(e)[:100]})")
        return
    data.setdefault("account", []).append({
        "at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "followers": int(st["followerCount"]), "likes": int(st["heartCount"]),
        "videos": int(st["videoCount"])})


def _age_h(rec, snap):
    """Hours from the post's due time to when Buffer last refreshed this snapshot."""
    t = snap.get("metrics_updated") or snap.get("at")
    if not t or not rec.get("due"):
        return None
    return (datetime.fromisoformat(t.replace("Z", "+00:00"))
            - datetime.fromisoformat(rec["due"].replace("Z", "+00:00"))).total_seconds() / 3600


def at_age(rec, hours=72):
    """views / avg % watched / likes per 1k at a fixed age (None if too young)."""
    pts = [(_age_h(rec, s), s) for s in rec.get("snapshots", [])]
    pts = [(a, s) for a, s in pts if a is not None]
    L = rec.get("length")
    views = growth_stats.value_at_age([(a, s.get("views") or 0) for a, s in pts], hours, True)
    likes = growth_stats.value_at_age([(a, s.get("likes") or 0) for a, s in pts], hours, True)
    watch = growth_stats.value_at_age(
        [(a, 100 * s["averageTimeWatched"] / L) for a, s in pts if s.get("averageTimeWatched") and L],
        hours)
    if watch is None and L:              # average, not a total: nearest later snapshot
        later = sorted(((a, s) for a, s in pts if a >= hours and s.get("averageTimeWatched")),
                       key=lambda x: x[0])      # equal ages would compare the dicts
        if later and later[0][0] <= hours + 48:
            watch = 100 * later[0][1]["averageTimeWatched"] / L
    return {"views": views, "watch": watch,
            "likes_1k": 1000 * likes / views if views and likes is not None else None}


def rows(data):
    out = []
    for pid, r in data["posts"].items():
        s = r["snapshots"][-1] if r.get("snapshots") else {}
        views = s.get("views") or 0
        avg = s.get("averageTimeWatched")
        L = r.get("length")
        out.append({
            "due": (r.get("due") or "")[:16], "match": r["match"], "fmt": r["format"], "len": L,
            "variant": r.get("variant", "cup"),
            "views": views, "likes": s.get("likes", 0), "comments": s.get("comments", 0),
            "shares": s.get("shares", 0),
            "avg_watch": avg, "avg_pct": round(100 * avg / L, 1) if avg and L else None,
            "like_rate": round(100 * s.get("likes", 0) / views, 2) if views else None,
        })
    return sorted(out, key=lambda x: x["due"])


def report(data):
    print(f"{'posted (UTC)':16} {'match':12} fmt len views likes com shr avg_s avg%  like%")
    for x in rows(data):
        print(f"{x['due']:16} {x['match']:12} {x['fmt']:3} {x['len'] or '':>3} {x['views']:>5.0f} "
              f"{x['likes']:>5.0f} {x['comments']:>3.0f} {x['shares']:>3.0f} "
              f"{x['avg_watch'] if x['avg_watch'] is not None else '':>5} "
              f"{x['avg_pct'] if x['avg_pct'] is not None else '':>5} {x['like_rate'] if x['like_rate'] is not None else '':>5}")


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


AB_FROM = "2026-10-13"      # cup vs royale A/B started (main.ARMS)
AB_MIN_N, AB_MAX_N = 10, 20   # per arm (growth-lab README; watch % detects ~1.19x at 15)


# Cadence went 2 -> 10/day on 2026-10-21, partway through A1 (then 7/day from 10-23).
# EXPERIMENTS.md amendment: A1 is also shown split before/after this date. INFO ONLY:
# the verdict above the split still uses every post since AB_FROM, unchanged.
CADENCE_FROM = "2026-10-21"
KILL_TRIP_VIEWS72 = 375       # posting.json kill switch: median views@72h since CADENCE_FROM
KILL_MIN_N = 5                # scored posts needed before the check can say TRIP


def split_lines(data):
    """A1 before/after the cadence change, plus the 4/day kill-switch check."""
    def scored(lo, hi):
        out = {}
        for arm in ("cup", "royale"):
            recs = [r for r in data["posts"].values() if r.get("variant", "cup") == arm
                    and lo <= (r.get("due") or "") < hi]
            out[arm] = [v for v in (at_age(r)["views"] for r in recs) if v is not None]
        return out

    fmt = lambda xs: f"{_median(xs):.0f}" if xs else "n/a"
    lines = [f"split at {CADENCE_FROM} (cadence 2 -> 10/day; info only, verdict above uses all posts):"]
    for label, lo, hi in ((f"before {CADENCE_FROM}", AB_FROM, CADENCE_FROM),
                          (f"from {CADENCE_FROM}", CADENCE_FROM, "9999")):
        s = scored(lo, hi)
        lines.append(f"  {label}: cup n={len(s['cup'])} views@72h {fmt(s['cup'])}  "
                     f"royale n={len(s['royale'])} views@72h {fmt(s['royale'])}")
    since = sum(scored(CADENCE_FROM, "9999").values(), [])
    if len(since) < KILL_MIN_N:
        state = f"too few to judge (need {KILL_MIN_N})"
    elif _median(since) < KILL_TRIP_VIEWS72:
        state = "TRIP - set kill_switch true in posting.json (4/day)"
    else:
        state = "OK"
    lines.append(f"kill switch (posts since {CADENCE_FROM}, n={len(since)}): median views@72h "
                 f"{fmt(since)} vs trip < {KILL_TRIP_VIEWS72} -> {state}; also trip on any Studio "
                 f"'not eligible for For You' (posting.json)")
    return lines


def compare(data):
    """Cup vs Escape Royale since AB_FROM, every post scored at 72h (not its latest
    snapshot). Judged by growth_stats on views@72h; avg % watched and likes per 1k
    are guardrails. (Amended 2026-10-19 before min n, EXPERIMENTS.md A1: % watched
    penalises the longer royale, ~29s vs ~21s.)"""
    lines = []
    arms = {}
    for arm in ("cup", "royale"):
        recs = [r for r in data["posts"].values()
                if r.get("variant", "cup") == arm and (r.get("due") or "") >= AB_FROM]
        sc = [at_age(r) for r in recs]
        arms[arm] = sc
        ok = [x for x in sc if x["views"] is not None]
        md = lambda k, f: f.format(_median([x[k] for x in ok])) if [x for x in ok if x[k] is not None] else "n/a"
        lines.append(f"{arm:7} posted {len(recs):2}, scored @72h {len(ok):2}  avg watched "
                     f"{md('watch', '{:.0f}%')}  views@72h {md('views', '{:.0f}')}  likes {md('likes_1k', '{:.1f}')}/1k")
    c, t = arms["cup"], arms["royale"]
    res = growth_stats.judge(
        [x["views"] for x in c], [x["views"] for x in t], AB_MIN_N, AB_MAX_N,
        guardrails={"avg % watched": ([x["watch"] for x in c], [x["watch"] for x in t], True),
                    "likes/1k": ([x["likes_1k"] for x in c], [x["likes_1k"] for x in t], True)})
    lines.append(growth_stats.format_result("royale vs cup", "views@72h", res).replace("**", ""))
    lines += split_lines(data)
    acct = data.get("account") or []
    if acct:
        lines.append(f"followers {acct[0]['followers']} ({acct[0]['at'][:10]}) -> "
                     f"{acct[-1]['followers']} ({acct[-1]['at'][:10]})")
    return "\n".join(lines)


if __name__ == "__main__":
    if "--compare" in sys.argv:
        print(compare(load()))
    elif "--report" in sys.argv:
        report(load())
    else:
        try:
            d = collect()
        except buffer_client.BufferRateLimited as e:
            # never read as "no posts": stop with analytics.json untouched; the next daily run retries
            print(f"STOPPED: {e}")
            sys.exit(1)
        report(d)
        print(compare(d))
        # weekly A/B digest to Telegram (Mondays)
        if datetime.now(timezone.utc).weekday() == 0:
            import notify
            notify.send("<b>Escape Cup A/B: cup vs royale</b>\n<pre>" + compare(d) + "</pre>")
        # also dump the raw metric names once, so new fields are visible in the log
        last = next(iter(d["posts"].values()), {}).get("snapshots", [{}])[-1]
        print("metric fields:", sorted(k for k in last if k not in ("at", "metrics_updated")))
