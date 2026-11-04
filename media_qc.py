"""Rule-based video and audio checks on a finished video (growth-lab MASTER, vendored like
upload_gate. Edit here, bump QC_VERSION, then roll out with `py sync_vendored.py --bot <name>`).

    report = media_qc.check(path, bot="chess")
    print(media_qc.log_line(report, path))     # one "[qc] {...}" line, greppable in the Actions logs
    if not report["ok"]: ...a check could not run, treat it as a block...

One FFmpeg pass plus small NumPy checks, nothing else to install:
    black, freeze, silence, integrated loudness, true peak, clipping, sample rate,
    audio shorter than video, decode errors, and a flash ESTIMATE.

Warn-only rollout: every rule starts as a warning (report["warnings"]) and never blocks. Promote a
rule by adding its name to ENFORCED once its warnings have been reviewed for about 2 weeks. A check
that cannot run is always a failure (report["failures"]): a QC error must never read as a pass.

Not checked here on purpose: harshness (limits need Max's ear first; Claude cannot hear) and voice
quality. These checks add to the AI reviews and legal gates and replace none of them.

The flash figure follows the WCAG general-flash wording on a coarse grid. It is a risk estimate,
not a compliance result: it does not check red flashes or patterns, and nobody has validated it.

Thresholds: only loudness and true peak have published standards behind them (EBU R 128, -1 dBTP);
48 kHz is YouTube's recommendation; black, freeze, silence and clipping limits are editorial starting
points, tuned per bot in BOT_RULES against real videos.
Evidence: what-if-shorts/research_notes/Open source video audio checking/rule_based_checks_and_bot_fit.md
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

QC_VERSION = "2026-11-04"

# Rules that block. Empty during the warn-only period (started 2026-11-04, review about 2026-11-18).
ENFORCED = frozenset()

DEFAULT_RULES = {
    "audio": True,               # False = a format with no sound; audio rules are skipped
    "lufs": (-17.0, -12.0),      # integrated loudness window
    "max_true_peak": -1.0,       # dBTP (EBU R 128 ceiling); upload_gate still blocks at +0.5
    "max_black": 0.4,            # seconds in one stretch; any black in the first second also warns
    "max_freeze": 3.0,           # seconds of identical picture in one stretch
    "max_silence": 3.0,          # seconds under -50 dB
    "sample_rate": 48000,        # YouTube's recommended audio rate
    "max_audio_short": 0.1,      # seconds the audio may end before the picture
    "max_flash": 3.0,            # flashes in any one second (estimate)
}

# Measured against each bot's real videos on 2026-11-04. A format that holds still or goes quiet on
# purpose gets its own allowance here, so its warnings stay worth reading.
BOT_RULES = {
    "chess": {"lufs": (-16.0, -12.0),
              "max_freeze": 6.0},        # holds the board still on purpose (3.3-3.9 s measured); gate limit 6 s
    "escape": {"lufs": (-17.0, -15.0)},
    "space": {"lufs": (-17.0, -11.0)},   # same window as spacepipe/qc.py
    "quiz": {"lufs": (-16.0, -12.0)},
    "bcf": {"max_audio_short": 0.6},     # the picture runs 0.5 s past the narration by design
    "whatif": {},
}

BLACK_FILTER = "blackdetect=d=0.1:pix_th=0.05"
FREEZE_FILTER = "freezedetect=n=0.001:d=0.8"
SILENCE_FILTER = "silencedetect=n=-50dB:d=1"
GRID_W, GRID_H = 36, 64          # about 30 px cells on a 1080x1920 frame

# WCAG 2.x general flash: a pair of opposing relative-luminance changes of at least 10% of the
# maximum, where the darker image is below 0.80.
FLASH_SWING = 0.10
FLASH_DARK = 0.80
FLASH_AREA = 0.25                # share of the screen, the rough WCAG reading
FLASH_AREA_STRICT = 0.10         # a 10-degree field on a phone may be nearer this (unconfirmed)

CLIP_LEVEL = 0.98                # about -0.2 dBFS: a lossy codec ripples around a clipped top
CLIP_RUN = 3                     # consecutive samples at that level make one flat top

_DECODE_ERROR = re.compile(r"\b(error|corrupt|invalid data|non-existing PPS)\b", re.I)


class QCError(RuntimeError):
    """A check could not run, or its output could not be read."""


def ffmpeg_bin():
    for c in (os.environ.get("FFMPEG_BIN"), shutil.which("ffmpeg"),
              r"C:\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"):
        if c and os.path.exists(c):
            return c
    return "ffmpeg"


def ffprobe_bin(ffmpeg=None):
    d, name = os.path.split(ffmpeg or ffmpeg_bin())
    cand = os.path.join(d, name.replace("ffmpeg", "ffprobe")) if d else ""
    return cand if cand and os.path.exists(cand) else (shutil.which("ffprobe") or "ffprobe")


def rules_for(bot=None, **overrides):
    if bot is not None and bot not in BOT_RULES:
        raise QCError(f"unknown bot {bot!r}")
    unknown = set(overrides) - set(DEFAULT_RULES)
    if unknown:
        raise QCError(f"unknown rule(s) {sorted(unknown)}")
    return {**DEFAULT_RULES, **BOT_RULES.get(bot, {}), **overrides}


# --------------------------------------------------------------------- parsing --
def _longest(durations, starts, duration):
    """The longest stretch, counting one still running at the end (reported without a duration)."""
    durations = list(durations)
    if len(starts) > len(durations):
        durations.append(max(0.0, duration - starts[-1]))
    return round(max(durations, default=0.0), 2)


def parse_log(log, *, has_audio, duration):
    """The measures in FFmpeg's log. Raises QCError when an expected measure is missing."""
    blacks = [float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", log)]
    m = {"longest_black": round(max(blacks, default=0.0), 2),
         "black_starts": [float(x) for x in re.findall(r"black_start:\s*([\d.]+)", log)],
         "longest_freeze": _longest([float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", log)],
                                    [float(x) for x in re.findall(r"freeze_start:\s*([\d.]+)", log)], duration),
         "decode_errors": sum(1 for ln in log.splitlines()
                              if _DECODE_ERROR.search(ln) and "error concealment" not in ln.lower())}
    if not has_audio:
        return m
    _, found, summary = log.rpartition("Summary:")
    lufs = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    peak = re.search(r"Peak:\s+(\S+) dBFS", summary)
    if not found or not lufs:
        raise QCError("no loudness summary in the FFmpeg log")
    if not peak:
        raise QCError("no true peak in the FFmpeg log")
    m["lufs"] = float(lufs.group(1))
    try:
        m["true_peak"] = float(peak.group(1))
    except ValueError:
        m["true_peak"] = None                  # "-inf" on a silent track
    else:
        if m["true_peak"] in (float("inf"), float("-inf")):
            m["true_peak"] = None
    m["longest_silence"] = _longest([float(x) for x in re.findall(r"silence_duration:\s*([\d.]+)", log)],
                                    [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", log)], duration)
    return m


# ----------------------------------------------------------------- NumPy checks --
def relative_luminance(rgb):
    """Relative luminance (0-1) of a uint8 (..., 3) array, using the sRGB curve."""
    import numpy as np
    c = np.arange(256, dtype=np.float32) / 255.0
    lut = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)
    linear = lut[rgb]
    return 0.2126 * linear[..., 0] + 0.7152 * linear[..., 1] + 0.0722 * linear[..., 2]


def flash_estimate(lum, fps, area):
    """Highest flashes-per-second in any one-second window, over at least `area` of the cells.
    lum: float array (frames, cells). An estimate only; see the module docstring."""
    import numpy as np
    n, cells = lum.shape
    if n < 3 or cells == 0:
        return 0.0
    direction = np.zeros(cells, np.int8)       # 0 unknown, 1 last change went up, -1 went down
    hi, lo = lum[0].copy(), lum[0].copy()
    changes = np.zeros((n, cells), bool)
    for t in range(1, n):
        cur = lum[t]
        hi = np.maximum(hi, cur)
        lo = np.minimum(lo, cur)
        up = (direction <= 0) & (cur - lo >= FLASH_SWING) & (lo < FLASH_DARK)
        down = (direction >= 0) & (hi - cur >= FLASH_SWING) & (cur < FLASH_DARK) & ~up
        changes[t] = up | down
        direction[up] = 1
        direction[down] = -1
        hi = np.where(up, cur, hi)
        lo = np.where(down, cur, lo)
    window = max(1, int(round(fps)))
    total = np.cumsum(changes, axis=0, dtype=np.int16)
    counts = total[-1:] if n <= window else total[window:] - total[:-window]
    k = max(1, int(np.ceil(area * cells)))     # the cell at this rank sets the rate for that area
    per_window = np.partition(counts, cells - k, axis=1)[:, cells - k]
    return float(int(per_window.max()) // 2)   # a flash is a PAIR of opposing changes


def clipped_runs(samples):
    """Flat tops: runs of CLIP_RUN or more samples at full scale, summed over channels (frames, ch)."""
    import numpy as np
    runs = 0
    for ch in range(samples.shape[1]):
        x = samples[:, ch]
        for at_top in (x >= CLIP_LEVEL, x <= -CLIP_LEVEL):
            if not at_top.any():
                continue
            edges = np.diff(np.concatenate(([0], at_top.astype(np.int8), [0])))
            lengths = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
            runs += int((lengths >= CLIP_RUN).sum())
    return runs


# ------------------------------------------------------------------- the pass --
def _probe(path, ffprobe):
    r = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, timeout=120)
    if r.returncode != 0:
        raise QCError(f"ffprobe failed: {r.stderr.decode(errors='replace').strip()[-160:]}")
    return json.loads(r.stdout.decode(errors="replace"))


def _end(stream, fallback=None):
    """Where a stream ends (start + duration), or `fallback` when the container does not say."""
    try:
        return float(stream.get("start_time") or 0) + float(stream["duration"])
    except (KeyError, TypeError, ValueError):
        return fallback


def _fps(stream):
    num, _, den = (stream.get("avg_frame_rate") or "0/1").partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _run_pass(path, ffmpeg, has_audio, grid_path, audio_path):
    graph = f"[0:v:0]{BLACK_FILTER},{FREEZE_FILTER},scale={GRID_W}:{GRID_H}:flags=area,format=rgb24[grid]"
    args = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-v", "info", "-y", "-i", path]
    outputs = ["-map", "[grid]", "-f", "rawvideo", grid_path]
    if has_audio:
        # ebur128 resamples what it is given, so the raw samples come from their own branch.
        graph += (f";[0:a:0]asplit[meter][raw];[meter]ebur128=peak=true:framelog=verbose,{SILENCE_FILTER},anullsink"
                  f";[raw]aformat=sample_fmts=flt[audio]")
        outputs += ["-map", "[audio]", "-f", "f32le", audio_path]
    r = subprocess.run(args + ["-filter_complex", graph] + outputs, capture_output=True, timeout=900)
    log = r.stderr.decode(errors="replace")
    if r.returncode != 0:
        last = log.strip().splitlines()[-1] if log.strip() else "no output"
        raise QCError(f"ffmpeg exit {r.returncode}: {last.replace(path, '<video>')[:160]}")
    return log


def _measure(path, ffmpeg, ffprobe, rules):
    import numpy as np
    info = _probe(path, ffprobe)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v:
        raise QCError("no video stream")
    duration = float((info.get("format") or {}).get("duration") or v.get("duration") or 0)
    fps = _fps(v)
    has_audio = bool(a) and bool(rules["audio"])
    with tempfile.TemporaryDirectory(prefix="media_qc_") as tmp:
        grid_path, audio_path = os.path.join(tmp, "grid.rgb"), os.path.join(tmp, "audio.f32")
        log = _run_pass(path, ffmpeg, has_audio, grid_path, audio_path)
        grid = np.fromfile(grid_path, np.uint8)
        samples = np.fromfile(audio_path, np.float32) if has_audio else None
    m = parse_log(log, has_audio=has_audio, duration=duration)
    m["has_audio_stream"] = bool(a)
    m["sample_rate"] = int(a["sample_rate"]) if a and a.get("sample_rate") else None

    frames = grid.size // (GRID_W * GRID_H * 3)
    expected = int(v.get("nb_frames") or 0) or int(fps * duration)
    if frames < 2 or (expected and frames < expected * 0.9):
        raise QCError(f"decoded only {frames} of about {expected} frames")
    lum = relative_luminance(grid[:frames * GRID_W * GRID_H * 3].reshape(frames, -1, 3))
    rate = fps or frames / max(duration, 0.01)
    m["flash_est_per_s"] = flash_estimate(lum, rate, FLASH_AREA)
    m["flash_est_strict_per_s"] = flash_estimate(lum, rate, FLASH_AREA_STRICT)

    if has_audio:
        channels = int(a.get("channels") or 0)
        if channels < 1 or m["sample_rate"] is None or samples.size < channels:
            raise QCError("no audio samples decoded")
        pcm = samples[:samples.size // channels * channels].reshape(-1, channels)
        decoded = pcm.shape[0] / m["sample_rate"]
        said = float(a["duration"]) if a.get("duration") else decoded
        if decoded < said * 0.9:
            raise QCError(f"decoded only {decoded:.1f}s of {said:.1f}s of audio")
        m["clipped_runs"] = clipped_runs(pcm)
        video_end = _end(v, frames / rate)
        audio_end = _end(a, decoded)
        m["audio_short_by"] = round(max(0.0, video_end - audio_end), 3)
    return m


def _judge(m, rules):
    """(rule, message) for every rule the measures break. Fixed words and numbers only: these lines
    reach the public ledgers."""
    flags = []
    add = lambda rule, text: flags.append((rule, text))
    if m["longest_black"] > rules["max_black"]:
        add("black", f"{m['longest_black']:.2f}s of black picture (limit {rules['max_black']}s)")
    if any(t < 1.0 for t in m["black_starts"]):
        add("black_open", "black picture in the first second")
    if m["longest_freeze"] > rules["max_freeze"]:
        add("freeze", f"{m['longest_freeze']:.2f}s of frozen picture (limit {rules['max_freeze']}s)")
    if m["decode_errors"]:
        add("decode_errors", f"{m['decode_errors']} decode error line(s)")
    if not m["has_audio_stream"]:
        if rules["audio"]:
            add("no_audio", "no audio stream")
    elif rules["audio"]:
        low, high = rules["lufs"]
        if not low <= m["lufs"] <= high:
            add("loudness", f"{m['lufs']} LUFS outside {low} to {high}")
        if m["true_peak"] is not None and m["true_peak"] > rules["max_true_peak"]:
            add("true_peak", f"{m['true_peak']} dBTP above {rules['max_true_peak']}")
        if m["clipped_runs"]:
            add("clipping", f"{m['clipped_runs']} flat-topped run(s) at full scale")
        if m["longest_silence"] > rules["max_silence"]:
            add("silence", f"{m['longest_silence']:.2f}s of silence (limit {rules['max_silence']}s)")
        if m["sample_rate"] != rules["sample_rate"]:
            add("sample_rate", f"{m['sample_rate']} Hz audio, expected {rules['sample_rate']}")
        if m["audio_short_by"] > rules["max_audio_short"]:
            add("audio_short", f"audio ends {m['audio_short_by']:.2f}s before the picture")
    worst = max(m["flash_est_per_s"], m["flash_est_strict_per_s"])
    if worst > rules["max_flash"]:
        area = FLASH_AREA if m["flash_est_per_s"] == worst else FLASH_AREA_STRICT
        add("flash_estimate", f"about {worst:g} flashes a second over {area:.0%} of the screen "
                              f"(limit {rules['max_flash']:g}) - an estimate, not a compliance result")
    return flags


def check(path, *, bot=None, ffmpeg=None, ffprobe=None, enforce=(), **overrides):
    """Measure one finished video. Never raises for a bad file or a broken tool: anything that stops
    a check from running is returned as a failure.

    Returns {ok, failures, warnings, flags: [{rule, message}], metrics, version}. `ok` is False only
    when a check could not run or a rule in ENFORCED (or `enforce`) was broken."""
    started = time.perf_counter()
    report = {"ok": False, "failures": [], "warnings": [], "flags": [], "metrics": {}, "version": QC_VERSION}
    try:
        rules = rules_for(bot, **overrides)
        ffmpeg = ffmpeg or ffmpeg_bin()
        m = _measure(path, ffmpeg, ffprobe or ffprobe_bin(ffmpeg), rules)
    except Exception as exc:                   # fail-closed: a check that can't run blocks
        detail = str(exc).replace(str(path), "<video>")[:200]
        report["failures"].append(f"media QC check could not run: {detail}")
        report["metrics"]["seconds_taken"] = round(time.perf_counter() - started, 2)
        return report
    blocking = ENFORCED | frozenset(enforce)
    for rule, text in _judge(m, rules):
        line = f"QC {rule}: {text}"
        report["flags"].append({"rule": rule, "message": line})
        (report["failures"] if rule in blocking else report["warnings"]).append(line)
    m.pop("black_starts", None)
    m["seconds_taken"] = round(time.perf_counter() - started, 2)
    report["metrics"] = m
    report["ok"] = not report["failures"]
    return report


def log_line(report, name=""):
    """One line for the Actions log: `gh run view --log | grep "\\[qc\\]"` is the warn-only review."""
    return "[qc] " + json.dumps({"file": os.path.basename(str(name)), "ok": report["ok"],
                                 "flags": [f["rule"] for f in report["flags"]],
                                 "failures": report["failures"], "metrics": report["metrics"],
                                 "version": report["version"]}, default=str)


def main(argv=None):
    """py media_qc.py <video> [--bot name]"""
    import argparse
    ap = argparse.ArgumentParser(description="Rule-based QC of a finished video (warn-only rollout)")
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--bot", choices=sorted(BOT_RULES))
    a = ap.parse_args(argv)
    bad = 0
    for path in a.videos:
        report = check(path, bot=a.bot)
        print(log_line(report, path))
        bad += not report["ok"]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
