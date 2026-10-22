# APR Installable Local Alpha Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. Worker capsules supply task-specific scope and constraints.

**Goal:** A newcomer follows the public page and repository instructions to install MCP plus the skill and record/review a task in Codex on macOS.

**Architecture:** Preserve the existing Python standard-library stdio bridge, private loopback service, SQLite store and task lifecycle. Ship a documented manual install and coherent static landing/docs. Hosted publication, hooks, new runtimes and installer code are deferred.

**Tech Stack:** Python/SQLite, MCP JSON-RPC, Codex skills, static HTML/CSS/JS, existing unittest and documentation checker.

## 1. Engineering: install and first task
- [x] Update README.md, docs/operators/local-mcp.md and task-recording.md into a newcomer path with concrete clone/config/skill/reload/activation instructions.
- [x] Update source skill discovery/activation metadata narrowly, retaining lifecycle/privacy/human-rating contracts.
- [x] Rehearse JSON-RPC initialize/tools/list and task baseline/evidence/finish with temporary synthetic APR_DATA_DIR; check replay/restart and stop daemon.
- [x] Reproduce acceptance-blocking defects before any minimal code fix; add meaningful regression checks only for behavior changes.
- [x] Record actual versions, evidence and gaps in docs/verification/local-alpha-install.md. Run python3 -m unittest discover -s tests after substantive completion.

## 2. Website: parallel preparation in separate repository
- [x] Update site/index.html with installation CTA and current task lifecycle; retain editorial design.
- [x] Update docs/local-mcp-reference.md and site/docs/index.html together; link canonical public installation rather than duplicate it.
- [x] State experimental Codex/macOS scope and hosted publishing limits. Separate local storage from model-provider exposure.
- [x] Run python3 scripts/check-docs.py; inspect desktop/mobile rendered pages and navigation.

## 3. Independent acceptance and review
- [ ] A separate worker follows instructions in a clean synthetic checkout and named real agent host; verify actual skill/tool discovery and complete task link.
- [x] Controller verifies resulting isolated records and service restart; synthetic UI review does not substitute for the owner's genuine rating.
- [x] Independent review checks scope first and then correctness; resolve material findings and recheck affected behavior. No broad routine security fan-out.

## 4. Handoff and release preparation
- [ ] Commit and push one focused implementation branch per repository, open and attach reviewer-ready PRs, identify tested revisions and remaining real-user acceptance.
- [ ] Prepare existing Sites deployment preserving identity/audience and exporting only public site files; no production promotion before applicable owner approval/acceptance.
- [ ] Give owner a short first-task prompt and direct local review link. Owner performs genuine review and public sharing decision.

Done at this agent boundary: implementation, independent checks and concrete release candidate ready for owner acceptance; do not claim public release from source tests alone. Repositories use main; generic master wording maps to the verified project default main.

Controller evidence: isolated real CLI and resumed two-turn flow passed; a newcomer user-level install and desktop reload remain owner acceptance. Final repository suite: 152 tests passed in 52.282 seconds.
