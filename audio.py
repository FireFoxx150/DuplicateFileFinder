"""Match audio files by what they sound like, so re-encoded copies still match.

Each file becomes a chroma fingerprint: the energy in the 12 musical pitch classes for every
short frame, turned into bits that compare pitch classes within a frame and change over time.
The fingerprint of a copy in another format is nearly identical, so bits agree almost everywhere.
"""
import shutil
import subprocess

import numpy as np

RATE = 11025
FRAME = 2048
HOP = 1024
MAX_SECONDS = 120


def _pcm(path):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise OSError("ffmpeg is needed for audio similarity")
    proc = subprocess.run(
        [exe, "-v", "error", "-t", str(MAX_SECONDS), "-i", path, "-ac", "1", "-ar", str(RATE),
         "-f", "s16le", "-"],
        capture_output=True, timeout=180,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise OSError("ffmpeg could not read this audio")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def fingerprint(path):
    """Boolean array with one row per frame and 24 bits per row."""
    x = _pcm(path)
    if len(x) < FRAME + HOP:
        raise OSError("audio is too short to fingerprint")
    n = 1 + (len(x) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(FRAME), axis=1)) ** 2
    freqs = np.fft.rfftfreq(FRAME, 1 / RATE)
    chroma = np.zeros((n, 12))
    for k in np.nonzero((freqs >= 80) & (freqs <= 2000))[0]:
        pitch_class = int(round(12 * np.log2(freqs[k] / 440.0) + 69)) % 12
        chroma[:, pitch_class] += spec[:, k]
    chroma /= chroma.sum(axis=1, keepdims=True) + 1e-9
    within = chroma[:-1] > np.roll(chroma[:-1], -1, axis=1)
    over_time = chroma[1:] > chroma[:-1]
    return np.concatenate([within, over_time], axis=1)


def agreement(a, b):
    """Percentage of fingerprint bits that match over the frames both files share."""
    m = min(len(a), len(b))
    if m == 0:
        return 0.0
    return 100.0 * float(np.mean(a[:m] == b[:m]))
