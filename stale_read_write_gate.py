#!/usr/bin/env python3
"""stale_read_write_gate (t679 / c162): compare-on-write — block a full-file Write when
the file changed since this session last saw it.

The defect (real, not hypothetical): two chats hold the same file; chat B saves; chat A
saves its whole-file version; B's work is gone with NO signal to either. Recorded on
c162 for 2026-07-01 — sid 0d950a25's ~/.claude/hooks writes clobbered by commit e861deb,
a wasted full build.

Mechanism: read_ledger_recorder records sha256 of the bytes this session saw or produced
(PostToolUse Read|Write|Edit). Here, on PreToolUse Write, the file is re-hashed and
compared. Mismatch => somebody else wrote it => block with the remedy (re-Read).
This is a RECOMPUTE block, not a shape check: it hashes the actual bytes on disk at
decision time. A fabricated or stale ledger entry cannot pass it.

Why sha256 and not mtime: RFC 9110 classes a modification date as an implicitly WEAK
validator and recommends "a collision-resistant hash of representation content"; git
names the class racily-clean; Windows guarantees a timestamp only when the writing
handle closes. c162's original mtime-TTL proposal was refuted on that evidence.

Scope is `Write` ONLY, deliberately. `Edit` requires an exact old_string match, so a
foreign change to that region already fails safely and a change elsewhere leaves a
surgical edit correct. `Write` replaces the whole file — the only lane that silently
discards another session's work, and the lane the real clobber used.

Why this gate can live inside ~/.claude/hooks/ when claim_before_edit_gate's t534
cross-session axis cannot: that hook's _is_exempt() returns True for the whole hooks dir
BEFORE any claim logic runs, so its cross-session check is unreachable exactly where the
clobber happened. This gate needs no such exemption because its remedy is to READ the
file — an action it never gates — so it cannot wedge the estate's own repair path.
Narrowing that other exemption is a separate, riskier change and is NOT bundled here.

Every unknown ALLOWS. No ledger entry, no file on disk, unreadable ledger, path outside
the estate, no session id, an oversize file — all exit 0. The single blocking condition
is a recorded hash that no longer matches the bytes on disk.

Universal safety (hook-creator v8.7.0):
  [1] Fail-open: decide() wraps its whole body and returns allow on any error; main()
      returns 0 on unparseable stdin; the __main__ guard exits 0 on any uncaught error
      (tests: test_fail_open_bad_payload, test_fail_open_ledger_raises,
      test_fail_open_hash_raises)
  [2] Escape hatch: disable_stale_read_write_gate.flag OR popup-unlock
      (unlock_hook_edit.py --minutes N); per-hook mode switch
      mode-stale_read_write_gate.json / env STALE_READ_WRITE_GATE_MODE =
      block|advisory|off (native default: advisory — zero soak)
  [3] Deadlock bypass: *.flag, mode-*.json, settings.json, and the logs/state/manifest
      surfaces are exempt in _is_exempt() BEFORE any hash logic, so both escape hatches
      stay writable while the gate is active; `Edit` is never gated at all; and the
      remedy for a real block is `Read`, which this hook does not mediate
      (tests: test_allows_flag_file_even_when_stale, test_allows_mode_file_even_when_stale,
      test_allows_settings_json_even_when_stale, test_allows_edit_tool_even_when_stale)
  [4] Security: no eval/exec/os.system/popen/shell=True; stdin JSON, the ledger, and the
      target file are all read-only here; the session id is sanitized to a bare filename
      inside lib.read_ledger before touching the filesystem.
"""
import json
import os
import sys
import time
from pathlib import Path

HOOK_NAME = "stale_read_write_gate"
HOME = Path.home()
HOOKS_DIR = HOME / ".claude" / "hooks"
SCRIPTS_DIR = Path.home() / ".claude" / "scripts"
DISABLE_FLAG = HOOKS_DIR / f"disable_{HOOK_NAME}.flag"
MODE_FILE = HOOKS_DIR / f"mode-{HOOK_NAME}.json"
LOG_FILE = HOME / ".claude" / "logs" / f"{HOOK_NAME}.jsonl"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))
try:
    from lib import read_ledger
except Exception:  # pragma: no cover - environment breakage
    read_ledger = None


def _norm(path: str) -> str:
    """Normal form for every comparison: realpath, forward slashes, casefolded
    (t355 — Windows paths are case-insensitive)."""
    try:
        p = os.path.realpath(os.path.expanduser(str(path)))
    except (OSError, ValueError):
        p = os.path.normpath(os.path.expanduser(str(path)))
    return p.replace("\\", "/").casefold()


# v1 scope: the home estate only — where the measured collisions happen.
ESTATE_ROOT = _norm(str(HOME / ".claude"))
SETTINGS_P = _norm(str(HOME / ".claude" / "settings.json"))

# Telemetry / escape-hatch surfaces: high-churn, written by many mechanisms, and never
# the thing a session hand-authors. Gating them would be noise, not protection.
EXEMPT_SEGMENTS = ("/logs/", "/state/", "/session-manifests/", "/.git/",
                   "/__pycache__/", "/archive/")


def _hook_mode() -> str:
    """Precedence: env > mode file > native default `advisory` (warn-first, zero soak)."""
    env = os.environ.get("STALE_READ_WRITE_GATE_MODE")
    if env in ("block", "advisory", "off"):
        return env
    try:
        mode = json.loads(MODE_FILE.read_text(encoding="utf-8")).get("mode")
        if mode in ("block", "advisory", "off"):
            return mode
    except Exception:
        pass
    return "advisory"


def _is_exempt(nt: str) -> bool:
    """[3] deadlock bypass + v1 scope. Runs BEFORE any hash logic.

    NOTE the deliberate omission: ~/.claude/hooks/ is NOT exempt. That directory is the
    surface this gate exists to protect, and exempting it is precisely the flaw that
    makes claim_before_edit_gate's cross-session axis unreachable. Safe here because the
    remedy is Read, not a write."""
    if not nt.startswith(ESTATE_ROOT + "/"):
        return True                      # outside the estate: v1 scope
    if nt == SETTINGS_P or nt.endswith(".flag"):
        return True                      # escape hatches must stay writable
    base = os.path.basename(nt)
    if base.startswith("mode-") and base.endswith(".json"):
        return True                      # the mode switch itself
    if any(seg in nt for seg in EXEMPT_SEGMENTS):
        return True                      # telemetry/state surfaces
    return False


def _message(target: str, recorded: str, current: str) -> str:
    return "\n".join([
        f"{HOOK_NAME} (t679/c162): {target}",
        "  changed on disk since this session last read it.",
        f"    you last saw : sha256 {recorded[:12]}…",
        f"    on disk now  : sha256 {current[:12]}…",
        "  Another session (or an out-of-band edit) wrote this file. Writing now would",
        "  replace their version wholesale — the lost-update clobber c162 records.",
        "",
        "  REMEDY — Read the file again, re-apply your change to the CURRENT content,",
        "  then write. A Read refreshes the ledger and clears this block; no flag, no",
        "  mode change, and no other session has to do anything.",
        "  (Edit is never gated by this hook — a surgical Edit is already safe.)",
        "",
        f"  Escape (only if you intend to discard their version): "
        f"touch {DISABLE_FLAG}",
        f"  or STALE_READ_WRITE_GATE_MODE=off (or mode-{HOOK_NAME}.json).",
    ])


def decide(payload: dict):
    """Pure decision seam: ('allow','') or ('stale', message). Fail-open on any error."""
    try:
        if read_ledger is None:
            return "allow", ""                       # degraded -> never block
        if (payload.get("tool_name") or "") != "Write":
            return "allow", ""                       # Write lane only (see header)
        target = (payload.get("tool_input") or {}).get("file_path") or ""
        if not target:
            return "allow", ""
        if _is_exempt(_norm(target)):
            return "allow", ""
        sid = (payload.get("session_id")
               or os.environ.get("CLAUDE_CODE_SESSION_ID") or "")
        if not sid:
            return "allow", ""                       # no identity -> must not deadlock
        recorded = read_ledger.lookup(sid, target)
        if not recorded:
            return "allow", ""                       # never read it / new file
        current = read_ledger.file_hash(target)
        if not current:
            return "allow", ""                       # gone, oversize, unreadable
        if current == recorded:
            return "allow", ""                       # exactly what I last saw
        return "stale", _message(target, recorded, current)
    except Exception:
        return "allow", ""                           # [1] fail-open


def _log(payload: dict, action: str, mode: str) -> None:
    """Best-effort telemetry for the advisory soak. Never raises."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "hook": HOOK_NAME, "mode": mode, "action": action,
               "tool": payload.get("tool_name", ""),
               "target": (payload.get("tool_input") or {}).get("file_path", ""),
               "sid": payload.get("session_id")
                      or os.environ.get("CLAUDE_CODE_SESSION_ID") or "",
               "degraded": read_ledger is None}
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def run(payload: dict) -> int:
    """Escape hatch + mode switch around decide(). Returns the process exit code."""
    try:
        if DISABLE_FLAG.exists():                    # [2] escape hatch
            return 0
        mode = _hook_mode()
        if mode == "off":
            return 0
        action, msg = decide(payload)
        if action != "stale":
            return 0
        if mode == "block":
            _log(payload, "block", mode)
            print(msg, file=sys.stderr)
            return 2
        _log(payload, "would-block", mode)
        print(f"[advisory — would block in enforce mode]\n{msg}", file=sys.stderr)
        return 0
    except Exception:
        return 0                                     # [1] fail-open


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0                                     # [1] fail-open on bad stdin
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
    # Born-instrumented (hook-creator Step 8c): mechanism_run records the block/allow
    # verdict without touching control flow; the no-op stub means a missing or broken
    # fabric_lib can never make this gate block.
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
            if rc == 2:
                _m.effect(blocked=1)
            else:
                _m.effect(allowed=1)
        sys.exit(rc)
    except Exception as exc:                         # [1] fail-open
        print(f"{HOOK_NAME} error (fail-open): {exc}", file=sys.stderr)
        _self_report()
        sys.exit(0)
