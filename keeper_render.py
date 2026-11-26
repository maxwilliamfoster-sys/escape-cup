"""Render a Beat The Keeper match (keeper.py) to a 1080x1920 60fps MP4.

Reuses Escape Cup's text sprites, particles, soft audio bank, music bed and loudness
normalisation, so the sound rules (nothing sharp, -16 LUFS) carry over unchanged.
Look: a night pitch instead of the neon ring arena, so the league never looks like
the country cup. Balls are kit colours + a three-letter code (no crests, see keeper.py).
"""
import math
import os
import subprocess

import cairo

import audio
import keeper as K
import music
from render import (FPS, H, W, Particles, TextCache, _hook_box, _rounded, ffmpeg_bin,
                    normalize_audio, rgb)

CARD_SECONDS = 2.0
HOOK = "Which club scores 3 first?"
KEEPER_COL = "#C6FF3D"
GOLD = "#FFD84D"
PEG_FLASH_S = 0.28
GOAL_TEXT_S = 1.1


def _lum(hexcol):
    r, g, b = rgb(hexcol)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ui_colours(a, b):
    """Readable, distinct score colours: the kit's primary, or its secondary if two clash."""
    pa, pb = K.kit(a)[0], K.kit(b)[0]
    dist = sum((x - y) ** 2 for x, y in zip(rgb(pa), rgb(pb))) ** 0.5
    if _lum(pa) < 0.12:
        pa = K.kit(a)[1]
    if _lum(pb) < 0.12:
        pb = K.kit(b)[1]
    if dist < 0.35:
        pb = K.kit(b)[1]
    return pa, pb


def _pitch(text, a, b, header):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(s)
    g = cairo.LinearGradient(0, 0, 0, H)
    g.add_color_stop_rgb(0, *rgb("#04110A"))
    g.add_color_stop_rgb(0.45, *rgb("#0A2616"))
    g.add_color_stop_rgb(1, *rgb("#04110A"))
    cr.set_source(g)
    cr.paint()
    # mowing stripes inside the arena
    for k, y in enumerate(range(int(K.TOP), int(K.NET_BOTTOM), 70)):
        if k % 2 == 0:
            cr.rectangle(K.WALL_L, y, K.WALL_R - K.WALL_L, 70)
            cr.set_source_rgba(1, 1, 1, 0.025)
            cr.fill()
    rg = cairo.RadialGradient(K.CX, 980, 0, K.CX, 980, 700)
    rg.add_color_stop_rgba(0, 0.55, 1.0, 0.7, 0.10)
    rg.add_color_stop_rgba(1, 0.55, 1.0, 0.7, 0.0)
    cr.set_source(rg)
    cr.paint()
    # pitch markings: centre circle + penalty box around the goal (decoration only)
    cr.set_source_rgba(1, 1, 1, 0.07)
    cr.set_line_width(4)
    cr.arc(K.CX, 760, 120, 0, 2 * math.pi)
    cr.stroke()
    cr.rectangle(K.CX - 300, 1180, 600, K.GOAL_Y - 1180)
    cr.stroke()
    cr.arc(K.CX, 1180, 90, math.pi, 2 * math.pi)
    cr.stroke()
    # arena walls and floor ramps
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for width, alpha in ((16, 0.10), (6, 0.95)):
        cr.set_line_width(width)
        cr.set_source_rgba(1, 1, 1, alpha)
        for ax, ay, bx, by in K.SEGS:
            cr.move_to(ax, ay)
            cr.line_to(bx, by)
        cr.stroke()
    _hook_box(cr, text, HOOK, 232)
    ca, cb = ui_colours(a, b)
    for side, code in enumerate((a, b)):
        prim, sec, _ = K.kit(code)
        x = 92 if side == 0 else W - 92
        _rounded(cr, x - 30, 330, 60, 44, 10)
        cr.set_source_rgb(*rgb(prim))
        cr.fill_preserve()
        cr.set_source_rgb(*rgb(sec))
        cr.set_line_width(5)
        cr.stroke()
        nm = K.name(code).upper()
        if side == 0:
            text.draw(cr, nm, 140, 368, 40, "#FFFFFF", align="left", max_w=240)
        else:
            text.draw(cr, nm, W - 140, 368, 40, "#FFFFFF", align="right", max_w=240)
    text.draw(cr, header, W / 2, 428, 24, "#9FC7AE", bold=True, max_w=900, shadow=False)
    return s


def _kit_ball(cr, text, code, x, y, r, alpha=1.0):
    prim, sec, pattern = K.kit(code)
    cr.save()
    cr.arc(x, y + 5, r, 0, 2 * math.pi)
    cr.set_source_rgba(0, 0, 0, 0.35 * alpha)
    cr.fill()
    cr.arc(x, y, r, 0, 2 * math.pi)
    cr.clip()
    cr.set_source_rgba(*rgb(prim), alpha)
    cr.paint()
    if pattern == "stripes":
        sw = r * 2 / 5
        for k in (1, 3):
            cr.rectangle(x - r + k * sw, y - r, sw, 2 * r)
        cr.set_source_rgba(*rgb(sec), alpha)
        cr.fill()
    else:
        cr.rectangle(x - r, y + r * 0.42, 2 * r, r)           # shorts-coloured band
        cr.set_source_rgba(*rgb(sec), alpha)
        cr.fill()
    hl = cairo.RadialGradient(x - r * 0.35, y - r * 0.4, 0, x - r * 0.35, y - r * 0.4, r * 1.1)
    hl.add_color_stop_rgba(0, 1, 1, 1, 0.35 * alpha)
    hl.add_color_stop_rgba(1, 1, 1, 1, 0.0)
    cr.set_source(hl)
    cr.paint()
    cr.restore()
    cr.arc(x, y, r, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, alpha)
    cr.set_line_width(max(3.0, r * 0.1))
    cr.stroke()
    ink = "#111111" if _lum(prim) > 0.6 else "#FFFFFF"
    text.draw(cr, code, x, y + r * 0.32, max(18, int(r * 0.72)), ink, alpha=alpha, shadow=False)


def _goal(cr, ripple_t, t):
    """Net under the goal line; it bulges for a moment after a goal."""
    top, bot = K.GOAL_Y, K.NET_BOTTOM
    bulge = 0.0
    if ripple_t is not None and 0 <= t - ripple_t < 0.6:
        age = t - ripple_t
        bulge = 26 * math.exp(-age * 6) * math.cos(age * 22)
    cr.set_line_width(2)
    cr.set_source_rgba(1, 1, 1, 0.35)
    for k in range(9):
        x = K.POST_L + (K.POST_R - K.POST_L) * k / 8
        cr.move_to(x, top)
        cr.curve_to(x, top + 30, x + bulge * 0.3, bot - 10 + bulge, x, bot + bulge * 0.6)
    for k in range(1, 5):
        y = top + (bot - top) * k / 4 + bulge * (k / 4)
        cr.move_to(K.POST_L, y)
        cr.line_to(K.POST_R, y)
    cr.stroke()
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_width(10)
    cr.set_source_rgb(1, 1, 1)
    for px, py in K.POSTS:
        cr.move_to(px, py)
        cr.line_to(px, bot + bulge * 0.6)
    cr.stroke()


def _keeper(cr, text, kx, flash):
    x0, x1 = kx - K.KEEPER_HALF, kx + K.KEEPER_HALF
    y = K.KEEPER_Y
    col = rgb(KEEPER_COL)
    glow = 0.25 + 0.5 * flash
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for width, alpha in ((2 * K.KEEPER_RAD + 16, glow), (2 * K.KEEPER_RAD, 1.0)):
        cr.move_to(x0 + K.KEEPER_RAD, y)
        cr.line_to(x1 - K.KEEPER_RAD, y)
        cr.set_source_rgba(*col, alpha * (1.0 if width == 2 * K.KEEPER_RAD else 0.35))
        cr.set_line_width(width)
        cr.stroke()
    for gx in (x0 + K.KEEPER_RAD, x1 - K.KEEPER_RAD):          # gloves
        cr.arc(gx, y, K.KEEPER_RAD + 4, 0, 2 * math.pi)
        cr.set_source_rgb(1, 1, 1)
        cr.fill()
    text.draw(cr, "GK", kx, y + 9, 24, "#173300", shadow=False)


def _burst(parts, t, x, y, color, n=36):
    for k in range(n):
        a = -math.pi * (k + 0.5) / n                      # upwards fan out of the net
        spd = parts.rng.uniform(160, 520)
        parts.items.append([t, x, y, math.cos(a) * spd, math.sin(a) * spd, parts.rng.uniform(0.5, 1.0),
                            rgb(color), parts.rng.uniform(3, 7), "shard", 0])


def render(match, info, out_path, workdir):
    """info: header, result_line, next_line, cta."""
    os.makedirs(workdir, exist_ok=True)
    a, b = match.home, match.away
    codes = [a, b]
    text = TextCache()
    parts = Particles(match.seed)
    frames = match.frames
    n_sim = len(frames)
    n_card = int(CARD_SECONDS * FPS)
    total = n_sim + n_card
    win_t = n_sim / FPS
    layer = _pitch(text, a, b, info["header"])
    ucol = ui_colours(a, b)

    goals_by_f, peg_hits, saves = {}, {}, []
    for e in match.events:
        if e.kind == "break":
            goals_by_f.setdefault(int(e.t * FPS), []).append(e)
        elif e.kind == "bounce" and e.ring >= 0:
            peg_hits.setdefault(int(e.t * FPS), []).append(e)
        elif e.kind == "save":
            saves.append(e)

    wav = os.path.join(workdir, "keeper_audio.wav")
    audio.write_wav(audio.build(match, total / FPS, win_t, match.seed,
                                music=music.bed(match.seed, total / FPS, ffmpeg_bin())), wav)
    wav = normalize_audio(wav)
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", wav, "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "14",
           "-pix_fmt", "yuv420p", "-g", "60", "-c:a", "aac", "-b:a", "192k", "-shortest",
           "-movflags", "+faststart", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)

    score = [0, 0]
    bump = [-9.0, -9.0]
    peg_flash = {}                      # peg index -> (t, colour)
    last_goal = None                    # (t, side)
    trails = [[], []]
    for f in range(total):
        t = f / FPS
        in_card = f >= n_sim
        kx, balls = frames[min(f, n_sim - 1)]
        for e in goals_by_f.get(f, []):
            score[e.ball] += 1
            bump[e.ball] = t
            last_goal = (t, e.ball)
            _burst(parts, t, e.x, K.GOAL_Y, ucol[e.ball])
            trails[e.ball] = []
        for e in peg_hits.get(f, []):
            peg_flash[e.ring] = (t, ucol[e.ball])
        if f == n_sim:
            parts.confetti(t, ucol[match.winner])

        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_surface(layer, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        # live score
        for side in (0, 1):
            pop = max(0.0, 1 - (t - bump[side]) / 0.3)
            x = W / 2 - 70 if side == 0 else W / 2 + 70
            text.draw(cr, str(score[side]), x, 378, int(64 + 30 * pop), ucol[side])
        text.draw(cr, "-", W / 2, 372, 48, "#FFFFFF")
        if not in_card and max(score) == K.GOALS_TO_WIN - 1:
            pulse = 0.55 + 0.45 * abs(math.sin(t * 5))
            msg = "NEXT GOAL WINS" if score[0] == score[1] else "MATCH POINT"
            text.draw(cr, msg, W / 2, 486, 34, GOLD, alpha=pulse)

        # pegs (flash in the hitter's colour)
        for k, (px, py) in enumerate(K.PEGS):
            fl = peg_flash.get(k)
            age = t - fl[0] if fl else 9.0
            if age < PEG_FLASH_S:
                kk = 1 - age / PEG_FLASH_S
                cr.arc(px, py, K.PEG_R + 14 * kk, 0, 2 * math.pi)
                cr.set_source_rgba(*rgb(fl[1]), 0.45 * kk)
                cr.fill()
            cr.arc(px, py, K.PEG_R, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.fill()

        _goal(cr, last_goal[0] if last_goal else None, t)
        save_flash = max([max(0.0, 1 - (t - s.t) / 0.3) for s in saves if 0 <= t - s.t] or [0.0])
        _keeper(cr, text, kx, save_flash)
        parts.draw(cr, t)

        # balls + trails
        for i, (x, y, in_net) in enumerate(balls):
            if not in_card and not in_net:
                trails[i].append((x, y))
                del trails[i][:-12]
            n = len(trails[i])
            for j, (tx, ty) in enumerate(trails[i][:-1]):
                fade = (j + 1) / n
                cr.arc(tx, ty, K.BALL_R * (0.3 + 0.55 * fade), 0, 2 * math.pi)
                cr.set_source_rgba(*rgb(ucol[i]), 0.28 * fade)
                cr.fill()
        for i, (x, y, in_net) in enumerate(balls):
            _kit_ball(cr, text, codes[i], x, y, K.BALL_R)

        # callouts
        if not in_card:
            live_saves = [s for s in saves if 0 <= t - s.t < 0.7]
            if live_saves:
                s = live_saves[-1]
                k = min(1.0, (0.7 - (t - s.t)) / 0.2)
                text.draw(cr, "SAVE!", s.x, K.KEEPER_Y - 60, 46, KEEPER_COL, alpha=k)
            if last_goal and t - last_goal[0] < GOAL_TEXT_S:
                age = t - last_goal[0]
                pop = max(0.0, 1 - age / 0.18)
                k = min(1.0, (GOAL_TEXT_S - age) / 0.25)
                side = last_goal[1]
                text.draw(cr, "GOAL!", W / 2, 640, 120 + int(24 * pop), ucol[side], alpha=k)
                text.draw(cr, K.name(codes[side]).upper(), W / 2, 712, 46, "#FFFFFF", alpha=k, max_w=900)

        # end card
        if in_card:
            ct = t - win_t
            k = min(1.0, ct / 0.3)
            cr.rectangle(0, 0, W, H)
            cr.set_source_rgba(0.01, 0.04, 0.02, 0.82 * k)
            cr.fill()
            parts.draw(cr, t)
            wcode = codes[match.winner]
            slide = (1 - k) ** 3 * 120
            text.draw(cr, "3 POINTS", W / 2, 600 + slide, 46, GOLD, alpha=k)
            _kit_ball(cr, text, wcode, W / 2, 735 + slide, 96, alpha=k)
            text.draw(cr, K.name(wcode).upper(), W / 2, 950 + slide, 100, "#FFFFFF", alpha=k, max_w=940)
            text.draw(cr, info["result_line"], W / 2, 1020 + slide, 40, "#C9F2D6", bold=True, alpha=k, max_w=940)
            k2 = min(1.0, max(0.0, (ct - 0.45) / 0.35))
            if k2 > 0:
                text.draw(cr, "NEXT MATCH", W / 2, 1130, 32, "#9FC7AE", bold=True, alpha=k2)
                text.draw(cr, info["next_line"], W / 2, 1205, 60, "#FFFFFF", alpha=k2, max_w=960)
                text.draw(cr, info["cta"], W / 2, 1282, 40, GOLD, alpha=k2, max_w=940)

        surf.flush()
        proc.stdin.write(surf.get_data())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out_path
