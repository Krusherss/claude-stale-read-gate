"""TDD (Step 2) for stale_read_write_gate (t679 / c162).

RED-first: every test fails before stale_read_write_gate.py exists.

The gate's one true positive: a `Write` whose target's bytes on disk differ from the
bytes this session last saw or produced. Everything else allows — the fail-open
direction table in validation/stale_read_write_gate/tier-score.md is pinned here.
"""
import importlib
import sys
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_HOME = _REPO.parent

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

rl = importlib.import_module("lib.read_ledger")
gate = importlib.import_module("stale_read_write_gate")

SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
OTHER = "99999999-8888-7777-6666-555555555555"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Isolate the ledger, the SOAK LOG, and treat tmp_path as the protected estate
    root so the scope check can be exercised without writing to the real ~/.claude.

    LOG_FILE redirect (t753 leg A): without it, every run of this suite appends its
    synthetic block/would-block rows to the LIVE soak log — the denominator the
    2026-07-29 unwire decision is read off. Measured 2026-07-26: 27 of that log's 45
    rows were this suite's, targeting pytest tmpdirs. Same class as t751 finding 2.
    """
    monkeypatch.setattr(rl, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(gate, "ESTATE_ROOT", gate._norm(str(tmp_path / "estate")))
    monkeypatch.setattr(gate, "LOG_FILE",
                        tmp_path / "logs" / "stale_read_write_gate.jsonl")
    monkeypatch.setenv("STALE_READ_WRITE_GATE_MODE", "block")
    (tmp_path / "estate").mkdir(parents=True, exist_ok=True)
    yield


def _estate_file(tmp_path, name="target.py", content=b"v1"):
    f = tmp_path / "estate" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(content)
    return f


def _payload(path, sid=SID, tool="Write"):
    return {"tool_name": tool, "tool_input": {"file_path": str(path)},
            "session_id": sid}


# ------------------------------------------------------- the one true positive

def test_blocks_write_when_file_changed_since_read(tmp_path):
    f = _estate_file(tmp_path)
    rl.record(SID, f)                 # this session read v1
    f.write_bytes(b"v2 from another chat")
    assert gate.run(_payload(f)) == 2


def test_block_message_names_file_and_remedy(tmp_path):
    f = _estate_file(tmp_path)
    rl.record(SID, f)
    f.write_bytes(b"changed")
    action, msg = gate.decide(_payload(f))
    assert action == "stale"
    assert f.name in msg
    assert "Read" in msg          # the remedy must be stated
    assert "disable_stale_read_write_gate.flag" in msg


# ------------------------------------------------------- discriminators (allow)

def test_allows_write_when_hash_matches(tmp_path):
    """DISCRIMINATOR: must pass in the same wiring that blocks above, or the gate
    is indistinguishable from one that blocks everything."""
    f = _estate_file(tmp_path)
    rl.record(SID, f)
    assert gate.run(_payload(f)) == 0


def test_allows_when_no_ledger_entry(tmp_path):
    f = _estate_file(tmp_path)
    assert gate.run(_payload(f)) == 0


def test_allows_new_file_creation(tmp_path):
    ghost = tmp_path / "estate" / "brand_new.py"
    assert gate.run(_payload(ghost)) == 0


def test_allows_other_sessions_ledger_entry(tmp_path):
    """A foreign session's recorded hash must not gate my write."""
    f = _estate_file(tmp_path)
    rl.record(OTHER, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0


def test_allows_outside_estate(tmp_path):
    outside = tmp_path / "elsewhere" / "x.py"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"v1")
    rl.record(SID, outside)
    outside.write_bytes(b"v2")
    assert gate.run(_payload(outside)) == 0


def test_allows_edit_tool_even_when_stale(tmp_path):
    """Edit is deliberately out of scope: old_string matching already fails safely."""
    f = _estate_file(tmp_path)
    rl.record(SID, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f, tool="Edit")) == 0


def test_allows_when_no_session_id(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    f = _estate_file(tmp_path)
    rl.record(SID, f)
    f.write_bytes(b"changed")
    p = _payload(f)
    p.pop("session_id")
    assert gate.run(p) == 0


# ------------------------------------------- [3] deadlock bypass / escape hatches

def test_allows_flag_file_even_when_stale(tmp_path):
    f = _estate_file(tmp_path, "disable_something.flag")
    rl.record(SID, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0


def test_allows_mode_file_even_when_stale(tmp_path):
    f = _estate_file(tmp_path, "mode-stale_read_write_gate.json")
    rl.record(SID, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0


def test_allows_settings_json_even_when_stale(tmp_path, monkeypatch):
    f = _estate_file(tmp_path, "settings.json")
    monkeypatch.setattr(gate, "SETTINGS_P", gate._norm(str(f)))
    rl.record(SID, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0


def test_allows_ledger_and_log_surfaces(tmp_path):
    for name in ("state/read_ledger/x.json", "logs/y.jsonl",
                 "session-manifests/z/reads.jsonl"):
        f = _estate_file(tmp_path, name)
        rl.record(SID, f)
        f.write_bytes(b"changed")
        assert gate.run(_payload(f)) == 0, name


def test_gating_a_hooks_file_is_INTENDED(tmp_path):
    """The hooks dir is the surface being protected — it must NOT be exempt here.
    This is the deliberate divergence from claim_before_edit_gate._is_exempt."""
    f = _estate_file(tmp_path, "hooks/some_hook.py")
    rl.record(SID, f)
    f.write_bytes(b"clobbered by another chat")
    assert gate.run(_payload(f)) == 2


def test_disable_flag_allows(tmp_path, monkeypatch):
    flag = tmp_path / "disable.flag"
    flag.write_text("", encoding="utf-8")
    monkeypatch.setattr(gate, "DISABLE_FLAG", flag)
    f = _estate_file(tmp_path)
    rl.record(SID, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0


# ------------------------------------------------- [t753] soak-lane hygiene guard

def test_suite_never_appends_to_the_live_soak_log(tmp_path):
    """GUARD (t753 leg A): a block raised by this suite must write its telemetry into
    tmp_path, NEVER into ~/.claude/logs/stale_read_write_gate.jsonl.

    That live log is the denominator the 2026-07-29 unwire decision is read off, and
    a soak lane polluted by its own test suite reads as busy when it is empty. RED
    without the LOG_FILE redirect in _isolated — measured 2026-07-26, 27 of the live
    log's first 45 rows were this suite's, every one targeting a pytest tmpdir."""
    live = _REPO / "logs" / "stale_read_write_gate.jsonl"
    before = live.read_bytes() if live.exists() else b""

    f = _estate_file(tmp_path)
    rl.record(SID, f)
    f.write_bytes(b"v2 from another chat")
    assert gate.run(_payload(f)) == 2      # a REAL block, so _log() definitely ran

    after = live.read_bytes() if live.exists() else b""
    assert after == before, "this suite appended a synthetic row to the LIVE soak log"
    assert (tmp_path / "logs" / "stale_read_write_gate.jsonl").exists(), \
        "telemetry went somewhere other than the redirected tmp_path log"


# ---------------------------------------------------------------- mode switch

def test_advisory_mode_warns_but_allows(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("STALE_READ_WRITE_GATE_MODE", "advisory")
    f = _estate_file(tmp_path)
    rl.record(SID, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0
    assert "advisory" in capsys.readouterr().err.lower()


def test_off_mode_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("STALE_READ_WRITE_GATE_MODE", "off")
    f = _estate_file(tmp_path)
    rl.record(SID, f)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0


def test_native_default_is_advisory(monkeypatch, tmp_path):
    """Zero soak -> warn-first. Pins the shipped default against silent escalation."""
    monkeypatch.delenv("STALE_READ_WRITE_GATE_MODE", raising=False)
    monkeypatch.setattr(gate, "MODE_FILE", tmp_path / "absent-mode.json")
    assert gate._hook_mode() == "advisory"


def test_mode_file_read_when_env_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("STALE_READ_WRITE_GATE_MODE", raising=False)
    mf = tmp_path / "mode.json"
    mf.write_text('{"mode": "block"}', encoding="utf-8")
    monkeypatch.setattr(gate, "MODE_FILE", mf)
    assert gate._hook_mode() == "block"


# ------------------------------------------------------------ [1] fail-open

def test_fail_open_bad_payload():
    assert gate.run({}) == 0
    assert gate.run({"tool_name": "Write", "tool_input": {}}) == 0


def test_fail_open_ledger_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger unreadable")
    monkeypatch.setattr(rl, "lookup", boom)
    f = _estate_file(tmp_path)
    f.write_bytes(b"changed")
    assert gate.run(_payload(f)) == 0


def test_fail_open_hash_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("hash failed")
    monkeypatch.setattr(rl, "file_hash", boom)
    f = _estate_file(tmp_path)
    assert gate.run(_payload(f)) == 0
