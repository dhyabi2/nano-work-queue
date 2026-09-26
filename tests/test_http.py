"""The HTTP surface, driven over a real loopback socket.

The server is started on 127.0.0.1 with an ephemeral port and the node is
still the fake, so nothing leaves the machine and no Nano node is needed.
"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from helpers import BAD_ADDRESS, GOOD_ADDRESS, JOB_BODY, build

from nano_work_queue import store
from nano_work_queue.http_app import RateLimiter, make_handler
from nano_work_queue.settlement import SettlementWorker

BUYER = "test-buyer-token"


class HttpCase(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer

        self.service, self.node, self.clock = build()
        handler = make_handler(
            self.service,
            buyer_token_getter=lambda: BUYER,
            rate_limiter=RateLimiter(self.clock, limit=30),
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # -- helper ------------------------------------------------------------
    def req(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            return e.code, (json.loads(raw) if raw.strip().startswith("{") else raw)

    def buyer(self):
        return {"X-Buyer-Token": BUYER}

    # -- tests -------------------------------------------------------------
    def test_health_is_public(self):
        status, body = self.req("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["node"], "reachable")

    def test_posting_needs_the_buyer_token(self):
        status, body = self.req("POST", "/v1/jobs", JOB_BODY)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

        status, _ = self.req("POST", "/v1/jobs", JOB_BODY,
                             {"X-Buyer-Token": "wrong"})
        self.assertEqual(status, 401)

        status, body = self.req("POST", "/v1/jobs", JOB_BODY, self.buyer())
        self.assertEqual(status, 201)
        self.assertEqual(body["price_xno"], "0.050000")

    def test_full_cycle_over_http(self):
        _s, job = self.req("POST", "/v1/jobs", JOB_BODY, self.buyer())
        job_id = job["id"]

        status, claim = self.req("POST", f"/v1/jobs/{job_id}/claim",
                                 {"seller": "arion",
                                  "payout_address": GOOD_ADDRESS})
        self.assertEqual(status, 201)
        token = claim["claim_token"]
        self.assertEqual(claim["payout_address"], GOOD_ADDRESS)

        status, delivered = self.req(
            "POST", f"/v1/jobs/{job_id}/deliver",
            {"payload": [{"vendor": "acme", "monthly": 49}]},
            {"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(delivered["state"], "delivered")

        status, accepted = self.req("POST", f"/v1/jobs/{job_id}/accept", {},
                                    self.buyer())
        self.assertEqual(status, 200)
        self.assertEqual(accepted["settlement"], "pending")

        SettlementWorker(self.service).run_once()

        status, receipt = self.req("GET", f"/v1/receipts/{job_id}")
        self.assertEqual(status, 200)
        self.assertEqual(receipt["state"], "settled")
        self.assertTrue(store.is_block_hash(receipt["block_hash"]))
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(receipt["price_xno"], "0.050000")

        status, listing = self.req("GET", "/v1/jobs?state=settled")
        self.assertEqual(listing["totals"]["settled"], 1)
        self.assertEqual(listing["totals"]["paid_xno_total"], "0.050000")

    def test_receipt_content_negotiation(self):
        _s, job = self.req("POST", "/v1/jobs", JOB_BODY, self.buyer())
        self.req("POST", f"/v1/jobs/{job['id']}/claim",
                 {"seller": "a", "payout_address": GOOD_ADDRESS})
        status, text = self.req("GET", f"/v1/receipts/{job['id']}", None,
                                {"Accept": "text/plain"})
        self.assertEqual(status, 200)
        self.assertIn("job_id:", text)
        self.assertIn("verify:", text)

    def test_bad_checksum_over_http_is_400_and_stores_nothing(self):
        _s, job = self.req("POST", "/v1/jobs", JOB_BODY, self.buyer())
        status, body = self.req("POST", f"/v1/jobs/{job['id']}/claim",
                                {"seller": "eddie",
                                 "payout_address": BAD_ADDRESS})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_address")
        self.assertEqual(body["field"], "payout_address")
        self.assertIn("Nothing was stored", body["message"])
        status, fresh = self.req("GET", f"/v1/jobs/{job['id']}")
        self.assertEqual(fresh["state"], "open")

    def test_unknown_route_and_unknown_job(self):
        status, body = self.req("GET", "/v1/nope")
        self.assertEqual(status, 404)
        status, body = self.req("GET", "/v1/jobs/job_0000000000000000")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_oversized_body_is_413(self):
        _s, job = self.req("POST", "/v1/jobs", JOB_BODY, self.buyer())
        big = {"seller": "a", "payout_address": GOOD_ADDRESS,
               "note": "x" * (70 * 1024)}
        status, body = self.req("POST", f"/v1/jobs/{job['id']}/claim", big)
        self.assertEqual(status, 413)
        self.assertEqual(body["error"], "payload_too_large")

    def test_rate_limit_is_429(self):
        from http.server import ThreadingHTTPServer

        service, _n, clock = build()
        handler = make_handler(service, buyer_token_getter=lambda: BUYER,
                               rate_limiter=RateLimiter(clock, limit=3))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            codes = []
            for _ in range(5):
                r = urllib.request.Request(f"http://127.0.0.1:{port}/v1/jobs")
                try:
                    with urllib.request.urlopen(r, timeout=10) as resp:
                        codes.append(resp.status)
                except urllib.error.HTTPError as e:
                    codes.append(e.code)
            self.assertEqual(codes[:3], [200, 200, 200])
            self.assertIn(429, codes)
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_no_402_anywhere_on_the_seller_path(self):
        _s, job = self.req("POST", "/v1/jobs", JOB_BODY, self.buyer())
        job_id = job["id"]
        _s, claim = self.req("POST", f"/v1/jobs/{job_id}/claim",
                             {"seller": "a", "payout_address": GOOD_ADDRESS})
        seller_calls = [
            self.req("GET", "/v1/jobs"),
            self.req("GET", f"/v1/jobs/{job_id}"),
            self.req("GET", f"/v1/receipts/{job_id}"),
            self.req("POST", f"/v1/jobs/{job_id}/deliver",
                     {"payload": [{"v": 1}]},
                     {"Authorization": f"Bearer {claim['claim_token']}"}),
        ]
        for status, body in seller_calls:
            self.assertNotEqual(status, 402)
            blob = repr(body).lower()
            for bad in ("payment_required", "escrow", "deposit", "x-402"):
                self.assertNotIn(bad, blob)


class McpSurface(unittest.TestCase):
    def test_exactly_four_tools_each_opening_with_the_operator_sentence(self):
        from nano_work_queue.mcp_server import (
            FIRST_SENTENCE, SERVER_NAME, tool_list,
        )

        service, _n, _c = build()
        payload = tool_list(service)
        self.assertEqual(payload["server"], SERVER_NAME)
        names = [t["name"] for t in payload["tools"]]
        self.assertEqual(names, ["list_jobs", "get_job", "claim_job",
                                 "deliver_job"])
        for tool in payload["tools"]:
            self.assertTrue(
                tool["description"].startswith(FIRST_SENTENCE),
                f"{tool['name']} does not open with the operator sentence",
            )
            self.assertNotIn("handler", tool)

    def test_mcp_tools_drive_the_same_service(self):
        from nano_work_queue.mcp_server import call

        service, _n, _c = build()
        posted = service.post_job(dict(JOB_BODY))
        listed = call(service, "list_jobs", {})
        self.assertEqual(listed["jobs"][0]["id"], posted["id"])

        claim = call(service, "claim_job",
                     {"job_id": posted["id"], "seller": "arion",
                      "payout_address": GOOD_ADDRESS})
        self.assertEqual(claim["state"], "claimed")

        delivered = call(service, "deliver_job",
                         {"job_id": posted["id"],
                          "claim_token": claim["claim_token"],
                          "payload": [{"v": 1}]})
        self.assertEqual(delivered["state"], "delivered")

    def test_mcp_errors_use_the_same_vocabulary(self):
        from nano_work_queue.mcp_server import call

        service, _n, _c = build()
        posted = service.post_job(dict(JOB_BODY))
        out = call(service, "claim_job",
                   {"job_id": posted["id"], "seller": "eddie",
                    "payout_address": BAD_ADDRESS})
        self.assertEqual(out["error"], "invalid_address")
        self.assertEqual(call(service, "no_such_tool", {})["error"], "not_found")


class AgentCard(unittest.TestCase):
    def test_card_has_one_skill_and_a_real_exchange(self):
        from nano_work_queue.a2a import SKILL_ID, agent_card
        from nano_work_queue.address import is_valid
        from nano_work_queue.mcp_server import FIRST_SENTENCE

        card = agent_card("https://queue.example")
        self.assertEqual(len(card["skills"]), 1)
        skill = card["skills"][0]
        self.assertEqual(skill["id"], SKILL_ID)
        self.assertEqual(skill["name"], "Sell work, get paid in XNO")
        self.assertEqual(skill["description"], FIRST_SENTENCE)
        self.assertEqual(skill["inputModes"], ["application/json"])
        self.assertEqual(skill["outputModes"], ["application/json"])

        exchange = skill["examples"][0]["exchange"]
        self.assertEqual(len(exchange), 3)               # claim -> deliver -> receipt
        addr = exchange[0]["response"]["body"]["payout_address"]
        self.assertTrue(is_valid(addr), "the card's example address must validate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
