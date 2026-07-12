"""Sound effects: tone generation and playback, plus the master AudioSettings."""

import array
import atexit
import functools
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

from .settings import load_data, save_data

# ---------------------------------------------------------------------------
# Master volume / mute
# ---------------------------------------------------------------------------

def _clamp(volume: float) -> float:
    """Clamp to 0.0-1.0, rounded to 2 decimals so 10% steps stay exact."""
    return min(1.0, max(0.0, round(volume, 2)))


class AudioSettings:
    """Master volume (0.0-1.0, default 0.7) and mute. Shared instance: `settings`.

    Steps are 10% and clamped. User-driven changes (step_up/step_down/
    toggle_mute) are persisted to a small settings JSON in the same XDG dir
    as the state file; `set_volume` and direct attribute writes (CLI flags)
    deliberately are not, so flags override for one run without sticking.
    """

    STEP = 0.1

    def __init__(self, volume: float = 0.7, muted: bool = False) -> None:
        self.volume = _clamp(volume)
        self.muted = muted

    def set_volume(self, volume: float) -> None:
        self.volume = _clamp(volume)

    def step_up(self) -> None:
        self.set_volume(self.volume + self.STEP)
        save_settings("volume")

    def step_down(self) -> None:
        self.set_volume(self.volume - self.STEP)
        save_settings("volume")

    def toggle_mute(self) -> None:
        self.muted = not self.muted
        save_settings("muted")

    def effective(self) -> float:
        """Playback level: 0.0 when muted, else the master volume."""
        return 0.0 if self.muted else self.volume

    def percent(self) -> int:
        return int(round(self.volume * 100))


settings = AudioSettings()


def load_settings() -> None:
    """Load persisted volume/mute into `settings`. Missing/corrupt file keeps defaults."""
    data = load_data()
    try:
        settings.set_volume(float(data.get("volume", settings.volume)))
        settings.muted = bool(data.get("muted", settings.muted))
    except (ValueError, TypeError):
        pass


def save_settings(*keys: str) -> None:
    """Persist the named keys ("volume"/"muted"; no args = both) via
    settings.py's merge-on-save.

    Merging matters: CLI flags override for one run without being persisted,
    so a toggle_mute must write only "muted" — writing both would silently
    replace the saved volume with a transient --volume flag value."""
    save_data({key: getattr(settings, key) for key in keys or ("volume", "muted")})


# ---------------------------------------------------------------------------
# PCM scaling (audioop was removed in 3.13 — this is the pure replacement)
# ---------------------------------------------------------------------------

def scale_pcm(data: bytes, factor: float) -> bytes:
    """Scale raw signed 16-bit little-endian PCM by `factor` (0.0-1.0).

    Pure Python: int16 samples are multiplied and truncated. A trailing odd
    byte (mid-sample split) is passed through untouched.
    """
    if factor >= 1.0:
        return data
    if factor <= 0.0:
        return b"\x00" * len(data)
    tail = b""
    if len(data) % 2:
        data, tail = data[:-1], data[-1:]
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder == "big":
        samples.byteswap()
    for i in range(len(samples)):
        samples[i] = int(samples[i] * factor)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples.tobytes() + tail


def _scale_wav(data: bytes, factor: float) -> bytes:
    """Return a copy of a 16-bit WAV with its samples scaled by `factor`."""
    import io
    import wave
    if factor >= 1.0:
        return data
    with wave.open(io.BytesIO(data), "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setparams(params)
        out.writeframes(scale_pcm(frames, factor))
    return buf.getvalue()


_sound_dir: str | None = None
_sound_files: dict[str, str] = {}
_players: list[subprocess.Popen] = []


def _cleanup_sounds() -> None:
    """Remove the per-session tempdir, but only after giving any in-flight
    player a moment to finish — otherwise the rmtree could delete a WAV out
    from under a just-spawned aplay/afplay (silencing the final chime)."""
    deadline = time.time() + 3.0
    for p in _players:
        if p.poll() is None:
            try:
                p.wait(timeout=max(0.0, deadline - time.time()))
            except (subprocess.TimeoutExpired, OSError):
                pass
    if _sound_dir is not None:
        shutil.rmtree(_sound_dir, ignore_errors=True)


def stop_sounds() -> None:
    """Stop any in-flight tone players (hard mute). Finished procs are reaped
    on the next play_sound / at exit, so just terminate the live ones."""
    for p in _players:
        if p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass


def _sound_path(sound_data: bytes) -> str:
    """Return the path of a temp WAV for `sound_data`, writing it once per session.

    Files live in a per-session tempdir that is removed at interpreter exit,
    so playback never leaks orphaned WAVs.
    """
    global _sound_dir
    if _sound_dir is None:
        _sound_dir = tempfile.mkdtemp(prefix="exercise-coach-")
        atexit.register(_cleanup_sounds)
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
    import io
    import math
    import struct
    import wave
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
    import io
    import wave
    tone1 = _generate_tone(880, 120, 1.0)
    tone2 = _generate_tone(1175, 200, 1.0)
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
    import io
    import wave
    tones = [
        _generate_tone(784, 100, 1.0),
        _generate_tone(988, 100, 1.0),
        _generate_tone(1319, 250, 1.0),
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


@functools.cache
def sound_set_complete() -> bytes:
    return _generate_reward_tone()


@functools.cache
def sound_group_complete() -> bytes:
    return _generate_exercise_complete_tone()


@functools.cache
def sound_rest_done() -> bytes:
    return _generate_tone(1047, 300, 1.0)  # C5 ping when rest finishes


@functools.cache
def sound_rest_warning() -> bytes:
    # Pre-rest-end tick at T-5s (recommendations §7): a single soft E5 blip,
    # deliberately shorter and quieter than the rest-done ding — generated
    # below full amplitude, then scaled by the master volume like every tone.
    return _generate_tone(660, 120, 0.4)


def play_sound(sound_data: bytes) -> None:
    """Play a WAV sound from bytes (non-blocking), scaled to the master volume.

    Tones are generated once at full amplitude; the int16 PCM is scaled here at
    play time. Temp files are cached per (tone, volume). Muted / zero volume
    skips playback entirely."""
    vol = settings.effective()
    if vol <= 0.0:
        return
    try:
        path = _sound_path(_scale_wav(sound_data, vol))
        for cmd in (["afplay", path], ["aplay", "-q", path]):
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                _players[:] = [p for p in _players if p.poll() is None]  # reap finished
                _players.append(proc)
                return
            except FileNotFoundError:
                continue
    except OSError:
        pass
