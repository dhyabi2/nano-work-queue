# nano-work-queue — audit, 2026-09-27

First audit of this repository. Read in full: `amounts.py`, `address.py`,
`store.py`, `service.py`, `settlement.py`, `node.py`, `http_app.py`, and the
id generators in `consent.py`.

## What was checked, and how

Baseline before any change, all green: `python3 -m pytest -q` (102 tests,
20 subtests) and `python3 e2e_check.py` (95/95).

**Secrets, tree and full history.** Scanned every commit reachable from every
ref for 64-hex values, token-shaped assignments, and the usual provider key
prefixes. Two 64-hex constants and one `clm_` token appear in the tree; all
three are documentation examples in `a2a.py` or fixtures, and none is a
credential. Nothing to report.

**The money path is integer-only, as `amounts.py` claims.** No float is
reachable from it. `parse_xno` refuses ints, floats, bools, `"1e3"`, `"Inf"`,
`"NaN"`, a leading sign and anything with more than 30 decimal places, and the
scaling is done by shifting digit strings rather than through `Decimal`
arithmetic — so the 28-significant-digit context cannot round an amount. Round
tripping `format_xno` through `parse_xno` is exact at full precision. No defect
found here.

**The address codec is correct.** `normalise` decodes the 52-character body,
drops the four pad bits, and compares the declared checksum against
`blake2b(public_key, digest_size=5)` reversed. The `body[0] in "13"` rule is
right: that character carries four zero pad bits plus the top key bit, so only
those two symbols can appear. A one-character alteration is refused.

**Authentication was checked endpoint by endpoint.** Every endpoint that can
move money or change the queue on the buyer's behalf — `post_job`, `accept`,
`reject`, `close`, `consent_create` — goes through `_need_buyer()`, and both
that check and the claim-token check use `hmac.compare_digest` against a SHA-256
digest rather than comparing the token itself. Claim tokens reach a log only as
an 8-character stub, and the handler's default request logger is disabled so a
URL can never carry one into a log. No defect found here.

**The state machine holds its two invariants.** Only pairs in `TRANSITIONS` are
possible, and `mark_settled` refuses anything that is not a 64-character hex
block hash — so no path can report a payment with no block behind it. The
settlement worker adopts an existing send for a job id before publishing
another, so an ambiguous timeout cannot pay twice.

## Fixed

**Pagination silently restarted at page one** (`service.py`, `list_jobs`). The
cursor was resolved by looking for its job id inside the *filtered* list:

```python
ids = [j.id for j in jobs]
start = ids.index(cursor) + 1 if cursor in ids else 0
```

The filter is `state`, which defaults to `open`, and this queue moves jobs out
of `open` continuously — a claim, a TTL reap, a reject. `next_cursor` is always
the last job of the page just served, so the single job a cursor names is
exactly the one most likely to have been claimed by the time the caller asks
for the next page. When that happened the cursor was not in `ids`, `start` fell
back to `0`, and page two came back as page one — with no error and no
indication anything had been skipped or repeated. A seller agent walking
`/v1/jobs?state=open` re-reads jobs it has already seen; if claims keep arriving
at the rate it pages, it never advances at all.

Demonstrated on five open jobs with `limit=2`: after claiming the job named by
`next_cursor`, page two returned `['job_a580af…', 'job_bf4eb4…']` where
`job_a580af…` had already been served on page one.

The cursor is now resolved against the whole store, not the filtered list, and
the page starts strictly after that job's `(created_at, id)` sort key — so
paging is stable whatever the job's state has become. A cursor naming no job at
all is now a `400 bad_request` rather than a silent restart.

Proved by `test_pagination_advances_when_the_cursor_job_leaves_the_filter`,
which fails against the old code with the repeat shown above and passes after,
and `test_pagination_refuses_a_cursor_that_names_no_job`, which fails with
`AssertionError: ApiError not raised`. Full suite after the change: 104 tests,
20 subtests, plus 95/95 end-to-end.

## Found, not fixed

**`reject` leaves the previous seller on a reopened job.** `service.py` clears
`claim_token_hash`, `claim_expires_at`, `claimed_at`, `delivered_at` and
`payload` when a rejected job returns to `open`, but not `seller` or
`payout_address` — unlike `release` and `reap_expired_claims`, which clear both.
The job is then `open` and unclaimed while `GET /v1/jobs/<id>` still names the
rejected seller and the public `GET /v1/receipts/<id>` still shows their
`payout_address`. It cannot cause a wrong payment: `claim` assigns
`job.payout_address` unconditionally, so the stale value is overwritten the
moment anyone claims the job again. Left alone because whether a rejection
should stay on the public receipt is a product decision, not a defect — the
receipt arguably exists to record exactly that. Worth an owner's view.

**An oversized `Content-Length` is refused without draining the body.**
`http_app.py` raises `payload_too_large` before reading `rfile`, and the handler
speaks HTTP/1.1 with keep-alive, so the unread body bytes are left in the
connection and the next request on that same connection is parsed from the
middle of the previous one. Only reachable by a client that both oversizes a
body and reuses the connection; not demonstrated against a live server, so it
is recorded rather than claimed.

**`SettlementWorker.run_all` has an unreachable guard.** `before = len(settled)`
is captured, then an id is appended before `len(settled) == before` is tested,
so that branch can never be true. It is marked `pragma: no cover` and is
harmless. Not touched: changing it would be noise.
