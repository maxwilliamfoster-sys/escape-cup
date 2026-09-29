"""
ASMR-style soundtrack for a match - no music, just soft collision sounds.

History (all user feedback): v1 fixed melody, octave up, hard attack = "sharp and
annoying"; v2 added CC0 background music = not wanted; v3 crisp click taps +
glassy clacks = "still too sharp, hurting my ears". v4 (this):

  - bounce = a felt-mallet tone in the 130-440 Hz range with a 9 ms swell, over
    a low padded thud; loudness follows the real impact speed,
  - two balls touching = a soft low wooden knock (~420 Hz),
  - ring breaking = a few low padded puffs around the ring plus a warm tone,
  - win = a slow soft rising chime,
  - every sound panned to where it happens on screen,
  - the whole mix is steeply low-passed at 1.8 kHz: nothing sharp survives.

Everything is synthesised in numpy: no samples, nothing to license.
"""
import random
import wave

import numpy as np

SR = 44100
OUT_PEAK = 0.89
LIFT = 0.92                                # ~-16 LUFS: soft sounds read as calm, not loud
KNEE = 0.70
KEYS = [48, 50, 51, 53, 55, 56, 57]       # C, D, Eb, F, G, Ab, A - varies per video


def _band(noise, lo, hi):
    spec = np.fft.rfft(noise)
    f = np.fft.rfftfreq(len(noise), 1 / SR)
    spec *= 1 / np.sqrt(1 + (lo / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / hi) ** 4)
    return np.fft.irfft(spec, len(noise))


def _lowpass(x, fc):
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return np.fft.irfft(spec / np.sqrt(1 + (f / fc) ** 4), len(x))


def _tone(freq, dur, partials, attack):
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for ratio, amp, decay in partials:
        f = freq * ratio
        if f < SR / 2:
            out += amp * np.sin(2 * np.pi * f * t) * np.exp(-decay * t)
    return out * (1 - np.exp(-t / attack))


def _hz(midi):
    return 440.0 * 2 ** ((midi - 69) / 12)


class Bank:
    """Pre-rendered soft sounds. Round-robin variants avoid a machine-gun repeat.

    v3 was still "sharp, hurting my ears": its click taps (900-4200 Hz, 2 ms),
    2.3-3.5 kHz marble clack and 2.5 ms plink attacks all sat in the ear's most
    sensitive band. v4 keeps everything warm and low: felt-mallet tones in the
    130-440 Hz range with a 9 ms swell, a low padded thud instead of a click,
    and nothing designed above ~1.2 kHz.
    """

    def __init__(self, rng):
        self.rng = rng
        n = np.random.default_rng(7)
        self.thuds = []
        for _ in range(6):
            d = int(0.09 * SR)
            t = np.arange(d) / SR
            puff = _band(n.standard_normal(d), 70, 420) * (1 - np.exp(-t / 0.004)) * np.exp(-t / 0.022)
            self.thuds.append(puff / np.max(np.abs(puff)))
        self.puffs = []
        for _ in range(8):
            d = int(0.07 * SR)
            t = np.arange(d) / SR
            p = _band(n.standard_normal(d), 180, 1100) * (1 - np.exp(-t / 0.006)) * np.exp(-t / 0.018)
            self.puffs.append(p / np.max(np.abs(p)))
        self._tones = {}

    def thud(self):
        return self.thuds[self.rng.randrange(len(self.thuds))]

    def puff(self):
        return self.puffs[self.rng.randrange(len(self.puffs))]

    def felt(self, midi):
        # felt mallet on wood: pure fundamental, a trace of octave, slow-ish swell, short ring
        if midi not in self._tones:
            self._tones[midi] = _tone(_hz(midi), 0.7, [(1, 1.0, 7.5), (2.0, 0.05, 14)], 0.009)
        return self._tones[midi]

    def knock(self, rngf):
        # two balls touching: a soft low wooden knock, not a glassy click
        return _tone(420 * rngf, 0.18, [(1, 1.0, 26), (1.5, 0.25, 34)], 0.004)

    def chime(self, midi, dur=2.2):
        key = ("c", midi)
        if key not in self._tones:
            self._tones[key] = _tone(_hz(midi), dur, [(1, 1.0, 2.2), (2.0, 0.05, 4)], 0.025)
        return self._tones[key]


def _pan(x):
    """Screen x -> (left, right) constant-power gains, kept away from hard-pan."""
    p = max(-1.0, min(1.0, (x - 540) / 540)) * 0.75
    a = (p + 1) * np.pi / 4
    return np.cos(a), np.sin(a)


def _add(L, R, start, clip, gain, x=540.0):
    i = int(start * SR)
    if i < 0 or i >= len(L):
        return
    n = min(len(clip), len(L) - i)
    gl, gr = _pan(x)
    L[i:i + n] += clip[:n] * gain * gl
    R[i:i + n] += clip[:n] * gain * gr


def _energy(v, lo, hi):
    """Impact speed -> 0..1, with a soft curve so mid hits still read."""
    return float(np.clip((v - lo) / (hi - lo), 0, 1)) ** 0.8


def build(match, total_seconds, win_time, rng_seed=0):
    prng = random.Random(rng_seed)
    bank = Bank(prng)
    n = int((total_seconds + 0.5) * SR)
    L, R = np.zeros(n), np.zeros(n)

    tonic = KEYS[rng_seed % len(KEYS)]
    pent = [tonic + o * 12 + s for o in range(4) for s in (0, 2, 4, 7, 9)]
    registers = [[m for m in pent if 48 <= m <= 64], [m for m in pent if 55 <= m <= 69]]
    pos = [len(registers[0]) // 2, len(registers[1]) // 2]

    for e in match.events:
        if e.kind == "bounce":
            k = _energy(e.v, 250, 1300)
            reg = registers[e.ball]
            pos[e.ball] = min(len(reg) - 1, max(0, pos[e.ball] + prng.choice([-2, -1, -1, 1, 1, 2])))
            _add(L, R, e.t, bank.felt(reg[pos[e.ball]]), 0.14 + 0.20 * k, e.x)
            _add(L, R, e.t, bank.thud(), 0.05 + 0.12 * k, e.x)            # harder = more body
        elif e.kind == "clash":
            k = _energy(e.v, 150, 1400)
            _add(L, R, e.t, bank.knock(prng.uniform(0.95, 1.05)), 0.10 + 0.18 * k, e.x)
        elif e.kind == "break":
            # soft crumble: a few low padded puffs around the ring, then a warm tone
            for _ in range(14):
                dt = prng.expovariate(1 / 0.06)
                if dt > 0.35:
                    continue
                gx = 540 + prng.uniform(-1, 1) * match.rings[e.ring].radius
                _add(L, R, e.t + dt, bank.puff(), prng.uniform(0.04, 0.08) * (1 - dt / 0.4), gx)
            _add(L, R, e.t, _tone(_hz(tonic - 12), 0.6, [(1, 1.0, 7), (2, 0.1, 12)], 0.012), 0.20, e.x)
            _add(L, R, e.t + 0.03, bank.chime(tonic + 24 + 7 * (e.ring >= 3)), 0.08, e.x)

    # win: slow, soft rising chime spread across the stereo field
    for j, iv in enumerate((0, 4, 7, 12, 16)):
        _add(L, R, win_time + 0.15 + j * 0.16, bank.chime(tonic + 24 + iv), 0.12, 300 + j * 120)

    # small soft room: a few low-passed reflections, cross-fed between channels
    out = []
    for ch, other in ((L, R), (R, L)):
        wet = np.zeros_like(ch)
        for delay, g, src in ((0.023, 0.22, ch), (0.037, 0.18, other), (0.061, 0.13, ch),
                              (0.097, 0.09, other), (0.149, 0.05, ch)):
            d = int(delay * SR)
            wet[d:] += src[:-d] * g
        mix = ch + _lowpass(wet, 1500)
        out.append(_lowpass(_lowpass(mix, 1800), 1800))      # steep: nothing sharp survives
    stereo = np.stack(out, axis=1)[: int(total_seconds * SR)]
    stereo = stereo / (np.max(np.abs(stereo)) or 1.0) * OUT_PEAK * LIFT
    # peak-only soft knee: the ~2 dB lift only touches the loudest transients
    mag = np.abs(stereo)
    over = mag > KNEE
    stereo[over] = np.sign(stereo[over]) * (KNEE + (OUT_PEAK - KNEE) * np.tanh((mag[over] - KNEE) / (OUT_PEAK - KNEE)))
    return stereo


def write_wav(samples, path):
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    if pcm.ndim == 1:
        pcm = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
