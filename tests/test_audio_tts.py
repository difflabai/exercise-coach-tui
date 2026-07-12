"""Tests for exercise_coach.audio (tone generation/playback) and exercise_coach.tts
(backend selection, captions, process reaping).

The autouse ``no_audio`` fixture replaces ``audio.play_sound`` and
``tts._start_say`` with recorders. These tests exercise the *real* functions,
so the originals are captured at import time (collection runs before any
fixture) and the modules' subprocess/shutil references are re-patched with
recording fakes — no real process is ever spawned.
"""

import io
import json
import math
import subprocess
import types
import wave

import pytest

from exercise_coach import audio, tts

# Real functions, captured before the autouse no_audio fixture patches them.
REAL_PLAY_SOUND = audio.play_sound
REAL_START_SAY = tts._start_say

SAMPLE_RATE = 44100


# ---------------------------------------------------------------------------
# Fake subprocess machinery
# ---------------------------------------------------------------------------

class RecordingPipe(io.BytesIO):
    """BytesIO that snapshots its contents on close (piper stdin gets closed)."""

    value: bytes = b""

    def close(self) -> None:
        self.value = self.getvalue()
        super().close()


class FakePopen:
    """Records the command; supports poll/wait/terminate/kill and PIPE stdio."""

    def __init__(self, cmd, stdin=None, stdout=None, stderr=None, **_kw):
        self.cmd = list(cmd)
        self.stdin = RecordingPipe() if stdin == subprocess.PIPE else None
        self.stdout = RecordingPipe() if stdout == subprocess.PIPE else None
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def make_subprocess_stub(spawned: list, fail: tuple[str, ...] = ()):
    """A subprocess-module stand-in whose Popen records into ``spawned``.

    Commands whose argv[0] is in ``fail`` raise FileNotFoundError (binary
    missing), matching how the real Popen reports an absent player.
    """

    def popen(cmd, **kwargs):
        if cmd[0] in fail:
            raise FileNotFoundError(cmd[0])
        proc = FakePopen(cmd, **kwargs)
        spawned.append(proc)
        return proc

    return types.SimpleNamespace(
        Popen=popen,
        PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )


def wav_params(data: bytes) -> tuple[int, int, int, int]:
    """(nchannels, sampwidth, framerate, nframes) of a WAV byte string."""
    with wave.open(io.BytesIO(data), "rb") as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


# ---------------------------------------------------------------------------
# audio.py — tone generation
# ---------------------------------------------------------------------------

class TestToneGeneration:
    def test_generate_tone_is_valid_wav_with_expected_format(self):
        data = audio._generate_tone(440, 250, 0.5)
        nchannels, sampwidth, framerate, nframes = wav_params(data)
        assert nchannels == 1
        assert sampwidth == 2  # 16-bit
        assert framerate == SAMPLE_RATE
        assert nframes == SAMPLE_RATE * 250 // 1000  # 250ms exactly

    def test_generate_tone_amplitude_matches_volume(self):
        data = audio._generate_tone(440, 100, 0.5)
        with wave.open(io.BytesIO(data), "rb") as w:
            frames = w.readframes(w.getnframes())
        peak = max(
            abs(int.from_bytes(frames[i:i + 2], "little", signed=True))
            for i in range(0, len(frames), 2)
        )
        assert peak <= int(0.5 * 32767)
        assert peak > int(0.45 * 32767)  # a 100ms 440Hz sine reaches near-full swing
        assert not math.isclose(peak, 0)

    def test_set_complete_chime_is_two_notes_long(self):
        # 120ms + 200ms notes concatenated
        _, _, framerate, nframes = wav_params(audio.sound_set_complete())
        expected = SAMPLE_RATE * 120 // 1000 + SAMPLE_RATE * 200 // 1000
        assert framerate == SAMPLE_RATE
        assert nframes == expected

    def test_group_complete_chime_is_three_notes_long(self):
        # 100ms + 100ms + 250ms notes concatenated
        _, _, _, nframes = wav_params(audio.sound_group_complete())
        expected = sum(SAMPLE_RATE * ms // 1000 for ms in (100, 100, 250))
        assert nframes == expected

    def test_rest_done_is_valid_300ms_tone(self):
        nchannels, sampwidth, framerate, nframes = wav_params(audio.sound_rest_done())
        assert (nchannels, sampwidth, framerate) == (1, 2, SAMPLE_RATE)
        assert nframes == SAMPLE_RATE * 300 // 1000

    def test_tones_are_lazily_cached(self):
        # lru_cache: repeated calls return the identical object, not a rebuild
        assert audio.sound_set_complete() is audio.sound_set_complete()
        assert audio.sound_group_complete() is audio.sound_group_complete()
        assert audio.sound_rest_done() is audio.sound_rest_done()


# ---------------------------------------------------------------------------
# audio.py — play_sound temp-file handling
# ---------------------------------------------------------------------------

@pytest.fixture
def sound_env(no_audio, monkeypatch, tmp_path):
    """Wire the real play_sound to fakes: recorded Popen (afplay 'missing',
    aplay 'present'), a tempdir under tmp_path, and fresh module caches."""
    spawned: list[FakePopen] = []
    monkeypatch.setattr(audio, "subprocess", make_subprocess_stub(spawned, fail=("afplay",)))
    sound_dir = tmp_path / "sounds"

    def fake_mkdtemp(prefix=""):
        sound_dir.mkdir(exist_ok=True)
        return str(sound_dir)

    monkeypatch.setattr(audio, "tempfile", types.SimpleNamespace(mkdtemp=fake_mkdtemp))
    monkeypatch.setattr(audio, "atexit", types.SimpleNamespace(register=lambda _f: None))
    monkeypatch.setattr(audio, "_sound_dir", None)
    monkeypatch.setattr(audio, "_sound_files", {})
    monkeypatch.setattr(audio, "_players", [])
    return spawned, sound_dir


class TestPlaySound:
    def test_falls_back_to_aplay_and_plays_written_file(self, sound_env):
        spawned, sound_dir = sound_env
        data = audio.sound_rest_done()
        REAL_PLAY_SOUND(data)
        assert len(spawned) == 1
        cmd = spawned[0].cmd
        assert cmd[0] == "aplay"  # afplay was "missing"
        path = cmd[-1]
        assert path.startswith(str(sound_dir))
        with open(path, "rb") as f:
            assert f.read() == data

    def test_same_sound_reuses_one_temp_file(self, sound_env):
        spawned, sound_dir = sound_env
        data = audio.sound_set_complete()
        for _ in range(3):
            REAL_PLAY_SOUND(data)
        files = list(sound_dir.iterdir())
        assert len(files) == 1  # no accumulation across calls
        assert len({tuple(p.cmd) for p in spawned}) == 1  # same path every time

    def test_distinct_sounds_get_distinct_files(self, sound_env):
        _, sound_dir = sound_env
        REAL_PLAY_SOUND(audio.sound_set_complete())
        REAL_PLAY_SOUND(audio.sound_group_complete())
        assert len(list(sound_dir.iterdir())) == 2

    def test_finished_players_are_reaped(self, sound_env):
        _, _ = sound_env
        data = audio.sound_rest_done()
        for _ in range(5):
            REAL_PLAY_SOUND(data)
            audio._players[-1].returncode = 0  # proc finishes
        assert len(audio._players) == 1  # only the latest survives each reap

    def test_no_player_binary_is_a_silent_noop(self, no_audio, monkeypatch, tmp_path):
        spawned: list[FakePopen] = []
        monkeypatch.setattr(
            audio, "subprocess", make_subprocess_stub(spawned, fail=("afplay", "aplay"))
        )
        monkeypatch.setattr(
            audio, "tempfile",
            types.SimpleNamespace(mkdtemp=lambda prefix="": str(tmp_path)),
        )
        monkeypatch.setattr(audio, "atexit", types.SimpleNamespace(register=lambda _f: None))
        monkeypatch.setattr(audio, "_sound_dir", None)
        monkeypatch.setattr(audio, "_sound_files", {})
        monkeypatch.setattr(audio, "_players", [])
        REAL_PLAY_SOUND(audio.sound_rest_done())  # must not raise
        assert spawned == []
        assert audio._players == []


# ---------------------------------------------------------------------------
# tts.py — backend selection
# ---------------------------------------------------------------------------

@pytest.fixture
def tts_env(no_audio, monkeypatch, tmp_path):
    """Factory: install fake which/Popen into tts; returns the spawn log.

    install("piper", "aplay", ...) makes exactly those binaries "exist".
    piper_rate (when set) also creates a model file + json and points
    PIPER_MODEL at it.
    """
    spawned: list[FakePopen] = []

    def install(*available: str, piper_rate: int | None = None):
        monkeypatch.setattr(
            tts, "shutil",
            types.SimpleNamespace(
                which=lambda name: f"/usr/bin/{name}" if name in available else None
            ),
        )
        monkeypatch.setattr(tts, "subprocess", make_subprocess_stub(spawned))
        monkeypatch.setattr(tts, "_say_procs", [])
        if piper_rate is not None:
            model = tmp_path / "en_US-test.onnx"
            model.write_bytes(b"onnx")
            (tmp_path / "en_US-test.onnx.json").write_text(
                json.dumps({"audio": {"sample_rate": piper_rate}})
            )
            monkeypatch.setenv("PIPER_MODEL", str(model))
        else:
            # Keep _piper_model from scanning the developer's real voice dirs.
            monkeypatch.delenv("PIPER_MODEL", raising=False)
            monkeypatch.setattr(tts, "_piper_model", lambda: None)
        return spawned

    return install


class TestBackendSelection:
    def test_piper_preferred_when_all_backends_exist(self, tts_env):
        spawned = tts_env("piper", "aplay", "say", "espeak-ng", "espeak", piper_rate=16000)
        REAL_START_SAY("hello world")
        assert [p.cmd[0] for p in spawned] == ["piper", "aplay"]
        piper, aplay = spawned
        assert piper.cmd == ["piper", "--model", piper.cmd[2], "--output_raw"]
        assert piper.cmd[2].endswith("en_US-test.onnx")
        assert piper.stdin.value == b"hello world"  # text fed via stdin
        # aplay is told the model's sample rate from the .onnx.json
        assert aplay.cmd[:3] == ["aplay", "-q", "-r"]
        assert "16000" in aplay.cmd
        assert tts._say_procs == spawned

    def test_piper_without_aplay_falls_back(self, tts_env):
        spawned = tts_env("piper", "say")  # aplay missing -> piper chain unusable
        REAL_START_SAY("hi")
        assert [p.cmd for p in spawned] == [["say", "hi"]]

    def test_piper_without_model_falls_back(self, tts_env):
        spawned = tts_env("piper", "aplay", "espeak-ng")  # binaries yes, model no
        REAL_START_SAY("hi")
        assert [p.cmd for p in spawned] == [["espeak-ng", "hi"]]

    def test_say_only(self, tts_env):
        spawned = tts_env("say")
        REAL_START_SAY("hi")
        assert [p.cmd for p in spawned] == [["say", "hi"]]
        assert tts._say_procs == spawned

    def test_espeak_ng_fallback(self, tts_env):
        spawned = tts_env("espeak-ng", "espeak")
        REAL_START_SAY("hi")
        assert [p.cmd for p in spawned] == [["espeak-ng", "hi"]]

    def test_plain_espeak_is_last_resort(self, tts_env):
        spawned = tts_env("espeak")
        REAL_START_SAY("hi")
        assert [p.cmd for p in spawned] == [["espeak", "hi"]]

    def test_no_backend_is_a_silent_noop(self, tts_env):
        spawned = tts_env()  # nothing installed
        REAL_START_SAY("hi")  # must not raise
        assert spawned == []
        assert tts._say_procs == []

    def test_fallback_cmd_order(self, tts_env):
        tts_env("say", "espeak-ng", "espeak")
        assert tts._tts_fallback_cmd("x")[0] == "say"
        tts_env("espeak-ng", "espeak")
        assert tts._tts_fallback_cmd("x")[0] == "espeak-ng"

    def test_new_speech_terminates_previous(self, tts_env):
        spawned = tts_env("say")
        REAL_START_SAY("first")
        first = spawned[0]
        REAL_START_SAY("second")
        assert first.terminated
        assert tts._say_procs == [spawned[1]]


# ---------------------------------------------------------------------------
# tts.py — captions and proc reaping
# ---------------------------------------------------------------------------

class TestCaptionsAndReaping:
    def test_say_sets_caption(self, no_audio):
        tts.say("Three sets of squats.")
        text, age = tts.current_caption()
        assert text == "Three sets of squats."
        assert 0 <= age < 60  # just set (real wall clock)
        assert no_audio.spoken == ["Three sets of squats."]

    def test_speak_skips_empty_lines(self, no_audio):
        tts._set_caption("")
        tts.speak(None)
        tts.speak("")
        assert no_audio.spoken == []
        assert tts.current_caption()[0] == ""
        tts.speak("real line")
        assert no_audio.spoken == ["real line"]
        assert tts.current_caption()[0] == "real line"

    def test_reap_drops_finished_procs_only(self, monkeypatch):
        finished = FakePopen(["say", "done"])
        finished.returncode = 0
        running = FakePopen(["say", "going"])
        monkeypatch.setattr(tts, "_say_procs", [finished, running])
        tts._reap()
        assert tts._say_procs == [running]

    def test_terminate_say_stops_running_and_clears_list(self, monkeypatch):
        monkeypatch.setattr(tts, "subprocess", make_subprocess_stub([]))
        running = FakePopen(["say", "going"])
        monkeypatch.setattr(tts, "_say_procs", [running])
        tts.terminate_say()
        assert running.terminated
        assert tts._say_procs == []
