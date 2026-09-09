"""TDD (Step 2) for read_ledger_recorder + lib.read_ledger (t679 / c162).

RED-first: every test here fails before lib/read_ledger.py and
read_ledger_recorder.py exist.

The ledger's meaning under test: an entry is "the exact bytes this session last SAW or
PRODUCED at this path". Read refreshes it; so does the session's own Write/Edit.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

rl = importlib.import_module("lib.read_ledger")
recorder = importlib.import_module("read_ledger_recorder")

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _isolated_ledger(tmp_path, monkeypatch):
    """Never touch the real ~/.claude/state/read_ledger during tests, and treat tmp_path
    as the in-scope estate so the scope narrowing can be exercised without writing to
    the real ~/.claude."""
    monkeypatch.setattr(rl, "LEDGER_DIR", tmp_path / "read_ledger")
    monkeypatch.setattr(recorder, "ESTATE_ROOT",
                        str(tmp_path).replace("\\", "/").casefold())
    yield


def _payload(tool, path, sid=SID):
    return {"tool_name": tool, "tool_input": {"file_path": str(path)}, "session_id": sid}


# --------------------------------------------------------------- lib: hashing

def test_file_hash_is_sha256_of_bytes(tmp_path):
    import hashlib
    f = tmp_path / "a.py"
    f.write_bytes(b"print(1)\n")
    assert rl.file_hash(f) == hashlib.sha256(b"print(1)\n").hexdigest()


def test_file_hash_changes_with_content(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"one")
    h1 = rl.file_hash(f)
    f.write_bytes(b"two")
    assert rl.file_hash(f) != h1


def test_file_hash_missing_file_is_none(tmp_path):
    assert rl.file_hash(tmp_path / "nope.py") is None


def test_file_hash_directory_is_none(tmp_path):
    assert rl.file_hash(tmp_path) is None


def test_file_hash_oversize_file_is_none(tmp_path, monkeypatch):
    """A huge file is not hashed -> no entry -> the gate allows. Fails OPEN."""
    monkeypatch.setattr(rl, "MAX_HASH_BYTES", 4)
    f = tmp_path / "big.py"
    f.write_bytes(b"more than four bytes")
    assert rl.file_hash(f) is None


# --------------------------------------------------------------- lib: ledger

def test_record_then_lookup_roundtrip(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"x")
    h = rl.record(SID, f)
    assert h and rl.lookup(SID, f) == h


def test_lookup_unknown_path_is_none(tmp_path):
    assert rl.lookup(SID, tmp_path / "never_read.py") is None


def test_lookup_is_session_scoped(tmp_path):
    """Another session's ledger entry must never answer for mine."""
    f = tmp_path / "a.py"
    f.write_bytes(b"x")
    rl.record("other-sid", f)
    assert rl.lookup(SID, f) is None


def test_record_overwrites_previous_hash(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"one")
    rl.record(SID, f)
    f.write_bytes(b"two")
    h2 = rl.record(SID, f)
    assert rl.lookup(SID, f) == h2


def test_lookup_path_spelling_insensitive(tmp_path):
    """Windows paths are case-insensitive (t355) and separators vary."""
    f = tmp_path / "Aa.py"
    f.write_bytes(b"x")
    rl.record(SID, f)
    weird = str(f).replace("/", "\\").swapcase() if "\\" in str(f) else str(f)
    assert rl.lookup(SID, str(f).replace("/", "\\")) is not None
    assert rl.lookup(SID, weird) is not None or weird == str(f)


def test_load_corrupt_ledger_is_empty_not_raise(tmp_path):
    rl.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    rl.ledger_path(SID).write_text("{not json", encoding="utf-8")
    assert rl.load(SID) == {}


def test_record_prunes_to_max_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(rl, "MAX_ENTRIES", 3)
    for i in range(6):
        f = tmp_path / f"f{i}.py"
        f.write_bytes(bytes([i]))
        rl.record(SID, f)
    assert len(rl.load(SID)) <= 3


def test_record_missing_file_records_nothing(tmp_path):
    assert rl.record(SID, tmp_path / "ghost.py") is None
    assert rl.load(SID) == {}


# --------------------------------------------------------------- hook behaviour

def test_read_records_hash(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"content")
    assert recorder.run(_payload("Read", f)) == 0
    assert rl.lookup(SID, f) == rl.file_hash(f)


def test_write_refreshes_hash(tmp_path):
    """My own Write must refresh the ledger, or my second Write false-blocks."""
    f = tmp_path / "a.py"
    f.write_bytes(b"v1")
    recorder.run(_payload("Read", f))
    f.write_bytes(b"v2")
    recorder.run(_payload("Write", f))
    assert rl.lookup(SID, f) == rl.file_hash(f)


def test_edit_refreshes_hash(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"v1")
    recorder.run(_payload("Read", f))
    f.write_bytes(b"v2")
    recorder.run(_payload("Edit", f))
    assert rl.lookup(SID, f) == rl.file_hash(f)


def test_no_session_id_records_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    f = tmp_path / "a.py"
    f.write_bytes(b"x")
    p = _payload("Read", f)
    p.pop("session_id")
    assert recorder.run(p) == 0
    assert rl.load(SID) == {}


def test_out_of_scope_path_records_nothing(tmp_path, monkeypatch):
    """Scope narrowing: only files the gate can act on are hashed. The cost of this hook
    is paid on EVERY Read, so hashing workspace files buys nothing."""
    monkeypatch.setattr(recorder, "ESTATE_ROOT",
                        str(tmp_path / "estate").replace("\\", "/").casefold())
    outside = tmp_path / "workspace" / "big.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"x")
    assert recorder.run(_payload("Read", outside)) == 0
    assert rl.lookup(SID, outside) is None


def test_in_scope_path_still_records(tmp_path, monkeypatch):
    """Discriminator for the test above — the scope check must not disable everything."""
    estate = tmp_path / "estate"
    monkeypatch.setattr(recorder, "ESTATE_ROOT",
                        str(estate).replace("\\", "/").casefold())
    inside = estate / "hooks" / "x.py"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_bytes(b"x")
    assert recorder.run(_payload("Read", inside)) == 0
    assert rl.lookup(SID, inside) == rl.file_hash(inside)


def test_unrelated_tool_records_nothing(tmp_path):
    f = tmp_path / "a.py"
    f.write_bytes(b"x")
    assert recorder.run(_payload("Bash", f)) == 0
    assert rl.lookup(SID, f) is None


def test_disable_flag_short_circuits(tmp_path, monkeypatch):
    flag = tmp_path / "disable.flag"
    flag.write_text("", encoding="utf-8")
    monkeypatch.setattr(recorder, "DISABLE_FLAG", flag)
    f = tmp_path / "a.py"
    f.write_bytes(b"x")
    assert recorder.run(_payload("Read", f)) == 0
    assert rl.lookup(SID, f) is None


def test_observer_never_blocks_on_internal_error(tmp_path, monkeypatch):
    """[1] fail-open: a broken ledger layer must still exit 0."""
    def boom(*a, **k):
        raise RuntimeError("ledger exploded")
    monkeypatch.setattr(rl, "record", boom)
    f = tmp_path / "a.py"
    f.write_bytes(b"x")
    assert recorder.run(_payload("Read", f)) == 0


def test_bad_payload_never_blocks():
    assert recorder.run({}) == 0
    assert recorder.run({"tool_name": "Read", "tool_input": {}}) == 0
