# Local MCP reliability — implementation plan (2026-10-15)

Delegated implementation of `docs/superpowers/specs/2026-10-15-local-mcp-reliability-design.md`.
Base `9767588a4ffa17915ef017603cd2fa47c2ec5f64`, branch `codex/local-mcp-reliability`.

## Steps

1. [x] Plan — write this file with concrete steps/files/acceptance.
2. [x] Failing tests first — add `tests/` suite (unittest discovery) covering schema,
   store, service, bridge, UI static assertions; run and record red evidence.
3. [x] `review_schema.py` — extract validation (review context/full, ratings, skip,
   NaN/Infinity rejection, field preservation) with no service/store imports.
4. [x] `review_store.py` — SQLite store: data-dir resolution, perms, symlink refusal,
   one tracked migration sequence + `schema_migrations`, jobs/ratings/snapshots,
   same-token persistence, revision, transactional save (identical→receipt,
   conflict→error), import (dry-run/backup/atomic/idempotent/provenance).
5. [x] `local_service.py` — loopback HTTP service + `start/serve/stop/status/import`
   CLI + IPC helpers: persisted port + per-job token, startup/daemon locks, bounded
   wait + identity verify, occupied-port error, Bearer internal auth, exact Host +
   same-origin checks, route/size/JSON constraints, health/status hygiene,
   browser card/submit with revision, static UI serving.
6. [x] `mcp_server.py` — thin stdio bridge: same tool schemas, `expires_in_seconds:null`,
   no 24h claim, ensure-service then authenticated internal calls; remove embedded
   HTTP/ledger logic.
7. [x] UI `rating-popup/` — `Save locally`/`Skip review` texts, revision submit,
   skipped/saved/loading/error states, in-flight + unload guards, no sample-as-real,
   no automatic-capture claims, wrapping fixes.
8. [x] Skill + docs — `⭐ Review this task` link text, no expiry claim, `expires null`
   contract; `README.md` + `docs/operators/local-mcp.md` (commands, data location,
   private files, URL/restart semantics, manual Codex integration, import, conflict
   recovery, retention/limitations, owner walkthrough, no `~/.codex` edits);
   prototype hook modules marked unsupported (unchanged, uninstalled).
9. [x] Verification — full suite on fresh `APR_DATA_DIR`s incl. stdio bridge + HTTP,
   same-URL, restart, persistence-after-MCP-exit, single-daemon concurrency,
   occupied-port, auth/Origin/Host/type/body/skip rejections, transactional +
   concurrent saves, stale-revision, frozen snapshot, skip semantics, modes,
   migrations/import, JS syntax; record in `docs/verification/local-mcp-reliability.md`.
10. [x] Final report — STATUS, changed files, test red/green, untested items, isolated
    QA instructions without credential printing, no auto-Codex claims.

## Acceptance (from task §1–9)

- stdio JSON-RPC thin client → per-user loopback service owning SQLite + UI.
- Data outside source; `APR_DATA_DIR` override; 0700 dirs / 0600 files; no symlink
  following for sensitive files; no credential/token logging.
- Detached on-demand service survives stdio exit; persisted port + per-job token;
  restart reuses port; occupied port = clear error; startup + daemon locks; bounded
  wait + identity verify; 127.0.0.1 only; start/stop/status CLI.
- Internal writes need per-user Bearer credential (header only); browser uses scoped
  job token; exact Host + same-origin enforced; constrained routes/sizes; JSON for
  mutations; NaN/Infinity + non-boolean skip rejected; health/status leak nothing.
- Same `rating_url` per `review_id` across requests/restarts; old links show latest
  pending snapshot; `expires_in_seconds:null`; no 24h claim; saved snapshot frozen;
  new job = new `review_id`; no auto-rotate on skip/save.
- Transactional unique-per-job save; identical retry → same receipt; conflict →
  clear error; revision-guarded browser save (stale → reload feedback); concurrent
  identical safe; `record_review` preserved with validation.
- Skip writes nothing, no rotation; exact `skip:true`; UI states correct;
  `Save locally`/`Skip review`; no public/cloud claims; caller-supplied model/context;
  `⭐ Review this task` link text; hooks stay off.
- JSONL import CLI with dry-run + private backup; full-file validate before apply;
  atomic + repeatable; conflicts/malformed abort cleanly; provenance + digest +
  timestamps preserved; original untouched.
- README/operator docs complete; active Codex config untouched; install left to
  controller/owner.

## Checkpoints

- [ ] RED: new tests fail against old `mcp_server.py` (no service/store/modules).
- [x] GREEN: full `python3 -m unittest discover -s tests` passes on fresh temp dirs.
- [ ] Evidence file written; services stopped; `git status` reviewed, uncommitted.

## Repair pass (controller findings 1–9)

- [x] RED: `tests/test_regressions.py` (15 tests) fails/errors against the
  first candidate (scope rejection, snapshot overwrite, stale-PID kill,
  missing backup/revision helpers, text-mode `.decode()`).
- [x] GREEN: 37 non-network tests pass
  (`test_regressions` + `test_store` + `test_schema`); `node --check` clean.
- [x] HOST: controller runs `tests/test_service.py` (loopback blocked in the
  worker); evidence in `docs/verification/local-mcp-reliability.md`.
- [ ] No commit/push; changes left uncommitted for controller review.

## Final bounded repair pass (2026-10-15, controller review blockers)

- [x] Private HTTP: proxy-disabled opener (`ProxyHandler({})` → no proxy
  chain entries; direct `HTTPHandler`/`HTTPSHandler` only), `_NoRedirect`
  refuses all redirects incl. loopback, `validate_base_url` exact-loopback
  only; `http_call` uses the private opener. Worker-mock policy tests +
  host sink tests in `tests/test_service.py` (`RedirectProxyLeakTest`).
- [x] Read-only `status`/`stop` (`load_existing_config`, never creates);
  serve creation contract (`_serve_load_config`: spawned child read-only
  via `APR_SPAWNED_SERVE`, direct serve serializes via startup lock, no
  deadlock); `stop` stays authenticated, never signals PID.
- [x] DB sidefiles fail closed pre-open (`_check_db_files_preopen` for
  `""`/`-wal`/`-shm`/`-journal` with sqlite-connect spy test); swallowed
  chmod removed (`secure_file`/`secure_existing_private` raise
  `StoreError`); lock/log 0600 enforced on existing files; backup sidecars
  checked.
- [x] Precise exceptions (`ImportAbort`, `FileExistsError`) replacing
  generic `assertRaises(Exception)`; unique `/tmp` probe child, no unlink
  of unrelated files.
- [x] Host tests added: occupied persisted port (error, config unchanged),
  malformed JSON/content-type/NaN/oversize/bad skip/Host+Origin on browser
  + private endpoints, repeated MCP processes same URL, redirect/proxy
  sink tests, cold serve/start race.
- [x] Install guide: single canonical block in
  `docs/operators/local-mcp.md` (verified
  `codex mcp add agent-performance-review -- python3 <checkout>/mcp_server.py`
  + TOML equivalent, FULL skill copy); duplicate block removed; README
  points at the guide.
- [x] GREEN (worker): 57 non-network tests
  (`test_final_repair` 20 + prior 37); `py_compile` clean.
- [x] HOST: controller runs full `tests/test_service.py` incl. new host
  tests; evidence in `docs/verification/local-mcp-reliability.md`.
- [ ] No commit/push; changes left uncommitted for controller review.

## Controller acceptance checkpoint

- Final host suite: 94 tests pass; JS syntax and diff checks pass.
- Rendered desktop/mobile, delayed-save, conflict, retry, saved and skipped states
  verified with synthetic data; test overrides removed.
- Independent Droid findings resolved; source artifact reconciled after the last
  worker timeout, with test-double/assertion corrections verified by controller.
- Original pre-module RED checkpoint was not independently captured; later
  behavioral regressions and host failures are recorded in the verification report.
- PR handoff is the implementation boundary. Merge, reviewed active installation,
  real-history import if requested, and real multi-turn Codex acceptance remain.
