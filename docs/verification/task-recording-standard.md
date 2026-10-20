# Task recording standard verification

## Verified scope

The final controller run completed `python3 -m unittest discover -s tests`: 150 tests passed in 52.300 seconds. In the separate community website repository, 20 tests passed, including the real PGlite migration/RLS path and jsdom bundle UI. The documentation checker passed for three pages, the source skill's `quick_validate` was valid, and local JavaScript and diff checks passed.

Browser validation used an isolated, explicitly illustrative fixture. It covered a negative outcome with an overall rating and absent optional ratings; typed 30-minute, $1 and 12,000-token measurements; expensive and efficient task rows; a 390-pixel viewport without horizontal overflow; selected-field redaction of a private sentinel; a saved task link after restarting service version 0.6.0; and local preview of an approved file in the community site. The browser did not use or modify real user history.

The public preview preserves unknown measurements as unknown. It includes only selected details and the reviewed snapshot. The community preview was local: there was no configuration change, publication, or removal.

## Fixes and compatibility

The final review addressed event-operation ambiguity, path and secret filtering, SQL `NULL` enum handling, and bare temporary-path filtering. Legacy finish-shaped event rows have no operation discriminator; retries that cannot be distinguished safely are rejected with guidance to use a new event ID. Existing records and links are preserved, but exact retry compatibility for every old event is not claimed.

An earlier partial UI implementation was reconciled against independent review and the final browser checks; four UI tests passed. A read-only Droid review found four defects. Its initial edit attempt was refused by permissions; the documented development-mode run applied repairs that passed review. A later native review identified the remaining temporary-path case, which was fixed and covered by a regression test.

## Limits

No live MCP integration, installed-skill rollout, native runtime hooks, hosted database apply, OAuth flow, public publish/removal proof, or real-history acceptance was performed. Resource values in the browser are illustrative typed fixture data, not measured runtime evidence. Source changes and local tests do not update an installed skill or restart a running service automatically.
