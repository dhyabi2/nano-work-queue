"""The consent page over a real loopback socket.

The service-layer tests prove the rules. These prove they survive the wire:
the status codes actually reach the client, the HTML form actually posts
without JavaScript, the receipt link on the page actually resolves, and the
page is never cached by a proxy.
"""

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer

from helpers import build, full_cycle

from nano_work_queue import conversations
from nano_work_queue.consent import ConsentService, DemandSource
from nano_work_queue.http_app import RateLimiter, make_handler
from nano_work_queue.settlement import SettlementWorker

BUYER = "test-buyer-token"


class Forms(HTMLParser):
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


class ConsentHttpCase(unittest.TestCase):
    settled_jobs = 1

    def setUp(self):
        self.service, self.node, self.clock = build()
        self.worker = SettlementWorker(self.service)
        for _ in range(self.settled_jobs):
            full_cycle(self.service)
            self.worker.run_all()
        self.walls = conversations.MemoryConversationStore()

        self.consents = ConsentService(DemandSource(self.service), self.clock,
                                       base_url="http://127.0.0.1:0",
                                       conversation_store=self.walls)
        handler = make_handler(
            self.service, buyer_token_getter=lambda: BUYER,
            rate_limiter=RateLimiter(self.clock, limit=1000),
            consents=self.consents)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        # The page publishes its own absolute URLs, so they have to point here.
        self.consents.base_url = self.base
        self.service.base_url = self.base
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def req(self, method, path, body=None, headers=None, form=None):
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            content_type = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            content_type = "application/json"
        else:
            data, content_type = None, "application/json"
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, resp.read().decode(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), dict(e.headers)

    def jreq(self, method, path, **kw):
        headers = dict(kw.pop("headers", None) or {})
        headers["Accept"] = "application/json"
        status, raw, _ = self.req(method, path, headers=headers, **kw)
        return status, json.loads(raw)

    def create(self, **body):
        body.setdefault("agent", "DeskCrew")
        status, data = self.jreq("POST", "/v1/consent", body=body,
                                 headers={"X-Buyer-Token": BUYER})
        self.assertEqual(status, 201, data)
        return data


class TestConsentPageOverHttp(ConsentHttpCase):
    def test_the_page_is_html_public_and_uncacheable(self):
        consent = self.create()
        status, html, headers = self.req(
            "GET", f"/v1/consent/{consent['consent_id']}")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("1 jobs settled, 0.050000 XNO paid.", html)

    def test_the_demand_block_comes_before_the_form(self):
        consent = self.create()
        html = self.req("GET", f"/v1/consent/{consent['consent_id']}")[1]
        self.assertLess(html.index("jobs settled"), html.index("<form"))
        self.assertLess(html.index("jobs settled"), html.index("No custody."))

    def test_the_receipt_link_on_the_page_resolves_without_auth(self):
        """Test 13's live half: the link is fetched, not just asserted."""
        consent = self.create()
        html = self.req("GET", f"/v1/consent/{consent['consent_id']}")[1]
        receipts = [h for h in Forms_parse(html).links if "/v1/receipts/" in h]
        self.assertTrue(receipts, "no receipt link on the page")
        status, body, _ = self.req("GET", urllib.parse.urlparse(receipts[0]).path)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["confirmed"])

    def test_the_jobs_link_resolves_too(self):
        consent = self.create()
        html = self.req("GET", f"/v1/consent/{consent['consent_id']}")[1]
        jobs = [h for h in Forms_parse(html).links if h.endswith("/v1/jobs")]
        self.assertTrue(jobs)
        self.assertEqual(self.req("GET", "/v1/jobs")[0], 200)

    def test_json_form_of_the_page(self):
        consent = self.create()
        status, data = self.jreq(
            "GET", f"/v1/consent/{consent['consent_id']}.json")
        self.assertEqual(status, 200)
        self.assertEqual(data["scope"], "receive_only")
        self.assertEqual(data["demand"], {"settled": 1,
                                          "paid_xno_total": "0.050000",
                                          "open": 0})
        self.assertTrue(data["revoke_url"].endswith("/revoke"))

    def test_unknown_id_is_404(self):
        status, data = self.jreq("GET", "/v1/consent/csn_ffffffffffffffff")
        self.assertEqual((status, data["error"]), (404, "not_found"))


class TestSigningOverHttp(ConsentHttpCase):
    def test_a_plain_html_form_post_approves(self):
        """No JavaScript: the button carries the decision, urlencoded."""
        consent = self.create()
        status, html, headers = self.req(
            "POST", f"/v1/consent/{consent['consent_id']}/sign",
            form={"signed_by": "R. Okonkwo", "decision": "approve",
                  "reason": ""})
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("Approved by R. Okonkwo", html)
        self.assertNotIn("<form", html)

    def test_a_plain_html_form_post_declines_and_records_the_reason(self):
        consent = self.create()
        status, html, _ = self.req(
            "POST", f"/v1/consent/{consent['consent_id']}/sign",
            form={"signed_by": "R. Okonkwo", "decision": "decline",
                  "reason": "our policy needs a named counterparty"})
        self.assertEqual(status, 200)
        self.assertIn("Declined by R. Okonkwo", html)
        walls = self.walls.walls()
        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0]["cluster"], "counterparty-identity")

    def test_declining_with_a_blank_reason_is_400_over_the_wire(self):
        consent = self.create()
        status, raw, _ = self.req(
            "POST", f"/v1/consent/{consent['consent_id']}/sign",
            form={"signed_by": "R", "decision": "decline", "reason": ""},
            headers={"Accept": "application/json"})
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(raw)["error"], "reason_required")
        self.assertEqual(self.walls.walls(), [])

    def test_json_clients_get_json(self):
        consent = self.create()
        status, data = self.jreq(
            "POST", f"/v1/consent/{consent['consent_id']}/sign",
            body={"signed_by": "R. Okonkwo", "decision": "approve"})
        self.assertEqual((status, data["state"]), (200, "signed"))
        self.assertEqual(data["signed_by"], "R. Okonkwo")

    def test_signing_twice_is_409(self):
        consent = self.create()
        self.jreq("POST", f"/v1/consent/{consent['consent_id']}/sign",
                  body={"signed_by": "R", "decision": "approve"})
        status, data = self.jreq(
            "POST", f"/v1/consent/{consent['consent_id']}/sign",
            body={"signed_by": "R", "decision": "approve"})
        self.assertEqual((status, data["error"]), (409, "invalid_state"))

    def test_revoke_needs_no_token_and_is_idempotent(self):
        consent = self.create()
        for _ in range(2):
            status, data = self.jreq(
                "POST", f"/v1/consent/{consent['consent_id']}/revoke")
            self.assertEqual((status, data["state"]), (200, "revoked"))

    def test_create_needs_the_buyer_token(self):
        status, data = self.jreq("POST", "/v1/consent",
                                 body={"agent": "DeskCrew"})
        self.assertEqual((status, data["error"]), (401, "unauthorized"))
        status, data = self.jreq("POST", "/v1/consent",
                                 body={"agent": "DeskCrew"},
                                 headers={"X-Buyer-Token": "wrong"})
        self.assertEqual(status, 401)

    def test_the_error_table_over_http(self):
        consent = self.create()
        cases = [
            ({"agent": "DeskCrew", "scope": "receive_only",
              "max_send_xno": "1.0"}, 400, "scope_conflict"),
            ({"agent": "DeskCrew", "expires_at": "2099-01-01T00:00:00Z"},
             400, "expiry_not_settable"),
        ]
        for body, status, code in cases:
            got, data = self.jreq("POST", "/v1/consent", body=body,
                                  headers={"X-Buyer-Token": BUYER})
            self.assertEqual((got, data["error"]), (status, code), body)
        got, data = self.jreq(
            "POST", f"/v1/consent/{consent['consent_id']}/sign",
            body={"signed_by": "", "decision": "approve"})
        self.assertEqual((got, data["error"]), (400, "invalid_signer"))


class TestNoDemandOverHttp(ConsentHttpCase):
    settled_jobs = 0

    def test_page_is_409_with_no_form_anywhere_in_the_body(self):
        consent = self.create()
        status, html, headers = self.req(
            "GET", f"/v1/consent/{consent['consent_id']}")
        self.assertEqual(status, 409)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertIn("No jobs have been paid yet.", html)
        parsed = Forms_parse(html)
        self.assertEqual((parsed.forms, parsed.inputs, parsed.buttons),
                         (0, [], []))

    def test_json_clients_get_the_same_status_and_code(self):
        consent = self.create()
        status, data = self.jreq("GET", f"/v1/consent/{consent['consent_id']}")
        self.assertEqual((status, data["error"]), (409, "no_demand_yet"))
        self.assertIn("No jobs have been paid yet.", data["message"])

    def test_signing_is_refused_too(self):
        consent = self.create()
        status, data = self.jreq(
            "POST", f"/v1/consent/{consent['consent_id']}/sign",
            body={"signed_by": "R", "decision": "approve"})
        self.assertEqual((status, data["error"]), (409, "no_demand_yet"))

    def test_the_page_appears_the_moment_a_job_settles(self):
        consent = self.create()
        self.assertEqual(
            self.req("GET", f"/v1/consent/{consent['consent_id']}")[0], 409)
        full_cycle(self.service)
        self.worker.run_all()
        status, html, _ = self.req(
            "GET", f"/v1/consent/{consent['consent_id']}")
        self.assertEqual(status, 200)
        self.assertIn("<form", html)


def Forms_parse(html):
    p = Forms()
    p.feed(html)
    return p


if __name__ == "__main__":
    unittest.main()
