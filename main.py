"""
Escape Cup - one physics match per TikTok video, posted via Buffer.

Normal run (CI, once a day, early UTC):
  renders the next matches of the bracket and schedules each one in Buffer at
  today's posting slots (UK evening), so GitHub's hours-late cron can't shift
  the posting time. If Buffer already holds enough scheduled posts, it does
  nothing - reruns can never double-post.

  python main.py                 # schedule up to len(SLOTS) matches
  python main.py --sample        # render the next match, send to Telegram only
  python main.py --sample --local  # render locally, no network at all
"""
import argparse
import copy
import hashlib
import html
import json
import os
import sys
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import audio
import director
import notify
import publish
import render
import sim
import teams
import tournament

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
WORK = os.path.join(ROOT, "work")
LONDON = ZoneInfo("Europe/London")
SLOTS = [(17, 0), (20, 30)]          # UK local posting times
MIN_LEAD = timedelta(minutes=25)     # never schedule a slot closer than this
HASHTAGS = "#ballescape #simulation #satisfying"

RESULT_LINE = {16: "advances to the Round of 16", 8: "reaches the quarter-finals",
               4: "reaches the semi-finals", 2: "reaches the Final"}


def _ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def upcoming_slots(now, taken, count):
    """Next `count` posting datetimes (UTC) after now+MIN_LEAD, skipping ones Buffer already holds."""
    out = []
    day = now.astimezone(LONDON).date()
    while len(out) < count:
        for h, mnt in SLOTS:
            slot = datetime(day.year, day.month, day.day, h, mnt, tzinfo=LONDON).astimezone(timezone.utc)
            if slot >= now + MIN_LEAD and all(abs((slot - t).total_seconds()) > 1800 for t in taken):
                out.append(slot)
                if len(out) == count:
                    break
        day += timedelta(days=1)
    return out


def build_one(state):
    """Render the next match. Returns (path, caption, meta, winner, seed, melody, r, m)."""
    nm = tournament.next_match(state)
    r, m = nm
    match = state["rounds"][r][m]
    a, b = match["a"], match["b"]
    d = tournament.describe(state, r, m)
    base = int(hashlib.sha1(f"{state['edition']}-{r}-{m}".encode()).hexdigest()[:7], 16)
    seed, drama = director.pick(base)
    phys = sim.simulate(seed)
    winner = [a, b][phys.winner]
    melody = audio.MELODY_NAMES[state.get("posts", 0) % len(audio.MELODY_NAMES)]
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
        cta = "Who wins? Comment below"
        tease = f"Next up: {teams.name(preview['a'])} vs {teams.name(preview['b'])} - comment your pick 👇"
    else:
        next_label = f"ESCAPE CUP #{state['edition'] + 1}"
        next_line = "NEW DRAW TOMORROW"
        cta = "Follow so you don't miss it"
        tease = f"Escape Cup #{state['edition'] + 1} starts tomorrow - who's your team? 👇"

    info = dict(a=a, b=b, header=d["header"], melody=melody, result_line=result,
                next_label=next_label, next_line=next_line, cta=cta, final=is_final)
    os.makedirs(OUT, exist_ok=True)
    os.makedirs(WORK, exist_ok=True)
    fname = f"ec{state['edition']:02d}_r{r}_m{m:02d}_{seed}.mp4"
    path = os.path.join(OUT, fname)
    render.render_match(phys, info, path, WORK)

    vs = f"{teams.name(a)} {teams.emoji(a)} vs {teams.name(b)} {teams.emoji(b)}"
    if is_final:
        head = f"THE FINAL 🏆 {vs} - who lifts Escape Cup #{state['edition']}?"
    else:
        head = f"{vs} - who breaks out first? {d['round_name']}, Escape Cup #{state['edition']}"
    caption = f"{head}\n{tease}\n{HASHTAGS} {teams.hashtag(a)} {teams.hashtag(b)}"
    meta = {"file": fname, "drama": drama, "round": d["round_name"], "a": a, "b": b,
            "winner": winner, "melody": melody}
    return path, caption, meta, winner, seed, melody, r, m


def check_failures(state, org, channel):
    """Alert once per Buffer post that failed to publish (e.g. TikTok rejected it)."""
    seen = set(state.setdefault("seen_errors", []))
    for p in publish.recent_posts(org, channel, statuses=("error",)):
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        msg = (p.get("error") or {}).get("message", "unknown error")
        notify.send(f"⚠️ <b>Escape Cup post failed in Buffer</b>\n{html.escape(msg)}\n"
                    f"<i>{html.escape((p.get('text') or '')[:120])}</i>")
    state["seen_errors"] = sorted(seen)[-50:]


def run(sample=False, local=False):
    state = tournament.load()
    if sample:
        work = copy.deepcopy(state)
        path, caption, meta, *_ = build_one(work)
        print(json.dumps(meta, indent=1))
        print(caption)
        if not local:
            notify.send_video(path, "🧪 <b>Escape Cup sample</b> (not posted)\n\n" + html.escape(caption))
        return

    org, channel = publish.tiktok_channel()
    check_failures(state, org, channel)
    pending = publish.pending_posts(org, channel)
    want = len(SLOTS) - len(pending)
    if want <= 0:
        print(f"[main] Buffer already holds {len(pending)} scheduled posts - nothing to do")
        return
    taken = [datetime.fromisoformat(p["dueAt"].replace("Z", "+00:00")) for p in pending if p.get("dueAt")]
    slots = upcoming_slots(datetime.now(timezone.utc), taken, want)

    for slot in slots:
        state = tournament.roll_over_if_done(state)
        path, caption, meta, winner, seed, melody, r, m = build_one(state)
        url = publish.host_video(path, meta["file"])
        post = publish.schedule_tiktok(channel, url, caption, slot)
        tournament.record(state, r, m, winner, seed, melody)
        match = state["rounds"][r][m]
        match["posted"] = slot.isoformat()
        match["buffer_id"] = post["id"]
        tournament.save(state)
        when = slot.astimezone(LONDON).strftime("%a %H:%M UK")
        notify.send_video(path, f"🎾 <b>Escape Cup scheduled</b> for {when}\n"
                                f"{html.escape(meta['round'])}: winner {html.escape(teams.name(winner))} "
                                f"(don't spoil it 🤫)\n\n{html.escape(caption)}")
    state = tournament.roll_over_if_done(state)
    tournament.save(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="render the next match, don't post")
    ap.add_argument("--local", action="store_true", help="with --sample: no Telegram")
    args = ap.parse_args()
    try:
        run(sample=args.sample, local=args.local)
    except Exception as e:
        traceback.print_exc()
        if not args.local:
            notify.send(f"❌ <b>Escape Cup failed</b>\n<code>{html.escape(str(e))[:900]}</code>\n"
                        f"Check GitHub Actions for the log.")
        sys.exit(1)


if __name__ == "__main__":
    main()
