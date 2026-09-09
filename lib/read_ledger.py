"""lib.read_ledger (t679 / c162): per-session content ledger for compare-on-write.

An entry means: "these are the exact bytes session S last SAW or PRODUCED at path P."
`read_ledger_recorder` writes it (PostToolUse Read|Write|Edit); `stale_read_write_gate`
reads it (PreToolUse Write) and blocks when the bytes on disk no longer match.

ONE shared module, two consumers — deliberately not restated in either hook (t598: the
same verb list restated in four places, none authoritative).

Why sha256 and not mtime. c162 originally specified an mtime-TTL compare; the t679
research pass refuted it. RFC 9110 classes a modification date as an implicitly WEAK
validator ("might not change for every change to the representation data") and
recommends "a collision-resistant hash of representation content"; git names the same
failure class *racily-clean*; Windows guarantees a timestamp only once the writing
handle is closed. The hash additionally doubles as a fencing token, which is why no
lease/TTL is required here.

Storage: ~/.claude/state/read_ledger/<session_id>.json — ONE FILE PER SESSION, so two
concurrent sessions never write the same file. That is the point of the mechanism; it
would be self-defeating to give it a shared mutable file of its own.

Every failure path returns the empty/None answer rather than raising: a ledger that
cannot be read must make the gate ALLOW, never block.
"""
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

HOME = Path.home()
LEDGER_DIR = HOME / ".claude" / "state" / "read_ledger"

# Structural size constants (NOT measured values — the metric-readiness rule
# explicitly excludes truncation/retention caps).
MAX_HASH_BYTES = 8 * 1024 * 1024   # above this, no entry -> gate allows (fails open)
MAX_ENTRIES = 500                  # newest-first retention per session

try:
    from lib.path_norm import norm as _norm_path  # realpath + casefold + forward slashes
except Exception:  # pragma: no cover - lib layout breakage
    try:
        from path_norm import norm as _norm_path  # type: ignore
    except Exception:
        def _norm_path(p):
            try:
                q = os.path.realpath(os.path.expanduser(str(p)))
            except (OSError, ValueError):
                q = os.path.normpath(os.path.expanduser(str(p)))
            return q.replace("\\", "/").casefold()


def key_for(path) -> str:
    """The ledger key: one normal form for every spelling of the same file.
    Windows paths are case-insensitive and separator-mixed (t355)."""
    return _norm_path(path)


def ledger_path(sid: str) -> Path:
    """Per-session ledger file. `sid` is sanitized to a bare filename so a hostile or
    malformed session id can never traverse out of LEDGER_DIR."""
    safe = "".join(ch for ch in str(sid) if ch.isalnum() or ch in "-_")[:80]
    return LEDGER_DIR / f"{safe or 'unknown'}.json"


def file_hash(path):
    """sha256 hex of the file's bytes, or None when it cannot/should not be hashed
    (missing, a directory, unreadable, or over MAX_HASH_BYTES). None always degrades
    to ALLOW downstream."""
    try:
        p = Path(os.path.expanduser(str(path)))
        if not p.is_file():
            return None
        if p.stat().st_size > MAX_HASH_BYTES:
            return None
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def load(sid: str) -> dict:
    """The session's ledger as {key: {"sha256":..., "ts":...}}. {} on any error —
    a corrupt ledger must read as 'I know nothing', never raise."""
    try:
        data = json.loads(ledger_path(sid).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(sid: str, data: dict) -> bool:
    """Atomic replace so a crash mid-write cannot leave a torn ledger."""
    try:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        target = ledger_path(sid)
        fd, tmp = tempfile.mkstemp(dir=str(LEDGER_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, target)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def record(sid: str, path):
    """Hash `path` now and store it as what `sid` last saw/produced.
    Returns the hash, or None when nothing was recorded."""
    h = file_hash(path)
    if not h or not sid:
        return None
    data = load(sid)
    data[key_for(path)] = {"sha256": h, "ts": time.time()}
    if len(data) > MAX_ENTRIES:
        newest = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
        data = dict(newest[:MAX_ENTRIES])
    return h if _save(sid, data) else None


def lookup(sid: str, path):
    """The hash `sid` last recorded for `path`, or None if it has none."""
    if not sid:
        return None
    entry = load(sid).get(key_for(path))
    if isinstance(entry, dict):
        return entry.get("sha256")
    return None
