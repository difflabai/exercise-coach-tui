"""Tests for master volume + mute (recommendations §1).

Covers: AudioSettings clamp/step/mute, the pure int16 PCM scaler, per-backend
TTS volume mapping (espeak -a, say [[volm]]), skip-at-zero behavior for both
tones and speech (captions must still fire), settings persistence round-trip,
and the global '-'/'+'/'='/'m' keys in run_screen.
"""

import io
import json
import math
import struct
import subprocess as real_subprocess
import sys
import types
import wave

import pytest

from exercise_coach import audio, cli, screens, tts, ui
from exercise_coach.audio import AudioSettings, scale_pcm
from exercise_coach.cassette import load_cassette_from_dict
from exercise_coach.events import Event

# Real functions, captured before the autouse no_audio fixture patches them.
REAL_PLAY_SOUND = audio.play_sound


def make_sine_pcm(n_samples: int = 440, amplitude: int = 20000) -> bytes:
    """A known S16LE sine buffer (one full cycle; hits exactly ±amplitude)."""
    return b"".join(
        struct.pack("<h", int(amplitude * math.sin(2 * math.pi * i / n_samples)))
        for i in range(n_samples)
    )


def pcm_samples(data: bytes) -> list[int]:
    return [s[0] for s in struct.iter_unpack("<h", data)]


def wav_peak(data: bytes) -> int:
    with wave.open(io.BytesIO(data), "rb") as w:
        frames = w.readframes(w.getnframes())
    return max(abs(s) for s in pcm_samples(frames))


# ---------------------------------------------------------------------------
# AudioSettings: clamp / step / mute
# ---------------------------------------------------------------------------

class TestAudioSettings:
    def test_defaults(self):
        s = AudioSettings()
        assert s.volume == 0.7
        assert s.muted is False
        assert s.effective() == 0.7

    def test_steps_are_ten_percent(self):
        s = AudioSettings(volume=0.5)
        s.step_up()
        assert s.volume == 0.6
        s.step_down()
        s.step_down()
        assert s.volume == 0.4

    def test_step_up_clamps_at_one(self):
        s = AudioSettings(volume=0.95)
        for _ in range(3):
            s.step_up()
        assert s.volume == 1.0

    def test_step_down_clamps_at_zero(self):
        s = AudioSettings(volume=0.15)
        for _ in range(4):
            s.step_down()
        assert s.volume == 0.0

    def test_set_volume_clamps(self):
        s = AudioSettings()
        s.set_volume(1.7)
        assert s.volume == 1.0
        s.set_volume(-0.3)
        assert s.volume == 0.0

    def test_toggle_mute_preserves_volume(self):
        s = AudioSettings(volume=0.6)
        s.toggle_mute()
        assert s.muted is True
        assert s.effective() == 0.0
        assert s.volume == 0.6  # level survives the mute
        s.toggle_mute()
        assert s.muted is False
        assert s.effective() == 0.6

    def test_percent(self):
        assert AudioSettings(volume=0.7).percent() == 70
        assert AudioSettings(volume=1.0).percent() == 100
        assert AudioSettings(volume=0.0).percent() == 0


# ---------------------------------------------------------------------------
# PCM scaler
# ---------------------------------------------------------------------------

class TestScalePcm:
    def test_half_volume_halves_every_sample(self):
        pcm = make_sine_pcm(amplitude=20000)
        scaled = scale_pcm(pcm, 0.5)
        assert len(scaled) == len(pcm)
        for orig, got in zip(pcm_samples(pcm), pcm_samples(scaled), strict=True):
            assert got == int(orig * 0.5)
        assert max(abs(s) for s in pcm_samples(scaled)) == 10000

    def test_zero_factor_is_silence_of_same_length(self):
        pcm = make_sine_pcm()
        assert scale_pcm(pcm, 0.0) == b"\x00" * len(pcm)

    def test_full_volume_is_identity(self):
        pcm = make_sine_pcm()
        assert scale_pcm(pcm, 1.0) == pcm

    def test_odd_trailing_byte_passes_through(self):
        pcm = make_sine_pcm() + b"\x7f"
        scaled = scale_pcm(pcm, 0.5)
        assert len(scaled) == len(pcm)
        assert scaled[-1:] == b"\x7f"

    def test_scale_wav_halves_tone_peak(self):
        tone = audio._generate_tone(440, 100, 1.0)
        scaled = audio._scale_wav(tone, 0.5)
        full_peak = wav_peak(tone)
        half_peak = wav_peak(scaled)
        assert full_peak > 32000  # tones are generated at full amplitude
        assert half_peak == pytest.approx(full_peak * 0.5, abs=2)
        # container format is preserved
        with wave.open(io.BytesIO(scaled), "rb") as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 44100)


# ---------------------------------------------------------------------------
# play_sound: scaled at play time, skipped at zero
# ---------------------------------------------------------------------------

class TestPlaySoundVolume:
    @pytest.fixture
    def played_wavs(self, monkeypatch):
        """Run the real play_sound with the file write and Popen faked out;
        returns the list of WAV byte-strings that would have been played."""
        wavs: list[bytes] = []

        def fake_sound_path(data: bytes) -> str:
            wavs.append(data)
            return "/nonexistent.wav"

        proc = types.SimpleNamespace(poll=lambda: 0)
        monkeypatch.setattr(audio, "_sound_path", fake_sound_path)
        monkeypatch.setattr(
            audio, "subprocess",
            types.SimpleNamespace(Popen=lambda *a, **k: proc, DEVNULL=None),
        )
        monkeypatch.setattr(audio, "_players", [])
        return wavs

    def test_pcm_scaled_to_master_volume_at_play_time(self, played_wavs):
        audio.settings.volume = 0.5
        tone = audio.sound_rest_done()
        REAL_PLAY_SOUND(tone)
        assert len(played_wavs) == 1
        assert wav_peak(played_wavs[0]) == pytest.approx(wav_peak(tone) * 0.5, abs=2)

    def test_muted_skips_playback_entirely(self, played_wavs):
        audio.settings.muted = True
        REAL_PLAY_SOUND(audio.sound_rest_done())
        assert played_wavs == []

    def test_zero_volume_skips_playback_entirely(self, played_wavs):
        audio.settings.volume = 0.0
        REAL_PLAY_SOUND(audio.sound_rest_done())
        assert played_wavs == []


# ---------------------------------------------------------------------------
# TTS backends: volume mapping and skip-at-zero
# ---------------------------------------------------------------------------

@pytest.fixture
def which(monkeypatch):
    """Factory: make exactly the named binaries 'exist' for tts."""
    def install(*names: str):
        monkeypatch.setattr(
            tts, "shutil",
            types.SimpleNamespace(
                which=lambda n: f"/usr/bin/{n}" if n in names else None
            ),
        )
    return install


class TestTTSVolumeMapping:
    # Full volume maps to espeak's nominal default amplitude of 100 (the flag
    # goes to 200, but that clips and is louder than the pre-volume baseline).
    @pytest.mark.parametrize("volume,amp", [(1.0, "100"), (0.7, "70"), (0.35, "35"), (0.0, "0")])
    def test_espeak_amplitude_maps_volume_to_0_100(self, which, volume, amp):
        audio.settings.volume = volume
        which("espeak-ng")
        assert tts._tts_fallback_cmd("hi") == ["espeak-ng", "-a", amp, "hi"]

    def test_plain_espeak_gets_amplitude_too(self, which):
        audio.settings.volume = 0.5
        which("espeak")
        assert tts._tts_fallback_cmd("hi") == ["espeak", "-a", "50", "hi"]

    def test_say_gets_inline_volm_markup(self, which):
        audio.settings.volume = 0.7
        which("say")
        assert tts._tts_fallback_cmd("hi") == ["say", "[[volm 0.70]] hi"]

    def test_muted_maps_to_zero_level(self, which):
        # (say/espeak are never reached when muted — say() skips first — but
        # the mapping itself must still be safe at effective()==0.)
        audio.settings.muted = True
        which("say")
        assert tts._tts_fallback_cmd("hi") == ["say", "[[volm 0.00]] hi"]

    def test_scaler_process_scales_raw_s16le(self):
        """The scaler child (`python -c _SCALER_SRC factor`) halves every sample."""
        pcm = make_sine_pcm(amplitude=20000)
        result = real_subprocess.run(
            [sys.executable, "-c", tts._SCALER_SRC, "0.5"],
            input=pcm, stdout=real_subprocess.PIPE, timeout=30,
        )
        assert result.returncode == 0
        assert pcm_samples(result.stdout) == [int(s * 0.5) for s in pcm_samples(pcm)]

    def test_piper_chain_is_fully_detached_below_full_volume(self, monkeypatch, which):
        """Below 1.0 the chain is piper -> scaler process -> aplay: three real
        subprocesses wired stdout-to-stdin, no in-process pump thread, so the
        final utterance keeps playing after the app exits (the detached-playback
        NOTE in tts.py) at any volume, not just >= 1.0."""
        audio.settings.volume = 0.5
        which("piper", "aplay")
        monkeypatch.setattr(tts, "_piper_model", lambda: "/fake/model.onnx")
        monkeypatch.setattr(tts, "_piper_sample_rate", lambda _m: 22050)

        spawned = []

        class Proc:
            def __init__(self, cmd, stdin=None, stdout=None, **_kw):
                self.cmd = cmd
                self.given_stdin = stdin
                self.stdin = io.BytesIO() if stdin == real_subprocess.PIPE else stdin
                self.stdout = (
                    io.BytesIO() if stdout == real_subprocess.PIPE else stdout
                )

            def poll(self):
                return None

        monkeypatch.setattr(
            tts, "subprocess",
            types.SimpleNamespace(
                Popen=lambda cmd, **kw: spawned.append(Proc(cmd, **kw)) or spawned[-1],
                PIPE=real_subprocess.PIPE,
                DEVNULL=real_subprocess.DEVNULL,
            ),
        )

        procs = tts._start_piper("hello")
        assert procs is not None and len(procs) == 3
        piper, scaler, aplay = procs
        assert piper.cmd[0] == "piper"
        assert scaler.cmd[:2] == [sys.executable, "-c"]
        assert scaler.cmd[2] == tts._SCALER_SRC
        assert float(scaler.cmd[3]) == 0.5
        assert aplay.cmd[0] == "aplay"
        # wiring: piper stdout feeds the scaler, scaler stdout feeds aplay
        assert scaler.given_stdin is piper.stdout
        assert aplay.given_stdin is scaler.stdout

    def test_piper_pipes_straight_to_aplay_at_full_volume(self, monkeypatch, which):
        audio.settings.volume = 1.0
        which("piper", "aplay")
        monkeypatch.setattr(tts, "_piper_model", lambda: "/fake/model.onnx")
        monkeypatch.setattr(tts, "_piper_sample_rate", lambda _m: 22050)

        spawned = []

        class Proc:
            def __init__(self, cmd, stdin=None, stdout=None, **_kw):
                self.cmd = cmd
                self.given_stdin = stdin
                self.stdin = io.BytesIO() if stdin == real_subprocess.PIPE else stdin
                self.stdout = io.BytesIO() if stdout == real_subprocess.PIPE else stdout

            def poll(self):
                return None

        monkeypatch.setattr(
            tts, "subprocess",
            types.SimpleNamespace(
                Popen=lambda cmd, **kw: spawned.append(Proc(cmd, **kw)) or spawned[-1],
                PIPE=real_subprocess.PIPE,
                DEVNULL=real_subprocess.DEVNULL,
            ),
        )

        procs = tts._start_piper("hello")
        assert procs is not None and len(procs) == 2
        piper, aplay = procs
        assert aplay.given_stdin is piper.stdout  # no scaler in between


class TestSkipAtZero:
    def test_say_when_muted_sets_caption_but_speaks_nothing(self, no_audio):
        audio.settings.muted = True
        tts.say("Next up: squats")
        assert tts.current_caption()[0] == "Next up: squats"  # caption BEFORE mute check
        assert no_audio.spoken == []

    def test_say_sync_when_muted_sets_caption_but_speaks_nothing(self, no_audio):
        audio.settings.muted = True
        tts.say_sync("Done")
        assert tts.current_caption()[0] == "Done"
        assert no_audio.spoken == []

    def test_say_at_zero_volume_skips_speech(self, no_audio):
        audio.settings.volume = 0.0
        tts.say("hello")
        assert no_audio.spoken == []

    def test_unmuted_speech_still_flows(self, no_audio):
        tts.say("hello")
        assert no_audio.spoken == ["hello"]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_round_trip(self, isolated_state):
        audio.settings.volume = 0.3
        audio.settings.muted = True
        audio.save_settings()
        audio.settings.volume = 0.7
        audio.settings.muted = False
        audio.load_settings()
        assert audio.settings.volume == 0.3
        assert audio.settings.muted is True

    def test_settings_live_next_to_state(self, isolated_state):
        audio.save_settings()
        assert (isolated_state / "settings.json").exists()

    def test_step_persists_only_the_volume_key(self, isolated_state):
        audio.settings.step_down()
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data == {"volume": 0.6}  # merge-write: untouched keys stay absent

    def test_toggle_mute_persists_on_change(self, isolated_state):
        audio.settings.toggle_mute()
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data["muted"] is True

    def test_mute_press_does_not_persist_transient_volume_flag(self, isolated_state):
        # User has 0.9 persisted, runs with --volume 45 (not persisted), then
        # presses 'm': only "muted" may be written — the saved 0.9 must survive.
        audio.settings.volume = 0.9
        audio.save_settings()
        audio.settings.set_volume(0.45)  # what the CLI flag does
        audio.settings.toggle_mute()     # the 'm' key
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data == {"volume": 0.9, "muted": True}

    def test_volume_step_does_not_persist_transient_mute_flag(self, isolated_state):
        audio.save_settings()  # persisted: 0.7, unmuted
        audio.settings.muted = True  # what --mute does
        audio.settings.step_down()   # the '-' key
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data == {"volume": 0.6, "muted": False}

    def test_missing_file_keeps_defaults(self, isolated_state):
        audio.load_settings()
        assert audio.settings.volume == 0.7
        assert audio.settings.muted is False

    def test_corrupt_file_keeps_defaults(self, isolated_state):
        isolated_state.mkdir(parents=True, exist_ok=True)
        (isolated_state / "settings.json").write_text("{not json")
        audio.load_settings()
        assert audio.settings.volume == 0.7
        assert audio.settings.muted is False

    @pytest.mark.parametrize("content", ["null", "[1, 2]", '"x"', "42"])
    def test_valid_json_non_object_keeps_defaults(self, isolated_state, content):
        # e.g. `echo null > settings.json` must not crash the app at launch
        isolated_state.mkdir(parents=True, exist_ok=True)
        (isolated_state / "settings.json").write_text(content)
        audio.load_settings()
        assert audio.settings.volume == 0.7
        assert audio.settings.muted is False

    @pytest.mark.parametrize("content", ["null", "[1, 2]"])
    def test_save_replaces_non_object_file(self, isolated_state, content):
        isolated_state.mkdir(parents=True, exist_ok=True)
        (isolated_state / "settings.json").write_text(content)
        audio.settings.toggle_mute()
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data == {"muted": True}

    def test_loaded_volume_is_clamped(self, isolated_state):
        isolated_state.mkdir(parents=True, exist_ok=True)
        (isolated_state / "settings.json").write_text(json.dumps({"volume": 4.2, "muted": False}))
        audio.load_settings()
        assert audio.settings.volume == 1.0


# ---------------------------------------------------------------------------
# CLI flags
# ---------------------------------------------------------------------------

class TestCliFlags:
    def test_volume_and_mute_flags_override_persisted(self, isolated_state, monkeypatch, capsys):
        audio.settings.volume = 0.9
        audio.save_settings()
        # --reset exits early, right after the settings are applied.
        monkeypatch.setattr("sys.argv", ["coach", "--reset", "--volume", "45", "--mute"])
        cli.main()
        assert audio.settings.volume == 0.45
        assert audio.settings.muted is True
        # flags must NOT persist: the file still holds the saved 0.9
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data["volume"] == 0.9
        assert data["muted"] is False

    def test_persisted_settings_load_at_startup(self, isolated_state, monkeypatch, capsys):
        audio.settings.volume = 0.2
        audio.settings.muted = True
        audio.save_settings()
        audio.settings.volume = 0.7
        audio.settings.muted = False
        monkeypatch.setattr("sys.argv", ["coach", "--reset"])
        cli.main()
        assert audio.settings.volume == 0.2
        assert audio.settings.muted is True


# ---------------------------------------------------------------------------
# run_screen global keys + footer
# ---------------------------------------------------------------------------

def run_keys(keys, keymap=None, tick=None):
    """Drive run_screen headlessly with a scripted key sequence."""
    queue = list(keys)
    return screens.run_screen(
        None, lambda: None, keymap or {"enter": Event.DONE}, tick,
        read_key=lambda: queue.pop(0) if queue else "",
        sleep=lambda _s: None,
    )


class TestRunScreenVolumeKeys:
    def test_minus_plus_m_adjust_without_leaving_screen(self):
        ev = run_keys(["-", "-", "+", "m", "enter"])
        assert ev is Event.DONE  # only 'enter' ended the screen
        assert audio.settings.volume == 0.6  # 0.7 - 0.1 - 0.1 + 0.1
        assert audio.settings.muted is True

    def test_equals_is_plus_alias(self):
        ev = run_keys(["=", "enter"])
        assert ev is Event.DONE
        assert audio.settings.volume == 0.8

    def test_second_m_unmutes(self):
        run_keys(["m", "m", "enter"])
        assert audio.settings.muted is False

    def test_hard_mute_stops_inflight_speech_and_chimes(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(screens, "terminate_say", lambda: calls.append("say"))
        monkeypatch.setattr(screens, "stop_sounds", lambda: calls.append("sounds"))
        run_keys(["m", "enter"])
        assert audio.settings.muted is True
        assert calls == ["say", "sounds"]  # muting silences what's playing NOW

    def test_unmute_does_not_touch_inflight_audio(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(screens, "terminate_say", lambda: calls.append("say"))
        monkeypatch.setattr(screens, "stop_sounds", lambda: calls.append("sounds"))
        audio.settings.muted = True
        run_keys(["m", "enter"])
        assert audio.settings.muted is False
        assert calls == []

    def test_stop_sounds_terminates_live_players_only(self, monkeypatch):
        live = types.SimpleNamespace(
            poll=lambda: None, terminated=[], terminate=lambda: live.terminated.append(True),
        )
        done = types.SimpleNamespace(
            poll=lambda: 0, terminated=[], terminate=lambda: done.terminated.append(True),
        )
        monkeypatch.setattr(audio, "_players", [live, done])
        audio.stop_sounds()
        assert live.terminated == [True]
        assert done.terminated == []

    def test_screen_specific_mapping_wins_over_global_key(self):
        # A screen that maps 'm' itself must receive it — the global mute
        # handler must not consume screen-specific keys.
        ev = run_keys(["m"], keymap={"m": Event.SKIP})
        assert ev is Event.SKIP
        assert audio.settings.muted is False

    def test_volume_keys_do_not_disturb_timers(self):
        # A tick-driven timer must end exactly on schedule regardless of how
        # many volume keys were pressed along the way.
        ticks = {"n": 0}

        def tick():
            ticks["n"] += 1
            return Event.DONE if ticks["n"] >= 10 else None

        ev = run_keys(["-", "+", "m", "m", "="], tick=tick)
        assert ev is Event.DONE
        assert ticks["n"] == 10  # ended on the tick schedule, not early
        assert audio.settings.volume == 0.8  # -10 +10 +10
        assert audio.settings.muted is False  # m pressed twice

    def test_footer_shows_volume_percent(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette())
        assert "🔊 70%" in ui.build_progress_bar(cassette)
        audio.settings.step_up()
        assert "🔊 80%" in ui.build_progress_bar(cassette)

    def test_footer_shows_mute_icon(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette())
        audio.settings.toggle_mute()
        bar = ui.build_progress_bar(cassette)
        assert "🔇" in bar
        assert "🔊" not in bar


# ---------------------------------------------------------------------------
# Timed hold "Get in position" countdown: keys work there too
# ---------------------------------------------------------------------------

class TestCountdownKeys:
    """The 3s pre-hold countdown is a real screen, not a blind sleep loop:
    volume keys (and the hold's own s/b) must not be silently dropped."""

    def _run_timed_hold(self, timed_cassette, keys):
        from io import StringIO

        from rich.console import Console
        from rich.live import Live

        cassette = load_cassette_from_dict(
            timed_cassette(rounds=1, seconds=3, cues=[[]])
        )
        group = cassette.phases[0].groups[0]
        ex = group.exercises[0]
        clock = {"t": 1_000_000.0}
        queue = list(keys)
        live = Live(console=Console(file=StringIO(), force_terminal=True, width=100, height=40))
        return screens.timed_hold(
            live, cassette, 0, 0, group, ex, 0, 0,
            now=lambda: clock["t"],
            read_key=lambda: queue.pop(0) if queue else "",
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        )

    def test_volume_and_mute_keys_apply_during_countdown(self, timed_cassette, no_audio):
        ev = self._run_timed_hold(timed_cassette, ["-", "m"])
        assert ev is Event.DONE  # keys never end the countdown; the hold still runs
        assert audio.settings.volume == 0.6
        assert audio.settings.muted is True

    def test_skip_during_countdown_skips_the_hold(self, timed_cassette, no_audio):
        ev = self._run_timed_hold(timed_cassette, ["s"])
        assert ev is Event.SKIP
        assert "Go" not in no_audio.spoken  # the hold never started

    def test_back_during_countdown_backs_out(self, timed_cassette, no_audio):
        ev = self._run_timed_hold(timed_cassette, ["b"])
        assert ev is Event.BACK
