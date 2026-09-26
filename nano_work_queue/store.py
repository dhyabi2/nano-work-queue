"""Job storage and the state machine.

The transition table here is the only way a job's state changes. Two
invariants are enforced in this module rather than in the HTTP layer, so
they hold no matter who calls:

  * only a transition named in `TRANSITIONS` is possible at all;
  * `settled` requires a block hash. `mark_settled` refuses without one,
    so no code path can report a payment that has no block behind it.
"""

import hashlib
import os
import secrets
import threading

from . import amounts

OPEN = "open"
CLAIMED = "claimed"
DELIVERED = "delivered"
ACCEPTED = "accepted"
SETTLED = "settled"
REJECTED = "rejected"
EXPIRED = "expired"

STATES = (OPEN, CLAIMED, DELIVERED, ACCEPTED, SETTLED, REJECTED, EXPIRED)

# (from, to) pairs that are allowed. Anything else is 409 invalid_state.
TRANSITIONS = frozenset(
    {
        (OPEN, CLAIMED),
        (CLAIMED, DELIVERED),
        (CLAIMED, OPEN),        # TTL reaper, or an explicit release
        (DELIVERED, ACCEPTED),
        (DELIVERED, REJECTED),
        (REJECTED, OPEN),       # automatic, same job id
        (ACCEPTED, SETTLED),
        (OPEN, EXPIRED),
    }
)

DELIVERABLES = ("text", "json", "url")


class IllegalTransition(Exception):
    """A transition that is not in the table, or settling with no block."""


def new_job_id() -> str:
    return "job_" + secrets.token_hex(8)


def new_claim_token() -> str:
    return "clm_" + secrets.token_hex(16)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def token_stub(token: str) -> str:
    """The only part of a claim token that may ever reach a log."""
    return (token or "")[:8]


class Job:
    __slots__ = (
        "id", "title", "spec", "deliverable", "accept_criteria", "price_raw",
        "state", "claim_ttl_s", "created_at", "claimed_at", "delivered_at",
        "settled_at", "seller", "payout_address", "block_hash",
        "claim_token_hash", "claim_expires_at", "rejected_reason", "notes",
        "payload", "settle_attempts",
    )

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))
        self.settle_attempts = 0

    @property
    def price_xno(self) -> str:
        return amounts.format_xno(self.price_raw)


class Store:
    """In-process job store. A single lock keeps transitions serialised."""

    def __init__(self, clock):
        self._jobs = {}
        self._lock = threading.RLock()
        self._clock = clock

    # -- reads -------------------------------------------------------------
    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def all_jobs(self):
        with self._lock:
            return list(self._jobs.values())

    def by_state(self, state):
        return [j for j in self.all_jobs() if j.state == state]

    def totals(self):
        """The demand proof: how many jobs settled and how much was paid."""
        settled = self.by_state(SETTLED)
        return {
            "open": len(self.by_state(OPEN)),
            "settled": len(settled),
            "paid_xno_total": amounts.format_xno(
                amounts.add(*[j.price_raw for j in settled]) if settled else 0
            ),
        }

    # -- writes ------------------------------------------------------------
    def add(self, job):
        with self._lock:
            self._jobs[job.id] = job
            return job

    def transition(self, job, to_state):
        """The single gate. Raises IllegalTransition for anything else."""
        with self._lock:
            if (job.state, to_state) not in TRANSITIONS:
                raise IllegalTransition(f"{job.state} -> {to_state}")
            if to_state == SETTLED and not job.block_hash:
                raise IllegalTransition(
                    "refusing to mark a job settled with no block hash"
                )
            job.state = to_state
            return job

    def mark_settled(self, job, block_hash):
        """Settle a job. Refuses without a 64-hex block hash, by design."""
        with self._lock:
            if not _is_block_hash(block_hash):
                raise IllegalTransition(
                    "a settled job needs a 64-character hex block hash; "
                    f"got {block_hash!r}"
                )
            job.block_hash = block_hash
            job.settled_at = self._clock.now_iso()
            return self.transition(job, SETTLED)

    def reap_expired_claims(self):
        """Return claims whose TTL elapsed to `open`. Returns the ids moved."""
        moved = []
        with self._lock:
            now = self._clock.now()
            for job in self._jobs.values():
                if job.state != CLAIMED or job.claim_expires_at is None:
                    continue
                if now >= job.claim_expires_at:
                    self.transition(job, OPEN)
                    job.claim_token_hash = None
                    job.claim_expires_at = None
                    job.claimed_at = None
                    job.seller = None
                    job.payout_address = None
                    moved.append(job.id)
        return moved


def _is_block_hash(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(c in "0123456789ABCDEF" for c in value.upper())
    )


is_block_hash = _is_block_hash


def env_buyer_token():
    """The buyer token, from the environment only. Never from a file."""
    return os.environ.get("DEMAND_QUEUE_BUYER_TOKEN", "")
