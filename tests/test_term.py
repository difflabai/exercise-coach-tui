"""term key reading: chunk splitting (PR B fix).

Key auto-repeat (or two presses within one poll window) coalesces several
escape sequences / characters into a single os.read chunk; every press must
still come out of read_key, one per call — never a silently dropped chunk.

Hermetic: split_keys is pure; read_key is driven with the term module's
stdin_ready/os/sys references stubbed (no TTY, no real stdin).
"""

import types

import pytest

from exercise_coach import term


@pytest.fixture(autouse=True)
def clear_pending_keys():
    """read_key's carry-over queue is module-global — keep tests independent."""
    term._pending_keys.clear()
    yield
    term._pending_keys.clear()


class TestSplitKeys:
    def test_single_sequences_keep_their_names(self):
        assert term.split_keys(b"\x1b[A") == ["up"]
        assert term.split_keys(b"\x1bOB") == ["down"]
        assert term.split_keys(b"\r") == ["enter"]
        assert term.split_keys(b"\n") == ["enter"]
        assert term.split_keys(b"\x1a") == ["ctrl-z"]
        assert term.split_keys(b"\x1b") == ["esc"]

    def test_coalesced_arrow_repeat_yields_every_press(self):
        # Holding an arrow key puts several sequences into one read chunk;
        # the old whole-chunk lookup dropped all of them (menu stutter bug).
        assert term.split_keys(b"\x1b[B\x1b[B\x1b[B") == ["down", "down", "down"]

    def test_mixed_modes_arrows_digit_and_enter(self):
        assert term.split_keys(b"\x1bOA\x1b[B2\r") == ["up", "down", "2", "enter"]

    def test_digit_then_enter_typed_fast(self):
        assert term.split_keys(b"2\r") == ["2", "enter"]

    def test_esc_followed_by_plain_char(self):
        assert term.split_keys(b"\x1bq") == ["esc", "q"]

    def test_unknown_escape_sequences_are_dropped_whole(self):
        # Home (\x1b[H), a parameterized sequence, and an SS3 key: none of
        # their payload bytes may leak through as ordinary characters (a
        # leaked "esc" would cancel the jump menu on a stray Home press).
        assert term.split_keys(b"\x1b[H\x1b[B") == ["down"]
        assert term.split_keys(b"\x1b[1;5C") == []
        assert term.split_keys(b"\x1bOH") == []

    def test_text_is_lowercased_one_key_per_character(self):
        assert term.split_keys(b"JQ") == ["j", "q"]

    def test_multibyte_utf8_char_is_one_key(self):
        assert term.split_keys("²é".encode()) == ["²", "é"]


class TestReadKeyQueue:
    def stub_stdin(self, monkeypatch, chunks):
        chunks = list(chunks)
        monkeypatch.setattr(term, "stdin_ready", lambda: bool(chunks))
        monkeypatch.setattr(
            term, "os", types.SimpleNamespace(read=lambda _fd, _n: chunks.pop(0)),
        )
        monkeypatch.setattr(
            term, "sys",
            types.SimpleNamespace(stdin=types.SimpleNamespace(fileno=lambda: 0)),
        )

    def test_read_key_returns_queued_keys_one_per_call(self, monkeypatch):
        self.stub_stdin(monkeypatch, [b"\x1b[B\x1b[B\r"])
        assert term.read_key() == "down"
        assert term.read_key() == "down"
        assert term.read_key() == "enter"
        assert term.read_key() == ""  # nothing left

    def test_drain_stdin_flushes_already_split_keys_too(self, monkeypatch):
        self.stub_stdin(monkeypatch, [b"\x1b[B\x1b[B"])
        assert term.read_key() == "down"
        term.drain_stdin()  # type-ahead flush must include the split queue
        assert term.read_key() == ""
