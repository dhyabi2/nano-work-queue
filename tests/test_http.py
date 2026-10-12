"""The HTTP surface, driven over a real loopback socket.

The server is started on 127.0.0.1 with an ephemeral port and the node is
still the fake, so nothing leaves the machine and no Nano node is needed.
"""

import contextlib
import io
import json
import socket
import threading
import unittest
import urllib.error
import urllib.request

from helpers import BAD_ADDRESS, GOOD_ADDRESS, JOB_BODY, build

from nano_work_queue import store
from nano_work_queue.http_app import RateLimiter, make_handler
from nano_work_queue.settlement import SettlementWorker

BUYER = "test-buyer-token"
# Built at runtime so the literal never appears in a traceback's source line:
# a test that finds it in stderr must have found the exception's message.
LEAKY_MESSAGE = "tok " + "clm_" + "SECRET\r\n" + "FAKE " + "LOG LINE"


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

    def test_the_401_tells_a_buyer_which_credential_to_send(self):
        """The message is a buyer agent's whole recovery path, so it has to name
        the buyer's own header - not the seller's.

        `errors.unauthorized` parameterised *which* credential was missing but
        hardcoded the remedy for the claim-token case, so a buyer with no
        `X-Buyer-Token` was told to "send it as 'Authorization: Bearer
        <claim_token>' from the claim response": the wrong header, the seller's
        credential, and a claim response a buyer never has. An agent that
        follows it fails again, and posting work is the first call a buyer makes.
        """
        for headers in (None, {"X-Buyer-Token": "wrong"}):
            with self.subTest(headers=headers):
                status, body = self.req("POST", "/v1/jobs", JOB_BODY, headers)
                self.assertEqual(status, 401)
                message = body["message"]
                self.assertIn("buyer token", message)
                self.assertIn("X-Buyer-Token", message)
                self.assertIn("DEMAND_QUEUE_BUYER_TOKEN", message)
                self.assertNotIn(
                    "Authorization: Bearer <claim_token>", message,
                    "a buyer is being sent to the seller's header")
                self.assertNotIn(
                    "Authorization", message,
                    "a buyer must not be pointed at the Authorization header")
                self.assertIn(
                    "claim token will not do", message,
                    "the message should pre-empt the swap it used to invite")

    def test_the_401_still_tells_a_seller_to_use_the_claim_token(self):
        """The other half of the same message: a seller delivering without the
        `Authorization` header must still get the claim-token remedy, unchanged.
        """
        _s, job = self.req("POST", "/v1/jobs", JOB_BODY, self.buyer())
        status, body = self.req(
            "POST", f"/v1/jobs/{job['id']}/deliver", {"payload": "x"})
        self.assertEqual(status, 401)
        self.assertIn("claim token", body["message"])
        self.assertIn("Authorization: Bearer <claim_token>", body["message"])

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


class NodeDownOverHttp(unittest.TestCase):
    """The buyer must get a status back when the node cannot be read."""

    def setUp(self):
        from http.server import ThreadingHTTPServer

        self.service, self.node, self.clock = build()
        self.node.up = False
        handler = make_handler(
            self.service,
            buyer_token_getter=lambda: BUYER,
            rate_limiter=RateLimiter(self.clock, limit=30),
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_post_job_answers_503_rather_than_closing_the_socket(self):
        data = json.dumps(JOB_BODY).encode()
        r = urllib.request.Request(self.base + "/v1/jobs", data=data,
                                   method="POST")
        r.add_header("Content-Type", "application/json")
        r.add_header("X-Buyer-Token", BUYER)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                self.fail(f"expected a 503, got {resp.status}")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 503)
            self.assertEqual(json.loads(exc.read().decode())["error"],
                             "node_unavailable")
        # No RemoteDisconnected: the connection carried a response.
        self.assertEqual(self.service.store.all_jobs(), [])


class BadNodeReplyOverHttp(NodeDownOverHttp):
    """A node that answers with garbage must still produce a status."""

    def setUp(self):
        super().setUp()
        self.node.up = True

        def broken():
            raise json.JSONDecodeError("Expecting value", "<html>502</html>", 0)

        self.node.balance_raw = broken


class UnexpectedErrorOverHttp(unittest.TestCase):
    """A handler bug must answer 500, not drop the connection."""

    SECRET = "clm_deadbeef internal detail /srv/queue.db"

    def setUp(self):
        from http.server import ThreadingHTTPServer

        self.service, self.node, self.clock = build()

        def boom():
            raise RuntimeError(self.SECRET)  # the frame name "boom" is asserted

        self.service.health = boom
        handler = make_handler(
            self.service,
            buyer_token_getter=lambda: BUYER,
            rate_limiter=RateLimiter(self.clock, limit=30),
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_unexpected_exception_answers_500_without_its_text(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            try:
                with urllib.request.urlopen(self.base + "/v1/health",
                                            timeout=10) as resp:
                    self.fail(f"expected a 500, got {resp.status}")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 500)
                raw = exc.read().decode()
        self.assertEqual(json.loads(raw), {"error": "internal"})
        self.assertNotIn("deadbeef", raw)
        self.assertNotIn("RuntimeError", raw)
        # The body says nothing, so the traceback has to land server-side.
        logged = captured.getvalue()
        self.assertIn("Traceback (most recent call last)", logged)
        self.assertIn("RuntimeError", logged)
        self.assertIn("boom", logged)

    def test_logged_traceback_carries_no_exception_message(self):
        def boom():
            raise RuntimeError(LEAKY_MESSAGE)

        self.service.health = boom
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(self.base + "/v1/health", timeout=10)
            ctx.exception.read()
            ctx.exception.close()
        logged = captured.getvalue()
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("clm_SECRET", logged)
        self.assertNotIn("FAKE LOG LINE", logged)
        self.assertNotIn("\r", logged)


class FailureAfterHeadersOverHttp(unittest.TestCase):
    """A failure after the status line went out must not add a second one."""

    def setUp(self):
        from http.server import ThreadingHTTPServer

        self.service, self.node, self.clock = build()
        base_handler = make_handler(
            self.service,
            buyer_token_getter=lambda: BUYER,
            rate_limiter=RateLimiter(self.clock, limit=30),
        )

        class Handler(base_handler):
            def _send(self, status, payload, content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", "100")
                self.end_headers()
                raise RuntimeError(LEAKY_MESSAGE)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_exactly_one_status_line_and_the_connection_closes(self):
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured):
            with socket.create_connection(("127.0.0.1", self.port),
                                          timeout=10) as sock:
                sock.sendall(b"GET /v1/health HTTP/1.1\r\n"
                             b"Host: localhost\r\n\r\n")
                data = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break  # the server closed the connection
                    data += chunk
        self.assertEqual(data.count(b"HTTP/1.1 "), 1, data)
        self.assertTrue(data.startswith(b"HTTP/1.1 200 "), data)
        self.assertNotIn(b"internal", data)
        logged = captured.getvalue()
        self.assertIn("Traceback (most recent call last)", logged)
        self.assertIn("RuntimeError", logged)
        self.assertIn("_send", logged)
        self.assertNotIn("clm_SECRET", logged)
        self.assertNotIn("FAKE LOG LINE", logged)


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
