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
CARD_SECONDS = 3.6
HOOK_SECONDS = 3.0
ROOT = os.path.dirname(os.path.abspath(__file__))
FONT_BLACK = os.path.join(ROOT, "assets", "fonts", "Montserrat-900.ttf")
FONT_BOLD = os.path.join(ROOT, "assets", "fonts", "Montserrat-800.ttf")

RING_COLORS = ["#FF4D6D", "#FF9F1C", "#FFE14D", "#3DDC97", "#4CC9F0", "#B388FF"]
FINAL_RED = "#FF3355"


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
        n = 72
        for k in range(n):
            a = 2 * math.pi * k / n + self.rng.uniform(-0.03, 0.03)
            spd = self.rng.uniform(80, 380)
            self.items.append([t, sim.CX + math.cos(a) * ring.radius, sim.CY + math.sin(a) * ring.radius,
                               math.cos(a) * spd, math.sin(a) * spd, self.rng.uniform(0.5, 1.0),
                               rgb(color), self.rng.uniform(4, 9), "shard", 0])

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


def _background():
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(s)
    g = cairo.LinearGradient(0, 0, 0, H)
    g.add_color_stop_rgb(0, *rgb("#0A0F24"))
    g.add_color_stop_rgb(0.5, *rgb("#161B3D"))
    g.add_color_stop_rgb(1, *rgb("#0A0F24"))
    cr.set_source(g)
    cr.paint()
    rg = cairo.RadialGradient(sim.CX, sim.CY, 0, sim.CX, sim.CY, 640)
    rg.add_color_stop_rgba(0, *rgb("#2A3570"), 0.55)
    rg.add_color_stop_rgba(1, *rgb("#2A3570"), 0.0)
    cr.set_source(rg)
    cr.paint()
    return s


def _header_layer(base, codes, text, flags, top_line, top_color, top_bold):
    """Background + the static header, baked once."""
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(s)
    cr.set_source_surface(base, 0, 0)
    cr.paint()
    text.draw(cr, top_line, W / 2, 205, 44 if not top_bold else 30, top_color, bold=top_bold,
              max_w=920)
    for side, code in enumerate(codes):
        cx = 250 if side == 0 else 830
        flags.rect(cr, code, cx, 290, 132, 88)
        text.draw(cr, teams.name(code).upper(), cx, 392, 50, "#FFFFFF", max_w=400)
    text.draw(cr, "VS", W / 2, 312, 58, "#FFFFFF")
    return s


def _stroke_arc(cr, r, start, end, color, width, alpha):
    cr.new_sub_path()
    cr.arc(sim.CX, sim.CY, r, start, end)
    cr.set_source_rgba(*rgb(color), alpha)
    cr.set_line_width(width)
    cr.stroke()


def render_match(match, info, out_path, workdir):
    """info: a, b (team codes), header, track (tracks.json entry), result_line, next_label,
    next_line, cta, final (bool)."""
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

    base = _background()
    hook_bg = _header_layer(base, codes, text, flags, "WHO BREAKS OUT FIRST?", "#FFD84D", False)
    main_bg = _header_layer(base, codes, text, flags, info["header"], "#9FB0E6", True)

    breaks_by_frame = {}
    for e in match.events:
        if e.kind == "break":
            breaks_by_frame.setdefault(int(e.t * FPS), []).append(e)
    popups = []
    ring_owner = [None] * sim.N_RINGS
    score = [0, 0]
    flash = {}

    wav = os.path.join(workdir, "audio.wav")
    audio.write_wav(audio.build(match, info["track"], total / FPS, win_t, match.seed), wav)

    cmd = [ffmpeg_bin(), "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", wav,
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
    cr = cairo.Context(surf)
    trails = [[], []]
    rcol = [rgb(teams.color(c)) for c in codes]

    for f in range(total):
        t = f / FPS
        angles, alive, balls = frames[min(f, n_sim - 1)]
        in_card = f >= n_sim

        for e in breaks_by_frame.get(f, []):
            parts.ring_burst(t, match.rings[e.ring], RING_COLORS[e.ring])
            ring_owner[e.ring] = e.ball
            score[e.ball] += 1
            flash[e.ring] = t
            popups.append((t, e.x, e.y - 50, f"+1 {teams.name(codes[e.ball]).upper()}",
                           teams.color(codes[e.ball])))
        if f == n_sim:
            parts.confetti(t, teams.color(codes[match.winner]))

        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_surface(hook_bg if t < HOOK_SECONDS else main_bg, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)

        # --- rings: soft glow (wide translucent strokes) + solid core ------
        final_phase = sum(alive) == 1
        for k, ring in enumerate(match.rings):
            if not alive[k]:
                continue
            start = angles[k] + ring.gap_half
            end = angles[k] + 2 * math.pi - ring.gap_half
            col = FINAL_RED if final_phase else RING_COLORS[k]
            _stroke_arc(cr, ring.radius, start, end, col, sim.RING_THICK + 22, 0.10)
            _stroke_arc(cr, ring.radius, start, end, col, sim.RING_THICK + 11, 0.22)
            _stroke_arc(cr, ring.radius, start, end, col, sim.RING_THICK, 1.0)

        for k, t0 in flash.items():
            age = t - t0
            if 0 <= age < 0.25:
                cr.arc(sim.CX, sim.CY, match.rings[k].radius + age * 120, 0, 2 * math.pi)
                cr.set_source_rgba(1, 1, 1, 0.8 * (1 - age / 0.25))
                cr.set_line_width(6)
                cr.stroke()

        parts.draw(cr, t)

        # --- balls + trails ------------------------------------------------
        for i, (x, y) in enumerate(balls):
            if not in_card:
                trails[i].append((x, y))
                del trails[i][:-16]
            n = len(trails[i])
            for j, (tx, ty) in enumerate(trails[i][:-1]):
                fade = (j + 1) / n
                cr.arc(tx, ty, sim.BALL_R * (0.35 + 0.55 * fade), 0, 2 * math.pi)
                cr.set_source_rgba(*rcol[i], 0.28 * fade)
                cr.fill()
        for i, (x, y) in enumerate(balls):
            cr.arc(x, y + 5, sim.BALL_R + 2, 0, 2 * math.pi)
            cr.set_source_rgba(0, 0, 0, 0.35)
            cr.fill()
            flags.ball(cr, codes[i], x, y, sim.BALL_R)

        for t0, x, y, s, col in popups:
            age = t - t0
            if 0 <= age < 1.0:
                alpha = 1 - max(0, age - 0.6) / 0.4
                text.draw(cr, s, min(max(x, 200), W - 200), y - age * 60, 38, col, alpha=alpha)

        # --- scoreboard ------------------------------------------------------
        if not in_card and final_phase:
            pulse = 0.55 + 0.45 * abs(math.sin(t * 5))
            text.draw(cr, "FINAL RING - NEXT BREAK WINS", W / 2, 1382, 34, "#FF4D6D",
                      alpha=pulse, max_w=900)
        elif not in_card:
            text.draw(cr, "RINGS BROKEN", W / 2, 1382, 28, "#9FB0E6", bold=True)
        slot_w = 56
        x0 = W / 2 - slot_w * (sim.N_RINGS - 1) / 2
        for k in range(sim.N_RINGS):
            x = x0 + k * slot_w
            if ring_owner[k] is None:
                cr.arc(x, 1432, 17, 0, 2 * math.pi)
                cr.set_source_rgba(*rgb(RING_COLORS[k]), 0.9)
                cr.set_line_width(4)
                cr.stroke()
            else:
                flags.ball(cr, codes[ring_owner[k]], x, 1432, 19, border=3)
        text.draw(cr, str(score[0]), 150, 1458, 76, teams.color(a))
        text.draw(cr, str(score[1]), 930, 1458, 76, teams.color(b))

        # --- winner card -------------------------------------------------------
        if in_card:
            ct = t - win_t
            k = min(1.0, ct / 0.35)
            cr.rectangle(0, 0, W, H)
            cr.set_source_rgba(*rgb("#050814"), 0.8 * k)
            cr.fill()
            parts.draw(cr, t)
            wcode = codes[match.winner]
            slide = (1 - k) ** 3 * 120
            text.draw(cr, "CHAMPION" if info.get("final") else "WINNER", W / 2, 560 + slide, 40,
                      "#FFD84D", alpha=k)
            flags.rect(cr, wcode, W / 2, 700 + slide, 300, 200, radius=20, border=6, alpha=k)
            text.draw(cr, teams.name(wcode).upper(), W / 2, 905 + slide, 104, "#FFFFFF",
                      alpha=k, max_w=940)
            text.draw(cr, info["result_line"], W / 2, 972 + slide, 38, "#C9D3F5", bold=True,
                      alpha=k, max_w=940)
            k2 = min(1.0, max(0.0, (ct - 0.5) / 0.4))
            if k2 > 0:
                cr.rectangle(W / 2 - 160, 1030, 320, 3)
                cr.set_source_rgba(1, 1, 1, 0.3 * k2)
                cr.fill()
                text.draw(cr, info["next_label"], W / 2, 1110, 32, "#9FB0E6", bold=True, alpha=k2)
                text.draw(cr, info["next_line"], W / 2, 1185, 60, "#FFFFFF", alpha=k2, max_w=960)
                text.draw(cr, info["cta"], W / 2, 1262, 40, "#FFD84D", alpha=k2, max_w=940)

        surf.flush()
        proc.stdin.write(surf.get_data())

    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out_path
