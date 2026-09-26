import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nano_work_queue.clock import FakeClock          # noqa: E402
from nano_work_queue.node import FakeNode            # noqa: E402
from nano_work_queue.service import Service          # noqa: E402

GOOD_ADDRESS = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
# The same address with its final character altered: well-formed, bad checksum.
BAD_ADDRESS = "nano_1111111111111111111111111111111111111111111111111111hifc8npq"

JOB_BODY = {
    "title": "Extract the pricing table from 3 named vendor pages as JSON",
    "spec": "Return one object per vendor with name and monthly price.",
    "deliverable": "json",
    "accept_criteria": "Three vendors, each with a numeric monthly price.",
    "price_xno": "0.05",
}


def build(balance_xno="1000", confirms=True, up=True, delay=0.0):
    clock = FakeClock()
    node = FakeNode(balance_xno=balance_xno, confirms=confirms, up=up, delay=delay)
    service = Service(node, clock=clock, base_url="https://queue.example")
    return service, node, clock


def post(service, **overrides):
    body = dict(JOB_BODY)
    body.update(overrides)
    return service.post_job(body)


def full_cycle(service, price_xno="0.05", payload=None):
    """post -> claim -> deliver -> accept. Returns (job_id, claim_token)."""
    job = post(service, price_xno=price_xno)
    claim = service.claim(job["id"], {"seller": "arion",
                                      "payout_address": GOOD_ADDRESS})
    service.deliver(
        job["id"],
        claim["claim_token"],
        {"payload": payload if payload is not None else [{"vendor": "a",
                                                          "monthly": 10}]},
    )
    service.accept(job["id"])
    return job["id"], claim["claim_token"]
