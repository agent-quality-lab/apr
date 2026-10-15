# Local MCP reliability — approved first milestone

Status: approved by Andrey on 2026-10-15; implementation delegated to Muse Code. Active runtime reconfiguration and real-ledger import remain a separate reviewed installation step.

## Verified baseline

Public source revision: `9767588a4ffa17915ef017603cd2fa47c2ec5f64`.
Codex CLI: `0.152.1`. The configured MCP launches an older prototype path;
its five server/state/UI files match the public checkout byte for byte.
No user-level APR hooks are installed.

An isolated synthetic stdio/HTTP pass establishes initialization, tool listing,
request, old-link snapshot refresh, saving, identical direct-save deduplication,
skip without saving, invalid star rejection, Host/Origin checks, and saved-ledger
survival after restart. It does not establish automatic runtime capture.

Observed gaps: each request returns a new URL, pending links disappear on
restart, numeric `skip: 1` is accepted, and a new ledger inherits mode 0644 on
this host. The popup also overclaims automatic capture and uses ambiguous
Submit/Skip wording. Controller isolated baseline evidence was retained locally; no user review data belongs in this repository.

## Options and recommendation

1. **Durable local MCP first (recommended):** prove one private, restart-safe
   review flow that Codex can use manually. This provides a usable foundation
   without needing a cloud account or claiming automatic capture.
2. **Automatic Codex integration first:** blocked on proving a supported
   presentation surface. Current documented Stop output can request another
   model continuation or surface warnings; neither proves a token-free clickable
   footer appended to the completed assistant message.
3. **Cloud submission first:** adds client authentication, consent, ownership
   policies and a transmission boundary while local reliability remains weak.

## Approved behavior

- Use **⭐ Review this task** as the short link label; never assert that an
  unfinished task is complete. Apply it consistently to the maintained skill
  and any prototype-generated link instructions.
- Keep the MCP stdio protocol as the agent-facing bridge. One per-user local
  service owns HTTP serving and a SQLite store outside source checkouts.
- Persist the service's selected loopback port and opaque job token. A port
  conflict reports a diagnostic; never silently change the port and strand old
  URLs. Bind only to 127.0.0.1. A browser cannot reach a stopped service until
  the supported start command or MCP client starts it again; do not claim
  browser links automatically launch software.
- Return the same URL for repeated requests for the same job. Earlier links
  show the latest supplied card until a rating is saved. Freeze the reviewed
  snapshot with the rating so later work is not attributed to an older rating.
- Save a submitted rating transactionally once. Exact retries return the saved
  receipt; conflicting retries fail clearly. Pending cards and saved ratings
  survive service restarts. Skip/dismiss saves no rating and does not silently
  start another job; creating another job is explicit.
- Label the form **Save locally** and **Skip review**. Explain that skipping
  discards the selected rating, and that nothing is published. Show loading,
  save, error, retry, skipped and already-saved states accurately; prevent
  repeated clicks while saving and block saving an unloaded card.
- Use owner-only data directories/files. Validate skip as a literal boolean,
  preserve loopback Host/Origin protections, and authenticate agent-to-service
  writes with a per-user credential kept out of source, output and the vault.
- Do not turn on hooks, inspect transcripts or tool payloads, estimate missing
  token/cost data, claim full automatic support, upload reviews or add accounts.

## Preservation and implementation boundary

Keep SQLite schema changes in ordered, tracked migrations with application
tracking. Rehearse fresh installation and an upgrade using synthetic JSONL data.
Provide a reviewed import command with a dry-run and backup; preserve every
existing review and its provenance. Do not read, import, move or remove the real
ledger as part of baseline QA or silently switch the active installation.

Use one public-repository PR for this local milestone. Runtime reconfiguration
and real-ledger import require an explicit, reviewable installation step after
local acceptance. Keep website/auth/sharing changes separate. The current
landing page remains accurate until actual implementation changes its claims.

## Acceptance

1. Initialize and list/call tools through stdio in a fresh synthetic installation.
2. Request twice for one job: URL is identical and the first link shows the new card.
3. Restart service and MCP: the same link resolves without losing the pending card.
4. Browser save creates one private local record; retry/concurrent submit creates
   no duplicate. Save failure remains visible and recoverable.
5. Skip writes no rating and does not close or rotate the underlying job.
6. Reject malformed/foreign-origin requests; token/cost remain unknown without evidence.
7. Rehearse import and interrupted/retried import without data loss or duplication.
8. Inspect desktop/mobile rendered form, loading/error/saved/skipped states, and
   keyboard interaction. Verify a real multi-turn manual Codex walkthrough after
   installation, reporting capture and footer support separately.

Runtime source: https://learn.chatgpt.com/docs/hooks (reviewed 2026-10-15).
Native hooks are not evidence of final-message mutation.
