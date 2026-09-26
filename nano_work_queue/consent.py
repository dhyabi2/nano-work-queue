"""One-click operator consent: a bounded, dated signature that carries its own
demand proof.

The load-bearing rule, and the reason this module exists at all: **the page
cannot be rendered, and a signature cannot be taken, while the queue has
settled zero jobs.** The operator's condition is not "convince me it is safe",
it is "show me someone is paying". A consent page with no payer on it proves in
one screen that there is nobody paying, which is worse than sending no message.

So `_demand()` is called before anything renders, it fetches live every time,
and a failure to fetch is a 503 rather than a page with the figures left out.
There is no cached copy and no fallback: both would let the page render without
the evidence, which is the one thing it must never do.
"""

import secrets
import threading

from . import amounts, conversations, errors

RECEIVE_ONLY = "receive_only"
RECEIVE_AND_SEND = "receive_and_send"
SCOPES = (RECEIVE_ONLY, RECEIVE_AND_SEND)

PENDING = "pending"
SIGNED = "signed"
DECLINED = "declined"
EXPIRED = "expired"
REVOKED = "revoked"

EXPIRY_DAYS = 90
EXPIRY_S = EXPIRY_DAYS * 86_400

AGENT_MAX = 80
SIGNER_MAX = 120
HINT_MAX = 200
REASON_MAX = 2000

NO_DEMAND_SENTENCE = "No jobs have been paid yet."


class DemandUnavailable(Exception):
    """The queue could not be read. Deliberately not recoverable here."""


def new_consent_id() -> str:
    return "csn_" + secrets.token_hex(8)


def demand_block(snapshot):
    """The three figures the `.json` body publishes, from a live snapshot."""
    return {
        "settled": snapshot["settled"],
        "paid_xno_total": snapshot["paid_xno_total"],
        "open": snapshot["open"],
    }


class Consent:
    __slots__ = ("id", "agent", "operator_hint", "scope", "max_send_xno",
                 "expires_at", "expires_epoch", "state", "signed_at",
                 "signed_by", "decline_reason", "created_at")

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))


class DemandSource:
    """The queue's own totals, read live.

    A thin wrapper rather than a direct call into `Service` so a test -- and an
    operator's outage -- can make the queue unreachable, which is the only way
    to prove the page fails closed instead of rendering without figures.
    """

    def __init__(self, service):
        self.service = service

    def snapshot(self):
        try:
            totals = self.service.store.totals()
            latest = self._latest_settled()
        except Exception as exc:                # noqa: BLE001 - any failure is unavailable
            raise DemandUnavailable(str(exc)) from None
        return {
            "settled": totals["settled"],
            "paid_xno_total": totals["paid_xno_total"],
            "open": totals["open"],
            "jobs_url": f"{self.service.base_url}/v1/jobs",
            "latest": latest,
        }

    def _latest_settled(self):
        from .store import SETTLED

        settled = [j for j in self.service.store.by_state(SETTLED)
                   if j.settled_at]
        if not settled:
            return None
        job = max(settled, key=lambda j: (j.settled_at, j.id))
        return {
            "job_id": job.id,
            "receipt_url": f"{self.service.base_url}/v1/receipts/{job.id}",
            "price_xno": job.price_xno,
            "block_hash": job.block_hash,
            "settled_at": job.settled_at,
        }


class ConsentService:
    def __init__(self, demand, clock, base_url="http://localhost:8080",
                 conversation_store=None):
        self.demand = demand
        self.clock = clock
        self.base_url = base_url.rstrip("/")
        self.conversations = (conversation_store
                              or conversations.MemoryConversationStore())
        self._consents = {}
        self._lock = threading.RLock()

    # -- create ------------------------------------------------------------
    def create(self, body):
        if "expires_at" in body:
            # Server-side only, always. A caller-set expiry is how a 90-day
            # grant quietly becomes a permanent one.
            raise errors.expiry_not_settable()

        agent = body.get("agent")
        if not isinstance(agent, str) or not (1 <= len(agent.strip()) <= AGENT_MAX):
            raise errors.bad_request(
                f"agent must be a string of 1..{AGENT_MAX} characters.",
                field="agent")
        agent = agent.strip()

        scope = body.get("scope", RECEIVE_ONLY)
        if scope not in SCOPES:
            raise errors.bad_request(f"scope must be one of {list(SCOPES)}.",
                                     field="scope")

        max_send = body.get("max_send_xno")
        if scope == RECEIVE_ONLY and max_send is not None:
            raise errors.scope_conflict()
        if scope == RECEIVE_AND_SEND:
            if max_send is None:
                raise errors.bad_request(
                    "receive_and_send needs a max_send_xno ceiling. An "
                    "unbounded send authority is not a bounded grant.",
                    field="max_send_xno")
            try:
                max_send = amounts.format_xno(amounts.parse_xno(max_send))
            except amounts.AmountError as exc:
                raise errors.bad_request(f"max_send_xno: {exc}.",
                                         field="max_send_xno") from None

        hint = body.get("operator_hint")
        if hint is not None and (not isinstance(hint, str)
                                 or len(hint) > HINT_MAX):
            raise errors.bad_request(
                f"operator_hint must be a string of at most {HINT_MAX} "
                "characters.", field="operator_hint")

        now = self.clock.now()
        consent = Consent(
            id=new_consent_id(),
            agent=agent,
            operator_hint=hint,
            scope=scope,
            max_send_xno=max_send if scope == RECEIVE_AND_SEND else None,
            created_at=self.clock.iso(now),
            expires_epoch=now + EXPIRY_S,
            expires_at=self.clock.iso(now + EXPIRY_S),
            state=PENDING,
            signed_at=None, signed_by=None, decline_reason=None,
        )
        with self._lock:
            self._consents[consent.id] = consent
        return self.as_json(consent)

    # -- read --------------------------------------------------------------
    def get(self, consent_id):
        with self._lock:
            consent = self._consents.get(consent_id)
        if consent is None:
            raise errors.not_found("consent")
        self._expire_if_due(consent)
        return consent

    def as_json(self, consent, demand=None):
        return {
            "consent_id": consent.id,
            "agent": consent.agent,
            "operator_hint": consent.operator_hint,
            "scope": consent.scope,
            "max_send_xno": consent.max_send_xno,
            "state": consent.state,
            "signed_at": consent.signed_at,
            "signed_by": consent.signed_by,
            "decline_reason": consent.decline_reason,
            "created_at": consent.created_at,
            "expires_at": consent.expires_at,
            "url": f"{self.base_url}/v1/consent/{consent.id}",
            "revoke_url": f"{self.base_url}/v1/consent/{consent.id}/revoke",
            # Whatever the caller already fetched, or a best-effort read.
            # Creating and revoking must work whether or not the queue has
            # paid anybody: the gate the spec puts on *rendering* and
            # *signing* is not a gate on issuing a consent or on letting an
            # operator withdraw one.
            "demand": demand if demand is not None else self._demand_safe(),
        }

    def view(self, consent_id):
        """Everything a render needs: the consent and live demand, or a 409/503.

        The demand fetch happens here, before any caller can produce output, so
        no rendering path exists that could skip it.
        """
        consent = self.get(consent_id)
        demand = self._demand()
        return consent, demand

    # -- sign / decline ----------------------------------------------------
    def sign(self, consent_id, body):
        consent = self.get(consent_id)
        # The demand check applies to the signature too, not only the page: a
        # form POSTed from a page rendered before the last job was closed must
        # not become a signature taken with no payer.
        snapshot = self._demand()
        if consent.state != PENDING:
            raise errors.invalid_consent_state(consent.state)

        decision = body.get("decision")
        if decision not in ("approve", "decline"):
            raise errors.bad_request(
                'decision must be "approve" or "decline".', field="decision")

        signed_by = body.get("signed_by")
        if not isinstance(signed_by, str) or not (
            1 <= len(signed_by.strip()) <= SIGNER_MAX
        ):
            raise errors.invalid_signer(SIGNER_MAX)
        signed_by = signed_by.strip()

        now_iso = self.clock.now_iso()
        if decision == "approve":
            with self._lock:
                consent.state = SIGNED
                consent.signed_at = now_iso
                consent.signed_by = signed_by
            return self.as_json(consent, demand=demand_block(snapshot))

        reason = body.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise errors.reason_required_for_decline()
        reason = reason.strip()[:REASON_MAX]
        with self._lock:
            consent.state = DECLINED
            consent.signed_by = signed_by
            consent.signed_at = now_iso
            consent.decline_reason = reason
        # A decline is a success. The reason is the payload.
        self.conversations.record_wall(
            reason, source=f"consent:{consent.id}", said_at=now_iso,
            meta={"agent": consent.agent, "scope": consent.scope,
                  "signed_by": signed_by})
        return self.as_json(consent, demand=demand_block(snapshot))

    def revoke(self, consent_id):
        """Idempotent, and needs no token beyond knowing the id.

        An operator who has to contact us to revoke has not been given a
        bounded grant; they have been given a negotiation.
        """
        consent = self.get(consent_id)
        with self._lock:
            consent.state = REVOKED
        return self.as_json(consent)

    # -- helpers -----------------------------------------------------------
    def _expire_if_due(self, consent):
        if consent.state == PENDING and self.clock.now() >= consent.expires_epoch:
            consent.state = EXPIRED

    def _demand(self):
        """Live demand, or the call fails. Never a page without figures."""
        try:
            snapshot = self.demand.snapshot()
        except DemandUnavailable:
            raise errors.demand_unavailable() from None
        if snapshot["settled"] <= 0:
            raise errors.no_demand_yet()
        return snapshot

    def _demand_safe(self):
        """The three figures, or None. Only for bodies that are not the page.

        `None` here is honest: it says we could not show demand, which is very
        different from showing a zero as though it were a figure. Nothing that
        renders a page or takes a signature may call this -- those go through
        `_demand()`, which fails.
        """
        try:
            snapshot = self.demand.snapshot()
        except DemandUnavailable:
            return None
        return demand_block(snapshot)
