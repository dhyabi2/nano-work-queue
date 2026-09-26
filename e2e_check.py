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
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nano_work_queue.clock import FakeClock            # noqa: E402
from nano_work_queue.consent import (                  # noqa: E402
    ConsentService, DemandSource, DemandUnavailable,
)
from nano_work_queue.conversations import MemoryConversationStore  # noqa: E402
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


def boot(balance="1000", confirms=True, with_consent=False):
    clock = FakeClock()
    node = FakeNode(balance_xno=balance, confirms=confirms)
    port_holder = {}
    service = Service(node, clock=clock, base_url="http://127.0.0.1")
    consents = walls = None
    if with_consent:
        walls = MemoryConversationStore()
        consents = ConsentService(DemandSource(service), clock,
                                  base_url="http://127.0.0.1",
                                  conversation_store=walls)
    handler = make_handler(service, buyer_token_getter=lambda: BUYER,
                           rate_limiter=RateLimiter(clock, limit=1000),
                           consents=consents)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port_holder["port"] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    service.base_url = f"http://127.0.0.1:{port_holder['port']}"
    if consents is not None:
        consents.base_url = service.base_url
        return service, node, clock, httpd, Client(service.base_url), consents, walls
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

    consent_page_checks()

    print("\n" + "=" * 70)
    if FAILURES:
        print(f"{len(FAILURES)} of {CHECKS} CHECKS FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print(f"ALL {CHECKS} END-TO-END CHECKS PASSED")
    return 0


class Page(HTMLParser):
    """Enough parsing to count what the consent spec forbids."""

    def __init__(self):
        super().__init__()
        self.inputs, self.buttons, self.forms, self.links = [], [], 0, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.forms += 1
        elif tag == "input":
            self.inputs.append(attrs)
        elif tag == "button":
            self.buttons.append(attrs)
        elif tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


def parse_page(html):
    p = Page()
    p.feed(html)
    return p


def raw_get(base, path, accept=None):
    r = urllib.request.Request(base + path)
    if accept:
        r.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


def form_post(base, path, fields, accept=None):
    data = urllib.parse.urlencode(fields).encode()
    r = urllib.request.Request(base + path, data=data, method="POST")
    r.add_header("Content-Type", "application/x-www-form-urlencoded")
    if accept:
        r.add_header("Accept", accept)
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def settle_one(service, call, price="0.05"):
    """post -> claim -> deliver -> accept -> settle, all over HTTP."""
    job = call("POST", "/v1/jobs", dict(JOB, price_xno=price),
               {"X-Buyer-Token": BUYER})[1]
    claim = call("POST", f"/v1/jobs/{job['id']}/claim",
                 {"seller": "arion", "payout_address": GOOD})[1]
    call("POST", f"/v1/jobs/{job['id']}/deliver",
         {"payload": [{"vendor": "a", "monthly": 10}]},
         {"Authorization": f"Bearer {claim['claim_token']}"})
    call("POST", f"/v1/jobs/{job['id']}/accept", None,
         {"X-Buyer-Token": BUYER})
    SettlementWorker(service).run_all()
    return job["id"]


def consent_page_checks():
    """The 13 numbered tests from the operator-consent spec, over real HTTP."""
    print("\n" + "=" * 70)
    print("OPERATOR CONSENT PAGE -- the 13 numbered spec tests, over HTTP")

    # ---- 1: zero settled jobs renders no form (the Pattern 6 test) -------
    service, node, clock, httpd, call, consents, walls = boot(with_consent=True)
    base = service.base_url
    try:
        csn = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                   {"X-Buyer-Token": BUYER})[1]["consent_id"]
        print("\n1. test_zero_settled_jobs_renders_no_form")
        status, html, _ = raw_get(base, f"/v1/consent/{csn}")
        parsed = parse_page(html)
        check("zero settled -> status", status, 409)
        check("the page says it in those words",
              "No jobs have been paid yet." in html, True)
        check("no <form on the page", parsed.forms, 0)
        check("no <input on the page", parsed.inputs, [])
        check("no <button on the page", parsed.buttons, [])
        check("a signature is refused too",
              call("POST", f"/v1/consent/{csn}/sign",
                   {"signed_by": "R", "decision": "approve"})[1]["error"],
              "no_demand_yet")

        # ---- 2: demand figures are live, not cached ----------------------
        print("\n2. test_demand_figures_are_live_not_cached")
        first_job = settle_one(service, call, "0.05")
        status, html, headers = raw_get(base, f"/v1/consent/{csn}")
        check("one settled job -> the page renders", status, 200)
        check("the figures are the queue's own",
              "1 jobs settled, 0.050000 XNO paid." in html, True)
        settle_one(service, call, "0.10")
        html2 = raw_get(base, f"/v1/consent/{csn}")[1]
        check("settling another moves the page",
              "2 jobs settled, 0.150000 XNO paid." in html2, True)
        check("the stale figure is gone", "1 jobs settled" in html2, False)
        check("the page is never cached", headers.get("Cache-Control"),
              "no-store")

        # ---- 3 / 4: scope ------------------------------------------------
        print("\n3. test_default_scope_is_receive_only")
        created = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                       {"X-Buyer-Token": BUYER})[1]
        check("default scope", created["scope"], "receive_only")
        check("no send ceiling", created["max_send_xno"], None)
        check("the page says it cannot send",
              "It cannot send." in raw_get(base,
                                           f"/v1/consent/{created['consent_id']}")[1],
              True)

        print("\n4. test_receive_only_cannot_carry_a_send_limit")
        status, body = call("POST", "/v1/consent",
                            {"agent": "DeskCrew", "scope": "receive_only",
                             "max_send_xno": "1.0"},
                            {"X-Buyer-Token": BUYER})
        check("receive_only + max_send_xno", (status, body["error"]),
              (400, "scope_conflict"))

        # ---- 5: the form -------------------------------------------------
        print("\n5. test_form_has_exactly_two_inputs")
        page_html = raw_get(base, f"/v1/consent/{csn}")[1]
        parsed = parse_page(page_html)
        check("exactly one form", parsed.forms, 1)
        check("exactly two inputs, named", sorted(i["name"] for i in parsed.inputs),
              ["reason", "signed_by"])
        check("exactly two buttons, carrying the decision",
              sorted(b["value"] for b in parsed.buttons),
              ["approve", "decline"])
        banned = ("email", "password", "card", "account", "address", "tel")
        check("no forbidden field, no hidden field",
              [i for i in parsed.inputs
               if i.get("type") == "hidden"
               or any(b in (i.get("name") or "").lower() for b in banned)],
              [])

        # ---- 6: no JavaScript --------------------------------------------
        print("\n6. test_page_renders_without_javascript")
        stripped = re.sub(r"<script.*?</script>", "", page_html, flags=re.S | re.I)
        check("there is no script to strip", stripped == page_html, True)
        for needed in ("jobs settled", "It cannot send.", "No custody.",
                       "<form", "signed_by"):
            check(f"still present without JS: {needed!r}",
                  needed in stripped, True)

        # ---- 7: expiry ---------------------------------------------------
        print("\n7. test_expiry_is_ninety_days_and_not_settable")
        status, body = call("POST", "/v1/consent",
                            {"agent": "DeskCrew",
                             "expires_at": "2099-01-01T00:00:00Z"},
                            {"X-Buyer-Token": BUYER})
        check("caller-supplied expiry", (status, body["error"]),
              (400, "expiry_not_settable"))
        detail = call("GET", f"/v1/consent/{csn}.json")[1]
        check("expiry is 90 days after creation",
              detail["expires_at"],
              clock.iso(clock.now() + 90 * 86400))

        # ---- 8 / 9: decline ------------------------------------------------
        print("\n8. test_decline_is_recorded_as_evidence")
        decl = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                    {"X-Buyer-Token": BUYER})[1]["consent_id"]
        status, html = form_post(base, f"/v1/consent/{decl}/sign",
                                 {"signed_by": "R. Okonkwo",
                                  "decision": "decline",
                                  "reason": "our policy needs a named counterparty"})
        check("a decline is a success", status, 200)
        check("the page says so", "Declined by R. Okonkwo" in html, True)
        rows = walls.walls()
        check("one wall was recorded", len(rows), 1)
        check("under walls", rows[0]["kind"], "walls")
        check("clustered, not forced", rows[0]["cluster"],
              "counterparty-identity")
        check("the operator's own words are kept", rows[0]["text"],
              "our policy needs a named counterparty")

        unk = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                   {"X-Buyer-Token": BUYER})[1]["consent_id"]
        form_post(base, f"/v1/consent/{unk}/sign",
                  {"signed_by": "R", "decision": "decline",
                   "reason": "we built this in house last quarter"})
        check("an unmatched reason goes to unclustered",
              walls.walls()[-1]["cluster"], "unclustered")

        print("\n9. test_decline_without_reason_is_rejected")
        nor = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                   {"X-Buyer-Token": BUYER})[1]["consent_id"]
        before = len(walls.walls())
        status, body = form_post(base, f"/v1/consent/{nor}/sign",
                                 {"signed_by": "R", "decision": "decline",
                                  "reason": ""}, accept="application/json")
        check("blank reason", (status, json.loads(body)["error"]),
              (400, "reason_required"))
        check("and nothing was recorded", len(walls.walls()), before)

        # ---- 10: revoke ----------------------------------------------------
        print("\n10. test_revoke_is_idempotent_and_needs_no_auth")
        rev = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                   {"X-Buyer-Token": BUYER})[1]["consent_id"]
        one = call("POST", f"/v1/consent/{rev}/revoke")
        two = call("POST", f"/v1/consent/{rev}/revoke")
        check("revoke with no token", (one[0], one[1]["state"]), (200, "revoked"))
        check("revoke twice", (two[0], two[1]["state"]), (200, "revoked"))
        check("then a signature is refused",
              call("POST", f"/v1/consent/{rev}/sign",
                   {"signed_by": "R", "decision": "approve"})[1]["error"],
              "invalid_state")

        # ---- 11: a signed page has no form ---------------------------------
        print("\n11. test_signed_page_shows_no_form")
        status, html = form_post(base, f"/v1/consent/{csn}/sign",
                                 {"signed_by": "R. Okonkwo",
                                  "decision": "approve", "reason": ""})
        check("approving over a plain HTML form", status, 200)
        signed = parse_page(raw_get(base, f"/v1/consent/{csn}")[1])
        check("the signed page carries no form", signed.forms, 0)
        check("and shows who signed",
              "Approved by R. Okonkwo" in raw_get(base, f"/v1/consent/{csn}")[1],
              True)

        # ---- 13: non-custody language and a live receipt --------------------
        print("\n13. test_no_custody_language_is_present_verbatim")
        live = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                    {"X-Buyer-Token": BUYER})[1]["consent_id"]
        html = raw_get(base, f"/v1/consent/{live}")[1]
        check('"We never hold" is on the page', "We never hold" in html, True)
        check('"private key" is on the page', "private key" in html, True)
        receipts = [h for h in parse_page(html).links if "/v1/receipts/" in h]
        check("a receipt link is on the page", bool(receipts), True)
        rpath = urllib.parse.urlparse(receipts[0]).path
        rstatus, rbody, _ = raw_get(base, rpath)
        check("and it resolves 200 with no auth", rstatus, 200)
        check("on a confirmed block", json.loads(rbody)["confirmed"], True)
    finally:
        httpd.shutdown()
        httpd.server_close()

    # ---- 12: demand unavailable is a hard failure ------------------------
    print("\n12. test_demand_unavailable_is_a_hard_failure")
    service, node, clock, httpd, call, consents, walls = boot(with_consent=True)
    base = service.base_url
    try:
        settle_one(service, call)
        csn = call("POST", "/v1/consent", {"agent": "DeskCrew"},
                   {"X-Buyer-Token": BUYER})[1]["consent_id"]
        check("the page renders while the queue is readable",
              raw_get(base, f"/v1/consent/{csn}")[0], 200)

        class Unreachable(DemandSource):
            def snapshot(self):
                raise DemandUnavailable("queue is not answering")

        consents.demand = Unreachable(service)
        status, html, _ = raw_get(base, f"/v1/consent/{csn}")
        parsed = parse_page(html)
        check("unreadable queue -> status", status, 503)
        check("and no form came back", (parsed.forms, parsed.buttons), (0, []))
        check("a signature is refused too",
              call("POST", f"/v1/consent/{csn}/sign",
                   {"signed_by": "R", "decision": "approve"})[1]["error"],
              "demand_unavailable")
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    sys.exit(main())
