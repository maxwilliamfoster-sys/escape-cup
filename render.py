"""
Render one Escape Cup match to a 1080x1920 60fps MP4.

Drawing is pycairo (pixman, SIMD-blended, anti-aliased). skia-python was tried
first and blended translucent pixels ~250x slower, which made a 30s video take
5 minutes. Text is rasterised once per unique string by Pillow (so we can use
Montserrat from a file, with a soft shadow baked in) and cached as sprites.

Cairo's ARGB32 is BGRA in memory on little-endian machines, so frames go to
FFmpeg as bgra.

Layout respects TikTok's overlays: nothing important above y~170 (tabs) or
below y~1480 (caption, username, sound), and the arena stays clear of the
right-hand button column.
"""
import math
import os
import random
import shutil
import subprocess

import cairo
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import audio
import sim
import teams

W, H = 1080, 1920
FPS = sim.FPS
CARD_SECONDS = 2.8
HOOK_SECONDS = 3.0
ROOT = os.path.dirname(os.path.abspath(__file__))
FONT_BLACK = os.path.join(ROOT, "assets", "fonts", "Montserrat-900.ttf")
FONT_BOLD = os.path.join(ROOT, "assets", "fonts", "Montserrat-800.ttf")

FINAL_RED = "#FF3355"

# Visual themes, rotated per video. TikTok treats automation that "sends
# repetitive content" as spam, so consecutive videos must not look identical:
# each theme changes the ring palette and the background tint.
# (bg_dark, bg_mid, glow, ring colours inner->outer)
THEMES = {
    "neon":   ("#0A0F24", "#161B3D", "#2A3570", ["#FF4D6D", "#FF9F1C", "#FFE14D", "#3DDC97", "#4CC9F0", "#B388FF"]),
    "ocean":  ("#051A24", "#0B2E3D", "#11546B", ["#7FFFD4", "#4DD8E6", "#38B6FF", "#5B8CFF", "#8A7CFF", "#C38BFF"]),
    "sunset": ("#1F0A16", "#361127", "#6B2045", ["#FFD166", "#FFB347", "#FF8C61", "#FF6B8B", "#E86AF0", "#A77BFF"]),
    "forest": ("#07160F", "#10281C", "#1F5A3A", ["#E9F59A", "#B8F28B", "#7BE495", "#4FD1A5", "#3CB4C8", "#5B8DEF"]),
    "candy":  ("#160B24", "#26143D", "#4B2A7A", ["#FF9AD5", "#FFB86B", "#FFF07A", "#8BF0C8", "#8FD3FF", "#C9A2FF"]),
    "ember":  ("#1A0C06", "#2E160B", "#6B3312", ["#FFF3B0", "#FFD166", "#FFA94D", "#FF7A45", "#FF5470", "#D65DB1"]),
}
FINAL_THEME = "gold"
THEMES[FINAL_THEME] = ("#140F02", "#2A2006", "#6B5410", ["#FFF6CC", "#FFE9A0", "#FFD966", "#FFC933", "#FFB300", "#FF9900"])
HOOK_TEXT = "Which country escapes first?"      # on-screen text is indexed by TikTok search


def ffmpeg_bin():
    return (os.environ.get("FFMPEG") or shutil.which("ffmpeg")
            or r"C:\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe")


def rgb(hexstr):
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _pil_to_surface(img):
    """RGBA Pillow image -> premultiplied cairo ARGB32 surface."""
    arr = np.asarray(img.convert("RGBA"), dtype=np.float32)
    a = arr[..., 3:4] / 255.0
    bgra = np.empty(arr.shape, dtype=np.uint8)
    bgra[..., 0] = (arr[..., 2] * a[..., 0]).astype(np.uint8)
    bgra[..., 1] = (arr[..., 1] * a[..., 0]).astype(np.uint8)
    bgra[..., 2] = (arr[..., 0] * a[..., 0]).astype(np.uint8)
    bgra[..., 3] = arr[..., 3].astype(np.uint8)
    h, w = bgra.shape[:2]
    stride = cairo.ImageSurface.format_stride_for_width(cairo.FORMAT_ARGB32, w)
    data = bytearray(stride * h)
    view = np.frombuffer(data, dtype=np.uint8).reshape(h, stride)
    view[:, : w * 4] = bgra.reshape(h, w * 4)
    return cairo.ImageSurface.create_for_data(data, cairo.FORMAT_ARGB32, w, h, stride)


class TextCache:
    def __init__(self):
        self._c = {}
        self._fonts = {}

    def _font(self, path, size):
        key = (path, size)
        if key not in self._fonts:
            self._fonts[key] = ImageFont.truetype(path, size)
        return self._fonts[key]

    def sprite(self, s, size, color, bold=False, max_w=None, shadow=True):
        key = (s, size, color, bold, max_w, shadow)
        if key in self._c:
            return self._c[key]
        path = FONT_BOLD if bold else FONT_BLACK
        font = self._font(path, size)
        while max_w and font.getlength(s) > max_w and size > 12:
            size -= 2
            font = self._font(path, size)
        l, t, r, b = font.getbbox(s)
        pad = 14
        w, h = int(r - l) + pad * 2, int(b - t) + pad * 2 + 6
        img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        if shadow:
            sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(sh).text((pad - l, pad - t + 4), s, font=font, fill=(0, 0, 0, 130))
            img = Image.alpha_composite(img, sh.filter(ImageFilter.GaussianBlur(4)))
        ImageDraw.Draw(img).text((pad - l, pad - t), s, font=font,
                                 fill=tuple(int(c * 255) for c in rgb(color)) + (255,))
        # anchor: x centre, y = cap baseline (bottom of glyph box)
        entry = (_pil_to_surface(img), w, h, pad, int(b - t))
        self._c[key] = entry
        return entry

    def draw(self, cr, s, x, y, size, color, bold=False, alpha=1.0, max_w=None, align="center",
             shadow=True):
        surf, w, h, pad, gh = self.sprite(s, size, color, bold, max_w, shadow)
        if align == "center":
            left = x - w / 2
        elif align == "left":
            left = x - pad
        else:
            left = x - w + pad
        top = y - gh - pad
        cr.set_source_surface(surf, left, top)
        if alpha >= 0.999:
            cr.paint()
        elif alpha > 0:
            cr.paint_with_alpha(alpha)


class Flags:
    def __init__(self):
        self._s = {}

    def surface(self, code):
        if code not in self._s:
            self._s[code] = cairo.ImageSurface.create_from_png(
                os.path.join(ROOT, "assets", "flags", f"{code}.png"))
        return self._s[code]

    def rect(self, cr, code, cx, cy, w, h, radius=12, border=4, alpha=1.0):
        s = self.surface(code)
        x, y = cx - w / 2, cy - h / 2
        _rounded(cr, x + 3, y + 7, w, h, radius)
        cr.set_source_rgba(0, 0, 0, 0.35 * alpha)
        cr.fill()
        cr.save()
        _rounded(cr, x, y, w, h, radius)
        cr.clip()
        cr.translate(x, y)
        cr.scale(w / s.get_width(), h / s.get_height())
        cr.set_source_surface(s, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint_with_alpha(alpha)
        cr.restore()
        if border:
            _rounded(cr, x, y, w, h, radius)
            cr.set_source_rgba(1, 1, 1, 0.92 * alpha)
            cr.set_line_width(border)
            cr.stroke()

    def ball(self, cr, code, x, y, r, border=4.5):
        s = self.surface(code)
        side = min(s.get_width(), s.get_height())
        cr.save()
        cr.arc(x, y, r, 0, 2 * math.pi)
        cr.clip()
        k = 2 * r / side
        cr.translate(x - r - (s.get_width() - side) / 2 * k, y - r - (s.get_height() - side) / 2 * k)
        cr.scale(k, k)
        cr.set_source_surface(s, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()
        cr.restore()
        cr.arc(x, y, r, 0, 2 * math.pi)
        cr.set_source_rgb(1, 1, 1)
        cr.set_line_width(border)
        cr.stroke()


def _rounded(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Particles:
    """Deterministic particle bursts (ring shatters, confetti)."""

    def __init__(self, seed):
        self.rng = random.Random(seed)
        self.items = []   # [t0, x, y, vx, vy, life, rgb, size, kind, spin]

    def ring_burst(self, t, ring, color):
        n = 40
        for k in range(n):
            a = 2 * math.pi * k / n + self.rng.uniform(-0.03, 0.03)
            spd = self.rng.uniform(80, 380)
            self.items.append([t, sim.CX + math.cos(a) * ring.radius, sim.CY + math.sin(a) * ring.radius,
                               math.cos(a) * spd, math.sin(a) * spd, self.rng.uniform(0.5, 1.0),
                               rgb(color), self.rng.uniform(3, 6), "shard", 0])

    def confetti(self, t, color):
        for _ in range(170):
            self.items.append([t + self.rng.uniform(0, 0.6), self.rng.uniform(80, W - 80),
                               self.rng.uniform(260, 520), self.rng.uniform(-90, 90),
                               self.rng.uniform(80, 360), self.rng.uniform(1.8, 3.4),
                               rgb(color) if self.rng.random() < 0.6 else (1, 1, 1),
                               self.rng.uniform(8, 14), "confetti", self.rng.uniform(0, 6.28)])

    def draw(self, cr, t):
        for t0, x, y, vx, vy, life, col, size, kind, spin in self.items:
            age = t - t0
            if age < 0 or age > life:
                continue
            fade = 1 - age / life
            g = 520 if kind == "shard" else 240
            px, py = x + vx * age, y + vy * age + 0.5 * g * age * age
            if kind == "shard":
                cr.arc(px, py, size * (0.4 + 0.6 * fade), 0, 2 * math.pi)
                cr.set_source_rgba(*col, fade)
                cr.fill()
            else:
                cr.save()
                cr.translate(px, py)
                cr.rotate(spin + age * 5)
                cr.rectangle(-size / 2, -size / 4, size, size / 2)
                cr.set_source_rgba(*col, min(1, fade * 1.5))
                cr.fill()
                cr.restore()


def _background(theme):
    dark, mid, glow, _ = THEMES[theme]
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(s)
    g = cairo.LinearGradient(0, 0, 0, H)
    g.add_color_stop_rgb(0, *rgb(dark))
    g.add_color_stop_rgb(0.5, *rgb(mid))
    g.add_color_stop_rgb(1, *rgb(dark))
    cr.set_source(g)
    cr.paint()
    rg = cairo.RadialGradient(sim.CX, sim.CY, 0, sim.CX, sim.CY, 640)
    rg.add_color_stop_rgba(0, *rgb(glow), 0.55)
    rg.add_color_stop_rgba(1, *rgb(glow), 0.0)
    cr.set_source(rg)
    cr.paint()
    return s


def _hook_box(cr, text, s, cy):
    """TikTok-native caption style: black text on a white rounded box. This is the
    exact look of the top ball-escape videos ("Will the ball escape?")."""
    surf, w, h, pad, gh = text.sprite(s, 54, "#0B0B0F", max_w=880, shadow=False)
    bw, bh = w + 24, gh + 46
    _rounded(cr, W / 2 - bw / 2, cy - bh / 2, bw, bh, 18)
    cr.set_source_rgb(1, 1, 1)
    cr.fill()
    text.draw(cr, s, W / 2, cy + gh / 2 - 2, 54, "#0B0B0F", max_w=880, shadow=False)


def _ring_colors(theme, n=None):
    """n colours interpolated across the theme's palette, inner -> outer."""
    n = n or sim.N_RINGS
    pal = [rgb(c) for c in THEMES[theme][3]]
    out = []
    for i in range(n):
        x = i / (n - 1) * (len(pal) - 1)
        j = min(int(x), len(pal) - 2)
        f = x - j
        out.append(tuple(pal[j][c] * (1 - f) + pal[j + 1][c] * f for c in range(3)))
    return out


def _static_layer(theme, codes, text, flags, small_line):
    """Background + hook box + team names, baked once (the score is drawn live)."""
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(s)
    dark = THEMES[theme][0]
    cr.set_source_rgb(*[c * 0.55 for c in rgb(dark)])          # near-black: rings pop
    cr.paint()
    rg = cairo.RadialGradient(sim.CX, sim.CY, 0, sim.CX, sim.CY, 560)
    rg.add_color_stop_rgba(0, *rgb(THEMES[theme][2]), 0.35)
    rg.add_color_stop_rgba(1, *rgb(THEMES[theme][2]), 0.0)
    cr.set_source(rg)
    cr.paint()
    _hook_box(cr, text, HOOK_TEXT, 232)
    for side, code in enumerate(codes):
        name = teams.name(code).upper()
        if side == 0:
            flags.rect(cr, code, 92, 352, 66, 44, radius=8, border=3)
            text.draw(cr, name, 140, 368, 40, "#FFFFFF", align="left", max_w=300)
        else:
            flags.rect(cr, code, W - 92, 352, 66, 44, radius=8, border=3)
            text.draw(cr, name, W - 140, 368, 40, "#FFFFFF", align="right", max_w=300)
    text.draw(cr, small_line, W / 2, 428, 24, "#9AA3BF", bold=True, max_w=900, shadow=False)
    return s


CONTRAST = ["#FFFFFF", "#4CC9F0", "#FF4D6D", "#3DDC97", "#B388FF"]


def team_colors(a, b):
    """Score/trail colours that can't be confused: if both flags give similar
    colours (Australia/Vietnam are both yellow), team B takes the most
    contrasting fallback."""
    ca, cb = teams.color(a), teams.color(b)
    dist = lambda x, y: sum((p - q) ** 2 for p, q in zip(rgb(x), rgb(y))) ** 0.5
    if dist(ca, cb) < 0.45:
        cb = max(CONTRAST, key=lambda c: dist(c, ca))
    return ca, cb


def render_match(match, info, out_path, workdir):
    """info: a, b (team codes), header, result_line, next_label, next_line, cta,
    final (bool), theme."""
    a, b = info["a"], info["b"]
    codes = [a, b]
    flags = Flags()
    text = TextCache()
    parts = Particles(match.seed)
    frames = match.frames
    n_sim = len(frames)
    n_card = int(CARD_SECONDS * FPS)
    total = n_sim + n_card
    win_t = n_sim / FPS

    theme = info.get("theme", "neon")
    ring_cols = _ring_colors(theme)
    layer = _static_layer(theme, codes, text, flags, info["header"])

    breaks_by_frame = {}
    for e in match.events:
        if e.kind == "break":
            breaks_by_frame.setdefault(int(e.t * FPS), []).append(e)
    score = [0, 0]
    bump = [-9.0, -9.0]          # time each side last scored (score pops)
    flash = {}

    wav = os.path.join(workdir, "audio.wav")
    audio.write_wav(audio.build(match, total / FPS, win_t, match.seed), wav)

    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", wav,
           "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "14",
           "-pix_fmt", "yuv420p", "-g", "60",
           "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    trails = [[], []]
    tcol = team_colors(a, b)
    rcol = [rgb(c) for c in tcol]

    for f in range(total):
        t = f / FPS
        angles, alive, balls = frames[min(f, n_sim - 1)]
        in_card = f >= n_sim

        for e in breaks_by_frame.get(f, []):
            col = ring_cols[e.ring]
            parts.ring_burst(t, match.rings[e.ring], "#%02x%02x%02x" % tuple(int(c * 255) for c in col))
            score[e.ball] += 1
            bump[e.ball] = t
            flash[e.ring] = t
        if f == n_sim:
            parts.confetti(t, tcol[match.winner])

        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_surface(layer, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # --- live score between the team names -------------------------------
        for side in (0, 1):
            pop = max(0.0, 1 - (t - bump[side]) / 0.25)
            size = int(64 + 26 * pop)
            x = W / 2 - 70 if side == 0 else W / 2 + 70
            text.draw(cr, str(score[side]), x, 378, size, tcol[side])
        text.draw(cr, "-", W / 2, 372, 48, "#FFFFFF")

        # --- rings: glow + core; the last ring turns red ------------------------
        final_phase = sum(alive) == 1
        for k, ring in enumerate(match.rings):
            if not alive[k]:
                continue
            start = angles[k] + ring.gap_half
            end = angles[k] + 2 * math.pi - ring.gap_half
            col = rgb(FINAL_RED) if final_phase else ring_cols[k]
            for width, alpha in ((sim.RING_THICK + 12, 0.16), (sim.RING_THICK, 1.0)):
                cr.new_sub_path()
                cr.arc(sim.CX, sim.CY, ring.radius, start, end)
                cr.set_source_rgba(*col, alpha)
                cr.set_line_width(width)
                cr.stroke()

        for k, t0 in flash.items():
            age = t - t0
            if 0 <= age < 0.22:
                cr.arc(sim.CX, sim.CY, match.rings[k].radius + age * 140, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.7 * (1 - age / 0.22))
                cr.set_line_width(4)
                cr.stroke()

        parts.draw(cr, t)

        # --- balls + trails ------------------------------------------------------
        for i, (x, y) in enumerate(balls):
            if not in_card:
                trails[i].append((x, y))
                del trails[i][:-14]
            n = len(trails[i])
            for j, (tx, ty) in enumerate(trails[i][:-1]):
                fade = (j + 1) / n
                cr.arc(tx, ty, sim.BALL_R * (0.35 + 0.55 * fade), 0, 2 * math.pi)
                cr.set_source_rgba(*rcol[i], 0.30 * fade)
                cr.fill()
        for i, (x, y) in enumerate(balls):
            flags.ball(cr, codes[i], x, y, sim.BALL_R, border=3.5)

        if not in_card and final_phase:
            pulse = 0.55 + 0.45 * abs(math.sin(t * 6))
            text.draw(cr, "LAST RING", W / 2, sim.CY + sim.OUTER_R + 62, 40, "#FF4D6D", alpha=pulse)

        # --- winner card -----------------------------------------------------------
        if in_card:
            ct = t - win_t
            k = min(1.0, ct / 0.3)
            cr.rectangle(0, 0, W, H)
            cr.set_source_rgba(0.02, 0.02, 0.05, 0.8 * k)
            cr.fill()
            parts.draw(cr, t)
            wcode = codes[match.winner]
            slide = (1 - k) ** 3 * 120
            text.draw(cr, "CHAMPION" if info.get("final") else "ESCAPED FIRST", W / 2, 600 + slide, 44,
                      "#FFD84D", alpha=k)
            flags.rect(cr, wcode, W / 2, 740 + slide, 300, 200, radius=20, border=6, alpha=k)
            text.draw(cr, teams.name(wcode).upper(), W / 2, 945 + slide, 104, "#FFFFFF",
                      alpha=k, max_w=940)
            text.draw(cr, info["result_line"], W / 2, 1010 + slide, 38, "#C9D3F5", bold=True,
                      alpha=k, max_w=940)
            k2 = min(1.0, max(0.0, (ct - 0.4) / 0.35))
            if k2 > 0:
                text.draw(cr, info["next_label"], W / 2, 1130, 32, "#9FB0E6", bold=True, alpha=k2)
                text.draw(cr, info["next_line"], W / 2, 1205, 60, "#FFFFFF", alpha=k2, max_w=960)
                text.draw(cr, info["cta"], W / 2, 1282, 40, "#FFD84D", alpha=k2, max_w=940)

        surf.flush()
        proc.stdin.write(surf.get_data())

    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out_path


# ----------------------------------------------------------------------------
# Escape Royale (variant B, 2026-10-08): 16 countries in one arena.
#
# Why: 1v1 Escape Cup videos held viewers ~5.7s (25%) with 0 comments - two random
# countries give a 91%-UK audience no stake. Every successful country format puts
# many countries on screen at once (@ballbattleroyale: "WHO WILL WIN? / 8 LEFT /
# COMMENT YOUR COUNTRY", 31k-208k views at 464 followers). Same hook box as the 1v1
# so the A/B isolates the format, plus the stake lines from frame 0.
# ----------------------------------------------------------------------------
ROYALE_CARD_SECONDS = 3.4


def _royale_layer(theme, n_countries, text, header):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(s)
    cr.set_source_rgb(*[c * 0.55 for c in rgb(THEMES[theme][0])])
    cr.paint()
    rg = cairo.RadialGradient(sim.CX, sim.CY, 0, sim.CX, sim.CY, 560)
    rg.add_color_stop_rgba(0, *rgb(THEMES[theme][2]), 0.35)
    rg.add_color_stop_rgba(1, *rgb(THEMES[theme][2]), 0.0)
    cr.set_source(rg)
    cr.paint()
    _hook_box(cr, text, HOOK_TEXT, 232)
    text.draw(cr, f"{n_countries} COUNTRIES  ·  1 WAY OUT", W / 2, 338, 44, "#FFFFFF", max_w=940)
    text.draw(cr, "COMMENT YOUR COUNTRY", W / 2, 390, 32, "#FFD84D", max_w=900)
    text.draw(cr, header, W / 2, 1512, 24, "#9AA3BF", bold=True, max_w=900, shadow=False)
    return s


def render_royale(match, info, out_path, workdir):
    """info: codes (list of team codes, ball order), header, theme, league_line,
    next_line, final (bool)."""
    codes = info["codes"]
    n = len(codes)
    flags = Flags()
    text = TextCache()
    parts = Particles(match.seed)
    frames = match.frames
    n_sim = len(frames)
    n_card = int(ROYALE_CARD_SECONDS * FPS)
    total = n_sim + n_card
    win_t = n_sim / FPS
    theme = info.get("theme", "neon")
    n_rings = len(match.rings)
    ring_cols = _ring_colors(theme, n_rings)
    layer = _royale_layer(theme, n, text, info["header"])
    r_ball = match.ball_r
    outer = match.rings[-1].radius
    podium = sim.standings(match)[:3]

    breaks_by_frame = {}
    for e in match.events:
        if e.kind == "break":
            breaks_by_frame.setdefault(int(e.t * FPS), []).append(e)
    score = [0] * n
    flash = {}
    leader = None

    wav = os.path.join(workdir, "audio.wav")
    audio.write_wav(audio.build(match, total / FPS, win_t, match.seed), wav)
    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", wav,
           "-c:v", "libx264", "-preset", "slow", "-tune", "animation", "-crf", "14",
           "-pix_fmt", "yuv420p", "-g", "60",
           "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    trails = [[] for _ in range(n)]
    tcol = [rgb(teams.color(c)) for c in codes]

    for f in range(total):
        t = f / FPS
        angles, alive, balls = frames[min(f, n_sim - 1)]
        in_card = f >= n_sim
        for e in breaks_by_frame.get(f, []):
            col = ring_cols[e.ring]
            parts.ring_burst(t, match.rings[e.ring], "#%02x%02x%02x" % tuple(int(c * 255) for c in col))
            score[e.ball] += 1
            flash[e.ring] = t
            top = max(range(n), key=lambda i: score[i])
            if score[top] > 0 and (leader is None or score[top] > score[leader]):
                leader = top
        if f == n_sim:
            parts.confetti(t, teams.color(codes[match.winner]))

        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_surface(layer, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # live leader line under the stake lines
        if leader is not None and not in_card:
            name = teams.name(codes[leader]).upper()
            flags.rect(cr, codes[leader], W / 2 - 230, 440, 54, 36, radius=6, border=2)
            text.draw(cr, f"LEADING: {name}  {score[leader]}", W / 2 - 192, 456, 34, "#FFFFFF",
                      align="left", max_w=640)

        final_phase = sum(alive) == 1
        for k, ring in enumerate(match.rings):
            if not alive[k]:
                continue
            start = angles[k] + ring.gap_half
            end = angles[k] + 2 * math.pi - ring.gap_half
            col = rgb(FINAL_RED) if final_phase else ring_cols[k]
            for width, alpha in ((sim.RING_THICK + 8, 0.14), (sim.RING_THICK - 1, 1.0)):
                cr.new_sub_path()
                cr.arc(sim.CX, sim.CY, ring.radius, start, end)
                cr.set_source_rgba(*col, alpha)
                cr.set_line_width(width)
                cr.stroke()
        for k, t0 in flash.items():
            age = t - t0
            if 0 <= age < 0.2:
                cr.arc(sim.CX, sim.CY, match.rings[k].radius + age * 120, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.6 * (1 - age / 0.2))
                cr.set_line_width(3)
                cr.stroke()
        parts.draw(cr, t)

        for i, (x, y) in enumerate(balls):
            if not in_card:
                trails[i].append((x, y))
                del trails[i][:-9]
            m_ = len(trails[i])
            for j, (tx, ty) in enumerate(trails[i][:-1]):
                fade = (j + 1) / m_
                cr.arc(tx, ty, r_ball * (0.3 + 0.5 * fade), 0, 2 * math.pi)
                cr.set_source_rgba(*tcol[i], 0.26 * fade)
                cr.fill()
        for i, (x, y) in enumerate(balls):
            flags.ball(cr, codes[i], x, y, r_ball, border=3)

        if not in_card:
            left = sum(alive)
            label = "LAST RING" if final_phase else f"{left} RINGS LEFT"
            alpha = (0.55 + 0.45 * abs(math.sin(t * 6))) if final_phase else 0.9
            text.draw(cr, label, W / 2, sim.CY + outer + 62, 38,
                      "#FF4D6D" if final_phase else "#C9D3F5", alpha=alpha)

        if in_card:
            ct = t - win_t
            k = min(1.0, ct / 0.3)
            cr.rectangle(0, 0, W, H)
            cr.set_source_rgba(0.02, 0.02, 0.05, 0.82 * k)
            cr.fill()
            parts.draw(cr, t)
            wcode = codes[match.winner]
            slide = (1 - k) ** 3 * 120
            text.draw(cr, "ESCAPED FIRST", W / 2, 560 + slide, 44, "#FFD84D", alpha=k)
            flags.rect(cr, wcode, W / 2, 700 + slide, 300, 200, radius=20, border=6, alpha=k)
            text.draw(cr, teams.name(wcode).upper(), W / 2, 905 + slide, 104, "#FFFFFF", alpha=k, max_w=940)
            k2 = min(1.0, max(0.0, (ct - 0.4) / 0.35))
            if k2 > 0:
                for j, (label, idx) in enumerate((("2ND", podium[1]), ("3RD", podium[2]))):
                    cx = W / 2 - 220 + j * 440
                    flags.rect(cr, codes[idx], cx, 1010, 84, 56, radius=8, border=3, alpha=k2)
                    text.draw(cr, f"{label}  {teams.name(codes[idx]).upper()}", cx, 1092, 34, "#C9D3F5",
                              bold=True, alpha=k2, max_w=420)
                text.draw(cr, info["league_line"], W / 2, 1178, 36, "#FFFFFF", alpha=k2, max_w=960)
                text.draw(cr, info["next_line"], W / 2, 1240, 32, "#9FB0E6", bold=True, alpha=k2, max_w=960)
                text.draw(cr, "Your country not in it? Comment it", W / 2, 1306, 38, "#FFD84D",
                          alpha=k2, max_w=940)

        surf.flush()
        proc.stdin.write(surf.get_data())

    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out_path
