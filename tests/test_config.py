"""Tests for the config.toml settings file (recommendations §7, PR D).

Covers: the TOML writer/reader round-trip (including exercise names with
quotes/backslashes/unicode in [paces]), unknown-key preservation on every
merge-save, the one-shot settings.json -> config.toml migration, runtime key
presses writing config.toml (never the old settings.json) post-migration,
the new voice / rest_default keys and their CLI precedence, and --quiet.

All hermetic: XDG_CONFIG_HOME/XDG_DATA_HOME point at tmp_path (conftest),
cli.play_cassette is replaced with a recorder where a run would start.
"""

import json

import pytest

from exercise_coach import audio, cli, screens, tts
from exercise_coach import settings as user_settings
from exercise_coach.events import Event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_config(text: str):
    path = user_settings.config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def write_legacy_json(data):
    path = user_settings.legacy_settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def run_main(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["coach", *argv])
    cli.main()


@pytest.fixture
def played(monkeypatch):
    """Replace cli.play_cassette with a recorder of (cassette, cassette_path)."""
    calls = []

    def _record(cassette, cassette_path=None, **_kwargs):
        calls.append((cassette, cassette_path))

    monkeypatch.setattr(cli, "play_cassette", _record)
    return calls


# ---------------------------------------------------------------------------
# TOML round-trip
# ---------------------------------------------------------------------------


class TestTomlRoundTrip:
    def test_scalars_round_trip(self, isolated_state):
        data = {
            "volume": 0.55, "muted": True, "buddy": False,
            "voice": "en_GB-alan-medium.onnx", "rest_default": 60,
        }
        user_settings.save_data(data)
        assert user_settings.load_data() == data

    def test_weird_pace_names_round_trip(self, isolated_state):
        """Exercise names become quoted TOML keys — quotes, backslashes,
        unicode, and control characters must all survive the round-trip."""
        paces = {
            'Farmer "Suitcase" Carry': 40,
            "Über-Row (Meadows')": 25,
            "Back\\Slash Raise": 19,
            "Tab\tand\nnewline": 22,
            "плие squat — 深蹲": 31,
        }
        user_settings.update_paces(paces)
        user_settings.paces = {}
        user_settings.load_paces()
        assert user_settings.paces == paces

    def test_lists_and_nested_tables_round_trip(self, isolated_state):
        """The writer must reproduce everything tomllib can hand back, or a
        merge-save would corrupt a hand-edited file."""
        data = {
            "volume": 0.7,
            "tags": ["morning", "gym", 3],
            "custom": {"nested": {"deep": True}, "flag": "x"},
        }
        user_settings.save_data(data)
        assert user_settings.load_data() == data

    def test_unknown_user_keys_survive_a_merge_save(self, isolated_state):
        write_config(
            "# my note\n"
            'theme = "dark"\n'
            "\n"
            "[keybindings]\n"
            'skip = "s"\n'
            "\n"
            "[keybindings.alt]\n"
            'skip = "S"\n'
        )
        audio.settings.step_down()  # persists {"volume": 0.6} via save_data
        data = user_settings.load_data()
        assert data["volume"] == 0.6
        assert data["theme"] == "dark"
        assert data["keybindings"] == {"skip": "s", "alt": {"skip": "S"}}

    def test_corrupt_file_loads_as_empty(self, isolated_state):
        write_config("volume = = 0.5\n")
        assert user_settings.load_data() == {}

    def test_non_utf8_file_loads_as_empty_and_is_replaced_on_save(self, isolated_state):
        """tomllib raises UnicodeDecodeError (not TOMLDecodeError) on invalid
        UTF-8 — e.g. a write truncated inside a multibyte pace name. It must
        read as {} (never crash startup) and the next save must replace it."""
        path = user_settings.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'volume = 0.5\n[paces]\n"\xc3')  # cut mid-multibyte
        assert user_settings.load_data() == {}
        user_settings.save_data({"volume": 0.8})
        assert user_settings.load_data() == {"volume": 0.8}

    def test_non_utf8_file_does_not_crash_cli_startup(self, isolated_state, monkeypatch, capsys):
        path = user_settings.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'volume = 0.5\n[paces]\n"\xc3')
        run_main(monkeypatch, "--reset")  # load_settings runs before --reset returns
        assert "State cleared" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# settings.json -> config.toml migration
# ---------------------------------------------------------------------------


class TestMigration:
    def test_migrates_all_keys_once_and_keeps_the_json(self, isolated_state):
        legacy = write_legacy_json({
            "volume": 0.4, "muted": True, "buddy": False,
            "paces": {"Goblet Squat": 22, 'Say "Uncle"': 30},
        })
        legacy_text = legacy.read_text()

        user_settings.migrate_legacy_settings()

        assert user_settings.load_data() == {
            "volume": 0.4, "muted": True, "buddy": False,
            "paces": {"Goblet Squat": 22, 'Say "Uncle"': 30},
        }
        # The old file is left in place, byte-identical (never delete user data).
        assert legacy.read_text() == legacy_text

    def test_second_migration_does_not_clobber_config(self, isolated_state):
        write_legacy_json({"volume": 0.4})
        user_settings.migrate_legacy_settings()
        user_settings.save_data({"volume": 0.9})  # user moved on since

        user_settings.migrate_legacy_settings()  # e.g. next app launch

        assert user_settings.load_data()["volume"] == 0.9

    def test_nothing_to_migrate_creates_no_config(self, isolated_state):
        user_settings.migrate_legacy_settings()
        assert not user_settings.config_file().exists()

    @pytest.mark.parametrize("content", ["{not json", "null", "[1, 2]"])
    def test_corrupt_or_non_object_json_is_ignored(self, isolated_state, content):
        write_legacy_json(content)
        user_settings.migrate_legacy_settings()
        assert not user_settings.config_file().exists()

    def test_json_nulls_are_dropped_without_aborting(self, isolated_state):
        """TOML has no null: a stray JSON null must cost only its own key."""
        write_legacy_json({"volume": 0.4, "broken": None, "paces": {"Squat": None, "Row": 20}})
        user_settings.migrate_legacy_settings()
        assert user_settings.load_data() == {"volume": 0.4, "paces": {"Row": 20}}

    def test_cli_startup_migrates_and_loads(self, isolated_state, monkeypatch, capsys):
        write_legacy_json({"volume": 0.3, "muted": True, "buddy": False})
        run_main(monkeypatch, "--reset")  # exits right after settings load
        assert user_settings.config_file().exists()
        assert audio.settings.volume == 0.3
        assert audio.settings.muted is True
        assert user_settings.buddy_enabled is False

    def test_volume_key_press_writes_config_toml_not_settings_json(
        self, isolated_state, monkeypatch, capsys,
    ):
        """Post-migration, runtime persistence ('-' key) must land in
        config.toml; the orphaned settings.json stays byte-identical."""
        legacy = write_legacy_json({"volume": 0.7})
        run_main(monkeypatch, "--reset")  # migrates + loads
        legacy_text = legacy.read_text()

        # The global '-' key, exactly as run_screen dispatches it.
        queue = ["-", "enter"]
        screens.run_screen(
            None, lambda: None, {"enter": Event.DONE},
            read_key=lambda: queue.pop(0) if queue else "",
            sleep=lambda _s: None,
        )

        assert user_settings.load_data()["volume"] == 0.6
        assert legacy.read_text() == legacy_text  # old file untouched
        assert json.loads(legacy_text)["volume"] == 0.7


# ---------------------------------------------------------------------------
# voice: preferred piper voice
# ---------------------------------------------------------------------------


class TestVoiceSetting:
    def test_preferred_voice_reads_the_key(self, isolated_state):
        user_settings.save_data({"voice": "en_GB-alan-medium.onnx"})
        assert user_settings.preferred_voice() == "en_GB-alan-medium.onnx"

    @pytest.mark.parametrize("value", ['""', "42", "true"])
    def test_missing_or_invalid_voice_is_none(self, isolated_state, value):
        assert user_settings.preferred_voice() is None
        write_config(f"voice = {value}\n")
        assert user_settings.preferred_voice() is None

    def test_cli_feeds_the_voice_into_tts(self, isolated_state, monkeypatch, capsys):
        write_config('voice = "en_GB-alan-medium.onnx"\n')
        run_main(monkeypatch, "--reset")
        assert tts.PIPER_PREFERRED_VOICE == "en_GB-alan-medium.onnx"

    def test_no_voice_key_keeps_the_default(self, isolated_state, monkeypatch, capsys):
        default = tts.PIPER_PREFERRED_VOICE
        run_main(monkeypatch, "--reset")
        assert tts.PIPER_PREFERRED_VOICE == default

    def test_piper_model_scan_prefers_the_configured_voice(
        self, isolated_state, monkeypatch, tmp_path,
    ):
        """The config voice slots into _piper_model's preferred-voice scan."""
        voices = tmp_path / "piper-voices"
        voices.mkdir()
        (voices / "aaa-first.onnx").write_bytes(b"x")  # sorted() would win otherwise
        (voices / "en_GB-alan-medium.onnx").write_bytes(b"x")
        monkeypatch.delenv("PIPER_MODEL", raising=False)
        monkeypatch.setattr(
            tts.os.path, "expanduser",
            lambda p: str(voices) if p == "~/.local/share/piper-voices" else "/nonexistent",
        )
        tts.PIPER_PREFERRED_VOICE = "en_GB-alan-medium.onnx"  # what cli.main does
        assert tts._piper_model() == str(voices / "en_GB-alan-medium.onnx")


# ---------------------------------------------------------------------------
# rest_default: config fallback, flags and cassettes override
# ---------------------------------------------------------------------------


class TestRestDefault:
    def test_reads_valid_int(self, isolated_state):
        user_settings.save_data({"rest_default": 45})
        assert user_settings.rest_default() == 45

    @pytest.mark.parametrize("value", ['"90"', "0", "-5", "true", "60.5"])
    def test_missing_or_invalid_falls_back_to_75(self, isolated_state, value):
        assert user_settings.rest_default() == 75
        write_config(f"rest_default = {value}\n")
        assert user_settings.rest_default() == 75

    def test_text_input_uses_config_rest_default(self, played, tmp_path, monkeypatch):
        write_config("rest_default = 45\n")
        file = tmp_path / "workout.txt"
        file.write_text("Squats 3x10 | 24kg\n")

        run_main(monkeypatch, str(file))

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 45

    def test_rest_flag_overrides_config(self, played, tmp_path, monkeypatch):
        write_config("rest_default = 45\n")
        file = tmp_path / "workout.txt"
        file.write_text("Squats 3x10 | 24kg\n")

        run_main(monkeypatch, str(file), "--rest", "20")

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 20

    def test_json_cassette_rest_beats_config(
        self, played, straight_cassette, tmp_path, monkeypatch,
    ):
        """rest_default only fills the gap when the cassette provides no rest;
        a JSON cassette's own rest always wins over the config."""
        write_config("rest_default = 45\n")
        file = tmp_path / "workout.json"
        file.write_text(json.dumps(straight_cassette(rest=90)))

        run_main(monkeypatch, str(file))

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 90

    def test_config_fills_json_cassette_with_no_rest_anywhere(
        self, played, tmp_path, monkeypatch,
    ):
        """A JSON cassette with neither group rest nor meta.rest_default is
        out-of-spec but tolerated; the persisted rest_default must fill the
        gap (not the hardcoded 75) — the exact case its docstring promises."""
        write_config("rest_default = 120\n")
        raw = {
            "version": "1.1", "meta": {"title": "Bare"},
            "phases": [{"type": "main", "groups": [
                {"type": "straight", "rounds": 2,
                 "exercises": [{"name": "Squat", "reps": 10}]},
            ]}],
        }
        file = tmp_path / "bare.json"
        file.write_text(json.dumps(raw))

        run_main(monkeypatch, str(file))

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 120

    def test_json_meta_rest_default_beats_config(self, played, tmp_path, monkeypatch):
        write_config("rest_default = 120\n")
        raw = {
            "version": "1.1", "meta": {"title": "Meta", "rest_default": 90},
            "phases": [{"type": "main", "groups": [
                {"type": "straight", "rounds": 2,
                 "exercises": [{"name": "Squat", "reps": 10}]},
            ]}],
        }
        file = tmp_path / "meta.json"
        file.write_text(json.dumps(raw))

        run_main(monkeypatch, str(file))

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 90


# ---------------------------------------------------------------------------
# --quiet: --mute --no-buddy for one run, persisting nothing
# ---------------------------------------------------------------------------


class TestQuietFlag:
    def test_quiet_mutes_and_hides_buddy_in_memory(self, isolated_state, monkeypatch, capsys):
        run_main(monkeypatch, "--quiet", "--reset")
        assert audio.settings.muted is True
        assert user_settings.buddy_enabled is False

    def test_quiet_persists_nothing_when_no_config_exists(
        self, isolated_state, monkeypatch, capsys,
    ):
        run_main(monkeypatch, "--quiet", "--reset")
        assert not user_settings.config_file().exists()
        assert not user_settings.legacy_settings_file().exists()

    def test_quiet_leaves_an_existing_config_byte_identical(
        self, isolated_state, monkeypatch, capsys,
    ):
        path = write_config('volume = 0.8\nmuted = false\nbuddy = true\n')
        before = path.read_text()

        run_main(monkeypatch, "--quiet", "--reset")

        assert path.read_text() == before  # day mode: nothing written
        assert audio.settings.volume == 0.8  # persisted settings still loaded
        assert audio.settings.muted is True  # ...then quiet mutes in memory
        assert user_settings.buddy_enabled is False

    def test_quiet_overrides_persisted_buddy_on_for_the_run(
        self, isolated_state, monkeypatch, capsys,
    ):
        write_config("buddy = true\n")
        run_main(monkeypatch, "--quiet", "--reset")
        assert user_settings.buddy_enabled is False
        assert user_settings.load_data() == {"buddy": True}  # file unchanged
