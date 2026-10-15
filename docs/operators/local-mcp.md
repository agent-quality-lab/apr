# Local MCP operator guide

Per-user loopback review flow: the stdio MCP server (`mcp_server.py`) is a thin
bridge. One detached local service (`local_service.py`) owns the SQLite store
and the browser rating UI. Standard library only; no new dependencies.

## Commands

All commands accept `--data-dir DIR` (default: the platform data directory).
`APR_DATA_DIR` overrides the default in every command, including MCP use.

```sh
python3 local_service.py start                 # start on demand (idempotent)
python3 local_service.py status                # running/stopped + port
python3 local_service.py stop                  # stop the detached service
python3 local_service.py import legacy.jsonl [--dry-run]
```

The MCP bridge starts the service automatically on the first tool call, so
manual `start` is only needed for browser-first QA. `stop` terminates the
detached daemon. Browser links need a running service; a link never launches
the service by itself — start it via `start` or any MCP tool call first.

## Default data location and private files

- macOS: `~/Library/Application Support/Agent Performance Review/`
- Other Unix: `$XDG_DATA_HOME/agent-performance-review/` or
  `~/.local/share/agent-performance-review/`

Contents (never commit, never copy into chat):

| File | Mode | Purpose |
|---|---|---|
| `config.json` | 0600 | persisted loopback port |
| `api_credential` | 0600 | random per-user Bearer credential for MCP-to-service writes |
| `reviews.db` (+ `-wal`/`-shm`) | 0600 | SQLite jobs, snapshots, ratings, migrations |
| `service.pid` | 0600 | running daemon pid/port |
| `service.lock` / `startup.lock` | 0600 | single-daemon + serialized-starter locks |
| `init.lock` | 0600 | serializes database open/migration and close/checkpoint boundaries |
| `service.log` | 0600 | daemon diagnostics (never credentials) |
| `backups/` | 0700 dir, 0600 files | pre-import database copies |

Before any private request sends its credential or review body, the client
verifies a fresh HMAC service-identity proof and reuses that same TCP connection
with reconnection disabled. A different process on the persisted port cannot
impersonate the service with a simple health response. Private transport ignores
proxy settings and never follows redirects.

Directories are 0700. Symlinked data directories or sensitive files are
refused. `reviews.jsonl` beside an older checkout is legacy read-only input
for `import`, never written by the service.

## Persistent URL and restart semantics

The first `start` picks a random free loopback port and keeps it in
`config.json`. Every `request_rating` for one `review_id` returns the SAME
`rating_url` (`http://127.0.0.1:PORT/review/<job-token>/`), including after
service or MCP restarts, until the data is explicitly removed. Earlier links
show the latest supplied snapshot while the job is pending. The tool returns
`expires_in_seconds: null`: there is no expiry clock.

If the persisted port is occupied by another process, `start`/`serve` fails
with a clear diagnostic and never silently moves to a different port (that
would strand existing links). Recovery: stop the process using the port, or
point `APR_DATA_DIR` at a fresh directory for an isolated instance.

Saving freezes the reviewed snapshot with its timestamp. A later `request`
with the same ID returns the saved URL and state without changing the rated
snapshot. A new job always needs an explicit new `review_id`. Skipping writes
no rating and leaves the job pending.

## Manual Codex integration (owner, after review — not installed here)

This repository does not install anything into `~/.codex` or any active
server/skill configuration; the current active Codex MCP still points at the
older prototype. After controller review, the owner runs these exact steps
(canonical; the README points here and carries no second copy):

1. Back up the existing Codex MCP config (`~/.codex/config.toml`) aside.
2. Register this checkout's bridge (verified against
   `codex mcp add --help` on 2026-10-15):

   ```sh
   codex mcp add agent-performance-review -- python3 <checkout>/mcp_server.py
   ```

   This writes the equivalent TOML entry:

   ```toml
   [mcp_servers.agent-performance-review]
   command = "python3"
   args = ["<checkout>/mcp_server.py"]
   ```

3. Copy the FULL maintained skill from this checkout — do not merely edit
   the footer link line in the older installed skill:

   ```sh
   cp -r <checkout>/skill/agent-performance-review/. \
     <maintained-skills-dir>/agent-performance-review/
   ```

   The maintained copy must then read `⭐ Review this task` and document
   `expires_in_seconds: null`.
4. Restart the Codex host, invoke the `agent-performance-review` skill for
   one synthetic `request_rating`, and confirm the footer link format.
5. Keep hooks off: `hooks.json` in this checkout is a prototype reference
   only; no hook modules are installed or wired.

## Legacy import

```sh
python3 local_service.py import /path/to/reviews.jsonl --dry-run   # validate only
python3 local_service.py import /path/to/reviews.jsonl             # apply
```

The whole file is parsed and validated before anything is written. Original
`recorded_at` values, context/ratings, and a SHA-256 source digest are
preserved per record. Import is atomic (one transaction) and repeatable:
identical records are skipped, while a conflicting `review_id` or a malformed
line aborts with no partial writes. A private backup of the database is taken
before every applying import. The original file is never modified or deleted.

## Conflict and error recovery

- `start` reports port occupied: see above; `status` shows the configured port.
- Browser save reports the card changed (HTTP 409): the page reloads the
  newest snapshot automatically — review it, then save again.
- Conflicting save for an already-saved job (HTTP 409): use a new `review_id`.
- `status` says stopped right after `start`: read `service.log` in the data
  directory (it contains no credentials).
- `stop` never signals a PID from `service.pid` directly: it asks the running
  service (authenticated with the private credential) to shut down, so a
  stale PID file can never terminate an unrelated process. A stale file is
  dropped and `stop` reports `stopped`.
- Two starters at once: the startup lock serializes them; both report the
  same `base_url`.

## Retention and limitations

- Pending cards and saved ratings persist until the data directory is
  explicitly deleted. There is no retention job and no remote copy.
- No automatic capture: model/context values are caller-supplied; token/cost
  numbers stay `unknown` unless the caller provides them.
- No auth, cloud, sharing, or website changes are part of this milestone.
- Isolated rendered desktop/mobile QA is recorded in the verification report.
  A real multi-turn Codex walkthrough remains pending the reviewed installation.

## Owner test walkthrough (isolated, synthetic)

```sh
export APR_DATA_DIR="$(mktemp -d /tmp/apr-qa.XXXXXX)"
python3 local_service.py start
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"request_rating","arguments":{"review_id":"qa-1","job_type":"coding","task":"synthetic task","action":"synthetic action","result":"synthetic result","verification":"unknown"}}}' \
  | python3 mcp_server.py
```

Open the printed `rating_url` in the internal browser while the service is
still running (nothing below stops it yet). `Save locally` stores one
record; `Skip review` stores nothing. Re-run the same `review_id` to
confirm the identical URL; `stop`, then `start` again, and confirm the card
survives at the same URL.

```sh
python3 local_service.py stop   # last step, after the browser walkthrough
```

## Install scope (not executed here)

Nothing in this checkout installs itself: no `~/.codex` edits, no hook
wiring, no service autostart. Prototype hook modules
(`capture_metadata_hook.py`, `runtime_hook.py`, `stop_hook.py`,
`run_state.py`, `hooks.json`) stay uninstalled and unsupported. The single
install procedure is "Manual Codex integration" above.
