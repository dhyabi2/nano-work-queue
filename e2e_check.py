#!/usr/bin/env python3
"""End-to-end check: start the real HTTP service and walk the acceptance tests.

This is not the unit suite. It boots the service on a loopback port, speaks
to it over HTTP exactly as a seller would, and prints each acceptance test
with the value it observed. The Nano node is the in-process fake, so nothing
leaves the machine.

    python3 e2e_check.py      -> prints each check, exits 0 only if all pass
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nano_work_queue.clock import FakeClock            # noqa: E402
from nano_work_queue.http_app import RateLimiter, make_handler   # noqa: E402
from nano_work_queue.mcp_server import FIRST_SENTENCE, tool_list  # noqa: E402
from nano_work_queue.node import FakeNode              # noqa: E402
from nano_work_queue.service import Service            # noqa: E402
from nano_work_queue.settlement import SettlementWorker  # noqa: E402

GOOD = "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
BAD = "nano_1111111111111111111111111111111111111111111111111111hifc8npq"
BUYER = "e2e-buyer-token"

FAILURES = []
CHECKS = 0


def check(label, got, want):
    global CHECKS
    CHECKS += 1
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}\n         got={got!r}")
    if not ok:
        print(f"         want={want!r}")
        FAILURES.append(label)


class Client:
    def __init__(self, base):
        self.base = base

    def __call__(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=15) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw[:1] in "{[" else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            return e.code, (json.loads(raw) if raw[:1] == "{" else raw)


def boot(balance="1000", confirms=True):
    clock = FakeClock()
    node = FakeNode(balance_xno=balance, confirms=confirms)
    port_holder = {}
    service = Service(node, clock=clock, base_url="http://127.0.0.1")
    handler = make_handler(service, buyer_token_getter=lambda: BUYER,
                           rate_limiter=RateLimiter(clock, limit=1000))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port_holder["port"] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    service.base_url = f"http://127.0.0.1:{port_holder['port']}"
    return service, node, clock, httpd, Client(service.base_url)


JOB = {
    "title": "Extract the pricing table from 3 named vendor pages as JSON",
    "spec": "One object per vendor: name and monthly price.",
    "deliverable": "json",
    "accept_criteria": "Three vendors, each with a numeric monthly price.",
    "price_xno": "0.05",
}
BUYER_H = {"X-Buyer-Token": BUYER}


def main():
    service, node, clock, httpd, http = boot()
    try:
        print("\n== health ==")
        s, h = http("GET", "/v1/health")
        check("health is 200 and the node is reachable", (s, h["node"]),
              (200, "reachable"))

        print("\n== 1. claim rejects a bad-checksum address (eddie_researcher) ==")
        _s, job = http("POST", "/v1/jobs", JOB, BUYER_H)
        jid = job["id"]
        s, body = http("POST", f"/v1/jobs/{jid}/claim",
                       {"seller": "eddie", "payout_address": BAD})
        check("status is 400 invalid_address", (s, body["error"]),
              (400, "invalid_address"))
        _s, fresh = http("GET", f"/v1/jobs/{jid}")
        check("the job is still open", fresh["state"], "open")
        check("no payout address was stored",
              service.store.get(jid).payout_address, None)

        print("\n== 2. claim accepts the known-good address ==")
        s, claim = http("POST", f"/v1/jobs/{jid}/claim",
                        {"seller": "arion", "payout_address": GOOD})
        check("status is 201 and state is claimed", (s, claim["state"]),
              (201, "claimed"))
        check("the echoed address matches byte for byte",
              claim["payout_address"], GOOD)
        check("a claim token was issued",
              claim["claim_token"].startswith("clm_"), True)
        token = claim["claim_token"]

        print("\n== 3. the seller never pays ==")
        seller_bodies = []
        for call in (("GET", "/v1/jobs", None, None),
                     ("GET", f"/v1/jobs/{jid}", None, None),
                     ("GET", f"/v1/receipts/{jid}", None, None)):
            st, bd = http(*call)
            seller_bodies.append((st, bd))
        blob = repr(seller_bodies).lower() + repr(claim).lower()
        check("no 402 on any seller call",
              [st for st, _ in seller_bodies if st == 402], [])
        check("no payment words anywhere on the seller path",
              [w for w in ("payment_required", "pay to", "deposit", "escrow",
                           "x-402") if w in blob], [])

        print("\n== 4. happy path produces a receipt with a block hash ==")
        s, delivered = http("POST", f"/v1/jobs/{jid}/deliver",
                            {"payload": [{"vendor": "acme", "monthly": 49},
                                         {"vendor": "globex", "monthly": 79},
                                         {"vendor": "initech", "monthly": 19}]},
                            {"Authorization": f"Bearer {token}"})
        check("deliver is 200", (s, delivered["state"]), (200, "delivered"))
        s, accepted = http("POST", f"/v1/jobs/{jid}/accept", {}, BUYER_H)
        check("accept returns pending settlement",
              (s, accepted["state"], accepted["settlement"]),
              (200, "accepted", "pending"))
        SettlementWorker(service).run_once()
        s, receipt = http("GET", f"/v1/receipts/{jid}")
        check("receipt says settled", receipt["state"], "settled")
        check("block hash is 64 hex", len(receipt["block_hash"]), 64)
        check("receipt is confirmed", receipt["confirmed"], True)
        check("receipt carries exactly the price", receipt["price_xno"],
              "0.050000")
        check("the node recorded exactly one send", len(node.sends), 1)
        check("and it paid the claimed address", node.sends[0]["to"], GOOD)
        print(f"         receipt_url = {service.base_url}/v1/receipts/{jid}")

        print("\n== 5. accept does not block on the node ==")
        import time

        s2, n2, c2, h2, http2 = boot()
        try:
            n2.delay = 5.0
            _s, j2 = http2("POST", "/v1/jobs", JOB, BUYER_H)
            _s, cl2 = http2("POST", f"/v1/jobs/{j2['id']}/claim",
                            {"seller": "arion", "payout_address": GOOD})
            http2("POST", f"/v1/jobs/{j2['id']}/deliver",
                  {"payload": [{"v": 1}]},
                  {"Authorization": f"Bearer {cl2['claim_token']}"})
            t0 = time.monotonic()
            st, acc = http2("POST", f"/v1/jobs/{j2['id']}/accept", {}, BUYER_H)
            elapsed = time.monotonic() - t0
            check("accept returned in under 500ms", elapsed < 0.5, True)
            print(f"         elapsed = {elapsed*1000:.1f}ms")
        finally:
            h2.shutdown(); h2.server_close()

        print("\n== 6. settlement is idempotent ==")
        s3, n3, c3, h3, http3 = boot()
        try:
            _s, j3 = http3("POST", "/v1/jobs", JOB, BUYER_H)
            _s, cl3 = http3("POST", f"/v1/jobs/{j3['id']}/claim",
                            {"seller": "arion", "payout_address": GOOD})
            http3("POST", f"/v1/jobs/{j3['id']}/deliver",
                  {"payload": [{"v": 1}]},
                  {"Authorization": f"Bearer {cl3['claim_token']}"})
            http3("POST", f"/v1/jobs/{j3['id']}/accept", {}, BUYER_H)
            w = SettlementWorker(s3)
            w.run_once(); w.run_once(); w.settle(s3.store.get(j3["id"]))
            check("exactly one send after three settlement passes",
                  len(n3.sends), 1)
            check("one block hash on the job",
                  len(s3.store.get(j3["id"]).block_hash), 64)
        finally:
            h3.shutdown(); h3.server_close()

        print("\n== 7. never settled without a block hash ==")
        s4, n4, c4, h4, http4 = boot(confirms=False)
        try:
            _s, j4 = http4("POST", "/v1/jobs", JOB, BUYER_H)
            _s, cl4 = http4("POST", f"/v1/jobs/{j4['id']}/claim",
                            {"seller": "arion", "payout_address": GOOD})
            http4("POST", f"/v1/jobs/{j4['id']}/deliver",
                  {"payload": [{"v": 1}]},
                  {"Authorization": f"Bearer {cl4['claim_token']}"})
            http4("POST", f"/v1/jobs/{j4['id']}/accept", {}, BUYER_H)
            SettlementWorker(s4, confirm_timeout_s=0).run_once()
            _s, jj = http4("GET", f"/v1/jobs/{j4['id']}")
            check("job stays accepted", jj["state"], "accepted")
            _s, hh = http4("GET", "/v1/health")
            check("health reports one settlement backlog",
                  hh["settlement_backlog"], 1)
            _s, rr = http4("GET", f"/v1/receipts/{j4['id']}")
            check("receipt is not confirmed", rr["confirmed"], False)
            from nano_work_queue.store import IllegalTransition
            try:
                s4.store.mark_settled(s4.store.get(j4["id"]), None)
                refused = False
            except IllegalTransition:
                refused = True
            check("the store refuses settled with a null block hash",
                  refused, True)
        finally:
            h4.shutdown(); h4.server_close()

        print("\n== 8. claim TTL returns the job to open ==")
        _s, j5 = http("POST", "/v1/jobs", dict(JOB, claim_ttl_s=60), BUYER_H)
        _s, cl5 = http("POST", f"/v1/jobs/{j5['id']}/claim",
                       {"seller": "arion", "payout_address": GOOD})
        clock.advance(61)
        service.store.reap_expired_claims()
        _s, j5b = http("GET", f"/v1/jobs/{j5['id']}")
        check("state is open again", j5b["state"], "open")
        st, body = http("POST", f"/v1/jobs/{j5['id']}/deliver",
                        {"payload": [{"v": 1}]},
                        {"Authorization": f"Bearer {cl5['claim_token']}"})
        check("the old token now fails 409 claim_expired",
              (st, body["error"]), (409, "claim_expired"))

        print("\n== 9. a rejected job reopens and the reason is public ==")
        _s, j6 = http("POST", "/v1/jobs", JOB, BUYER_H)
        _s, cl6 = http("POST", f"/v1/jobs/{j6['id']}/claim",
                       {"seller": "arion", "payout_address": GOOD})
        http("POST", f"/v1/jobs/{j6['id']}/deliver", {"payload": [{"v": 1}]},
             {"Authorization": f"Bearer {cl6['claim_token']}"})
        http("POST", f"/v1/jobs/{j6['id']}/reject",
             {"reason": "Only two vendors, and no monthly price."}, BUYER_H)
        _s, j6b = http("GET", f"/v1/jobs/{j6['id']}")
        check("job is open again", j6b["state"], "open")
        _s, r6 = http("GET", f"/v1/receipts/{j6['id']}")
        check("the reason is on the public receipt", r6["rejected_reason"],
              "Only two vendors, and no monthly price.")
        st, _b = http("POST", f"/v1/jobs/{j6['id']}/reject", {}, BUYER_H)
        check("rejecting with no reason is refused", st, 409)

        print("\n== 10. totals are the demand proof ==")
        s7, n7, c7, h7, http7 = boot()
        try:
            for price in ("0.05", "0.10"):
                _s, j = http7("POST", "/v1/jobs", dict(JOB, price_xno=price),
                              BUYER_H)
                _s, c = http7("POST", f"/v1/jobs/{j['id']}/claim",
                              {"seller": "arion", "payout_address": GOOD})
                http7("POST", f"/v1/jobs/{j['id']}/deliver",
                      {"payload": [{"v": 1}]},
                      {"Authorization": f"Bearer {c['claim_token']}"})
                http7("POST", f"/v1/jobs/{j['id']}/accept", {}, BUYER_H)
            SettlementWorker(s7).run_all()
            _s, listing = http7("GET", "/v1/jobs?state=settled")
            check("two settled", listing["totals"]["settled"], 2)
            check("paid total is the exact decimal string",
                  listing["totals"]["paid_xno_total"], "0.150000")
        finally:
            h7.shutdown(); h7.server_close()

        print("\n== 11. the claim token is never leaked ==")
        log = "\n".join(service.log_lines)
        check("the full token is absent from every log line",
              token in log, False)
        check("only the 8-character stub is logged", token[:8] in log, True)
        _s, jd = http("GET", f"/v1/jobs/{jid}")
        _s, rc = http("GET", f"/v1/receipts/{jid}")
        check("and absent from every public response",
              [k for k in (repr(jd), repr(rc)) if token in k], [])

        print("\n== 12. insufficient funds refuses to post ==")
        s8, n8, c8, h8, http8 = boot(balance="0.01")
        try:
            st, body = http8("POST", "/v1/jobs", dict(JOB, price_xno="0.05"),
                             BUYER_H)
            check("status is 409 insufficient_funds", (st, body["error"]),
                  (409, "insufficient_funds"))
            check("the available balance is named in the message",
                  "0.010000" in body["message"], True)
        finally:
            h8.shutdown(); h8.server_close()

        print("\n== 13. price is a decimal string end to end ==")
        s9, n9, c9, h9, http9 = boot()
        try:
            _s, j9 = http9("POST", "/v1/jobs", dict(JOB, price_xno="0.000001"),
                           BUYER_H)
            check("posted price is the exact string", j9["price_xno"],
                  "0.000001")
            check("and it is a string, not a number",
                  isinstance(j9["price_xno"], str), True)
            _s, c9b = http9("POST", f"/v1/jobs/{j9['id']}/claim",
                            {"seller": "arion", "payout_address": GOOD})
            check("the claim echoes it unchanged", c9b["price_xno"], "0.000001")
            http9("POST", f"/v1/jobs/{j9['id']}/deliver",
                  {"payload": [{"v": 1}]},
                  {"Authorization": f"Bearer {c9b['claim_token']}"})
            http9("POST", f"/v1/jobs/{j9['id']}/accept", {}, BUYER_H)
            SettlementWorker(s9).run_once()
            _s, r9 = http9("GET", f"/v1/receipts/{j9['id']}")
            check("the receipt carries it unchanged", r9["price_xno"],
                  "0.000001")

            for price in ("0.1", "0.2"):
                _s, j = http9("POST", "/v1/jobs", dict(JOB, price_xno=price),
                              BUYER_H)
                _s, c = http9("POST", f"/v1/jobs/{j['id']}/claim",
                              {"seller": "arion", "payout_address": GOOD})
                http9("POST", f"/v1/jobs/{j['id']}/deliver",
                      {"payload": [{"v": 1}]},
                      {"Authorization": f"Bearer {c['claim_token']}"})
                http9("POST", f"/v1/jobs/{j['id']}/accept", {}, BUYER_H)
            SettlementWorker(s9).run_all()
            _s, listing = http9("GET", "/v1/jobs?state=settled")
            check("0.000001 + 0.1 + 0.2 has no float drift",
                  listing["totals"]["paid_xno_total"], "0.300001")
        finally:
            h9.shutdown(); h9.server_close()

        print("\n== MCP surface ==")
        payload = tool_list(service)
        check("server name", payload["server"], "nano-work-queue")
        check("exactly the four tools",
              [t["name"] for t in payload["tools"]],
              ["list_jobs", "get_job", "claim_job", "deliver_job"])
        check("every description opens with the operator sentence",
              all(t["description"].startswith(FIRST_SENTENCE)
                  for t in payload["tools"]), True)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} CHECKS FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"ALL {CHECKS} END-TO-END CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
