# Agent Performance Review — Local Integrations

Public source for the local runtime integrations and review flow. The code is experimental; it is published for inspection and development, not as a supported or operational release.

## Contents

- `mcp_server.py` — local MCP stdio bridge (thin client of the loopback service).
- `local_service.py` — per-user loopback HTTP service: `start/stop/status/serve/import`.
- `review_schema.py` — shared validation (no service/store imports).
- `review_store.py` — SQLite persistence, tracked migrations, legacy JSONL import.
- `rating-popup/` — local rating form served by the service.
- `codex_capture.py` — opt-in command hook collector and exact captured-review URL listing.
- `hooks.codex.example.json` — disabled-by-default example; it is not installed or auto-loaded.
- `skill/agent-performance-review/` — Codex skill source.
- `capture_metadata_hook.py`, `runtime_hook.py`, `stop_hook.py`, `run_state.py`, and `hooks.json` — unsupported prototype hook adapters. They are not installed, not wired to any runtime, and not dependencies of the MCP bridge or service.
- `tests/` — maintained suite, run with `python3 -m unittest discover -s tests`.
- `docs/operators/local-mcp.md` — operator guide (commands, data location, import, recovery).

## Current status

The Codex review hooks were removed from the active user configuration. This checkout is not installed in any runtime. The native capture adapter is an opt-in local experiment and has synthetic fixture coverage only. It does not add an assistant footer, automatic link presentation, another model turn, or model context. Actual Codex resume identity continuity and hook coverage remain unverified.

The local flow keeps reviews on the user's computer. It must not collect prompts, responses, tool arguments or outputs, file paths, or document contents. Manual review cards use caller-supplied model and context values; opt-in Codex capture displays separately labeled local hook observations. Token and cost remain unknown because this capture path does not measure them.

## Local MCP setup

The MCP server uses Python's standard library. Configure an MCP-capable host to launch `mcp_server.py` as a stdio server, using the absolute path to this checkout. On the first tool call the bridge starts the per-user loopback service on demand; the rating page is served while that service is running.

For Codex, the canonical install (bridge registration verified against `codex mcp add --help`, full skill copy, no `~/.codex` edits here) is documented once in `docs/operators/local-mcp.md` under "Manual Codex integration"; this README carries no second copy.

Service control and data locations are documented in `docs/operators/local-mcp.md`. The default data directory lives outside the checkout (`~/Library/Application Support/Agent Performance Review/` on macOS, XDG equivalent elsewhere); `APR_DATA_DIR` overrides it. `request_rating` returns the same `rating_url` for a repeated `review_id` and `expires_in_seconds: null`. `hooks.codex.example.json` is an example only; activation requires a deliberate local config edit and a reviewed real-runtime acceptance run. Use `python3 /absolute/path/to/codex_capture.py list` to print each captured chat's own stable review URL. The collector never chooses a global latest chat.

## Privacy and local data

Submitted reviews are stored in the per-user SQLite database outside the source tree. Never commit databases, credentials, runtime state, prompts, responses, tool arguments or outputs, paths, or document contents. `reviews.jsonl` from older checkouts is legacy import input only. Sharing is not implemented by this local prototype.

## Release decisions

This project is distributed under the MIT License. Copyright (c) 2026 Andrey Zaynulin. See `LICENSE` for the full terms.
