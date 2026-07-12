"""Text-to-speech: piper/say/espeak backends, process lifecycle, and captions."""

import json
import os
import shutil
import subprocess
import time

CAPTION_DURATION = 12.0  # seconds to show caption

PIPER_PREFERRED_VOICE = "en_US-ryan-high.onnx"

_say_procs: list[subprocess.Popen] = []
_caption: str = ""
_caption_time: float = 0.0


def current_caption() -> tuple[str, float]:
    """Return (caption text, seconds since it was set)."""
    return _caption, time.time() - _caption_time


def _piper_model() -> str | None:
    """Locate a piper voice model. Honors $PIPER_MODEL, otherwise scans common dirs."""
    env = os.environ.get("PIPER_MODEL")
    if env and os.path.isfile(env):
        return env
    dirs = [os.path.expanduser(d) for d in (
        "~/.local/share/piper-voices", "~/piper-voices", "~/.local/share/piper",
    )]
    for d in dirs:
        preferred = os.path.join(d, PIPER_PREFERRED_VOICE)
        if os.path.isfile(preferred):
            return preferred
    for d in dirs:
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith(".onnx"):
                    return os.path.join(d, name)
    return None


def _piper_sample_rate(model: str) -> int:
    try:
        with open(model + ".json") as f:
            return int(json.load(f).get("audio", {}).get("sample_rate", 22050))
    except (OSError, ValueError):
        return 22050


def _start_piper(text: str) -> list[subprocess.Popen] | None:
    """Synthesize via piper and play via aplay. Returns the proc chain, or None if unavailable."""
    if not (shutil.which("piper") and shutil.which("aplay")):
        return None
    model = _piper_model()
    if not model:
        return None
    rate = _piper_sample_rate(model)
    try:
        piper = subprocess.Popen(
            ["piper", "--model", model, "--output_raw"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if piper.stdin is not None:
            piper.stdin.write(text.encode())
            piper.stdin.close()
        aplay = subprocess.Popen(
            ["aplay", "-q", "-r", str(rate), "-f", "S16_LE", "-c", "1", "-t", "raw", "-"],
            stdin=piper.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if piper.stdout is not None:
            piper.stdout.close()
        return [piper, aplay]
    except OSError:
        return None


def _tts_fallback_cmd(text: str) -> list[str]:
    """Single-command TTS fallback when piper isn't available."""
    if shutil.which("say"):
        return ["say", text]
    if shutil.which("espeak-ng"):
        return ["espeak-ng", text]
    if shutil.which("espeak"):
        return ["espeak", text]
    return []


def _set_caption(text: str) -> None:
    global _caption, _caption_time
    _caption = text
    _caption_time = time.time()


def _reap() -> None:
    """Drop finished procs from the live list, reaping their exit status."""
    global _say_procs
    _say_procs = [p for p in _say_procs if p.poll() is None]


def terminate_say() -> None:
    """Terminate any in-flight speech and reap the processes (no zombies)."""
    global _say_procs
    _reap()
    for p in _say_procs:
        try:
            p.terminate()
            p.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                p.kill()
                p.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except OSError:
            pass
    _say_procs = []


# NOTE: deliberately no atexit hook here. On a normal exit the last utterance
# (e.g. the session-complete line) should keep playing detached, exactly like
# the original single-file app; init reaps the orphaned processes. The paths
# that must silence speech (Ctrl-C, suspend, pause) call terminate_say()
# explicitly.


def _start_say(text: str) -> None:
    """Kick off TTS for `text`. Tries piper first, then a single-command fallback."""
    global _say_procs
    terminate_say()  # reaps finished procs, then stops any still speaking
    procs = _start_piper(text)
    if procs is None:
        cmd = _tts_fallback_cmd(text)
        if not cmd:
            return
        try:
            procs = [subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )]
        except OSError:
            return
    _say_procs = procs


def say(text: str) -> None:
    """Non-blocking speech."""
    _set_caption(text)
    _start_say(text)


def say_sync(text: str, wait: float = 0) -> None:
    """Blocking speech."""
    _set_caption(text)
    _start_say(text)
    if _say_procs:
        try:
            _say_procs[-1].wait()
        except OSError:
            pass
    if wait > 0:
        time.sleep(wait)


def speak(line: str | None) -> None:
    """Say a line if it exists. Skip silently if null/empty."""
    if line:
        say(line)


def speak_sync(line: str | None, wait: float = 0) -> None:
    """Blocking speak with null safety."""
    if line:
        say_sync(line, wait)
