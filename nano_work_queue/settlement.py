"""The settlement worker.

Separate from the request path on purpose: `accept` returns immediately and
this runs afterwards, so a slow or unreachable node can never hold a buyer's
accept open.

Two rules it will not break:
  * the job id is the idempotency key, and an existing send carrying that key
    is adopted rather than repeated, so a retry after an ambiguous result
    cannot pay twice;
  * a job only reaches `settled` with a confirmed block hash. A failure
    leaves it `accepted` and visible in /v1/health as backlog.
"""

from . import store
from .node import NodeError

CONFIRM_TIMEOUT_S = 60


class SettlementWorker:
    def __init__(self, service, confirm_timeout_s=CONFIRM_TIMEOUT_S):
        self.service = service
        self.confirm_timeout_s = confirm_timeout_s

    def pending(self):
        """Oldest-first accepted jobs with no block hash yet."""
        jobs = [
            j
            for j in self.service.store.by_state(store.ACCEPTED)
            if not j.block_hash
        ]
        return sorted(jobs, key=lambda j: (j.created_at or "", j.id))

    def run_once(self):
        """Settle the oldest pending job. Returns its id, or None if none."""
        jobs = self.pending()
        if not jobs:
            return None
        return self.settle(jobs[0])

    def run_all(self):
        settled = []
        while True:
            before = len(settled)
            job_id = self.run_once()
            if job_id is None:
                break
            settled.append(job_id)
            if len(settled) == before:  # pragma: no cover - defensive
                break
        return settled

    def settle(self, job):
        node = self.service.node
        service = self.service
        if job.state == store.SETTLED:
            # Already paid and recorded. Two workers racing for one job, or a
            # direct re-dispatch, must both land here rather than send again.
            return job.id
        try:
            # Adopt an earlier send for this job before publishing another.
            block_hash = node.find_send(job.id)
            if block_hash is None:
                block_hash = node.send(job.payout_address, job.price_raw, job.id)
            if not self._await_confirmation(block_hash):
                job.settle_attempts += 1
                service._log(
                    f"settlement pending {job.id}: block {block_hash} not "
                    f"confirmed; left accepted (attempt {job.settle_attempts})"
                )
                return None
            service.store.mark_settled(job, block_hash)
            service._log(f"settled {job.id} block={block_hash}")
            return job.id
        except (NodeError, OSError) as exc:
            job.settle_attempts += 1
            service._log(
                f"settlement failed {job.id}: {exc}; left accepted "
                f"(attempt {job.settle_attempts})"
            )
            return None

    def _await_confirmation(self, block_hash):
        """Poll for confirmation, bounded. No sleeping when already confirmed."""
        import time

        deadline = time.monotonic() + self.confirm_timeout_s
        while True:
            if self.service.node.is_confirmed(block_hash):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
