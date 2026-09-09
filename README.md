# claude-stale-read-gate

Prevent cross-session file clobbers in Claude Code with a content-hash ledger.

## The problem

Two Claude Code sessions open the same file. Session B saves. Session A saves its whole-file version. B's work is gone — no signal to either session. This is the lost-update problem, and it happens silently.

## How it works

Three components form a compare-on-write gate:

1. **`lib/read_ledger.py`** — a per-session content ledger. Every time a session reads, writes, or edits a file, the sha256 of its bytes is recorded in `~/.claude/state/read_ledger/<session_id>.json`. One file per session, so concurrent sessions never contend on the same ledger file.

2. **`read_ledger_recorder.py`** — a PostToolUse hook (Read | Write | Edit) that calls `read_ledger.record()` after every file operation. Write and Edit refresh the entry so a session's own second write doesn't false-block.

3. **`stale_read_write_gate.py`** — a PreToolUse hook (Write only) that re-hashes the file at decision time and compares against the ledger. A mismatch means someone else wrote the file since this session last saw it → block with a remedy ("Read the file again").

### Why sha256 and not mtime

RFC 9110 classes a modification date as an implicitly *weak* validator and recommends "a collision-resistant hash of representation content." Git names the same failure class *racily-clean*. Windows guarantees a timestamp only once the writing handle is closed. The hash doubles as a fencing token, so no lease or TTL is needed.

### Why Write only

`Edit` requires an exact `old_string` match, so a foreign change to that region already fails safely — and a change elsewhere leaves a surgical edit correct. `Write` replaces the whole file, the only lane that silently discards another session's work.

## Safety properties

- **Fail-open everywhere.** No ledger entry, missing file, unreadable ledger, path outside scope, no session ID, oversize file — all allow. The single blocking condition is a recorded hash that no longer matches disk.
- **Deadlock-proof.** The remedy is `Read` (which this hook never gates). Escape hatches (`.flag` files, `mode-*.json`, `settings.json`) are exempt before any hash logic runs.
- **Scope-limited.** v1 gates only `~/.claude/` — the estate where measured collisions happen. Telemetry surfaces (`/logs/`, `/state/`, `/.git/`) are exempt.

## Escape hatches

```bash
# temporary bypass
touch ~/.claude/hooks/disable_stale_read_write_gate.flag

# permanent mode switch
echo '{"mode": "off"}' > ~/.claude/hooks/mode-stale_read_write_gate.json

# or via environment
STALE_READ_WRITE_GATE_MODE=off   # block | advisory | off
```

The native default mode is `advisory` (warns but allows).

## Wiring into Claude Code

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Read|Write|Edit",
        "hooks": ["python ~/.claude/hooks/read_ledger_recorder.py"]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Write",
        "hooks": ["python ~/.claude/hooks/stale_read_write_gate.py"]
      }
    ]
  }
}
```

## Tests

```bash
pytest tests/ -q
```

46 tests cover the full fail-open direction table, deadlock bypass paths, mode switching, session isolation, and soak-log hygiene.

## Requirements

- Python 3.8+
- pytest (for tests only)

## License

MIT
