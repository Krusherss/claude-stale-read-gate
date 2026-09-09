#!/usr/bin/env python3
"""read_ledger_recorder (t679 / c162): PostToolUse observer — records sha256 of the
bytes this session just saw or produced, into the per-session read ledger.

Matcher Read|Write|Edit. An entry means "the exact bytes session S last SAW or PRODUCED
at path P"; `stale_read_write_gate` compares against it on the next Write and blocks
when another session changed the file in between (the 2026-07-01 c162 clobber class).

Write/Edit refresh the entry as well as Read — deliberately. A file this session just
wrote is content it knows about; without the refresh, its own second Write to the same
file would look like a foreign edit and false-block.

All ledger logic lives in lib/read_ledger.py, shared with the gate — not restated here
(t598: one authoritative site, callers point at it).

Prior art, checked and deliberately not extended: read_manifest_logger.py already
observes PostToolUse Read into session-manifests/<sid>/reads.jsonl, but that artifact is
load-bearing for read_before_claim_gate, whose predicate is "did this session READ this
file". This ledger must also refresh on Write, and Write-rows in reads.jsonl would let a
write satisfy a read-check. Different meanings -> different artifacts.
See validation/read_ledger_recorder/tier-score.md.

Universal safety (hook-creator v8.7.0):
  [1] Fail-open: run() wraps its whole body and returns 0; the __main__ guard exits 0 on
      any uncaught error (tests: test_observer_never_blocks_on_internal_error,
      test_bad_payload_never_blocks)
  [2] Escape hatch: disable_read_ledger_recorder.flag, checked first in run()
      (test: test_disable_flag_short_circuits)
  [3] Deadlock bypass: N/A by construction — a PostToolUse observer that never exits 2
      and writes only its own ledger under ~/.claude/state/read_ledger/. It cannot
      block any action, including edits to itself or to its disable flag.
  [4] Security: no eval/exec/os.system/popen/shell=True; stdin JSON and the target file
      are read-only; the session id is sanitized to a bare filename in
      lib.read_ledger.ledger_path before it reaches the filesystem.
"""
import json
import os
import sys
from pathlib import Path

HOOK_NAME = "read_ledger_recorder"
HOME = Path.home()
HOOKS_DIR = HOME / ".claude" / "hooks"
SCRIPTS_DIR = Path.home() / ".claude" / "scripts"
DISABLE_FLAG = HOOKS_DIR / f"disable_{HOOK_NAME}.flag"

RECORDING_TOOLS = ("Read", "Write", "Edit")

# Scope: only hash files the gate can actually act on. The gate's v1 scope is the home
# estate, so hashing anything else buys nothing while the cost is paid on EVERY Read —
# the highest-frequency event in a session. Kept deliberately identical to
# stale_read_write_gate.ESTATE_ROOT; if that scope widens, widen this with it.
ESTATE_ROOT = str(HOME / ".claude").replace("\\", "/").casefold()


def _in_scope(target: str) -> bool:
    """True when `target` sits inside the home estate — the only region the gate gates."""
    try:
        p = os.path.realpath(os.path.expanduser(target))
    except (OSError, ValueError):
        p = os.path.normpath(os.path.expanduser(target))
    return p.replace("\\", "/").casefold().startswith(ESTATE_ROOT + "/")

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
try:
    from lib import read_ledger
except Exception:  # pragma: no cover - environment breakage
    read_ledger = None


def run(payload: dict) -> int:
    """Record one observation. ALWAYS returns 0 — this hook never blocks anything."""
    try:
        if DISABLE_FLAG.exists():          # [2] escape hatch, before any work
            return 0
        if read_ledger is None:
            return 0                        # degraded: no ledger layer -> gate allows
        tool_name = payload.get("tool_name") or ""
        if tool_name not in RECORDING_TOOLS:
            return 0
        target = (payload.get("tool_input") or {}).get("file_path") or ""
        if not target:
            return 0
        if not _in_scope(target):
            return 0                        # outside the gate's scope -> nothing to hash
        sid = (payload.get("session_id")
               or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
        if not sid:
            return 0                        # no identity -> no ledger to write
        read_ledger.record(sid, target)
        return 0
    except Exception:
        return 0                            # [1] an observer never blocks


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                            # [1] fail-open on unparseable stdin
    return run(payload)


def _self_report():
    try:
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))
        from fabric_lib import self_report
        def check():
            return True, f"{HOOK_NAME} completed successfully"
        self_report(f"hook:{HOOK_NAME}", check, verbose=False)
    except Exception:
        pass


if __name__ == "__main__":
    # Born-instrumented (hook-creator Step 8c): mechanism_run records a heartbeat
    # without touching control flow. The no-op stub means a missing/broken fabric_lib
    # can never affect this hook.
    try:
        sys.path.insert(0, str(HOME / ".claude" / "scripts"))
        from fabric_lib import mechanism_run
    except Exception:
        from contextlib import contextmanager

        @contextmanager
        def mechanism_run(who, check_fn=None):
            class _N:
                def effect(self, **kw):
                    pass
            yield _N()
    try:
        with mechanism_run(f"hook:{HOOK_NAME}") as _m:
            rc = main()
            _m.effect(allowed=1)
        sys.exit(rc)
    except Exception as exc:                # [1] fail-open on any uncaught error
        print(f"{HOOK_NAME} error (fail-open): {exc}", file=sys.stderr)
        _self_report()
        sys.exit(0)
