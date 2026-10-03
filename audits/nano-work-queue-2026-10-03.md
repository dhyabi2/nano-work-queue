# nano-work-queue - audit 2026-10-03

Lens: can an outside agent claim a job here with no account and get paid in XNO. Last audited
2026-09-27, so this is the first pass since #1 and the README test-count correction.

**Nothing was found to fix.** That is the finding; the detail below is what was actually
exercised.

## Checked

- `python3 -m unittest discover -s tests -t tests`: **104 tests, OK** - which is exactly what
  `README.md:26` and `:247` advertise, so the count the last commit corrected is still true.
- `python3 e2e_check.py`: **ALL 95 END-TO-END CHECKS PASSED.**
- `python3 -m py_compile nano_work_queue/*.py tests/*.py e2e_check.py`: clean.
- **Amount handling.** No float touches an amount anywhere in `nano_work_queue/`. The only
  `float()` calls in the package are in `clock.py` (`self._t = float(start)`), a test clock.
  Raw is an integer throughout.
- **`settlement.py`, the path that pays the seller**, read line by line. Two properties hold
  as its docstring claims: `settle()` returns early when the job is already `SETTLED`, so two
  workers racing one job cannot both send; and `node.find_send(job.id)` is consulted and its
  block **adopted** before `node.send` is ever called, so a retry after an ambiguous result
  adopts rather than repeats. A job only reaches `settled` with a confirmed block hash - an
  unconfirmed or failed settlement increments `settle_attempts`, leaves the job `accepted`,
  and surfaces in `/v1/health` as backlog. `_await_confirmation` is bounded and does not
  sleep when the block is already confirmed.
- **The two startup refusals the README promises, against the code.** Both are real, in
  `http_app.py:main`:
  - `if not os.environ.get("DEMAND_QUEUE_BUYER_TOKEN"): raise SystemExit(...)` (`:325-326`).
  - `if not args.fake_node: raise SystemExit("No Nano node configured. Set NANO_NODE_URL and
    supply a NanoNode implementation, or pass --fake-node for local development.")` (`:327-331`).

  The second is worth stating plainly because it reads the other way at a glance: the
  unconditional `Service(FakeNode(), ...)` on the following line is **unreachable** without
  `--fake-node`, so the service cannot be started expecting a real rail and quietly "settle"
  jobs against a fake node. I misread it as unguarded on first pass and checked; it is
  guarded.
- **The honesty of the node seam, which is where a double payment would come from.**
  `NanoNode` is an interface and `FakeNode` is the only implementation in the tree, so there
  is no real node client here - and the README says exactly that, twice: "refuses to start
  against a real rail until a `NanoNode` implementation is wired in", and "`send` **must**
  make a retried call with the same `idempotency_key` adopt the earlier block instead of
  publishing a second one." That requirement is the load-bearing one, because Nano has no
  memo field, so implementing `find_send(job_id)` against a real node is not free work
  (`dhyabi2/nano-invoice` exists for that problem). The claim and the code agree, and the
  obligation is stated rather than hidden.
- State transitions are enforced in `store.py` rather than the HTTP layer, so the rule holds
  on the MCP and A2A surfaces too, which are thin wrappers over the same service.

## Found

Nothing. No defect on the XNO path, no float on an amount, and the two README claims that
matter most - the startup refusals and the idempotency obligation on a live node client -
are both borne out by the code.

## Fixed

Nothing, deliberately.

## Could not verify

- **Anything about a real rail.** Every test and `e2e_check.py` run against `FakeNode`, which
  is why the suite opens no socket and needs no node. The repository is structurally unable
  to pay anyone until an operator supplies a `NanoNode`, so the correctness of the settlement
  worker is verified only against a node that behaves as `FakeNode` does - in particular one
  whose `send` is genuinely idempotent by key. An operator who wires in a node that is not
  would have a double-payment path, and nothing here could catch it.
- No XNO moved and no node was reached.
