"""The service: one method per endpoint, no HTTP in sight.

The HTTP layer, the MCP tools and the end-to-end check all drive these
methods, so a rule proved here holds on every surface.

Deliver-first is structural, not a policy note: there is no method that
takes money from a seller, and no method that returns 402.
"""

from . import address, amounts, errors, store
from .clock import Clock
from .store import (
    ACCEPTED, CLAIMED, DELIVERED, EXPIRED, OPEN, REJECTED, SETTLED, Store,
)

MAX_BODY_BYTES = 64 * 1024
CLAIM_TTL_MIN = 60
CLAIM_TTL_MAX = 604_800
CLAIM_TTL_DEFAULT = 86_400

SELLER_INSTRUCTIONS = (
    "Deliver with header: Authorization: Bearer <claim_token>. "
    "You pay nothing. We pay on accept."
)
RECEIPT_VERIFY = (
    "Fetch this block from any Nano node or explorer. It sends exactly "
    "price_xno to payout_address. We hold no key of the seller's."
)


class Service:
    def __init__(self, node, clock=None, base_url="http://localhost:8080",
                 explorer="https://nanolooker.com/block/", log=None):
        self.clock = clock or Clock()
        self.store = Store(self.clock)
        self.node = node
        self.base_url = base_url.rstrip("/")
        self.explorer = explorer
        self.log_lines = [] if log is None else log

    # -- logging -----------------------------------------------------------
    def _log(self, message):
        self.log_lines.append(message)

    # -- buyer side --------------------------------------------------------
    def post_job(self, body):
        title = _require_str(body, "title", 1, 120)
        spec = _require_str(body, "spec", 1, 8000)
        accept_criteria = _require_str(body, "accept_criteria", 1, 2000)
        deliverable = body.get("deliverable")
        if deliverable not in store.DELIVERABLES:
            raise errors.bad_request(
                f"deliverable must be one of {list(store.DELIVERABLES)}.",
                field="deliverable",
            )

        try:
            price_raw = amounts.parse_xno(body.get("price_xno"))
        except amounts.AmountError as exc:
            raise errors.price_out_of_range(str(exc) + ".") from None
        if not amounts.in_price_range(price_raw):
            raise errors.price_out_of_range(
                f"price_xno {amounts.format_xno(price_raw)} is outside "
                '"0.000001".."100".'
            )

        ttl = body.get("claim_ttl_s", CLAIM_TTL_DEFAULT)
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not (
            CLAIM_TTL_MIN <= ttl <= CLAIM_TTL_MAX
        ):
            raise errors.bad_request(
                f"claim_ttl_s must be an integer between {CLAIM_TTL_MIN} and "
                f"{CLAIM_TTL_MAX}.",
                field="claim_ttl_s",
            )

        # Refuse to post what the account cannot pay. Better a refusal now
        # than a default on a delivery somebody already did the work for.
        committed = amounts.add(
            *[
                j.price_raw
                for j in self.store.all_jobs()
                if j.state in (OPEN, CLAIMED, DELIVERED, ACCEPTED)
            ]
        )
        available = self.node.balance_raw() - committed
        if price_raw > available:
            raise errors.insufficient_funds(amounts.format_xno(max(available, 0)))

        job = store.Job(
            id=store.new_job_id(),
            title=title,
            spec=spec,
            deliverable=deliverable,
            accept_criteria=accept_criteria,
            price_raw=price_raw,
            state=OPEN,
            claim_ttl_s=ttl,
            created_at=self.clock.now_iso(),
        )
        self.store.add(job)
        self._log(f"posted {job.id} at {job.price_xno} XNO")
        return self.job_detail(job, include_spec=True)

    def accept(self, job_id):
        job = self._job_or_404(job_id)
        self._need(job, DELIVERED)
        self.store.transition(job, ACCEPTED)
        self._log(f"accepted {job.id}; settlement queued")
        # Deliberately does not touch the node: accept must not block on it.
        return {
            "job_id": job.id,
            "state": job.state,
            "settlement": "pending",
            "receipt_url": self._receipt_url(job.id),
        }

    def reject(self, job_id, reason):
        job = self._job_or_404(job_id)
        self._need(job, DELIVERED)
        if not isinstance(reason, str) or not (1 <= len(reason.strip()) <= 1000):
            raise errors.reason_required()
        self.store.transition(job, REJECTED)
        job.rejected_reason = reason.strip()
        # rejected -> open is automatic and immediate, same job id.
        self.store.transition(job, OPEN)
        job.claim_token_hash = None
        job.claim_expires_at = None
        job.claimed_at = None
        job.delivered_at = None
        job.payload = None
        self._log(f"rejected {job.id}; reopened")
        return {
            "job_id": job.id,
            "state": job.state,
            "rejected_reason": job.rejected_reason,
            "receipt_url": self._receipt_url(job.id),
        }

    def close(self, job_id):
        job = self._job_or_404(job_id)
        self._need(job, OPEN)
        self.store.transition(job, EXPIRED)
        return {"job_id": job.id, "state": job.state}

    # -- seller side -------------------------------------------------------
    def claim(self, job_id, body):
        job = self._job_or_404(job_id)
        self.store.reap_expired_claims()
        self._need(job, OPEN)

        seller = _require_str(body, "seller", 1, 80)
        note = body.get("note")
        if note is not None and (not isinstance(note, str) or len(note) > 500):
            raise errors.bad_request("note must be a string of at most 500 "
                                     "characters.", field="note")

        payout = body.get("payout_address")
        normalised = None
        if payout is not None:
            # Validated BEFORE the claim is accepted. An invalid address is
            # never stored and never paid to.
            try:
                normalised = address.normalise(payout)
            except address.InvalidAddress as exc:
                raise errors.invalid_address(str(exc)) from None

        token = store.new_claim_token()
        self.store.transition(job, CLAIMED)
        job.claim_token_hash = store.hash_token(token)
        job.claimed_at = self.clock.now_iso()
        job.claim_expires_at = self.clock.now() + job.claim_ttl_s
        job.seller = seller
        job.payout_address = normalised
        job.notes = note
        # Only the stub ever reaches a log line.
        self._log(f"claimed {job.id} by {seller} token={store.token_stub(token)}")

        out = {
            "job_id": job.id,
            "claim_token": token,
            "state": job.state,
            "expires_at": self.clock.iso(job.claim_expires_at),
            "price_xno": job.price_xno,
            "deliver_to": f"{self.base_url}/v1/jobs/{job.id}/deliver",
            "instructions": SELLER_INSTRUCTIONS,
        }
        if normalised is not None:
            out["payout_address"] = normalised
        return out

    def release(self, job_id, token):
        job = self._job_or_404(job_id)
        self._authorise_claim(job, token)
        self.store.transition(job, OPEN)
        job.claim_token_hash = None
        job.claim_expires_at = None
        job.claimed_at = None
        job.seller = None
        job.payout_address = None
        return {"job_id": job.id, "state": job.state}

    def deliver(self, job_id, token, body):
        job = self._job_or_404(job_id)
        self.store.reap_expired_claims()
        self._authorise_claim(job, token)
        self._need(job, CLAIMED)

        payout = body.get("payout_address")
        if payout is not None:
            try:
                normalised = address.normalise(payout)
            except address.InvalidAddress as exc:
                raise errors.invalid_address(str(exc)) from None
            if job.payout_address and job.payout_address != normalised:
                raise errors.payout_address_conflict()
            job.payout_address = normalised
        if not job.payout_address:
            raise errors.payout_address_required()

        payload = body.get("payload")
        _check_payload(job.deliverable, payload)
        notes = body.get("notes")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 2000):
            raise errors.bad_request("notes must be a string of at most 2000 "
                                     "characters.", field="notes")

        self.store.transition(job, DELIVERED)
        job.payload = payload
        job.delivered_at = self.clock.now_iso()
        job.notes = notes or job.notes
        self._log(f"delivered {job.id}")
        return {
            "job_id": job.id,
            "state": job.state,
            "delivered_at": job.delivered_at,
            "review_deadline": self.clock.iso(self.clock.now() + job.claim_ttl_s),
            "receipt_url": self._receipt_url(job.id),
        }

    # -- public reads ------------------------------------------------------
    def list_jobs(self, state=OPEN, limit=20, cursor=None):
        if state is not None and state not in store.STATES:
            raise errors.bad_request(f"state must be one of {list(store.STATES)}.",
                                     field="state")
        if not isinstance(limit, int) or isinstance(limit, bool) or not (
            1 <= limit <= 100
        ):
            raise errors.bad_request("limit must be an integer 1..100.",
                                     field="limit")
        self.store.reap_expired_claims()
        jobs = sorted(
            (j for j in self.store.all_jobs() if state is None or j.state == state),
            key=lambda j: (j.created_at, j.id),
        )
        start = 0
        if cursor:
            ids = [j.id for j in jobs]
            start = ids.index(cursor) + 1 if cursor in ids else 0
        window = jobs[start : start + limit]
        nxt = window[-1].id if len(jobs) > start + limit and window else None
        return {
            "jobs": [self.job_summary(j) for j in window],
            "next_cursor": nxt,
            "totals": self.store.totals(),
        }

    def job_summary(self, job):
        return {
            "id": job.id,
            "title": job.title,
            "deliverable": job.deliverable,
            "price_xno": job.price_xno,
            "state": job.state,
            "claim_ttl_s": job.claim_ttl_s,
            "created_at": job.created_at,
            "url": f"{self.base_url}/v1/jobs/{job.id}",
        }

    def job_detail(self, job, include_spec=True):
        # payout_address is never exposed here, nor is the claim token.
        out = self.job_summary(job)
        if include_spec:
            out["spec"] = job.spec
            out["accept_criteria"] = job.accept_criteria
        out.update(
            {
                "claimed_at": job.claimed_at,
                "delivered_at": job.delivered_at,
                "settled_at": job.settled_at,
                "seller": job.seller,
                "receipt_url": self._receipt_url(job.id)
                if job.state in (DELIVERED, ACCEPTED, SETTLED, REJECTED, OPEN)
                and (job.delivered_at or job.rejected_reason)
                else None,
                "block_hash": job.block_hash,
            }
        )
        return out

    def get_job(self, job_id):
        return self.job_detail(self._job_or_404(job_id))

    def receipt(self, job_id):
        job = self._job_or_404(job_id)
        return {
            "job_id": job.id,
            "title": job.title,
            "seller": job.seller,
            "payout_address": job.payout_address,
            "price_xno": job.price_xno,
            "state": job.state,
            "accept_criteria": job.accept_criteria,
            "claimed_at": job.claimed_at,
            "delivered_at": job.delivered_at,
            "settled_at": job.settled_at,
            "block_hash": job.block_hash,
            "block_explorer_url": (self.explorer + job.block_hash)
            if job.block_hash
            else None,
            "confirmed": job.state == SETTLED and bool(job.block_hash),
            "rejected_reason": job.rejected_reason,
            "verify": RECEIPT_VERIFY,
        }

    def receipt_text(self, job_id):
        r = self.receipt(job_id)
        lines = [f"{k}: {'' if v is None else v}" for k, v in r.items()]
        return "\n".join(lines) + "\n"

    def health(self):
        try:
            funded = amounts.format_xno(self.node.balance_raw())
            node_state = "reachable" if self.node.reachable() else "unreachable"
        except Exception:
            funded = None
            node_state = "unreachable"
        return {
            "ok": node_state == "reachable",
            "funded_xno": funded,
            "open_jobs": len(self.store.by_state(OPEN)),
            "settlement_backlog": len(
                [j for j in self.store.by_state(ACCEPTED) if not j.block_hash]
            ),
            "node": node_state,
        }

    # -- helpers -----------------------------------------------------------
    def _receipt_url(self, job_id):
        return f"{self.base_url}/v1/receipts/{job_id}"

    def _job_or_404(self, job_id):
        job = self.store.get(job_id)
        if job is None:
            raise errors.not_found()
        return job

    def _need(self, job, wanted):
        if job.state != wanted:
            raise errors.invalid_state(job.state, wanted)

    def _authorise_claim(self, job, token):
        import hmac

        if not isinstance(token, str) or not token.startswith("clm_"):
            raise errors.unauthorized()
        if job.claim_token_hash is None:
            # The claim was reaped, or this job was never claimed.
            raise errors.claim_expired() if job.state == OPEN else errors.unauthorized()
        if not hmac.compare_digest(store.hash_token(token), job.claim_token_hash):
            # A well-formed token that belongs to another job.
            for other in self.store.all_jobs():
                if other.claim_token_hash and hmac.compare_digest(
                    store.hash_token(token), other.claim_token_hash
                ):
                    raise errors.forbidden()
            raise errors.unauthorized()


def _require_str(body, field, lo, hi):
    value = body.get(field)
    if not isinstance(value, str) or not (lo <= len(value.strip()) <= hi):
        raise errors.bad_request(
            f"{field} must be a string of {lo}..{hi} characters.", field=field
        )
    return value.strip()


def _check_payload(deliverable, payload):
    if payload is None:
        raise errors.invalid_payload("payload is absent.")
    if deliverable == "text":
        if not isinstance(payload, str) or not payload.strip():
            raise errors.invalid_payload("deliverable is 'text' so payload must "
                                         "be a non-empty string.")
    elif deliverable == "json":
        if not isinstance(payload, (dict, list)):
            raise errors.invalid_payload("deliverable is 'json' so payload must "
                                         "be a JSON object or array.")
    elif deliverable == "url":
        if not isinstance(payload, str) or not payload.startswith("https://"):
            raise errors.invalid_payload("deliverable is 'url' so payload must "
                                         "be an https URL.")
