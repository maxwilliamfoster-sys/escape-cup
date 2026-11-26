"""Soundtrack for Beat The Keeper (keeper.py): every bounce plays a note of a song.

v5 (2026-11-26, Max: "quiet down the noise of the marbles slightly ... a much more softer
asmr type sound"): bounces are now soft wooden ASMR taps (Voices.tap) an octave lower, still
pitched to the music's key; the room is drier (close, like ASMR) and the bounces sit 2.5 dB
lower against the music (MUSIC_REL_DB -4 -> -1.5). Notes below describe the earlier versions.

Escape Cup's ring arena plays random "bloops". The league asked for something much
more satisfying (owner, 2026-11-26), so here the match writes its own tune:

  - an ORIGINAL stadium-anthem bed runs under the whole match (owner asked for
    "Champions League music", 2026-11-26; the real anthem is copyrighted, so this is
    our own piece in that style): 3/4 time, choir 'aah' chords + a choir top line,
    broken-chord strings, timpani, horns for the finish. It plays the same 8-bar
    chord progression as the bounces and replaces the music bed, so nothing clashes,
  - each peg hit plays a soft kalimba note from the CURRENT chord, chosen by the peg
    row: a ball falling through the board plays a rising arpeggio, always in tune,
  - walls are a quiet low "bong", posts a soft bell, ball-on-ball a wooden knock,
  - a save is a round low "boing", a goal a quick rising run plus a soft crowd swell,
  - the win resolves to the home chord with a chime and a bigger crowd swell,
  - once a club is one goal away the timpani hit every beat and horns join; a
    timpani roll leads into the winning goal; the choir swells at every goal.

The softness rules from audio.py still hold: no clicks, swelled attacks, nothing
designed above ~1.4 kHz, every partial over 2 kHz dropped, the mix steeply low-passed.
Everything is synthesised in numpy (no samples, nothing to license, no scipy in CI).
"""
import copy

import numpy as np

import audio
import keeper as K
from audio import SR, _add, _energy, _highpass, _hz, _lowpass

BAR = 2.0                                  # seconds per chord: 3/4 at 90 BPM, the anthem's waltz count
BEAT = BAR / 3
# chords as semitone offsets from the key's tonic (major key)
I, II, III, IV, V, VI = (0, 4, 7), (2, 5, 9), (4, 7, 11), (5, 9, 12), (7, 11, 14), (9, 12, 16)
# 8-bar hymn/anthem progressions (generic harmony, nothing borrowed from a real anthem)
PROGRESSIONS = [
    [I, IV, I, V, VI, IV, V, I],
    [I, VI, IV, V, I, III, IV, V],
    [VI, IV, I, V, VI, IV, V, V],
    [I, V, VI, III, IV, I, IV, V],
    [IV, V, III, VI, II, V, I, I],
]
MAJOR = (0, 2, 4, 5, 7, 9, 11)
PENT = (0, 2, 4, 7, 9)                     # with real music: bounces use the key's major pentatonic
MUSIC_REL_DB = -1.5                        # league music RMS vs the bounce track, measured AFTER filtering
                                           # (the cup's -7 dB is set before; orchestral lows then vanish -> -12 dB)
ANTHEM_GAIN = 0.8                          # anthem ~2 dB under the bounces (RMS): music audible, bounces on top
NOTE_LO, NOTE_HI = 67, 88                  # G4..E6 (392-1319 Hz): what phone speakers play well
PEG_ROW_Y0, PEG_ROW_DY = 690.0, 115.0      # keeper._pegs rows

BELL = [(1, 1.0, 3.2), (2, 0.06, 6)]


def _tone(freq, dur, partials, attack, glide=0.0, glide_t=0.04):
    """audio._tone, minus any partial that would land above 2 kHz."""
    safe = [p for p in partials if freq * p[0] * (1 + max(glide, 0)) <= 2000]
    return audio._tone(freq, dur, safe, attack, glide, glide_t)


def _pcs(chord, tonic):
    return {(tonic + s) % 12 for s in chord}


def _tones(chord, tonic):
    """Chord notes inside the phone-friendly window, low to high."""
    pcs = _pcs(chord, tonic)
    return [m for m in range(NOTE_LO, NOTE_HI + 1) if m % 12 in pcs]


def _in_range(pc, lo, hi):
    return next(m for m in range(lo, hi + 12) if m % 12 == pc)


def _fold(m, lo=67, hi=79):
    """Same note, folded by octaves into ~390-780 Hz: soft, and still what phone speakers play
    (one octave lower failed quality.MIN_PHONE_PCT: 23% above 400 Hz)."""
    while m > hi:
        m -= 12
    while m < lo:
        m += 12
    return m


def _noise_swell(rng, dur, attack, decay):
    """Soft crowd-like roar: noise band-limited to ~200-1200 Hz with a slow swell."""
    n = int(dur * SR)
    x = rng.standard_normal(n)
    x = _highpass(_lowpass(_lowpass(x, 1100), 1100), 220)
    t = np.arange(n) / SR
    wob = 1 + 0.25 * np.sin(2 * np.pi * 3.1 * t) * np.sin(2 * np.pi * 0.7 * t)
    env = (1 - np.exp(-t / attack)) * np.exp(-t / decay) * wob
    x = x * env
    return x / (np.max(np.abs(x)) or 1.0)


def _reverb(x, rng, seconds=1.6, rt=0.38):
    """Convolve with a decaying, low-passed noise tail (numpy FFT, no scipy)."""
    n_ir = int(seconds * SR)
    t = np.arange(n_ir) / SR
    ir = rng.standard_normal(n_ir) * np.exp(-t / rt)
    ir = _lowpass(_lowpass(ir, 1600), 1600)
    ir[: int(0.012 * SR)] = 0                         # pre-delay: keeps the dry hit clear
    ir /= np.sqrt(np.sum(ir ** 2)) or 1.0
    size = 1 << int(np.ceil(np.log2(len(x) + n_ir)))
    y = np.fft.irfft(np.fft.rfft(x, size) * np.fft.rfft(ir, size), size)
    return y[: len(x)]


class Voices:
    def __init__(self):
        self._c = {}

    def get(self, key, make):
        if key not in self._c:
            self._c[key] = make()
        return self._c[key]

    def tap(self, m, ring=0.07):
        """Soft ASMR wooden tap (Max, 2026-11-26: "much more softer asmr type sound"): a warm
        body tone, one inharmonic wood partial and a tiny breath of filtered noise for texture.
        Rounded attack, short decay, nothing over ~1.6 kHz designed in."""
        def make():
            n = int((ring * 5 + 0.05) * SR)
            t = np.arange(n) / SR
            f = _hz(m)
            out = np.sin(2 * np.pi * f * t) * np.exp(-t / ring)
            if f * 2.32 <= 1800:
                out += 0.30 * np.sin(2 * np.pi * f * 2.32 * t) * np.exp(-t / (ring * 0.4))
            rng = np.random.default_rng(m)
            grit = _highpass(_lowpass(rng.standard_normal(n), 1400), 300) * np.exp(-t / 0.010)
            out += 0.18 * grit / (np.max(np.abs(grit)) or 1.0)
            out *= 1 - np.exp(-t / 0.0028)
            return out / (np.max(np.abs(out)) or 1.0)
        return self.get(("tap", m, ring), make)



    def bell(self, m):
        return self.get(("e", m), lambda: _tone(_hz(m), 1.8, BELL, 0.01))


    def choir(self, m, dur):
        """Choir 'aah': three slightly detuned voices, harmonics shaped by 'ah' formants
        (~750 / 1150 Hz), gentle delayed vibrato, slow swell. Nothing above 2 kHz."""
        def make():
            t = np.arange(int(dur * SR)) / SR
            f0 = _hz(m)
            out = np.zeros_like(t)
            for det, vph in ((-0.0035, 0.0), (0.0, 2.1), (0.004, 4.2)):
                vib = 1 + 0.005 * np.sin(2 * np.pi * 5.2 * t + vph) * np.clip((t - 0.25) / 0.3, 0, 1)
                ph = 2 * np.pi * np.cumsum(f0 * (1 + det) * vib) / SR
                h = 1
                while f0 * h <= 2000:
                    fh = f0 * h
                    g = 0.2 + np.exp(-((fh - 750) / 180) ** 2) + 0.6 * np.exp(-((fh - 1150) / 200) ** 2)
                    out += g / h ** 0.7 * np.sin(h * ph)
                    h += 1
            env = np.clip(t / 0.22, 0, 1) ** 2 * np.clip((dur - t) / 0.3, 0, 1)
            out *= env
            return out / (np.max(np.abs(out)) or 1.0)
        return self.get(("ch", m, dur), make)

    def strings(self, m, dur=0.55):
        """Short bowed note for the broken-chord string figure."""
        def make():
            t = np.arange(int(dur * SR)) / SR
            f0 = _hz(m)
            vib = 1 + 0.003 * np.sin(2 * np.pi * 5.8 * t)
            ph = 2 * np.pi * np.cumsum(f0 * vib) / SR
            out = sum(np.sin(h * ph) / h ** 1.3 for h in range(1, 12) if f0 * h <= 2000)
            env = (1 - np.exp(-t / 0.035)) * np.exp(-t / 0.35) * np.clip((dur - t) / 0.08, 0, 1)
            out = out * env
            return out / (np.max(np.abs(out)) or 1.0)
        return self.get(("st", m, dur), make)

    def horn(self, m, dur):
        """Soft French-horn-ish swell (dark: harmonics roll off from ~900 Hz)."""
        def make():
            t = np.arange(int(dur * SR)) / SR
            f0 = _hz(m)
            ph = 2 * np.pi * f0 * t
            out = sum(np.exp(-f0 * h / 900) / h ** 0.5 * np.sin(h * ph) for h in range(1, 10) if f0 * h <= 2000)
            env = np.clip(t / 0.35, 0, 1) ** 2 * np.clip((dur - t) / 0.35, 0, 1)
            out = out * env
            return out / (np.max(np.abs(out)) or 1.0)
        return self.get(("hn", m, dur), make)

    def timpani(self, m):
        """Low drum: the 2nd/3rd partials let phone speakers hear it; no click."""
        return self.get(("ti", m), lambda: _tone(_hz(m), 1.4,
                                                 [(1, 1.0, 3.0), (1.5, 0.55, 4.0), (2.0, 0.5, 4.5),
                                                  (2.5, 0.22, 6.0), (3.0, 0.12, 7.0)], 0.004))


def _scale_step(m, tonic, up):
    """Next note of the major scale above/below m."""
    k = m + (1 if up else -1)
    while (k - tonic) % 12 not in MAJOR:
        k += 1 if up else -1
    return k


def _anthem(v, L, R, tonic, chord_at, win_time, goal_times, tense_t, final_goal_t):
    """Original stadium-anthem bed in 3/4 (choir, strings, horns, timpani). It follows the
    same chords as the bounces, so the whole video is one piece of music in one key."""
    bar_t, top = 0.0, None
    while bar_t < win_time:
        ch = chord_at(bar_t)
        nxt = chord_at(bar_t + BAR) if bar_t + BAR < win_time else I
        dur = round(min(BAR, win_time - bar_t) + 0.3, 2)
        tense = bar_t >= tense_t
        # choir chord, low-mid voicing
        for j, m in enumerate(sorted(_in_range((tonic + s) % 12, 55, 66) for s in ch)):
            _add(L, R, bar_t, v.choir(m, dur), 0.030, 280 + 260 * j)
        # choir top line: a chord tone per beat, smooth voice leading, passing note on beat 3
        pcs = _pcs(ch, tonic)
        cands = [m for m in range(67, 80) if m % 12 in pcs]
        top = cands[len(cands) // 2] if top is None else min(cands, key=lambda m: (abs(m - top), m))
        line = [top, top]
        tgt = min((m for m in range(67, 80) if m % 12 in _pcs(nxt, tonic)), key=lambda m: abs(m - top))
        line.append(top if tgt == top else _scale_step(top, tonic, tgt > top))
        for b, m in enumerate(line):
            bt = bar_t + b * BEAT
            if bt < win_time:
                _add(L, R, bt, v.choir(m, round(BEAT + 0.25, 2)), 0.040, 540)
        # strings: broken-chord figure in quavers (6 a bar)
        arp = sorted(_in_range((tonic + s) % 12, 55, 66) for s in ch)
        arp = arp + [arp[0] + 12]
        for q, idx in enumerate((0, 1, 2, 3, 2, 1)):
            qt = bar_t + q * BEAT / 2
            if qt < win_time:
                _add(L, R, qt, v.strings(arp[idx]), 0.026 if q else 0.032, 360 + 90 * idx)
        # timpani on the downbeat (every beat once a club is one goal away)
        root = _in_range((tonic + ch[0]) % 12, 38, 49)
        for b in range(3):
            bt = bar_t + b * BEAT
            if bt < win_time and (b == 0 or tense):
                _add(L, R, bt, v.timpani(root), 0.11 if b == 0 else 0.07)
        if tense:                                      # horns join for the finish
            for m in (_in_range((tonic + ch[0]) % 12, 55, 66), _in_range((tonic + ch[2]) % 12, 55, 66)):
                _add(L, R, bar_t, v.horn(m, dur), 0.030, 540)
        bar_t += BAR
    # goals: the choir swells an octave up
    for gt in goal_times:
        for j, m in enumerate(sorted(_in_range((tonic + s) % 12, 67, 78) for s in chord_at(gt))):
            _add(L, R, gt, v.choir(m, 1.6), 0.030, 300 + 240 * j)
    # timpani roll into the winning goal, then the full final chord at the card
    troot = _in_range(tonic % 12, 38, 49)
    if final_goal_t is not None:
        n_roll = 14
        for k in range(n_roll):
            rt = final_goal_t - 0.9 + k * 0.9 / n_roll
            if rt > 0:
                _add(L, R, rt, v.timpani(troot if k % 2 else troot + 7 - 12 * (troot + 7 > 49)),
                     0.02 + 0.06 * k / n_roll, 400 + 280 * (k % 2))
        _add(L, R, final_goal_t, v.timpani(troot), 0.16)
    for j, m in enumerate(sorted(_in_range((tonic + s) % 12, 55, 66) for s in I) + [_in_range(tonic % 12, 67, 78)]):
        _add(L, R, win_time, v.choir(m, 2.6), 0.045, 260 + 180 * j)
        _add(L, R, win_time, v.horn(m, 2.6), 0.02, 540)
    _add(L, R, win_time, v.timpani(troot), 0.13)


def build(match, total_seconds, win_time, rng_seed=0, music=None, key_pc=None, grid=None, card_taps=()):
    """music: mono bed from league_music.bed (None = the original anthem); key_pc: its major key
    (league_music.key) - the bounce notes then play that key's pentatonic, so they never clash.
    grid: (period, phase) of the music's beat when the match was retimed onto it (keeper_sync);
    taps that couldn't be anchored are nudged onto the nearest 16th (audio only, <= 35 ms)."""
    nrng = np.random.default_rng(rng_seed)
    v = Voices()
    n = int((total_seconds + 0.5) * SR)
    hitL, hitR = np.zeros(n), np.zeros(n)
    bedL, bedR = np.zeros(n), np.zeros(n)

    tonic = audio.KEYS[rng_seed % len(audio.KEYS)]
    prog = PROGRESSIONS[(rng_seed // len(audio.KEYS)) % len(PROGRESSIONS)]

    if music is not None:
        tonic = 60 + (key_pc or 0) % 12

    def chord_at(t):
        if music is not None:
            return PENT
        return I if t >= win_time else prog[int(t // BAR) % len(prog)]

    # --- the bed: an original anthem in 3/4 on the same chords, resolving home at the win
    goals = sorted((e for e in match.events if e.kind == "break"), key=lambda e: e.t)
    score, tense_t = [0, 0], win_time                 # tense from the goal that puts a club on 2
    for e in goals:
        score[e.ball] += 1
        if max(score) == K.GOALS_TO_WIN - 1 and tense_t == win_time:
            tense_t = e.t
    final_goal_t = goals[-1].t if goals and max(score) >= K.GOALS_TO_WIN else None
    if music is None:
        _anthem(v, bedL, bedR, tonic, chord_at, win_time, [e.t for e in goals[:-1]], tense_t, final_goal_t)

    # --- the hits
    import keeper_sync
    for e in match.events:
        if grid is not None and e.kind in ("bounce", "clash", "save"):
            e = copy.copy(e)
            e.t = keeper_sync.nudge(e.t, *grid)
        ch = chord_at(e.t)
        tones = _tones(ch, tonic)
        if e.kind == "bounce":
            k = _energy(e.v, 250, 1300)
            if e.ring >= 0:                                       # peg: row picks the chord tone
                row = int(round((K.PEGS[e.ring][1] - PEG_ROW_Y0) / PEG_ROW_DY))
                m = tones[min(row + e.ball % 2, len(tones) - 1)]
                _add(hitL, hitR, e.t, v.tap(_fold(m)), 0.10 + 0.14 * k, e.x)       # warm, but where phones play
            elif e.ring == -2:                                    # goal post: a slightly brighter tap
                _add(hitL, hitR, e.t, v.tap(_fold(tones[-1]), 0.09), 0.09 + 0.06 * k, e.x)
            else:                                                 # wall / roof / ramp
                _add(hitL, hitR, e.t, v.tap(_in_range((tonic + ch[0]) % 12, 60, 71), 0.05),
                     0.04 + 0.05 * k, e.x)
        elif e.kind == "clash":
            if e.ring == -3:
                continue                                          # keeper contact: the save sound covers it
            k = _energy(e.v, 150, 1400)
            _add(hitL, hitR, e.t, v.tap(_in_range((tonic + ch[0]) % 12, 62, 73), 0.04), 0.05 + 0.07 * k, e.x)
        elif e.kind == "save":
            _add(hitL, hitR, e.t, v.tap(_in_range((tonic + ch[0]) % 12, 60, 71), 0.12), 0.22, e.x)   # soft low thup
            _add(hitL, hitR, e.t + 0.06, v.tap(_in_range((tonic + ch[0]) % 12, 62, 73), 0.08), 0.10, e.x)
        elif e.kind == "break":                                   # GOAL
            for j, m in enumerate(tones[:5]):
                _add(hitL, hitR, e.t + 0.02 + j * 0.06, v.tap(_fold(m), 0.10), 0.14 + 0.02 * j, 300 + 120 * j)
            _add(hitL, hitR, e.t, v.tap(_in_range((tonic + ch[0]) % 12, 60, 71), 0.14), 0.16, e.x)
            roar = _noise_swell(nrng, 1.8, 0.12, 0.55)
            _add(hitL, hitR, e.t, roar, 0.07, 400)
            _add(hitL, hitR, e.t + 0.013, roar, 0.07, 680)

    home = _tones(I, tonic)
    for j, m in enumerate(home[:4]):
        _add(hitL, hitR, win_time + 0.10 + j * 0.12, v.bell(m - 12), 0.12, 340 + j * 130)
    roar = _noise_swell(nrng, 2.6, 0.2, 0.9)
    _add(hitL, hitR, win_time, roar, 0.10, 380)
    _add(hitL, hitR, win_time + 0.017, roar, 0.10, 700)

    # league end cards (keeper_render): a soft tap as each table row lands (a rising ripple
    # down the table), and a bell arpeggio for the champions
    for j, (t, kind) in enumerate(card_taps):
        if kind == "row":
            m = _fold(home[(19 - j) % len(home)] + 12 * ((19 - j) // len(home) % 2))
            _add(hitL, hitR, t, v.tap(m, 0.06), 0.08, 260 + (j * 37) % 560)
        elif kind == "champ":
            for i, m in enumerate(home[:4]):
                _add(hitL, hitR, t + 0.12 * i, v.bell(m), 0.11, 340 + i * 130)

    # --- mix: hits through a soft room, bed under them, then audio.py's safety chain
    if music is not None:      # level-set under the hits, ducked at goals, lifted at match point, gone at the win
        bedL = _highpass(audio._music_bed(music, hitL, hitR, n, win_time, [e.t for e in goals]), 120)
        ref = max(1, min(n, int(win_time * SR)))
        hit_rms = np.sqrt(np.mean(((hitL[:ref] + hitR[:ref]) / 2) ** 2))
        bed_rms = np.sqrt(np.mean(_lowpass(_lowpass(bedL[:ref], 2300), 2300) ** 2))
        if bed_rms > 1e-6:                                    # silent clip: leave it silent, don't amplify noise
            bedL = bedL * hit_rms * 10 ** (MUSIC_REL_DB / 20) / bed_rms   # ducks/lifts kept: multiplicative
        bedR, bed_gain, bed_wet = bedL, 1.0, 0.0                  # the recording has its own room
    else:
        bed_gain, bed_wet = ANTHEM_GAIN, 0.6
    out = []
    for hit, bed, side in ((hitL, bedL * bed_gain, 0), (hitR, bedR * bed_gain, 1)):
        wet = _reverb(hit + bed_wet * bed, np.random.default_rng(rng_seed * 2 + side))
        mix = hit + bed + 0.14 * wet                          # close and dry, like ASMR
        mix = _lowpass(_lowpass(mix, 2300), 2300)             # steep: nothing sharp survives
        out.append(_highpass(mix, 120))
    stereo = np.stack(out, axis=1)[: int(total_seconds * SR)]
    nf = min(len(stereo), int(0.25 * SR))
    if nf:
        stereo[-nf:] *= np.linspace(1.0, 0.0, nf)[:, None] ** 2
    stereo = stereo / (np.max(np.abs(stereo)) or 1.0) * audio.OUT_PEAK * audio.LIFT
    mag = np.abs(stereo)
    over = mag > audio.KNEE
    span = audio.OUT_PEAK - audio.KNEE
    stereo[over] = np.sign(stereo[over]) * (audio.KNEE + span * np.tanh((mag[over] - audio.KNEE) / span))
    return stereo
