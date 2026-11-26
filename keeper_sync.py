"""Put Beat The Keeper's bounces on the beat of the music (Max, 2026-11-26).

The physics is never changed (same match, same score, same winner). Instead the PLAYBACK is
retimed: a smooth time warp speeds the replay up or slows it down a little (speed limited to
SPEED_MIN..SPEED_MAX, never paused or reversed) so that bounces land exactly on 16th notes of
the music and goals land on beats. Picture and sound are warped together, so what you see
still makes the sound you hear; a tap that couldn't be anchored is nudged in the audio only
by at most AUDIO_NUDGE (inside normal lip-sync tolerance).

beat_grid() finds the tempo and phase of the actual music bed (onset autocorrelation, refined
by a comb search); retime() returns a new KMatch with resampled frames and moved events, so
keeper_render draws it unchanged.
"""
import copy
import math

import numpy as np

import keeper as K

SPEED_MIN, SPEED_MAX = 0.8, 1.25     # replay speed limits (sim seconds per video second)
MIN_GAP = 0.045                      # sim s: events closer than this to the last anchor aren't anchored
AUDIO_NUDGE = 0.035                  # s: largest audio-only shift for a tap that couldn't be anchored
BPM_RANGE = (95.0, 160.0)


def _onsets(x, sr, hop):
    """Low-band onset strength (kick and bass hits carry a dance track's pulse)."""
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / sr)
    low = np.fft.irfft(spec / np.sqrt(1 + (f / 180.0) ** 4), len(x))
    n = len(low) // hop
    env = np.sqrt(np.mean(low[: n * hop].reshape(n, hop) ** 2, axis=1))
    env = np.log1p(env / (np.max(env) or 1.0) * 100)
    on = np.maximum(0.0, np.diff(env, prepend=env[0]))
    return on - np.convolve(on, np.ones(21) / 21, mode="same").clip(0)


def beat_grid(x, sr, bpm=None):
    """(period_s, phase_s) of the beat in mono signal x; phase is the first beat time in [0, period).
    bpm: the track's known tempo (credits.json) - then only the phase is searched, which is far
    more reliable on quiet stretches (an intro bed of "Recall" alone measured 108.85 vs 127)."""
    hop = int(sr * 0.005)
    on = _onsets(np.asarray(x, dtype=np.float64), sr, hop)
    rate = sr / hop
    t = np.arange(len(on)) / rate
    if bpm:
        coarse, periods = 60.0 / bpm, [60.0 / bpm]
    else:
        lags = np.arange(int(rate * 60 / BPM_RANGE[1]), int(rate * 60 / BPM_RANGE[0]) + 1)
        ac = np.array([np.dot(on[:-lag], on[lag:]) for lag in lags])
        coarse = lags[int(np.argmax(ac))] / rate
        periods = np.linspace(coarse * 0.985, coarse * 1.015, 121)
    best = (-1.0, coarse, 0.0)
    for period in periods:
        phases = np.arange(0, period, 0.0025)
        k = np.arange(int(t[-1] / period) + 1)
        score = [np.interp(ph + k * period, t, on).sum() for ph in phases]
        j = int(np.argmax(score))
        if score[j] > best[0]:
            best = (score[j], period, phases[j])
    return float(best[1]), float(best[2])


def _snap(lo, hi, want, step, phase):
    """Grid point (phase + k*step) inside [lo, hi] nearest to want, or None."""
    k0, k1 = math.ceil((lo - phase) / step), math.floor((hi - phase) / step)
    if k1 < k0:
        return None
    k = min(max(round((want - phase) / step), k0), k1)
    return phase + k * step


def anchors(match, period, phase):
    """Monotonic (sim_t, video_t) pairs, speed-limited. Pass 1 puts goals (and saves) on beats
    (else 8ths); pass 2 fits bounces onto 16ths between them, only where the next fixed anchor
    can still be reached within the speed limits."""
    def feasible(s0, v0, s1, v1):
        ds, dv = s1 - s0, v1 - v0
        return dv > 0 and ds / SPEED_MAX - 1e-9 <= dv <= ds / SPEED_MIN + 1e-9

    evs = sorted(match.events, key=lambda e: e.t)
    key_ev = [e for e in evs if e.kind in ("break", "save")]
    fixed = [(0.0, 0.0)]
    for e in key_ev:                                       # pass 1: the big moments
        s0, v0 = fixed[-1]
        ds = e.t - s0
        if ds < MIN_GAP:
            continue
        for step in (period, period / 2):
            g = _snap(v0 + ds / SPEED_MAX, v0 + ds / SPEED_MIN, v0 + ds, step, phase)
            if g is not None:
                fixed.append((e.t, g))
                break
    out = [fixed[0]]
    bounces = [e for e in evs if e.kind == "bounce"]
    for (s_next, v_next) in fixed[1:] + [(None, None)]:   # pass 2: bounces between them
        for e in bounces:
            s0, v0 = out[-1]
            if e.t <= s0 + MIN_GAP or (s_next is not None and e.t >= s_next - MIN_GAP):
                continue
            ds = e.t - s0
            lo, hi = v0 + ds / SPEED_MAX, v0 + ds / SPEED_MIN
            if s_next is not None:                         # must still reach the next fixed anchor
                rest = s_next - e.t
                lo, hi = max(lo, v_next - rest / SPEED_MIN), min(hi, v_next - rest / SPEED_MAX)
            # prefer keeping the current replay speed (drifting back to 1x) so motion stays smooth
            spd = 1.0
            if len(out) >= 2 and out[-1][1] > out[-2][1]:
                spd = 0.6 * (out[-1][0] - out[-2][0]) / (out[-1][1] - out[-2][1]) + 0.4
            g = _snap(lo, hi, v0 + ds / spd, period / 4, phase)
            if g is not None and g > v0:
                out.append((e.t, g))
        if s_next is not None:
            if feasible(out[-1][0], out[-1][1], s_next, v_next):
                out.append((s_next, v_next))
    sim, vid = zip(*out)
    return np.array(sim), np.array(vid)


def retime(match, period, phase):
    """A copy of match whose frames/events are on the warped (video) clock."""
    st, vt = anchors(match, period, phase)
    sim_end = len(match.frames) / K.FPS
    # past the last anchor, play at real speed
    st = np.append(st, sim_end + 1.0)
    vt = np.append(vt, vt[-1] + (st[-1] - st[-2]))
    to_vid = lambda s: float(np.interp(s, st, vt))        # noqa: E731
    out = copy.copy(match)
    n_frames = int(round(to_vid(sim_end) * K.FPS))
    frames = []
    src = match.frames
    for f in range(n_frames):
        s = float(np.interp(f / K.FPS, vt, st)) * K.FPS     # sim frame index (fractional)
        i = min(int(s), len(src) - 1)
        j = min(i + 1, len(src) - 1)
        a = s - i
        kx = src[i][0] * (1 - a) + src[j][0] * a
        balls = []
        for (x0, y0, n0), (x1, y1, _) in zip(src[i][1], src[j][1]):
            if math.hypot(x1 - x0, y1 - y0) > 120:          # a re-drop: don't smear across it
                balls.append((x0, y0, n0))
            else:
                balls.append((x0 + (x1 - x0) * a, y0 + (y1 - y0) * a, n0))
        frames.append((kx, balls))
    out.frames = frames
    out.events = [copy.copy(e) for e in match.events]
    for e in out.events:
        e.t = to_vid(e.t)
    out.duration = to_vid(match.duration)
    out.sync = {"period": period, "phase": phase, "anchors": len(st) - 2,
                "speed_min": float(np.min(np.diff(st[:-1]) / np.maximum(np.diff(vt[:-1]), 1e-9))) if len(st) > 2 else 1.0,
                "speed_max": float(np.max(np.diff(st[:-1]) / np.maximum(np.diff(vt[:-1]), 1e-9))) if len(st) > 2 else 1.0}
    return out


def nudge(t, period, phase):
    """Audio-only: move t onto the nearest 16th if it is within AUDIO_NUDGE of it."""
    step = period / 4
    g = phase + round((t - phase) / step) * step
    return g if abs(g - t) <= AUDIO_NUDGE else t


def on_grid_share(match, period, phase, tol=0.012):
    """Share of tap events (bounces/goals/saves) within tol of a 16th note."""
    step = period / 4
    ts = [e.t for e in match.events if e.kind in ("bounce", "break", "save")]
    if not ts:
        return 0.0
    off = [abs(((t - phase) / step - round((t - phase) / step)) * step) for t in ts]
    return sum(o <= tol for o in off) / len(ts)
