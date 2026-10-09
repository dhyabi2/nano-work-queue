# nano-work-queue — audit 2026-10-09

Same lens: can an agent pay, or get paid, in XNO with this code today without being hurt? This
queue **pays out** — a buyer posts work, a seller claims it with no signup, and the queue sends
XNO on accept — so the questions here are whether the wrong party can be paid, whether anyone
can be paid twice, and whether a seller can be told they were paid when they were not.

**Nothing was changed. No defect was proven, and this audit is the only file in the pull
request.** The notes at the end are notes, not findings dressed up as one.

## What I checked

Baseline on `main` (`32af575`), both commands CI runs:

```
python3 -m unittest discover -s tests -t tests   # Ran 106 tests — OK
python3 e2e_check.py                             # ALL 95 END-TO-END CHECKS PASSED
```

- **The README transcript, run for real, not read.** Started the service
  (`python3 -m nano_work_queue.http_app --fake-node --port 8099`) and executed all six curl
  steps in the "A full run, start to finish" block against it: post → board → claim → deliver →
  accept → receipt. Every step returned what the README says it returns, and the job reached
  `settled` with a 64-hex block hash and `confirmed: true`. The first step a new agent would
  take works.
- **`amounts.py` end to end.** Integer raw throughout; 1 XNO = 10\*\*30 raw. `parse_xno`
  refuses floats and ints outright, refuses `+`/`-`, refuses `1e-3`/`Inf`/`NaN`, refuses more
  than one `.`, and refuses more than 30 decimal places. The scaling is done by **shifting digit
  strings** (`int(whole) * RAW_PER_XNO + int(frac.ljust(30, "0"))`), not by `Decimal.scaleb`, and
  the comment at `amounts.py:46` names exactly why: `scaleb(30)` under the default
  28-significant-digit context returned `"1.000000000000000000000000000001"` one raw short and
  rounded `"99.999999999999999999999999999999"` up to a full 100 XNO. **The `prec=28` defect
  class that is open across four sibling repositories is not present here, and was deliberately
  designed out.** `format_xno` renders from `divmod`, never a float.
- **The state machine, for a wrong-address payout.** This was the one thing I expected to find
  and did not. `settle()` adopts an existing send by idempotency key
  (`settlement.py:60: node.find_send(job.id)`) and marks the job settled **without comparing
  that block's destination or amount to the job's** — so I traced every way the job's
  `payout_address` could change after a send could exist. It cannot: `TRANSITIONS`
  (`store.py:29`) allows `ACCEPTED → SETTLED` and nothing else out of `accepted`, so `release`
  (`service.py:191`) raises `IllegalTransition` on an accepted job, and `reap_expired_claims`
  (`store.py:148`) only touches `CLAIMED`. `deliver` refuses a conflicting address with
  `payout_address_conflict` (`service.py:215`). Sends only happen from `accepted`. The address is
  frozen before any send can exist, so the blind adopt is **not reachable today**. See the note
  below.
- **Paying twice.** The job id is the idempotency key, `find_send` is consulted before `send`,
  and `settle()` returns early when the job is already `SETTLED` (`settlement.py:56`) so two
  workers racing one job both land there. A send that publishes but never confirms leaves the job
  `accepted` and surfaced as `settlement_backlog` — `_await_confirmation` is bounded
  (`settlement.py:84`) and never marks settled on a timeout.
- **Reporting a payment that did not happen.** `mark_settled` refuses a null or malformed hash
  (`store.py:135`), and `transition` independently refuses `SETTLED` with no block hash
  (`store.py:127`). Two guards, in the one module, so no HTTP path can get around either.
- **Address validation.** `address.py` verifies the 5-byte blake2b checksum before a claim is
  accepted, using only the standard library. The suite includes the genesis account and a
  one-character mutation of it.
- **Authentication.** Both secrets are compared with `hmac.compare_digest` — the buyer token at
  `http_app.py:136`, the claim token at `service.py:386` (against a stored hash, not the token).
  The app refuses to start without `DEMAND_QUEUE_BUYER_TOKEN` (`http_app.py:325`). Claim tokens
  reach a log only through `token_stub`, which is the first 8 characters.
- **Shell and path handling.** No `os.system`, no `shell=True`, no `subprocess` outside the
  end-to-end harness. Nothing joins a caller-supplied string into a path.
- **Secrets, tree and full history.** Scanned `git log --all -p` for Nano account strings, `sk-`,
  `ghp_`, `AKIA`, and PEM headers. Three hits, all of them deliberate public test data: the
  all-ones burn address, a one-character-corrupted twin of it used to prove the checksum
  rejects a mutation, and the Nano genesis account. **No key, seed or token value anywhere in
  the tree or in any commit.** Nothing to rotate.

## What I found

**Nothing worth changing.** No crash, no wrong amount, no float on an amount, no acceptance that
should be a refusal, no broken import, no README step that fails, no secret. Finding nothing here
is the honest result, and this repository is the reason: the money path was built integer-only on
purpose and says so in its comments.

## Notes — not findings, and not acted on

- **An adopted send is trusted for its destination and amount.** As traced above, `settle()`
  marks a job settled on a block it got from `find_send(job.id)` without establishing that the
  block pays `job.payout_address` exactly `job.price_raw`. The published receipt tells a stranger
  the opposite — *"It sends exactly price_xno to payout_address"* (`service.py:27`) — so the
  service asserts a property the settlement path never checks. For a send this service published
  itself the property holds by construction, from the arguments passed at `settlement.py:62`.
  For an **adopted** block it rests entirely on the node implementation's idempotency index being
  right, and `NanoNode.find_send` (`node.py:26`) returns a bare hash, with no way for the service
  to check.

  **Not fixed here, deliberately.** It is not reachable through today's state machine, so there
  is nothing to show happening; and closing it needs a new method on the `NanoNode` interface so
  the service can read an adopted block's destination and amount. That is a decision about where
  the contract puts the verification — in the node implementation or in this service — and the
  repository has no real `NanoNode` wired in yet (it "refuses to start against a real rail until
  a `NanoNode` implementation is wired in"). **Whoever writes that implementation is the person
  this matters to**, and the cheap version of the answer is one line in `node.py`'s interface
  docstring making the destination-and-amount guarantee part of what `find_send` promises, beside
  the idempotency guarantee it already promises. Worth deciding before a real rail is wired in,
  not after.
- **`parse_xno`'s character filter admits non-ASCII digits.** `all(ch.isdigit() or ch == ".")`
  (`amounts.py:35`) is true for Arabic-Indic and full-width digits, and `Decimal` and `int` both
  accept them, so `"١٠"` and `"１５"` parse as 10 and 15 XNO. The amount that results is
  **correct** and renders back canonically through `format_xno`, so there is no wrong amount and
  no mismatch to exploit — it is only narrower than the docstring's "a plain decimal string".
  Not changed: tightening it to `"0123456789"` would be a change with no defect behind it, which
  is the kind of noise this run is meant not to produce.
- **`in_price_range` is checked at post time, not at settle time.** A balance that falls between
  the two leaves a job `accepted` and in the backlog rather than paying short, which is the
  honest outcome. Working as intended.

## What I could not verify

- **Against a real Nano node, and no XNO moved.** Every run above used `FakeNode`; the suite
  opens no socket by design. So nothing here establishes how a real `NanoNode` implementation
  behaves — above all the idempotency guarantee in `node.py`'s interface docstring, which is the
  load-bearing promise behind "a retry cannot pay twice" and which no code in this repository can
  enforce.
- **The hosted surface.** Everything was exercised against `127.0.0.1:8099` in this container.
- **`https://nanolooker.com/block/<hash>`**, the explorer the receipt points a stranger at: the
  container's network policy denies it, so "checkable by a stranger" is established from the
  receipt's shape rather than by following the link.
