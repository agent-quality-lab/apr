# Codex chat capture verification — 2026-10-16

Scope: the first opt-in local Codex capture slice, based on `64c2d13` and the accepted [CTO plan](../superpowers/plans/2026-10-16-codex-chat-capture.md). This is adapter/store/UI verification with synthetic data, not native runtime acceptance. The installed manual bridge and skill were not changed.

## Evidence and review

The owner requested two distinct GPT-6 Luna High agents with the CTO role, both bootstrapped from the canonical knowledge base. One planned; the other implemented. The planner then independently reviewed all changed source, tests and operator documentation. The controller reviewed the diff and ran host-side integration and rendered checks. Review repairs addressed missing-turn tool attribution, pending-card revision changes on coverage warnings, ambiguity in repeated resume/compact markers, and the distinction between live evidence and evidence frozen at rating time.

The frozen implementation passed the controller host suite: `python3 -m unittest discover -s tests` — **118 tests, 52.350 seconds, OK**. Python compilation, `node --check rating-popup/app.js`, example JSON parsing and `git diff --check` passed. An earlier full run found two outdated migration-count assertions; both were corrected to match additive migration 3 and retested. Independent final source review found no actionable finding.

Reviewed implementation hashes: `review_store.py` SHA-256 `7f7fde4612a9644dafa630ccd5407b073f60781d6b9c0c90f7dae43126716309`; `codex_capture.py` `52302326c8155c2056a098c475b77aa6c2af44dfb01b58b52d174865369316ed`; `rating-popup/app.js` `a2b0072048870c5307dea0573ca78079c244c3d16ed432e8296b17a0d78f01fb`; capture tests `11ab23c67229c47a8c08fa5a9a69e1219236a166e1ad39b5e914fef76145ef8c`.

## Isolated behavioral checks

- Two interleaved synthetic native sessions, three turns and paired tools each: separate review IDs/URLs and models/counts; no collection depends on opening a page.
- Duplicate tool receipt: event and tool totals unchanged.
- A new observation after opening a card: stale submission rejected with HTTP 409; current revision saves.
- An actual isolated service stop/start preserved both captured chat URLs and frozen evidence.
- A capture warning after saving was labeled separately; a pending warning was never labeled as post-rating.
- Save followed by later activity: rating, snapshot and evidence watermark remain frozen; live evidence is explicitly marked as later.
- Safe command-hook stdin entry: empty stdout; excluded prompt, tool input/output, path and raw native-ID sentinels absent from database bytes.
- Version-2 migration preserves existing manual rows, URLs and revisions; concurrent processes share one private salt.
- Rendered review page inspected at the normal desktop viewport and 390 × 844 narrow viewport: observed activity, partial coverage, saved read-only state and frozen watermark wrap without horizontal overflow.

## Runtime limits and rollout

No real Codex hooks were installed, enabled or trusted. No user transcript, review history or default review database was inspected or imported. Native startup and Interrupt delivery, actual native ID continuity across desktop resume/restart, complete hosted/subagent attribution and automatic assistant footer association remain unverified. Tokens/cost remain unknown. UTC hook receipt intervals describe approximate wall time, not compute time.

Repeated SessionStart occurrences lack a verified native occurrence ID. Resume/compact markers remain replay-safe and expose `lifecycle_occurrence_unverifiable`; model lists come from observed turn-start events. Changed native IDs are not merged by working directory or recency. Reused IDs on clear/fork are quarantined.

The example hook registration remains inactive. After source review/merge, inspect and deliberately trust an isolated registration, then verify two real chats across multiple turns and resume/restart before wider activation. Disable that registration to stop capture while preserving local records. Native zero-extra-generation footer presentation is a separate capability gate.
