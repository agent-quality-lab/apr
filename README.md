# Agent Performance Review — Local Integrations

Public source for the local runtime integrations and review flow. The code is experimental; it is published for inspection and development, not as a supported or operational release.

## Contents

- `mcp_server.py` — local MCP stdio bridge (thin client of the loopback service).
- `local_service.py` — per-user loopback HTTP service: `start/stop/status/serve/import`.
- `review_schema.py` — shared validation (no service/store imports).
- `review_store.py` and `task_store.py` — SQLite persistence, tracked migrations, legacy review import, and task lifecycle records.
- `task_schema.py` — validation for task baselines, evidence, human reviews, and share previews.
- `rating-popup/` — local rating form served by the service.
- `workspace-ui/` — local task history, evidence, human review, and share-preview interface served by the service.
- `codex_capture.py` — opt-in command hook collector and exact captured-review URL listing.
- `hooks.codex.example.json` — disabled-by-default example; it is not installed or auto-loaded.
- `skill/agent-performance-review/` — Codex skill source.
- `capture_metadata_hook.py`, `runtime_hook.py`, `stop_hook.py`, `run_state.py`, and `hooks.json` — unsupported prototype hook adapters. They are not installed, not wired to any runtime, and not dependencies of the MCP bridge or service.
- `tests/` — maintained suite, run with `python3 -m unittest discover -s tests`.
- `docs/operators/local-mcp.md` — operator guide (commands, data location, import, recovery).

## Current status

The Codex review hooks were removed from the active user configuration. This source does not install itself or reload runtime registrations; the quickstart's steps do. An already-running bridge/service must be restarted after a reviewed source update. The native capture adapter is an opt-in local experiment and has synthetic fixture coverage only. It does not add an assistant footer, automatic link presentation, another model turn, or model context. Actual Codex resume identity continuity and hook coverage remain unverified.

The local flow keeps task logs on the user's computer. The task lifecycle may preserve authorized task briefs, instructions, definitions, selected context, work logs, results, and artifacts, with source labels and coverage gaps. Credentials are excluded. The older optional Codex hook collector still captures content-free receipts only; it does not measure token use or cost. No capture path claims access to hidden reasoning.

The task-recording interface adds a separate lifecycle: `start_task` records the caller-supplied baseline, `append_evidence` adds source-labeled events, `finish_task` records the result without inventing a human rating, and the local workspace collects a review. Reusing a task request or evidence event is idempotent only when the original content is unchanged. Legacy reviews are imported into the task view with a missing baseline clearly marked. `prepare_share` creates a frozen preview from explicitly selected fields; it does not publish or upload anything.

<a id="local-mcp-setup"></a>

## Local alpha quickstart (Codex on macOS)

Requires macOS, Python 3 (tested with 3.14.2) and the Codex CLI. Windows is not supported; Linux and other hosts are untested. No account is needed.

1. Clone `https://github.com/agent-quality-lab/apr.git` to a normal folder.
2. Register `mcp_server.py` with `codex mcp add` and copy the full `skill/agent-performance-review` folder into `~/.agents/skills/` (back up any older copy first; never delete it blindly).
3. Restart Codex and start a task with `Use $agent-performance-review to record this task, then ...`.

The exact copyable commands, expected result, upgrade, restart and uninstall steps are in the canonical guide: [Codex on macOS quickstart](docs/operators/local-mcp.md#codex-on-macos-quickstart). `python3 scripts/smoke_local_alpha.py` runs a synthetic protocol check in a temporary data directory.

The MCP server uses Python's standard library. On the first tool call the bridge starts the per-user loopback service on demand; the review pages are served while that service is running.

Service control and data locations are documented in `docs/operators/local-mcp.md`. The default data directory lives outside the checkout (`~/Library/Application Support/Agent Performance Review/` on macOS, XDG equivalent elsewhere); `APR_DATA_DIR` overrides it. `request_rating` returns the same `rating_url` for a repeated `review_id` and `expires_in_seconds: null`. `hooks.codex.example.json` is an example only; activation requires a deliberate local config edit and a reviewed real-runtime acceptance run. Use `python3 /absolute/path/to/codex_capture.py list` to print each captured chat's own stable review URL. The collector never chooses a global latest chat.

## Privacy and local data

Submitted reviews are stored in the per-user SQLite database outside the source tree. Never commit databases, credentials, runtime state, prompts, responses, tool arguments or outputs, paths, or document contents. `reviews.jsonl` from older checkouts is legacy import input only. The local workspace creates frozen, selected share bundles; it never publishes automatically. The separate website requires explicit authenticated publication. See [task recording](docs/operators/task-recording.md) for the lifecycle, privacy boundary, and reviewed rollout steps.

## Release decisions

This project is distributed under the MIT License. Copyright (c) 2026 Andrey Zaynulin. See `LICENSE` for the full terms.
