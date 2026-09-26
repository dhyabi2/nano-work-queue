"""The one seam between this service and a Nano node.

Everything that touches the network lives behind `NanoNode`. The tests use
`FakeNode`, so the suite never opens a socket.

`send` takes an `idempotency_key` (the job id). A node implementation MUST
make a retried send with the same key adopt the earlier block rather than
publish a second one: an ambiguous timeout must never be able to pay twice.
"""

from . import amounts


class NodeError(RuntimeError):
    """The node could not be reached, or refused the operation."""


class NanoNode:
    """Interface. A real implementation talks RPC to a node."""

    def balance_raw(self) -> int:
        raise NotImplementedError

    def find_send(self, idempotency_key: str):
        """The block hash of an existing send carrying this key, or None."""
        raise NotImplementedError

    def send(self, to_address: str, amount_raw: int, idempotency_key: str) -> str:
        """Publish a send and return its block hash."""
        raise NotImplementedError

    def is_confirmed(self, block_hash: str) -> bool:
        raise NotImplementedError

    def reachable(self) -> bool:
        raise NotImplementedError


class FakeNode(NanoNode):
    """A deterministic node for tests and the end-to-end check.

    Records every send so a test can assert exactly one happened. `confirms`
    False models a node that publishes but never confirms — the job must then
    stay `accepted` and never reach `settled`.
    """

    def __init__(self, balance_xno="1000", confirms=True, up=True, delay=0.0):
        self._balance_raw = amounts.parse_xno(balance_xno)
        self.confirms = confirms
        self.up = up
        self.delay = delay
        self.sends = []            # [{to, amount_raw, key, hash}]
        self._by_key = {}

    # -- interface ---------------------------------------------------------
    def balance_raw(self) -> int:
        self._guard()
        return self._balance_raw

    def find_send(self, idempotency_key: str):
        self._guard()
        record = self._by_key.get(idempotency_key)
        return record["hash"] if record else None

    def send(self, to_address: str, amount_raw: int, idempotency_key: str) -> str:
        self._guard()
        existing = self._by_key.get(idempotency_key)
        if existing:                      # a real node must behave this way too
            return existing["hash"]
        block_hash = self._hash_for(idempotency_key)
        record = {
            "to": to_address,
            "amount_raw": amount_raw,
            "key": idempotency_key,
            "hash": block_hash,
        }
        self.sends.append(record)
        self._by_key[idempotency_key] = record
        self._balance_raw -= amount_raw
        return block_hash

    def is_confirmed(self, block_hash: str) -> bool:
        self._guard()
        if self.delay:
            import time

            time.sleep(self.delay)
        return bool(self.confirms) and block_hash in {s["hash"] for s in self.sends}

    def reachable(self) -> bool:
        return bool(self.up)

    # -- helpers -----------------------------------------------------------
    def _guard(self):
        if not self.up:
            raise NodeError("node unreachable")

    @staticmethod
    def _hash_for(key: str) -> str:
        import hashlib

        return hashlib.sha256(key.encode()).hexdigest()[:64].upper()
