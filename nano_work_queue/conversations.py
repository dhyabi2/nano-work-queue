"""Where a decline reason goes.

The reason an operator types when they say no is the most valuable text this
whole system collects: it is an objection in an operator's own words, which is
the one input the conversations loop has never had. A decline that is recorded
only as `state: "declined"` throws that away, so every decline is written here
in the shape the exporter reads, under `walls`.

Clustering is deliberately crude and deliberately visible. A reason that
matches no seeded cluster is written to `unclustered` rather than forced into
the nearest one: a wall we have not seen before is exactly the thing worth
noticing, and a bad match would hide it inside a cluster that already has an
answer.
"""

import json
import os
import threading

WALLS = "walls"
UNCLUSTERED = "unclustered"

# Seeded from the swarm's own refusal-pattern catalogue. Each cluster is a set
# of substrings; a reason belongs to the first cluster any of whose phrases it
# contains. Order matters only for a reason that would match two.
CLUSTERS = (
    ("demand-first-operator-gate",
     ("who is paying", "someone is paying", "no customer", "no demand",
      "show me revenue", "prove demand", "nobody is paying", "paying")),
    ("counterparty-identity",
     ("named counterparty", "counterparty", "legal entity", "kyc",
      "who you are", "identify yourself", "anonymous")),
    ("trust-custody",
     ("custody", "private key", "our keys", "hold our funds", "control our funds")),
    ("human-must-approve",
     ("needs sign-off", "sign off", "escalate", "my manager", "legal team",
      "security review", "compliance")),
    ("regulatory",
     ("regulat", "licence", "license", "sanction", "aml", "tax")),
)


def cluster_for(text):
    """The cluster this reason belongs to, or None. Never a forced match."""
    lowered = (text or "").lower()
    for name, phrases in CLUSTERS:
        if any(phrase in lowered for phrase in phrases):
            return name
    return None


class ConversationStore:
    """The interface. `record_wall` is the only thing consent needs."""

    def record_wall(self, text, source, said_at, meta=None):
        raise NotImplementedError

    def walls(self):
        raise NotImplementedError


class MemoryConversationStore(ConversationStore):
    def __init__(self):
        self._rows = []
        self._lock = threading.RLock()

    def record_wall(self, text, source, said_at, meta=None):
        row = {
            "kind": WALLS,
            "cluster": cluster_for(text) or UNCLUSTERED,
            "text": text,
            "source": source,
            "said_at": said_at,
            "meta": dict(meta or {}),
        }
        with self._lock:
            self._rows.append(row)
        return row

    def walls(self):
        with self._lock:
            return [dict(r) for r in self._rows if r["kind"] == WALLS]


class FileConversationStore(MemoryConversationStore):
    """JSON Lines on disk, so the exporter on the box can pick them up.

    Appended and fsynced one row at a time: a decline is a thing somebody did
    once, and losing it to a buffer on a restart loses it for good.
    """

    def __init__(self, path):
        super().__init__()
        self.path = path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    def record_wall(self, text, source, said_at, meta=None):
        row = super().record_wall(text, source, said_at, meta)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return row
