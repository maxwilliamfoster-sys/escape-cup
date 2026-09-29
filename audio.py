"""
Soundtrack for a match, built from the physics events.

Every wall bounce plays the NEXT note of a melody, so the match "plays" a tune
at the rhythm the balls set - the signature of the bouncing-ball genre. A ring
break is a bright bell chord plus a glassy noise burst; the win is a fanfare.

All melodies are public-domain compositions (pre-1929 / folk). Everything is
synthesised in numpy: no samples, no Content ID exposure.
"""
import wave

import numpy as np

SR = 44100

# MIDI note lists. Transcribed melodies only - rhythm comes from the bounces.
MELODIES = {
    "Ode to Joy": [64, 64, 65, 67, 67, 65, 64, 62, 60, 60, 62, 64, 64, 62, 62,
                   64, 64, 65, 67, 67, 65, 64, 62, 60, 60, 62, 64, 62, 60, 60,
                   62, 62, 64, 60, 62, 64, 65, 64, 60, 62, 64, 65, 64, 62, 60, 62, 55,
                   64, 64, 65, 67, 67, 65, 64, 62, 60, 60, 62, 64, 62, 60, 60],
    "Fur Elise": [76, 75, 76, 75, 76, 71, 74, 72, 69, 60, 64, 69, 71, 64, 68, 71, 72,
                  64, 76, 75, 76, 75, 76, 71, 74, 72, 69, 60, 64, 69, 71, 64, 72, 71, 69],
    "Mountain King": [57, 59, 60, 62, 64, 60, 64, 63, 59, 63, 62, 58, 62,
                      57, 59, 60, 62, 64, 60, 64, 69, 67, 64, 60, 64, 67],
    "Korobeiniki": [76, 71, 72, 74, 72, 71, 69, 69, 72, 76, 74, 72, 71, 72, 74, 76, 72, 69, 69,
                    74, 77, 81, 79, 77, 76, 72, 76, 74, 72, 71, 71, 72, 74, 76, 72, 69, 69],
    "Canon": [78, 76, 74, 73, 71, 69, 71, 73, 74, 73, 71, 69, 67, 66, 67, 64,
              66, 69, 67, 66, 64, 62, 64, 66, 67, 69, 71, 73, 74],
    "Twinkle": [60, 60, 67, 67, 69, 69, 67, 65, 65, 64, 64, 62, 62, 60,
                67, 67, 65, 65, 64, 64, 62, 67, 67, 65, 65, 64, 64, 62,
                60, 60, 67, 67, 69, 69, 67, 65, 65, 64, 64, 62, 62, 60],
    "Kleine Nachtmusik": [67, 62, 67, 62, 67, 62, 67, 71, 74, 72, 69, 72, 69, 72, 69, 66, 69, 62,
                          67, 67, 71, 69, 67, 67, 66, 66, 69, 72, 66, 69, 67],
    "The Entertainer": [62, 63, 64, 72, 64, 72, 64, 72, 72, 74, 75, 76, 72, 74, 76, 71, 74, 72,
                        62, 63, 64, 72, 64, 72, 64, 72, 69, 67, 66, 69, 72, 76, 74, 72, 69, 74],
}
MELODY_NAMES = sorted(MELODIES)


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


def _tone(freq, dur, partials, attack=0.004):
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for ratio, amp, decay in partials:
        f = freq * ratio
        if f < SR / 2:
            out += amp * np.sin(2 * np.pi * f * t) * np.exp(-decay * t)
    env = np.minimum(1.0, t / attack)
    return out * env


PIANO = [(1, 1.0, 3.2), (2, 0.42, 5.0), (3, 0.16, 7.5), (4, 0.06, 10.0), (5, 0.03, 13.0)]
BELL = [(1, 1.0, 2.2), (2.76, 0.45, 3.2), (5.40, 0.22, 4.5), (8.93, 0.08, 6.0)]


def _add(buf, start, clip, gain):
    i = int(start * SR)
    if i >= len(buf):
        return
    n = min(len(clip), len(buf) - i)
    buf[i:i + n] += clip[:n] * gain


def _shatter(rng):
    n = int(0.45 * SR)
    t = np.arange(n) / SR
    noise = rng.standard_normal(n)
    # crude high-pass: difference of noise, then shimmering decay
    hp = np.diff(noise, prepend=0.0)
    return hp * np.exp(-9 * t) * 0.35


def build(match, melody_name, total_seconds, win_time, rng_seed=0):
    rng = np.random.default_rng(rng_seed)
    buf = np.zeros(int((total_seconds + 0.5) * SR))
    notes = MELODIES[melody_name]
    idx = 0
    cache = {}
    for e in match.events:
        if e.kind == "bounce":
            midi = notes[idx % len(notes)] + 12      # up an octave: brighter, cuts through phone speakers
            idx += 1
            if midi not in cache:
                cache[midi] = _tone(_hz(midi), 1.3, PIANO)
            _add(buf, e.t, cache[midi], 0.30)
        elif e.kind == "clash":
            _add(buf, e.t, _tone(_hz(96), 0.12, [(1, 1.0, 40), (1.5, 0.5, 50)]), 0.12)
        elif e.kind == "break":
            root = 72 + (e.ring * 2)
            for k, iv in enumerate((0, 4, 7, 12)):
                _add(buf, e.t + k * 0.035, _tone(_hz(root + iv), 1.6, BELL), 0.13)
            _add(buf, e.t, _shatter(rng), 1.0)

    # win fanfare: rising major arpeggio, then a held chord
    root = 67
    for k, iv in enumerate((0, 4, 7, 12, 16)):
        _add(buf, win_time + 0.10 + k * 0.09, _tone(_hz(root + iv), 1.2, PIANO), 0.22)
    for iv in (0, 4, 7, 12):
        _add(buf, win_time + 0.62, _tone(_hz(root + iv), 2.6, BELL), 0.12)

    # small room: a few feedback-free early reflections, so notes don't sound dry
    wet = np.zeros_like(buf)
    for delay, g in ((0.029, 0.30), (0.047, 0.22), (0.071, 0.16), (0.113, 0.10)):
        d = int(delay * SR)
        wet[d:] += buf[:-d] * g
    out = buf + wet

    peak = np.max(np.abs(out)) or 1.0
    out = out / peak * 0.89
    # gentle soft-clip keeps dense passages from sounding harsh
    out = np.tanh(out * 1.2) / np.tanh(1.2)
    return out[: int(total_seconds * SR)]


def write_wav(samples, path):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())
