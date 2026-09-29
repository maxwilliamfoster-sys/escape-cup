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
    start slow), then 2/day - far below TikTok's 15/day API cap and the
    "10+ a day looks like a bot" zone.
  - weekdays 16:30 (after school) + 20:00 (UK evening peak 7-10pm);
    weekends 12:00 + 19:30.
"""
import argparse
import copy
import hashlib
import html
import json
import os
import random
import sys
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import director
import notify
import publish
import quality
import render
import sim
import teams
import tournament

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
WORK = os.path.join(ROOT, "work")
LONDON = ZoneInfo("Europe/London")

RAMP_DAYS = 7
RAMP_SLOTS = [(19, 30)]
WEEKDAY_SLOTS = [(16, 30), (20, 0)]
WEEKEND_SLOTS = [(12, 0), (19, 30)]
HORIZON = timedelta(hours=26)        # how far ahead to schedule (= your veto window)
MIN_LEAD = timedelta(minutes=25)     # never schedule a slot closer than this
MAX_ATTEMPTS = 3                     # different matches tried before a slot is skipped

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
    "{A} vs {B}: six rings, one way out. Physics simulation",
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
    return WEEKEND_SLOTS if day.weekday() >= 5 else WEEKDAY_SLOTS


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


def build_one(state, exclude_seeds=()):
    """Render + gate the next match. Returns dict with path, caption, report, failures..."""
    r, m = tournament.next_match(state)
    match = state["rounds"][r][m]
    a, b = match["a"], match["b"]
    d = tournament.describe(state, r, m)
    base = int(hashlib.sha1(f"{state['edition']}-{r}-{m}".encode()).hexdigest()[:7], 16)
    seed, drama = director.pick(base, exclude=exclude_seeds)
    phys = sim.simulate(seed)
    winner = [a, b][phys.winner]
    is_final = d["size"] == 1
    rng = random.Random(seed)

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
                next_label=next_label, next_line=next_line, cta=cta, final=is_final)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    fname = f"ec{state['edition']:02d}_r{r}_m{m:02d}_{seed}.mp4"
    path = os.path.join(OUT, fname)
    render.render_match(phys, info, path, WORK)

    caption = make_caption(state, a, b, d, preview, rng)
    report, failures = quality.check_video(path, len(phys.frames) / sim.FPS)
    failures += quality.check_content(a, b, winner, phys.breaks)
    failures += quality.check_caption(caption, a, b, state.get("recent_captions", []))
    return {"path": path, "caption": caption, "file": fname, "seed": seed, "winner": winner,
            "r": r, "m": m, "round": d["round_name"], "a": a, "b": b, "theme": theme,
            "drama": drama, "report": report, "failures": failures}


def build_passing(state):
    """Try up to MAX_ATTEMPTS different matches; return the first that passes, else None + reasons."""
    r, m = tournament.next_match(state)
    # seeds whose videos were vetoed (deleted in Buffer) or rejected are never reused
    tried = list(state.get("excluded_seeds", {}).get(f"{state['edition']}-{r}-{m}", []))
    reasons = []
    for _ in range(MAX_ATTEMPTS):
        v = build_one(state, exclude_seeds=tried)
        if not v["failures"]:
            return v, reasons
        tried.append(v["seed"])
        reasons.append(f"seed {v['seed']}: " + "; ".join(v["failures"]))
        print("[quality] FAILED", reasons[-1])
    return None, reasons


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
    title = f"{teams.name(dropped['a'])} v {teams.name(dropped['b'])}"
    if later_published:
        # A later match already went out, so the bracket can't be rewound. Keep
        # the result; mark it so it is only alerted once.
        dropped["status"] = "lost"
        notify.send(f"⚠️ <b>Escape Cup: {html.escape(title)} never reached TikTok</b> "
                    f"({html.escape(dropped['drop_reason'])}), and later matches were already "
                    f"published, so the bracket keeps its result.")
        return state

    # cancel every later still-scheduled post so the replay stays in order
    cancelled = []
    for e in log[drop_at + 1:]:
        if e.get("buffer_id") in pending_ids:
            publish.delete_post(e["buffer_id"])
            cancelled.append(f"{teams.name(e['a'])} v {teams.name(e['b'])}")
    key = f"{dropped['edition']}-{dropped['r']}-{dropped['m']}"
    state.setdefault("excluded_seeds", {}).setdefault(key, []).append(dropped["seed"])
    state = tournament.truncate(state, drop_at)
    notify.send(f"↩️ <b>Escape Cup: {html.escape(title)} was not published</b> "
                f"({html.escape(dropped['drop_reason'])}).\nIt will be re-simulated and scheduled again "
                f"in order" + (f"; also re-queued: {html.escape(', '.join(cancelled))}." if cancelled else "."))
    return state


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
    target = pending[e["buffer_id"]]
    fname = f"ec{e['edition']:02d}_r{e['r']}_m{e['m']:02d}_{e['seed']}.mp4"
    url = f"{publish.pages_base()}/v/{fname}"
    publish.delete_post(target["id"])
    post = publish.schedule_tiktok(channel, url, target["text"], datetime.now(timezone.utc))
    e["due"], e["buffer_id"] = datetime.now(timezone.utc).isoformat(), post["id"]
    tournament.save(state)
    final = publish.wait_until_published(post["id"])
    title = f"{teams.name(e['a'])} v {teams.name(e['b'])}"
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


def run(sample=False, local=False):
    state = tournament.load()
    if sample:
        work = copy.deepcopy(state)
        v = build_one(work)
        print(json.dumps({k: v[k] for k in ("file", "round", "a", "b", "winner", "theme", "drama",
                                            "report", "failures")}, indent=1))
        print(v["caption"])
        if not local:
            verdict = ("✅ passed quality gate\n" + qc_line(v["report"])) if not v["failures"] else \
                ("❌ FAILED quality gate:\n" + html.escape("; ".join(v["failures"])))
            notify.send_video(v["path"], f"🧪 <b>Escape Cup sample</b> (not posted)\n{verdict}\n\n"
                                         + html.escape(v["caption"]))
        return

    org, channel = publish.tiktok_channel()
    state = reconcile(state, org, channel)
    tournament.save(state)
    pending = publish.pending_posts(org, channel)
    taken = [datetime.fromisoformat(p["dueAt"].replace("Z", "+00:00")) for p in pending if p.get("dueAt")]
    slots = upcoming_slots(datetime.now(timezone.utc), taken, state)
    if not slots:
        print(f"[main] nothing to schedule ({len(pending)} already pending)")
        return

    for slot in slots:
        v, reasons = build_passing(state)
        when = slot.astimezone(LONDON).strftime("%a %d %b %H:%M UK")
        if v is None:
            # Stop here: filling a LATER slot with this match's successor would
            # break the order. The next run retries this match first.
            notify.send(f"🛑 <b>Escape Cup: nothing scheduled for {when}</b>\n"
                        f"{MAX_ATTEMPTS} renders failed the quality gate:\n"
                        + html.escape("\n".join(reasons))[:3000])
            break
        url = publish.host_video(v["path"], v["file"])
        post = publish.schedule_tiktok(channel, url, v["caption"], slot)
        state["recent_captions"] = (state.get("recent_captions", []) + [v["caption"]])[-40:]
        state = tournament.append(state, {
            "edition": state["edition"], "r": v["r"], "m": v["m"], "a": v["a"], "b": v["b"],
            "winner": v["winner"], "seed": v["seed"], "theme": v["theme"],
            "due": slot.isoformat(), "buffer_id": post["id"], "status": "scheduled"})
        tournament.save(state)
        notify.send_video(v["path"], f"🎾 <b>Escape Cup scheduled</b> for {when}\n"
                                     f"{html.escape(v['round'])}: winner {html.escape(teams.name(v['winner']))} "
                                     f"(don't spoil it 🤫)\n✅ {qc_line(v['report'])}\n"
                                     f"Don't like it? Delete it in Buffer before then - the bracket "
                                     f"rewinds and it gets re-simulated.\n\n{html.escape(v['caption'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="render the next match, don't post")
    ap.add_argument("--local", action="store_true", help="with --sample: no Telegram")
    ap.add_argument("--post-now", action="store_true",
                    help="publish the earliest pending post immediately and wait for TikTok")
    ap.add_argument("--reset-unpublished", action="store_true",
                    help="cancel our pending Buffer posts and redraw if nothing is published")
    args = ap.parse_args()
    try:
        if args.reset_unpublished:
            reset_unpublished()
        elif args.post_now:
            post_now()
        else:
            run(sample=args.sample, local=args.local)
    except Exception as e:
        traceback.print_exc()
        if not args.local:
            notify.send(f"❌ <b>Escape Cup failed</b>\n<code>{html.escape(str(e))[:900]}</code>\n"
                        f"Check GitHub Actions for the log.")
        sys.exit(1)


if __name__ == "__main__":
    main()
