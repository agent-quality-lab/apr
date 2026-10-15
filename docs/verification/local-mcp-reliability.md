# Local MCP reliability — verification

Date: 2026-10-15. Baseline: `9767588a4ffa17915ef017603cd2fa47c2ec5f64`.
Implementation: Muse Code; integration and rendered QA: controller; independent
source reviews: Droid. Everything below used isolated synthetic data.

## Regression evidence

The prototype baseline passed 12 of 16 bounded checks. It returned different
same-job URLs, lost pending cards on restart, accepted numeric skip, and created
an insufficiently private ledger. The first service candidate also failed host
startup. Regression tests reproduced legacy `scope: "local"` rejection, saved
snapshot replacement, stale-PID signaling, and inconsistent WAL backups.
Those behaviors were repaired before acceptance.

Independent review found additional proxy/redirect leakage, configuration
creation races, unproven service discovery, and editable in-flight ratings.
The controller reproduced redirect/bearer disclosure and fake-service acceptance
using owned loopback listeners and synthetic credentials. The repaired client
requires a fresh HMAC nonce proof, then sends private data on that same TCP
connection with automatic reconnection disabled. Proof bodies are bounded;
proxies and redirects are not used.

The final bounded worker run timed out after writing its corrections. Its result
was not counted as a successful worker job: the controller reconciled the diff,
fixed outdated test doubles (`read(size)`) and terminal-state assertions, and
verified the resulting artifact independently. The earlier worker shell could
not bind localhost; real integration checks ran on the approved host instead.

## Final commands

```sh
python3 -m unittest discover -s tests -v
node --check rating-popup/app.js
git diff --check
```

The final host run passed **94 tests in 51.577 seconds**. JS syntax and diff
whitespace checks also passed. Droid confirmed both final behavioral fixes;
its two remaining test-double/assertion findings were corrected and included
in this successful run. Tests cover schema/validation,
tracked migrations, owner-only files, pre-open symlink refusal, stable tokens,
frozen saved snapshots, transactional identical/conflicting/concurrent saves,
stale pending revisions, skip without saving, authenticated service identity,
closed-proof/no-reconnect behavior, proxy/redirect isolation, occupied persisted
ports, concurrent cold starters, repeated MCP processes, service/MCP restarts,
malformed requests and Host/Origin checks. Synthetic legacy import covers the
real local envelope, timestamps/context/ratings/provenance, duplicate rows,
conflicts, dry-run, live-WAL backup, idempotency and injected mid-import rollback.

## Rendered browser evidence

The controller exercised the real form in the internal browser:

- Save enabled only after an outcome and all four ratings. A save produced one
  record and “Saved on this computer. Nothing was published.” Reload was read-only.
- Skip after selecting ratings saved no record; the same link remained pending.
- Updating a pending snapshot rejected the old revision, showed the new summary,
  and allowed an explicit reviewed retry. Later requests could not alter saved work.
- Service failure produced a visible recoverable save error; restart, Retry and
  Save recovered at the same link. Failed summary loading disabled Save and Skip;
  restoring the connection and Retry loaded the actual card.
- An eight-second response delay left every outcome/reason/star and Save/Skip
  disabled, with Retry hidden. A save retried after failure stayed locked in flight.
- A competing save preserved the committed ratings and read-only confirmation,
  hid unsaved selections, kept all controls disabled, and offered no false Retry.
- At 375×812, long text wrapped with no horizontal overflow (scrollWidth=375),
  markup-like text stayed literal, and Enter activated the outcome with visible focus.

Temporary network blocking and viewport overrides were removed. Screenshots were
inspected locally; private token URLs and synthetic databases are not committed.
QA services were stopped. One separately labeled isolated owner preview may be
left running for manual testing; it contains no actual task history.

## Release boundary

No active Codex configuration, installed skill, hooks or actual history was
changed or imported. The maintained source skill and prototype instructions use
**⭐ Review this task**; prototype hooks remain off. The operator guide documents
bridge registration and the full skill copy after review. Merge, reviewed
installation and a real multi-turn Codex walkthrough remain acceptance steps.
Automatic capture/footer insertion, cloud authentication and uploads are separate
and unverified capabilities. Missing usage metrics stay unknown.
