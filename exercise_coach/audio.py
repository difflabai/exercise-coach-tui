"""Sound effects: tone generation and playback."""

import atexit
import functools
import hashlib
import os
import shutil
import subprocess
import tempfile

_sound_dir: str | None = None
_sound_files: dict[str, str] = {}


def _sound_path(sound_data: bytes) -> str:
    """Return the path of a temp WAV for `sound_data`, writing it once per session.

    Files live in a per-session tempdir that is removed at interpreter exit,
    so playback never leaks orphaned WAVs.
    """
    global _sound_dir
    if _sound_dir is None:
        _sound_dir = tempfile.mkdtemp(prefix="exercise-coach-")
        atexit.register(shutil.rmtree, _sound_dir, ignore_errors=True)
    key = hashlib.sha256(sound_data).hexdigest()[:16]
    path = _sound_files.get(key)
    if path is None or not os.path.exists(path):
        path = os.path.join(_sound_dir, f"{key}.wav")
        with open(path, "wb") as f:
            f.write(sound_data)
        _sound_files[key] = path
    return path


def _generate_tone(frequency: int, duration_ms: int, volume: float = 0.5) -> bytes:
    """Generate a WAV tone in memory. Returns raw WAV bytes."""
    import struct
    import wave
    import io
    import math
    sample_rate = 44100
    n_samples = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = b"".join(
            struct.pack("<h", int(volume * 32767 * math.sin(2 * math.pi * frequency * i / sample_rate)))
            for i in range(n_samples)
        )
        wf.writeframes(frames)
    return buf.getvalue()


def _generate_reward_tone() -> bytes:
    """A short rising two-note chime for set completion."""
    import io, wave
    tone1 = _generate_tone(880, 120, 0.4)
    tone2 = _generate_tone(1175, 200, 0.4)
    buf = io.BytesIO()
    with wave.open(io.BytesIO(tone1), "rb") as w1, wave.open(io.BytesIO(tone2), "rb") as w2:
        with wave.open(buf, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(44100)
            out.writeframes(w1.readframes(w1.getnframes()))
            out.writeframes(w2.readframes(w2.getnframes()))
    return buf.getvalue()


def _generate_exercise_complete_tone() -> bytes:
    """A three-note ascending chime for exercise/group completion."""
    import io, wave
    tones = [
        _generate_tone(784, 100, 0.4),
        _generate_tone(988, 100, 0.4),
        _generate_tone(1319, 250, 0.4),
    ]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(44100)
        for t in tones:
            with wave.open(io.BytesIO(t), "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
    return buf.getvalue()


@functools.lru_cache(maxsize=None)
def sound_set_complete() -> bytes:
    return _generate_reward_tone()


@functools.lru_cache(maxsize=None)
def sound_group_complete() -> bytes:
    return _generate_exercise_complete_tone()


@functools.lru_cache(maxsize=None)
def sound_rest_done() -> bytes:
    return _generate_tone(1047, 300, 0.5)  # C5 ping when rest finishes


def play_sound(sound_data: bytes) -> None:
    """Play a WAV sound from bytes (non-blocking). Temp files are cached per tone."""
    try:
        path = _sound_path(sound_data)
        for cmd in (["afplay", path], ["aplay", "-q", path]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue
    except OSError:
        pass
