"""Render a Beat The Keeper match (keeper.py) to a 1080x1920 60fps MP4.

Reuses Escape Cup's text sprites, particles and loudness normalisation; the sound is
keeper_audio.py over a Kevin MacLeod suspense track (league_music.py).

Look (v2, 2026-11-26: owner wanted it clearly different from the country cup): a
TV-broadcast football style instead of the cup's white caption box + neon rings:
  - slanted gold broadcast ribbon for the hook, italic (skewed) type throughout,
  - a stadium scoreboard: kit-coloured team panels either side of a live score box,
  - a night pitch with mowing stripes; pegs are training disc cones (orange/yellow
    by row) that light up in the hitter's colour,
  - tapered ribbon trails, a full-width kit-coloured "GOAL!" band,
  - a "FULL TIME" scoreboard end card with "+3 PTS" and a next-match ticket.
Balls are kit colours + a three-letter code (no crests, see keeper.py).
"""
import math
import os
import subprocess

import cairo

import keeper as K
import keeper_audio
import keeper_sync
import league_music
from audio import write_wav
from render import FPS, H, W, Particles, TextCache, _rounded, ffmpeg_bin, normalize_audio, rgb

CARD_SECONDS = 2.2
HOOK = "WHICH CLUB SCORES 3 FIRST?"
KEEPER_COL = "#C6FF3D"
GOLD = "#FFD84D"
INK = "#07140C"
CONE_COLS = ("#FF8A2A", "#FFD84D")          # alternate peg rows
PEG_FLASH_S = 0.30
GOAL_TEXT_S = 1.15
SKEW = -0.2                                 # italic slant for broadcast type
BOARD_Y0, BOARD_Y1 = 286, 438               # scoreboard panel


def _lum(hexcol):
    r, g, b = rgb(hexcol)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _ink(hexcol):
    return "#0B0B0F" if _lum(hexcol) > 0.55 else "#FFFFFF"


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


def _sk(cr, text, s, x, y, size, color, **kw):
    """Draw text slanted like a sports broadcast graphic."""
    cr.save()
    cr.translate(x, y)
    cr.transform(cairo.Matrix(1, 0, SKEW, 1, 0, 0))
    text.draw(cr, s, 0, 0, size, color, **kw)
    cr.restore()


def _slab(cr, x0, y0, x1, y1, lean=18):
    """Parallelogram (slanted panel) path."""
    cr.move_to(x0 + lean, y0)
    cr.line_to(x1 + lean, y0)
    cr.line_to(x1 - lean, y1)
    cr.line_to(x0 - lean, y1)
    cr.close_path()


def _ribbon(cr, text, s, cy):
    surf, w, h, pad, gh = text.sprite(s, 50, INK, max_w=820, shadow=False)
    half = (w - 2 * pad) / 2 + 56
    _slab(cr, W / 2 - half + 8, cy - 46 + 8, W / 2 + half + 8, cy + 46 + 8, 22)
    cr.set_source_rgba(0, 0, 0, 0.45)
    cr.fill()
    _slab(cr, W / 2 - half, cy - 46, W / 2 + half, cy + 46, 22)
    cr.set_source_rgb(*rgb(GOLD))
    cr.fill()
    _slab(cr, W / 2 - half - 34, cy - 46, W / 2 - half - 14, cy + 46, 22)   # accent stripe
    cr.set_source_rgb(*rgb(KEEPER_COL))
    cr.fill()
    _sk(cr, text, s, W / 2, cy + gh / 2 - 2, 50, INK, max_w=820, shadow=False)


def _board(cr, text, a, b, header):
    """Stadium scoreboard: kit panels left/right, an empty score box in the middle."""
    _rounded(cr, 40, BOARD_Y0, W - 80, BOARD_Y1 - BOARD_Y0, 22)
    cr.set_source_rgba(0.01, 0.05, 0.03, 0.92)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.18)
    cr.set_line_width(3)
    cr.stroke()
    text.draw(cr, header, W / 2, BOARD_Y0 + 34, 22, "#9FC7AE", bold=True, max_w=900, shadow=False)
    top, bot = BOARD_Y0 + 50, BOARD_Y1 - 14
    for side, code in enumerate((a, b)):
        prim, sec, _ = K.kit(code)
        x0, x1 = (74, 420) if side == 0 else (W - 420, W - 74)
        _slab(cr, x0, top, x1, bot, 14)
        cr.set_source_rgb(*rgb(prim))
        cr.fill()
        sx0, sx1 = (x1 - 22, x1 - 8) if side == 0 else (x0 + 8, x0 + 22)
        _slab(cr, sx0, top, sx1, bot, 14)
        cr.set_source_rgb(*rgb(sec))
        cr.fill()
        nm = K.name(code).upper()
        mid = (top + bot) / 2
        _sk(cr, text, nm, (x0 + x1) / 2 + (-10 if side == 0 else 10), mid + 15, 40, _ink(prim),
            max_w=290, shadow=False)
    _slab(cr, W / 2 - 104, top - 4, W / 2 + 104, bot + 4, 14)
    cr.set_source_rgb(0, 0, 0)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.35)
    cr.set_line_width(2)
    cr.stroke()


def _pitch(text, a, b, header):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(s)
    g = cairo.LinearGradient(0, 0, 0, H)
    g.add_color_stop_rgb(0, *rgb("#030C07"))
    g.add_color_stop_rgb(0.45, *rgb("#0B2B18"))
    g.add_color_stop_rgb(1, *rgb("#030C07"))
    cr.set_source(g)
    cr.paint()
    # diagonal mowing stripes (the cup has none) inside the arena
    cr.save()
    cr.rectangle(K.WALL_L, K.TOP, K.WALL_R - K.WALL_L, K.NET_BOTTOM - K.TOP)
    cr.clip()
    for k in range(-12, 24):
        x = K.WALL_L + k * 90
        cr.move_to(x, K.TOP)
        cr.line_to(x + 45, K.TOP)
        cr.line_to(x + 45 + 500, K.NET_BOTTOM)
        cr.line_to(x + 500, K.NET_BOTTOM)
        cr.close_path()
    cr.set_source_rgba(1, 1, 1, 0.03)
    cr.fill()
    cr.restore()
    # floodlight glows from the top corners
    for fx in (K.WALL_L, K.WALL_R):
        rg = cairo.RadialGradient(fx, K.TOP, 0, fx, K.TOP, 760)
        rg.add_color_stop_rgba(0, 0.85, 1.0, 0.85, 0.13)
        rg.add_color_stop_rgba(1, 0.85, 1.0, 0.85, 0.0)
        cr.set_source(rg)
        cr.paint()
    # pitch markings: halfway arc + penalty box around the goal (decoration only)
    cr.set_source_rgba(1, 1, 1, 0.08)
    cr.set_line_width(4)
    cr.arc(K.CX, K.TOP, 150, 0, math.pi)
    cr.stroke()
    cr.rectangle(K.CX - 300, 1180, 600, K.GOAL_Y - 1180)
    cr.stroke()
    cr.arc(K.CX, 1180, 90, math.pi, 2 * math.pi)
    cr.stroke()
    # advertising-board style walls: thick dark rails with a lime edge
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for width, col, alpha in ((22, (0, 0, 0), 0.55), (10, rgb("#1E3A28"), 1.0), (3, rgb(KEEPER_COL), 0.9)):
        cr.set_line_width(width)
        cr.set_source_rgba(*col, alpha)
        for ax, ay, bx, by in K.SEGS:
            cr.move_to(ax, ay)
            cr.line_to(bx, by)
        cr.stroke()
    _ribbon(cr, text, HOOK, 196)
    _board(cr, text, a, b, header)
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


def _cone(cr, px, py, row, flash):
    """Training disc cone seen from above; pops and tints when hit."""
    t, col = flash if flash else (None, None)
    k = max(0.0, 1 - t / PEG_FLASH_S) if t is not None else 0.0
    if k > 0:
        cr.arc(px, py, K.PEG_R + 22 * k, 0, 2 * math.pi)
        cr.set_source_rgba(*rgb(col), 0.5 * k)
        cr.fill()
    r = K.PEG_R + 3 + 4 * k
    base = rgb(CONE_COLS[row % 2])
    fill = tuple(c * (1 - k) + w * k for c, w in zip(base, rgb(col) if k > 0 else base))
    cr.arc(px, py + 3, r, 0, 2 * math.pi)
    cr.set_source_rgba(0, 0, 0, 0.4)
    cr.fill()
    cr.arc(px, py, r, 0, 2 * math.pi)
    cr.set_source_rgb(*fill)
    cr.fill()
    cr.arc(px, py, r * 0.38, 0, 2 * math.pi)
    cr.set_source_rgb(*rgb("#0B2B18"))
    cr.fill()


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


def _trail(cr, pts, col):
    """Tapered ribbon behind a ball (the cup uses dotted trails)."""
    n = len(pts)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for j in range(1, n):
        f = j / n
        cr.move_to(*pts[j - 1])
        cr.line_to(*pts[j])
        cr.set_line_width(2 * K.BALL_R * (0.15 + 0.7 * f))
        cr.set_source_rgba(*rgb(col), 0.30 * f)
        cr.stroke()


def _burst(parts, t, x, y, color, n=36):
    for k in range(n):
        a = -math.pi * (k + 0.5) / n                      # upwards fan out of the net
        spd = parts.rng.uniform(160, 520)
        parts.items.append([t, x, y, math.cos(a) * spd, math.sin(a) * spd, parts.rng.uniform(0.5, 1.0),
                            rgb(color), parts.rng.uniform(3, 7), "shard", 0])


def _goal_band(cr, text, t0, t, col, club):
    """Full-width kit-coloured band that sweeps in with GOAL!."""
    age = t - t0
    k_in = min(1.0, age / 0.16)
    k_out = min(1.0, (GOAL_TEXT_S - age) / 0.25)
    k = max(0.0, min(k_in, k_out))
    if k <= 0:
        return
    shift = (1 - k_in) ** 2 * -W
    cy = 660
    cr.save()
    cr.translate(shift, 0)
    _slab(cr, -60, cy - 92, W + 60, cy + 40, 40)
    cr.set_source_rgba(*rgb(col), 0.94 * k_out)
    cr.fill()
    _slab(cr, -60, cy + 40, W + 60, cy + 92, 40)
    cr.set_source_rgba(0, 0, 0, 0.75 * k_out)
    cr.fill()
    pop = max(0.0, 1 - age / 0.18)
    _sk(cr, text, "GOAL!", W / 2, cy + 22, 116 + int(20 * pop), _ink(col), alpha=k_out, shadow=False)
    _sk(cr, text, club, W / 2, cy + 82, 40, "#FFFFFF", alpha=k_out, max_w=900, shadow=False)
    cr.restore()


def _end_card(cr, text, parts, t, ct, match, codes, ucol, info):
    k = min(1.0, ct / 0.3)
    cr.rectangle(0, 0, W, H)
    cr.set_source_rgba(0.01, 0.04, 0.02, 0.86 * k)
    cr.fill()
    parts.draw(cr, t)
    slide = (1 - k) ** 3 * 120
    _ribbon(cr, text, "FULL TIME", 500 + slide)
    # scoreboard
    y0 = 600 + slide
    _rounded(cr, 50, y0, W - 100, 420, 30)
    cr.set_source_rgba(0.0, 0.03, 0.015, 0.95 * k)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.22 * k)
    cr.set_line_width(3)
    cr.stroke()
    for side, code in enumerate(codes):
        won = side == match.winner
        a = k * (1.0 if won else 0.5)
        x = 255 if side == 0 else W - 255
        if won:
            rg = cairo.RadialGradient(x, y0 + 165, 0, x, y0 + 165, 170)
            rg.add_color_stop_rgba(0, *rgb(GOLD), 0.32 * k)
            rg.add_color_stop_rgba(1, *rgb(GOLD), 0.0)
            cr.set_source(rg)
            cr.paint()
        _kit_ball(cr, text, code, x, y0 + 165, 92, alpha=a)
        _sk(cr, text, K.name(code).upper(), x, y0 + 330, 44, "#FFFFFF", alpha=a, max_w=380)
        if won:
            k3 = min(1.0, max(0.0, (ct - 0.3) / 0.25))
            if k3 > 0:
                pop = 1 + 0.25 * max(0.0, 1 - (ct - 0.3) / 0.2)
                cw, ch = 190 * pop, 62 * pop
                _slab(cr, x - cw / 2, y0 + 372 - ch / 2, x + cw / 2, y0 + 372 + ch / 2, 12)
                cr.set_source_rgba(*rgb(GOLD), k3)
                cr.fill()
                _sk(cr, text, "+3 PTS", x, y0 + 388, int(38 * pop), INK, alpha=k3, shadow=False)
    sc = match.goals
    _sk(cr, text, f"{sc[0]}-{sc[1]}", W / 2, y0 + 205, 120, "#FFFFFF", alpha=k)
    # next-match ticket
    k2 = min(1.0, max(0.0, (ct - 0.55) / 0.35))
    if k2 > 0:
        ty = 1110
        _rounded(cr, 130, ty, W - 260, 210, 24)
        cr.set_source_rgba(1, 1, 1, 0.06 * k2)
        cr.fill_preserve()
        cr.set_source_rgba(*rgb(KEEPER_COL), 0.8 * k2)
        cr.set_dash([18, 12])
        cr.set_line_width(4)
        cr.stroke()
        cr.set_dash([])
        text.draw(cr, info.get("next_label", "NEXT MATCH"), W / 2, ty + 58, 30, KEEPER_COL, bold=True, alpha=k2)
        _sk(cr, text, info["next_line"], W / 2, ty + 140, 60, "#FFFFFF", alpha=k2, max_w=780)
        _sk(cr, text, info["cta"], W / 2, ty + 300, 44, GOLD, alpha=k2, max_w=900)


TABLE_SECONDS = 3.2               # league table after FULL TIME (league.py, 2026-11-26)
CHAMP_SECONDS = 3.2               # last matchday only: the season's champions
ROW_Y0, ROW_H = 392, 53           # 20 rows end at 1452, above TikTok's caption area (y > 1480)


def _dim(cr, k, a=0.93):
    cr.rectangle(0, 0, W, H)
    cr.set_source_rgba(0.01, 0.04, 0.02, a * k)
    cr.fill()


def _table_card(cr, text, parts, t, ct, info):
    """All 20 clubs after this matchday; the featured clubs' rows are lit in their kit colour."""
    k = min(1.0, ct / 0.25)
    _dim(cr, 1.0, 0.95)
    parts.draw(cr, t)
    title = "FINAL TABLE" if info.get("champion") else "LEAGUE TABLE"
    _ribbon(cr, text, title, 250 - (1 - k) ** 3 * 80)
    text.draw(cr, info.get("table_sub", ""), W / 2, 338, 28, "#9FC7AE", bold=True, alpha=k, shadow=False)
    hy = ROW_Y0 - 14
    for label, x in (("P", 724), ("GD", 836), ("PTS", 968)):
        text.draw(cr, label, x, hy, 24, "#9FC7AE", bold=True, alpha=k, shadow=False)
    lit = set(info.get("highlight", ()))
    for i, row in enumerate(info["table"][:20]):
        kr = min(1.0, max(0.0, (ct - 0.15 - 0.035 * i) / 0.22))
        if kr <= 0:
            continue
        y = ROW_Y0 + i * ROW_H
        slide = (1 - kr) ** 2 * 60
        code = row["code"]
        if code in lit:
            _slab(cr, 58 + slide, y + 4, W - 58 + slide, y + ROW_H - 4, 10)
            prim, sec, _ = K.kit(code)
            lit_col = prim if _lum(prim) > 0.12 else sec          # Newcastle's black would vanish
            cr.set_source_rgba(*rgb(lit_col), 0.38 * kr)
            cr.fill()
        elif i % 2 == 0:
            cr.rectangle(60, y + 4, W - 120, ROW_H - 8)
            cr.set_source_rgba(1, 1, 1, 0.045 * kr)
            cr.fill()
        base = y + ROW_H / 2 + 12
        pos_col = GOLD if i == 0 else "#FFFFFF"
        text.draw(cr, str(i + 1), 104 + slide, base, 30, pos_col, bold=True, alpha=kr, shadow=False)
        _kit_ball(cr, text, code, 168 + slide, y + ROW_H / 2, 19, alpha=kr)
        text.draw(cr, K.name(code).upper(), 206 + slide, base, 31, "#FFFFFF", bold=True, alpha=kr,
                  max_w=440, align="left", shadow=False)
        gd = row["gf"] - row["ga"]
        text.draw(cr, str(row["p"]), 724 + slide, base, 30, "#D8E8DD", alpha=kr, shadow=False)
        text.draw(cr, f"{gd:+d}" if gd else "0", 836 + slide, base, 30, "#D8E8DD", alpha=kr, shadow=False)
        text.draw(cr, str(row["pts"]), 968 + slide, base, 34, GOLD, bold=True, alpha=kr, shadow=False)


def _champion_card(cr, text, parts, t, ct, info):
    code = info["champion"]
    row = next(r for r in info["table"] if r["code"] == code)
    k = min(1.0, ct / 0.3)
    _dim(cr, 1.0, 0.95)
    rg = cairo.RadialGradient(W / 2, 860, 0, W / 2, 860, 520)
    rg.add_color_stop_rgba(0, *rgb(GOLD), 0.34 * k)
    rg.add_color_stop_rgba(1, *rgb(GOLD), 0.0)
    cr.set_source(rg)
    cr.paint()
    parts.draw(cr, t)
    _ribbon(cr, text, "CHAMPIONS", 420 - (1 - k) ** 3 * 120)
    text.draw(cr, info.get("champ_sub", ""), W / 2, 530, 32, "#9FC7AE", bold=True, alpha=k, shadow=False)
    pop = 1 + 0.18 * max(0.0, 1 - ct / 0.35)
    _kit_ball(cr, text, code, W / 2, 840, 170 * pop, alpha=k)
    k2 = min(1.0, max(0.0, (ct - 0.35) / 0.3))
    _sk(cr, text, K.name(code).upper(), W / 2, 1120, 96, "#FFFFFF", alpha=k2, max_w=960)
    _sk(cr, text, f"{row['pts']} PTS  ·  {row['w']} WINS FROM {row['p']}", W / 2, 1210, 44, GOLD,
        alpha=k2, max_w=900)
    k3 = min(1.0, max(0.0, (ct - 0.9) / 0.3))
    if k3 > 0:
        text.draw(cr, info.get("champ_next", ""), W / 2, 1360, 38, KEEPER_COL, bold=True, alpha=k3,
                  max_w=900)


def card_seconds(info):
    """Length of everything after the winning goal."""
    return (CARD_SECONDS + (TABLE_SECONDS if info.get("table") else 0.0)
            + (CHAMP_SECONDS if info.get("champion") else 0.0))


def render(match, info, out_path, workdir):
    """info: header, result_line, next_line, cta (result_line is for captions now:
    the FULL TIME scoreboard shows the score itself). League posts (league.py) add
    table (league.standings rows), table_sub, highlight (codes to light up) and, on the
    last matchday, champion, champ_sub, champ_next; next_label overrides "NEXT MATCH"."""
    os.makedirs(workdir, exist_ok=True)
    cards = card_seconds(info)
    # music first: the replay is retimed so bounces land on its beat (keeper_sync); long
    # enough for the slowest allowed replay plus the cards
    bed = league_music.bed(match.seed, len(match.frames) / FPS / keeper_sync.SPEED_MIN + cards + 1.0,
                           ffmpeg_bin())
    grid = None
    if bed is not None:
        grid = keeper_sync.beat_grid(bed, 44100, bpm=(league_music.info(match.seed) or {}).get("bpm"))
        match = keeper_sync.retime(match, *grid)
    a, b = match.home, match.away
    codes = [a, b]
    text = TextCache()
    parts = Particles(match.seed)
    frames = match.frames
    n_sim = len(frames)
    n_card = int(round(cards * FPS))
    total = n_sim + n_card
    win_t = n_sim / FPS
    info["match_seconds"] = win_t          # the retimed match length (quality.check_video needs it)
    layer = _pitch(text, a, b, info["header"])
    ucol = ui_colours(a, b)
    rows = {k: int(round((py - K.PEGS[0][1]) / 115.0)) for k, (_, py) in enumerate(K.PEGS)}

    goals_by_f, peg_hits, saves = {}, {}, []
    for e in match.events:
        if e.kind == "break":
            goals_by_f.setdefault(int(e.t * FPS), []).append(e)
        elif e.kind == "bounce" and e.ring >= 0:
            peg_hits.setdefault(int(e.t * FPS), []).append(e)
        elif e.kind == "save":
            saves.append(e)

    wav = os.path.join(workdir, "keeper_audio.wav")
    # suspense music (league_music, CC BY: credit it with league_music.credit(seed)); if none
    # decodes, keeper_audio falls back to its own anthem
    taps = []
    if info.get("table"):            # one soft tap per table row as it slides in (_table_card timing)
        taps += [(win_t + CARD_SECONDS + 0.15 + 0.035 * i, "row") for i in range(min(20, len(info["table"])))]
    if info.get("champion"):
        taps.append((win_t + CARD_SECONDS + TABLE_SECONDS + 0.05, "champ"))
    write_wav(keeper_audio.build(match, total / FPS, win_t, match.seed, music=bed,
                                 key_pc=league_music.key(match.seed), grid=grid, card_taps=taps), wav)
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
        champ_f = n_sim + int(round((CARD_SECONDS + TABLE_SECONDS) * FPS))
        if info.get("champion") and f == champ_f:
            parts.confetti(t, K.kit(info["champion"])[0])
            parts.confetti(t + 0.25, GOLD)

        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_surface(layer, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)

        # live score in the scoreboard box
        for side in (0, 1):
            pop = max(0.0, 1 - (t - bump[side]) / 0.3)
            x = W / 2 - 50 if side == 0 else W / 2 + 50
            _sk(cr, text, str(score[side]), x, 412, int(70 + 26 * pop), ucol[side])
        _sk(cr, text, "-", W / 2, 404, 44, "#FFFFFF")
        if not in_card and max(score) == K.GOALS_TO_WIN - 1:
            pulse = 0.55 + 0.45 * abs(math.sin(t * 5))
            msg = "NEXT GOAL WINS" if score[0] == score[1] else "MATCH POINT"
            _sk(cr, text, msg, W / 2, 494, 32, GOLD, alpha=pulse)

        for k, (px, py) in enumerate(K.PEGS):
            fl = peg_flash.get(k)
            _cone(cr, px, py, rows[k], (t - fl[0], fl[1]) if fl and t - fl[0] < PEG_FLASH_S else None)

        _goal(cr, last_goal[0] if last_goal else None, t)
        save_flash = max([max(0.0, 1 - (t - s.t) / 0.3) for s in saves if 0 <= t - s.t] or [0.0])
        _keeper(cr, text, kx, save_flash)
        parts.draw(cr, t)

        for i, (x, y, in_net) in enumerate(balls):
            if not in_card and not in_net:
                trails[i].append((x, y))
                del trails[i][:-14]
            _trail(cr, trails[i], ucol[i])
        for i, (x, y, in_net) in enumerate(balls):
            _kit_ball(cr, text, codes[i], x, y, K.BALL_R)

        if not in_card:
            live_saves = [s for s in saves if 0 <= t - s.t < 0.7]
            if live_saves:
                s = live_saves[-1]
                k = min(1.0, (0.7 - (t - s.t)) / 0.2)
                _sk(cr, text, "SAVE!", s.x, K.KEEPER_Y - 60, 50, KEEPER_COL, alpha=k)
            if last_goal and t - last_goal[0] < GOAL_TEXT_S:
                side = last_goal[1]
                _goal_band(cr, text, last_goal[0], t, ucol[side], K.name(codes[side]).upper())
        else:
            ct = t - win_t
            if ct < CARD_SECONDS or not info.get("table"):
                _end_card(cr, text, parts, t, ct, match, codes, ucol, info)
            elif ct < CARD_SECONDS + TABLE_SECONDS or not info.get("champion"):
                _table_card(cr, text, parts, t, ct - CARD_SECONDS, info)
            else:
                _champion_card(cr, text, parts, t, ct - CARD_SECONDS - TABLE_SECONDS, info)

        surf.flush()
        proc.stdin.write(surf.get_data())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out_path
