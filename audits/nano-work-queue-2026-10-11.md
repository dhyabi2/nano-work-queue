# nano-work-queue — audit 2026-10-11

Lens: can an agent get paid in XNO through this queue today, without being hurt.

## Checked

- Baseline on `main` (`a962af7`): `python3 -m unittest discover -s tests -t tests` **106 passed**,
  `python3 e2e_check.py` **95 checks passed**. README's quickstart runs as written.
- The amount path end to end: `amounts.parse_xno` / `format_xno` / `add` / `in_price_range`.
  Raw is an integer throughout; `parse_xno` refuses ints, floats, bools, signs, `1e-3`, `Inf`,
  `NaN` and more than 30 dp, and scales by shifting digit strings rather than through
  `Decimal.scaleb`, so the 28-significant-digit default context cannot round an amount.
  `grep` for `float` across `nano_work_queue/` finds it only in `clock.py` (seconds), never on
  an amount.
- `address.normalise`: `nano_`/`xrb_` prefix, 60-character body, first character `1` or `3`,
  blake2b-5 checksum compared little-endian. A one-character-off address is refused before a
  claim is stored, and again before a delivery is accepted.
- The state machine in `store.py`: `TRANSITIONS` is the only way a state changes, and
  `mark_settled` refuses a hash that is not 64 hex characters, so no path can publish a receipt
  with no block behind it. `reap_expired_claims` and `release` both clear `seller` and
  `payout_address`, and `claim` overwrites both, so a reopened job cannot pay the previous
  seller's address.
- `settlement.py`: `find_send(job.id)` adopts an earlier send before publishing another, so an
  ambiguous timeout cannot pay twice; a job stays `accepted` and shows in
  `/v1/health.settlement_backlog` until a block confirms.
- Every node call site (`grep` for `.node.`): four in all. `health()` wraps its read,
  `SettlementWorker.settle` catches `NodeError`/`OSError`, `_await_confirmation` is bounded —
  and `service.post_job` was the one that was not wrapped. See below.
- Auth: `X-Buyer-Token` and the claim token are both compared with `hmac.compare_digest`; a
  well-formed token belonging to another job answers 403, not 401. No key, seed or token is in
  the tree (`git grep` over the working tree clean; the service refuses to start without
  `DEMAND_QUEUE_BUYER_TOKEN` from the environment).
- Deliver-first: there is no endpoint that takes money from a seller and no path that returns
  402, as the README claims; the MCP surface exposes only the four seller tools.

## Found and fixed

**`POST /v1/jobs` dropped the connection instead of answering, whenever the Nano node could not
be read.** `service.py:86` was `available = self.node.balance_raw() - committed`. The balance
read is the only node call on a request path (it exists to refuse work the account could not
pay for), and a `NodeError` escaping it reached `ThreadingHTTPServer`, which answers an
unhandled exception by logging a traceback and closing the socket. Reproduced over a loopback
socket against `FakeNode(up=False)`: the buyer gets `http.client.RemoteDisconnected: Remote end
closed connection without response` — no status, no error code, and no way to tell whether the
job was stored. A buyer agent cannot retry safely on that.

The fix is a refusal, and only a refusal: the read is wrapped and a down node now answers
**503 `node_unavailable`** ("Nothing was stored. Retry once GET /v1/health reports the node
reachable"), matching how `consent.py` already treats an unreadable queue (503
`demand_unavailable`) and how `health()` and the settlement worker already treat an unreachable
node. Nothing about which jobs are accepted, which amount is paid or where it is sent changes.

Two laws, both failing before the change and passing after:
`ErrorPaths.test_post_refuses_with_a_status_when_the_node_is_down` (503, code, nothing stored)
and `NodeDownOverHttp.test_post_job_answers_503_rather_than_closing_the_socket` (a real socket;
before the fix it raises `RemoteDisconnected`).

**The README advertised 106 unit tests.** The suite now runs 108, so both copies of that line
were corrected. Suite after the change: **108 passed**, e2e **95 passed**.

## Could not verify

- No real Nano node was contacted: `NanoNode` is an interface here and the repository ships only
  `FakeNode`, so the idempotency rule `send` places on a real implementation ("a retried send
  with the same key must adopt the earlier block") is asserted against the fake, not against a
  node. That rule is where a double payment would come from, and it lives in whatever
  implementation an operator wires in.
- `RateLimiter._hits` keeps one list per client address for the process's lifetime. Not a money
  defect and not touched here.
