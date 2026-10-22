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
| `capture.log` | 0600 | sanitized hook failure class and timestamp only |
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

## Task recording and local workspace

The MCP bridge also exposes `get_setup`, `start_task`, `append_evidence`, `finish_task`,
`get_task`, `list_tasks`, and `prepare_share`. These tools record one task's
caller-supplied goal and success criteria, source-labeled evidence, an
agent-reported result, and a separate human review. `start_task` returns a
stable local `task_url`; opening it serves the workspace at `/app/`. Task
records and selected-field share previews use the same private SQLite database
and follow its data-directory permissions and retention.

Use the same `request_id` and identical baseline to safely retry
`start_task`. Evidence is keyed by `event_id`: identical retries are
idempotent, while different content under an existing ID is rejected. Criteria
changes are recorded as evidence and advance the task revision. Finish records
the result and moves the task to `awaiting_review` (or `interrupted`); it never
creates a human completion answer or rating. The browser review must use the
current revision. Legacy reviews appear in the task workspace with
`baseline_missing` set; no historical baseline is inferred.

`prepare_share` is available only after a human review. It freezes a versioned
preview from explicitly selected fields and reports withheld fields. It does
not publish or upload the preview. Treat the returned payload as private until
the reviewer deliberately transfers it to a separately configured publishing
service.

## Optional native Codex capture (example only)

`codex_capture.py` accepts lifecycle command-hook JSON on stdin and writes an
allowlisted projection directly to the same private SQLite store. It emits no
stdout, makes no model request, and never reads transcript files. The example
registration is `hooks.codex.example.json`; replace its placeholder source
path and merge it into Codex configuration only when deliberately opting in.
This checkout does not install or activate it. The collector stores HMACed
session, turn, and tool IDs using a random private per-store salt. Prompt text,
tool names, arguments, results, paths, and raw hook objects are discarded.
Each hook's SQLite busy wait and store-init lock wait are capped at 250 ms;
lock contention produces a content-free per-chat gap when the store can be
written, or one fixed `capture_error` line in the private `capture.log`
otherwise. These caps reduce contention time; they are not a guarantee that
every callback completes before Codex's hook deadline. The example gives
`Interrupt` the supported 3-second timeout, whose end-to-end behavior still
requires isolated runtime verification.

After capture, run:

```sh
python3 /absolute/path/to/agent-performance-review/codex_capture.py list
```

The command starts the local service and prints the exact stable review URL for
each captured native session. It does not guess from the most recent chat.
Review cards show receipt-observed wall timing, local tool starts, unknown token
and cost values, and coverage gaps. A new captured event changes the pending
card revision; an already-open rating form must reload before it can save. On
save, the store atomically freezes the event count and derived projection seen
at that moment. Later evidence is shown as post-rating evidence and does not
change the saved snapshot or saved evidence watermark.

`resume` can reuse the same URL only when Codex supplies the same native
session ID. Actual identity continuity across desktop resume/restart has not
been verified. A changed ID creates a separate review; the adapter does not
join by working directory or recency. Reused IDs on `clear` or `fork` are
flagged unsupported. Tool events without a previously observed root turn start
are marked unattributed and excluded from root totals. Hosted tools and missed
hooks remain coverage gaps. The `Stop` hook records a stopped turn, not task
success. This capture does not provide the repeated assistant footer link;
automatic link presentation requires a separate verified host capability.
SessionStart has no documented occurrence ID, so repeated `resume` or
`compact` markers deduplicate by source and expose an occurrence-ambiguity gap;
model identity is taken from turn events, not treated as a complete list from
resume markers. A recognized lifecycle hook without a native session ID writes only a fixed
sanitized failure class to local diagnostics.

Rollout remains opt-in until a human verifies real startup, multiple turns,
interleaved chats, resume identity, restart persistence, rating freeze behavior,
and the URL in the intended Codex desktop. Disable the registration to stop
capture. The review records remain in the existing local APR data store and
follow its normal retention handling.

## Codex on macOS quickstart

This is the one canonical install procedure; the README links here. It covers
Codex on macOS only. The service uses POSIX `fcntl` locks, so Windows is not
supported; Linux and other MCP hosts are untested. Local APR needs no account.
Nothing here edits Codex files by hand or enables hooks.

**Prerequisites:** macOS, `git`, Python 3 on your `PATH` (tested with Python
3.14.2; earlier 3.x versions are untested), and the Codex CLI (tested with
`codex-cli 0.152.1`). Check them with `python3 --version` and `codex --version`.

1. Clone to a normal folder (not a temporary directory). Steps 2–3 reuse
   `APR_HOME`, so run them in the same terminal:

   ```sh
   APR_HOME="$HOME/apr"
   git clone https://github.com/agent-quality-lab/apr.git "$APR_HOME"
   ```

2. Register the MCP bridge. The quoted absolute paths are expanded now and
   saved, so later spaces in paths or a different `PATH` inside Codex do not
   break the launch:

   ```sh
   codex mcp add agent-performance-review -- "$(command -v python3)" "$APR_HOME/mcp_server.py"
   codex mcp get agent-performance-review
   ```

3. Install the complete skill folder in the user skill location Codex
   documents, `$HOME/.agents/skills` (a repository-local `.agents/skills`
   also works for one project). If an older copy is already there, move it to
   a timestamped backup outside the discovery directory first, so no stale
   files remain and any local customizations can be restored:

   ```sh
   SKILLS="$HOME/.agents/skills"
   mkdir -p "$SKILLS"
   if [ -e "$SKILLS/agent-performance-review" ]; then
     BACKUP="$HOME/apr-skill-backup-$(date +%Y%m%d-%H%M%S)"
     mv "$SKILLS/agent-performance-review" "$BACKUP"
     echo "Previous skill moved to $BACKUP"
   fi
   cp -R "$APR_HOME/skill/agent-performance-review" "$SKILLS/"
   ```

   Older installations may also have a copy under
   `$HOME/.codex/skills/agent-performance-review` or in a project's
   `.agents/skills`. Codex can see more than one, so check which path the
   session actually lists for `agent-performance-review` and back up (do not
   delete) any duplicate you no longer want discovered. To restore a backup,
   move it back to the same folder name.

4. Fully quit and reopen Codex (or start a new `codex` CLI session). A session
   that was already running does not pick up a new MCP server or skill.
5. In the new session, give this exact first task:

   ```text
   Use $agent-performance-review to record this task, then list three acceptance criteria for a password reset form.
   ```

   Expected: the APR tools are available (`get_setup`, `start_task`,
   `append_evidence`, `finish_task`, `get_task`, `list_tasks`,
   `prepare_share`, plus the legacy `request_rating`); the agent calls
   `get_setup` and `start_task` before answering, `finish_task` when it
   delivers the criteria, and ends with `[⭐ Review this task](http://127.0.0.1:PORT/app/...)`.
   The first tool call starts the local service automatically.
6. Open the link, submit your own outcome and overall stars, and check that the
   task appears in your private history. Saving freezes the reviewed snapshot,
   so to check continuation, ask for a follow-up or rework in the same chat
   *before* submitting the review: the agent must reuse the same task and link
   rather than start a new one. Work after a saved review does not silently
   change that frozen review.

If only `request_rating` appears, the host is running the older bridge: re-run
step 2 against this checkout and restart Codex.

### Upgrade, restart, and removal

- **Upgrade:** `git -C "$APR_HOME" pull`, then `python3 "$APR_HOME/local_service.py" stop`,
  repeat step 3, and restart Codex. The next tool call starts the updated
  service, which applies checked-in migrations. Existing task and review links
  keep working because the port and tokens are stored in the data directory.
- **Normal restart:** after a reboot or `stop`, any APR tool call (or
  `python3 "$APR_HOME/local_service.py" start`) brings the service back on the
  same port. A link opened while the service is stopped will not load until
  then.
- **Port conflict:** `start` refuses to move to a new port so links never
  break. Free the port shown by `status`, or set `APR_DATA_DIR` to a fresh
  directory for a separate instance with new links.
- **Duplicate or conflicting requests:** retrying `start_task`,
  `append_evidence`, or `finish_task` with the same ID and identical content
  returns the existing record. Different content under an existing ID is
  rejected; send corrections as new evidence with a new `event_id`.
- **Uninstall:** `python3 "$APR_HOME/local_service.py" stop`,
  `codex mcp remove agent-performance-review`, and delete
  `$HOME/.agents/skills/agent-performance-review` (plus any older copy under
  `$HOME/.codex/skills` you installed yourself) and the clone.
  This leaves your history in the data directory below; delete that directory
  yourself only if you want to erase it. There is no remote copy.

Native capture stays off unless you separately opt in to the documented
`hooks.codex.example.json`; `hooks.json` and its legacy hook modules remain
unsupported prototype references.

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
- Legacy cards use caller-supplied metadata, and the optional Codex hook still
  records content-free receipts. The new task lifecycle can preserve rich authorized
  task context with provenance. No path invents token/cost measurements or captures
  hidden reasoning. See [task recording](task-recording.md).
- The local service never authenticates a community account or uploads records.
  It exports selected approved snapshots for the separate community client.
  Live hosted migrations, OAuth configuration, and deployment are separate steps.
- Isolated rendered desktop/mobile QA is recorded in the verification report.
  A real multi-turn Codex walkthrough remains pending; see
  [local-alpha install verification](../verification/local-alpha-install.md).

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
MCP/skill install procedure is "Codex on macOS quickstart" above. Native hook
capture is a separate opt-in example described earlier.
