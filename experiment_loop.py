"""experiment_loop — the nightly autonomous A/B loop for one channel (stdlib only).

The master copy lives in Projects/Content-Bots/growth-lab, next to growth_stats.py.
Both are vendored into each bot repo; edit them here, then re-copy.

The user chose full autonomy on 2026-10-15: winners are PROMOTED into the
default config and losers REVERTED with no human step. The guard rails keep
that safe:
- only levers and values listed in the bot's levers.json can ever be applied
  (pre-vetted; every render still goes through the bot's own quality gates)
- one running experiment per channel (chess's legacy motion+choice 2x2 is the one
  exception); a new one starts only when none is running
- verdicts change config only on Sundays (UTC) or at max n: see DECISION_WEEKDAY
- verdicts come from growth_stats.judge (bootstrap CI, stricter early stop,
  leave-one-out, guardrails) with the measured min/max posts per arm
- no data (API failure) = no decisions; paused = no decisions
- this module only edits the two data files experiments.json and loop_config.json:
  it never touches credentials, code or workflows, and never deletes anything

Files in each bot repo:
  levers.json       allowed levers: {channel, min_n, max_n, not_before, max_days,
                    levers: [{lever, kind, control, variants, hypothesis}]}
  loop_config.json  {"paused": false, "values": {lever: current default}}
  experiments.json  the ledger: {"experiments": [entry, ...]}
"""
import html
import json
import os
import random
from datetime import date

import growth_stats as gs

DECISION_WEEKDAY = 6          # Sunday (date.weekday()); weekly looks, as judge() was simulated for

ENDED = ("promoted", "reverted", "stalled")


# ------------------------------------------------------------------ files
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_repo(repo_dir):
    """(ledger, levers, config) from a bot repo, with safe defaults."""
    led = load_json(os.path.join(repo_dir, "experiments.json"), {"experiments": []})
    lev = load_json(os.path.join(repo_dir, "levers.json"), {"levers": []})
    cfg = load_json(os.path.join(repo_dir, "loop_config.json"), {"paused": False, "values": {}})
    return led, lev, cfg


# ------------------------------------------------------------------ render time
def _running(ledger):
    exps = (ledger or {}).get("experiments") if isinstance(ledger, dict) else None
    return [e for e in (exps or []) if isinstance(e, dict) and e.get("status") == "running"
            and "lever" in e and "id" in e and "control" in e and "variant" in e]


def values_for_post(ledger, config, rng=None, force_arm=None):
    """Lever values for the post being made now, plus the arm it was given.

    Returns ({lever: value}, {experiment_id: "control" | "variant"}). Levers that
    are absent mean "use the bot's own default". Never raises: broken files give
    ({}, {}) and the bot renders exactly as before.
    """
    try:
        rng = rng or random.Random()
        vals = dict((config or {}).get("values") or {})
        if (config or {}).get("paused"):
            return vals, {}
        arms = {}
        for e in _running(ledger):
            arm = force_arm or ("variant" if rng.random() < 0.5 else "control")
            vals[e["lever"]] = e[arm]
            arms[e["id"]] = arm
        return vals, arms
    except Exception:
        return {}, {}


# ------------------------------------------------------------------ nightly decision
def _summ(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    s = sorted(vals)
    mid = len(s) // 2
    med = s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2
    return {"n": len(s), "median": round(med, 4)}


def _arm_values(posts, exp_id, key):
    out = {"control": [], "variant": []}
    for p in posts or []:
        if not isinstance(p, dict):
            continue
        arm = (p.get("arms") or {}).get(exp_id)
        if arm in out:
            out[arm].append(p.get(key))
    return out


def _lever_map(levers):
    out = {}
    for l in (levers or {}).get("levers") or []:
        if isinstance(l, dict) and "lever" in l:
            out[l["lever"]] = l
    return out


def evaluate(ledger, levers, config, posts, today, data_ok=True):
    """One nightly pass for one channel. Returns (ledger, config, actions).

    posts: [{"id", "arms": {exp_id: arm}, "hold": primary metric at 72h or None,
             "views72": views at 72h or None}]
    """
    today = str(today)[:10]
    ledger = ledger if isinstance(ledger, dict) else {}
    ledger.setdefault("experiments", [])
    config = config if isinstance(config, dict) else {}
    if not isinstance(config.get("values"), dict):
        config["values"] = {}
    levers = levers if isinstance(levers, dict) else {}
    lmap = _lever_map(levers)
    channel = levers.get("channel", "?")
    min_n, max_n = int(levers.get("min_n", 10)), int(levers.get("max_n", 20))
    max_days = int(levers.get("max_days", 42))
    actions = []

    if config.get("paused"):
        return ledger, config, [f"{channel}: paused (loop_config.json) - no changes"]
    if not data_ok:
        return ledger, config, [f"{channel}: analytics unavailable - no decisions tonight"]

    # 1. judge running experiments
    for e in _running(ledger):
        lever = lmap.get(e["lever"])
        if lever is None:
            actions.append(f"{channel}: {e['id']} uses '{e['lever']}', not in levers.json - left alone")
            continue
        e_min, e_max = int(e.get("min_n", min_n)), int(e.get("max_n", max_n))
        hold = _arm_values(posts, e["id"], "hold")
        views = _arm_values(posts, e["id"], "views72")
        e["control_metrics"] = dict(_summ(hold["control"]), views72=_summ(views["control"]).get("median"))
        e["variant_metrics"] = dict(_summ(hold["variant"]), views72=_summ(views["variant"]).get("median"))
        if lever.get("primary") == "views72":
            # e.g. a length lever: a shorter video gets a higher % watched for free,
            # so it is judged on views and the hold metric only guards
            res = gs.judge(views["control"], views["variant"], e_min, e_max,
                           guardrails={"hold": (hold["control"], hold["variant"], True)})
        else:
            res = gs.judge(hold["control"], hold["variant"], e_min, e_max,
                           guardrails={"views@72h": (views["control"], views["variant"], True)})
        e["verdict"] = res["verdict"]
        e["last_judged"] = today
        e["result"] = gs.format_result(e["id"], e.get("metric", "hold"), res).replace("**", "")
        age = (date.fromisoformat(today) - date.fromisoformat(e["start_date"][:10])).days
        # growth_stats.judge's interim_level=0.98 was simulated for WEEKLY looks (8.7% false
        # alarms over 4); nightly decisions inflate that. So verdicts change config only on
        # Sundays, or once both arms reach max n (no more data is coming).
        decided = res["verdict"] in (gs.WINNER, gs.LOSER, gs.INCONCLUSIVE, gs.BLOCKED)
        at_max = min(len(hold["control"]), len(hold["variant"])) >= e_max
        if decided and not at_max and date.fromisoformat(today).weekday() != DECISION_WEEKDAY:
            e["pending_verdict"] = res["verdict"]
            actions.append(f"{channel}: {e['id']} looks like {res['verdict']} - decided on Sunday")
            continue
        e.pop("pending_verdict", None)
        if res["verdict"] == gs.WINNER:
            e["status"], e["ended"] = "promoted", today
            config["values"][e["lever"]] = e["variant"]
            actions.append(f"{channel}: PROMOTED {e['lever']} = {e['variant']!r} ({e['result']})")
        elif res["verdict"] in (gs.LOSER, gs.INCONCLUSIVE, gs.BLOCKED):
            e["status"], e["ended"] = "reverted", today
            config["values"][e["lever"]] = e["control"]
            actions.append(f"{channel}: REVERTED {e['lever']} to {e['control']!r} ({e['result']})")
        elif age > max_days:
            e["status"], e["ended"] = "stalled", today
            config["values"][e["lever"]] = e["control"]
            actions.append(f"{channel}: STALLED after {age} days without enough posts - "
                           f"{e['lever']} back to {e['control']!r}")
        else:
            actions.append(f"{channel}: running {e['id']} ({e['result']})")

    # 2. start at most one new experiment, only when nothing is running
    if not _running(ledger):
        nb = str(levers.get("not_before", "1970-01-01"))[:10]
        if today < nb:
            actions.append(f"{channel}: next test not before {nb}")
        else:
            nxt = next_candidate(ledger, levers, config)
            if nxt is None:
                actions.append(f"{channel}: queue empty - add levers/variants to levers.json")
            else:
                lever, control, variant = nxt
                n = 1 + sum(1 for x in ledger["experiments"]
                            if isinstance(x, dict) and x.get("lever") == lever["lever"])
                e = {"id": f"{channel}-{lever['lever']}-{n}", "channel": channel,
                     "lever": lever["lever"], "kind": lever.get("kind", ""),
                     "hypothesis": lever.get("hypothesis", ""), "control": control,
                     "variant": variant, "start_date": today, "status": "running",
                     "min_n": min_n, "max_n": max_n,
                     "metric": ("views@72h" if lever.get("primary") == "views72"
                                else levers.get("metric", "hold")),
                     "control_metrics": {}, "variant_metrics": {}}
                ledger["experiments"].append(e)
                actions.append(f"{channel}: STARTED {e['id']}: {e['lever']} {control!r} -> "
                               f"{variant!r} ({e['hypothesis']})")
    return ledger, config, actions


def next_candidate(ledger, levers, config):
    """(lever, control, variant) for the first untested variant, or None."""
    done = {(x.get("lever"), json.dumps(x.get("variant"), sort_keys=True))
            for x in (ledger or {}).get("experiments", []) if isinstance(x, dict)}
    for lever in (levers or {}).get("levers") or []:
        if not isinstance(lever, dict) or "lever" not in lever:
            continue
        current = (config.get("values") or {}).get(lever["lever"], lever.get("control"))
        for v in lever.get("variants") or []:
            if v == current or (lever["lever"], json.dumps(v, sort_keys=True)) in done:
                continue
            return lever, current, v
    return None


# ------------------------------------------------------------------ digest
def _fmt_val(v):
    return json.dumps(v) if isinstance(v, (dict, list)) else repr(v)


def digest_section(name, ledger, levers, config, summary, actions):
    """Plain-text block for one channel. Never raises."""
    out = [f"<b>{name}</b>"]
    try:
        s = summary or {}
        bits = []
        if s.get("views") is not None:
            bits.append(f"{s['views']:,.0f} views")
        if s.get("retention") is not None:
            bits.append(f"hook hold {100 * s['retention']:.0f}%")
        if s.get("swipe_away") is not None:
            bits.append(f"swipe-away {100 * s['swipe_away']:.0f}%")
        if s.get("posts") is not None:
            bits.append(f"{s['posts']} posts")
        out.append("7 days: " + (" · ".join(bits) if bits else "no data"))
    except Exception as exc:
        out.append(f"7 days: (summary error: {exc})")
    try:
        exps = [e for e in (ledger or {}).get("experiments", []) if isinstance(e, dict)]
        run = [e for e in exps if e.get("status") == "running"]
        for e in run:
            c, v = e.get("control_metrics") or {}, e.get("variant_metrics") or {}
            out.append(f"▶ Running: {e.get('lever')} {_fmt_val(e.get('control'))} vs "
                       f"{_fmt_val(e.get('variant'))} since {e.get('start_date')} - "
                       f"n={v.get('n', 0)} vs {c.get('n', 0)} (needs {e.get('min_n')}) · "
                       f"{e.get('verdict', 'not judged yet')}")
        ended = sorted((e for e in exps if e.get("status") in ENDED),
                       key=lambda e: str(e.get("ended", "")))[-3:]
        for e in ended:
            out.append(f"✓ {str(e.get('status', '')).upper()} {e.get('ended')}: {e.get('lever')} "
                       f"{_fmt_val(e.get('variant'))} ({e.get('verdict')})")
        if not run:
            nxt = next_candidate(ledger or {}, levers or {}, config or {"values": {}})
            out.append(f"Next: {nxt[0]['lever']} {_fmt_val(nxt[1])} -> {_fmt_val(nxt[2])}"
                       if nxt else "Next: queue empty")
        else:
            led2 = {"experiments": exps + [dict(e, status="reverted") for e in run]}
            nxt = next_candidate(led2, levers or {}, config or {"values": {}})
            out.append(f"Next planned: {nxt[0]['lever']} -> {_fmt_val(nxt[2])}"
                       if nxt else "Next planned: queue empty")
    except Exception as exc:
        out.append(f"(experiments error: {exc})")
    for a in actions or []:
        out.append(f"• {a}")
    # Sent with parse_mode=HTML: a "<" in a hypothesis ("<=15s") or an error made
    # Telegram reject the whole digest. Only the bold header is markup.
    return "\n".join([out[0]] + [html.escape(str(x), quote=False) for x in out[1:]])


def build_digest(sections):
    """sections: [(name, callable returning text)]. Each failure becomes a line."""
    parts = ["🧪 <b>Experiment loop - morning digest</b>"]
    for name, fn in sections:
        try:
            parts.append(fn())
        except Exception as exc:
            parts.append(f"<b>{name}</b>\n⚠️ section failed: {html.escape(str(exc), quote=False)}")
    return "\n\n".join(parts)


def safe_send(send, text):
    """Send the digest; any error is printed instead of raised. Returns True on success."""
    try:
        send(text)
        return True
    except Exception as exc:
        print(f"[loop] Telegram send failed: {exc}")
        try:
            send(f"⚠️ Experiment digest could not be sent in full: {exc}")
        except Exception:
            pass
        return False
