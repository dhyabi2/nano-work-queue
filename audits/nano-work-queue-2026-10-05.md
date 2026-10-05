# nano-work-queue — code audit, 2026-10-05

Scope: the tree at `4e77320` (`main`). Lens: can a seller agent get paid in XNO through this
queue, and can a buyer get work posted at all?

Baseline, before any change:

```
$ python3 -m unittest discover -s tests -t tests   # Ran 104 tests — OK
$ python3 e2e_check.py                             # ALL 95 END-TO-END CHECKS PASSED
$ python3 -m py_compile nano_work_queue/*.py tests/*.py e2e_check.py   # clean
```

The 2026-10-03 audit concluded "nothing to fix on the XNO path". This run took a different
route in: instead of reading the money path again, it **ran the README's quickstart as a new
agent would**, from `git clone` to the receipt.

## Found and fixed — the 401 told a buyer to send the seller's credential

Running the quickstart, the first call failed, and the message it failed with is the defect:

```
$ curl -s -X POST $BASE/v1/jobs -d '{...,"price_xno":"0.05"}'
{"error": "unauthorized",
 "message": "Missing or invalid buyer token. Nothing changed. Send it as
             'Authorization: Bearer <claim_token>' from the claim response."}
```

Every message in `errors.py` deliberately ends with the one thing the caller should do next,
because an agent's only recovery path is the text it reads back. `unauthorized` parameterised
**which** credential was missing but hardcoded the remedy for the claim-token case:

```python
def unauthorized(what="claim token"):               # errors.py:68
    return ApiError(401, "unauthorized",
        f"Missing or invalid {what}. Nothing changed. Send it as "
        "'Authorization: Bearer <claim_token>' from the claim response.")
```

`http_app.py:137` calls it as `errors.unauthorized("buyer token")` when `X-Buyer-Token` is
missing or does not match. So a buyer was told to use **the seller's header**, carrying **the
seller's credential**, out of **a claim response a buyer never has**. An agent that follows the
remedy fails again, with no indication of what the right header is — and posting work is the
first call a buyer makes, so nothing gets posted, nothing gets claimed, and nobody gets paid in
XNO.

**Fixed** by keying the remedy to the credential. The buyer case now names `X-Buyer-Token` and
`DEMAND_QUEUE_BUYER_TOKEN`, and says explicitly that a claim token will not do — pre-empting the
exact swap the old text invited. The claim-token case is byte-identical to before.

Two laws in `tests/test_http.py`, over a real loopback socket. Against the unfixed `errors.py`:

```
FAIL: test_the_401_tells_a_buyer_which_credential_to_send (headers=None)
AssertionError: 'X-Buyer-Token' not found in "Missing or invalid buyer token. Nothing
  changed. Send it as 'Authorization: Bearer <claim_token>' from the claim response."
FAIL: test_the_401_tells_a_buyer_which_credential_to_send (headers={'X-Buyer-Token': 'wrong'})
```

The second law pins the other half — a seller delivering with no `Authorization` header must
still get the claim-token remedy unchanged — so the fix cannot drift the other way.

After: **106 unit tests OK** (104 before), 95/95 end-to-end, `py_compile` clean.

## Checked and clean

- **The README quickstart runs, start to finish.** Driven against a live
  `python3 -m nano_work_queue.http_app --fake-node`: post (`0.05` XNO quoted back as
  `"0.050000"`), board, claim, deliver, accept, receipt. The receipt came back `settled` with a
  64-hex `block_hash`, `confirmed: true`, the payout address unchanged from the claim, and a
  block-explorer URL. Every documented step and every documented response shape held. The test
  counts in the README (104, 95) matched the tree exactly, and
  `python3 -m unittest discover -s tests -t tests` — the README's own invocation, `-t` included —
  works.
- **No float anywhere on an amount.** `amounts.py` is integer raw throughout. `parse_xno`
  refuses anything that is not a plain decimal *string*: ints and floats outright, `bool`
  explicitly, leading `+`/`-`, `1e-3`, `Inf`, `NaN`, more than one `.`, and non-digit
  characters. The scaling is done by shifting digit strings
  (`int(whole) * RAW_PER_XNO + int(frac.ljust(30, "0"))`), **not** `Decimal.scaleb`, with the
  comment naming what that avoided: under the default 28-significant-digit context
  `"1.000000000000000000000000000001"` came back one raw short and
  `"99.999999999999999999999999999999"` rounded *up* to a full 100 XNO. `format_xno` renders
  from `divmod`, never a division.
- **Paying twice is structurally prevented.** The job id is the idempotency key;
  `SettlementWorker.settle` calls `node.find_send(job.id)` and adopts an existing block before
  publishing, returns early if the job is already `SETTLED`, and `store.transition` refuses to
  reach `SETTLED` without a block hash. `mark_settled` validates the hash is 64-hex before
  storing it. A failure leaves the job `accepted` and visible as backlog in `/v1/health` rather
  than lost or double-paid.
- **Settlement cannot be reported before it happened.** A job reaches `settled` only after
  `node.is_confirmed(block_hash)` returns true inside a bounded poll; an unconfirmed block leaves
  it `accepted` with `settle_attempts` incremented. This is the check `vend` and `unstuck` were
  both missing — here it is done properly.
- **Posting what the account cannot pay is refused up front.** `post_job` sums every job in
  `open`/`claimed`/`delivered`/`accepted` and refuses when the price exceeds
  `node.balance_raw() - committed`, with the comment giving the right reason: better a refusal
  now than a default on work somebody already did.
- **`accept` does not touch the node**, so an unreachable node cannot hold a buyer's accept open.
- **The buyer token is compared in constant time** (`hmac.compare_digest`, `http_app.py:138`).
- **No secret in the tree.** No seed, key or token value in any tracked file; the buyer token is
  read from the environment and the README issues it with `secrets.token_hex(16)`.

## Found, not fixed — reported instead

- **`committed` double-counts a job whose send is already on-chain.** An `accepted` job that has
  been sent but not yet marked `settled` is counted in `committed` while the node's balance has
  already dropped by it, so `available` is understated for that window and a post may be refused
  that the account could afford. It errs on the safe side — a refusal, never an overdraft — so it
  is recorded rather than changed.
- **A server started with no `DEMAND_QUEUE_BUYER_TOKEN` answers every buyer call with the same
  401** that blames the caller's token (`not expected or not hmac.compare_digest(...)`,
  `http_app.py:137`). The operator's actual mistake — the variable is unset — is invisible in the
  message. Distinguishing the two cases is a judgement about what a 401 should disclose, so it is
  left for the owner.
- **`_await_confirmation` polls every 10 ms for up to 60 s** (`settlement.py`), which is up to
  ~6000 `is_confirmed` calls against a real node for one settlement. Correct, but hard on a
  public RPC. Changing the interval is tuning, not a defect, so it is not carried here.

## Could not verify

- **Against a real Nano node.** No socket left the machine. The whole suite and the end-to-end
  check run against `FakeNode`, and the quickstart above was driven with `--fake-node`, so what
  is proved is the service's own logic and HTTP surface — not that a real node honours the
  `idempotency_key` contract `node.py` documents as a MUST. That contract is the one thing
  standing between an ambiguous timeout and a double payment, and only a connected node can
  confirm it.
- **`NanoNode`'s real implementation.** `node.py` defines the interface and `FakeNode`; no RPC
  implementation is in this tree, so whichever one the operator supplies is unaudited here.
- **The consent path** (`consent.py`, `consent_page.py`, 590 + 289 tests of its own) was not read
  this run; it is green and off the settlement path.
