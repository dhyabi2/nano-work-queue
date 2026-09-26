"""The HTTP surface: stdlib only, no framework, no dependency to audit.

Routing is a small table of (method, regex) -> handler. Buyer endpoints check
`X-Buyer-Token` in constant time against DEMAND_QUEUE_BUYER_TOKEN, which is
read from the environment and never from a file.
"""

import hmac
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import consent_page, errors, store
from .service import MAX_BODY_BYTES, Service

RATE_LIMIT_PER_MIN = 30

_JOB = r"(?P<job_id>job_[0-9a-f]{16})"
_CSN = r"(?P<consent_id>csn_[0-9a-f]{16})"


class RateLimiter:
    """30 requests per minute per address on the public endpoints."""

    def __init__(self, clock, limit=RATE_LIMIT_PER_MIN):
        self.clock = clock
        self.limit = limit
        self._hits = {}

    def check(self, who):
        now = self.clock.now()
        window = [t for t in self._hits.get(who, []) if now - t < 60]
        if len(window) >= self.limit:
            self._hits[who] = window
            raise errors.rate_limited()
        window.append(now)
        self._hits[who] = window


def make_handler(service, buyer_token_getter=store.env_buyer_token,
                 rate_limiter=None, consents=None):
    limiter = rate_limiter or RateLimiter(service.clock)

    routes = [
        ("GET", re.compile(r"^/v1/health$"), "health"),
        ("GET", re.compile(r"^/v1/jobs$"), "list_jobs"),
        ("POST", re.compile(r"^/v1/jobs$"), "post_job"),
        ("GET", re.compile(rf"^/v1/jobs/{_JOB}$"), "get_job"),
        ("POST", re.compile(rf"^/v1/jobs/{_JOB}/claim$"), "claim"),
        ("POST", re.compile(rf"^/v1/jobs/{_JOB}/deliver$"), "deliver"),
        ("POST", re.compile(rf"^/v1/jobs/{_JOB}/release$"), "release"),
        ("POST", re.compile(rf"^/v1/jobs/{_JOB}/accept$"), "accept"),
        ("POST", re.compile(rf"^/v1/jobs/{_JOB}/reject$"), "reject"),
        ("POST", re.compile(rf"^/v1/jobs/{_JOB}/close$"), "close"),
        ("GET", re.compile(rf"^/v1/receipts/{_JOB}$"), "receipt"),
        # The consent page. Public and unauthenticated except for create:
        # an operator who has to hold a token to read what they are being
        # asked to sign will not read it.
        ("GET", re.compile(rf"^/v1/consent/{_CSN}\.json$"), "consent_json"),
        ("GET", re.compile(rf"^/v1/consent/{_CSN}$"), "consent_page"),
        ("POST", re.compile(rf"^/v1/consent/{_CSN}/sign$"), "consent_sign"),
        ("POST", re.compile(rf"^/v1/consent/{_CSN}/revoke$"), "consent_revoke"),
        ("POST", re.compile(r"^/v1/consent$"), "consent_create"),
    ]

    class Handler(BaseHTTPRequestHandler):
        server_version = "nano-work-queue"
        protocol_version = "HTTP/1.1"

        # -- plumbing ----------------------------------------------------
        def log_message(self, fmt, *args):
            # Never let the default logger echo a URL or header anywhere a
            # claim token could ride along. The service logs what matters.
            return

        def _send(self, status, payload, content_type="application/json; charset=utf-8"):
            if isinstance(payload, (dict, list)):
                raw = json.dumps(payload, indent=1).encode("utf-8")
            else:
                raw = str(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _error(self, exc):
            self._send(exc.status, exc.body())

        def _error_html(self, exc):
            """The same status and code, rendered as a page with no form on it."""
            self._send(exc.status, consent_page.render_error(exc),
                       content_type="text/html; charset=utf-8")

        def _body(self):
            length = self.headers.get("Content-Length")
            try:
                n = int(length or 0)
            except ValueError:
                raise errors.bad_request("Content-Length is not a number.") from None
            if n > MAX_BODY_BYTES:
                raise errors.payload_too_large()
            if n == 0:
                return {}
            raw = self.rfile.read(n)
            if len(raw) > MAX_BODY_BYTES:
                raise errors.payload_too_large()
            content_type = (self.headers.get("Content-Type") or "").lower()
            if content_type.startswith("application/x-www-form-urlencoded"):
                # The consent page posts a plain HTML form: it has to work
                # with JavaScript disabled, so it cannot send JSON.
                try:
                    fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
                except UnicodeDecodeError:
                    raise errors.bad_request("Body is not valid UTF-8.") from None
                return {k: v[-1] for k, v in fields.items()}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise errors.bad_request("Body is not valid JSON.") from None
            if not isinstance(parsed, dict):
                raise errors.bad_request("Body must be a JSON object.")
            return parsed

        def _claim_token(self):
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Bearer "):
                raise errors.unauthorized()
            return header[len("Bearer ") :].strip()

        def _need_buyer(self):
            supplied = self.headers.get("X-Buyer-Token") or ""
            expected = buyer_token_getter() or ""
            if not expected or not hmac.compare_digest(supplied, expected):
                raise errors.unauthorized("buyer token")

        # -- dispatch ----------------------------------------------------
        def _handle(self, method):
            parsed = urlparse(self.path)
            for verb, pattern, name in routes:
                if verb != method:
                    continue
                m = pattern.match(parsed.path)
                if not m:
                    continue
                try:
                    getattr(self, f"_do_{name}")(m, parse_qs(parsed.query))
                except errors.ApiError as exc:
                    self._error(exc)
                except store.IllegalTransition as exc:
                    self._error(errors.ApiError(409, "invalid_state", str(exc)))
                return
            self._error(errors.not_found("route"))

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        # -- handlers ----------------------------------------------------
        def _public(self):
            limiter.check(self.client_address[0] if self.client_address else "-")

        def _do_health(self, m, q):
            self._send(200, service.health())

        def _do_list_jobs(self, m, q):
            self._public()
            state = (q.get("state") or [store.OPEN])[0]
            if state == "all":
                state = None
            limit_raw = (q.get("limit") or ["20"])[0]
            try:
                limit = int(limit_raw)
            except ValueError:
                raise errors.bad_request("limit must be an integer 1..100.",
                                         field="limit") from None
            cursor = (q.get("cursor") or [None])[0]
            self._send(200, service.list_jobs(state=state, limit=limit,
                                              cursor=cursor))

        def _do_get_job(self, m, q):
            self._public()
            self._send(200, service.get_job(m.group("job_id")))

        def _do_post_job(self, m, q):
            self._need_buyer()
            self._send(201, service.post_job(self._body()))

        def _do_claim(self, m, q):
            self._public()
            self._send(201, service.claim(m.group("job_id"), self._body()))

        def _do_deliver(self, m, q):
            self._public()
            body = self._body()
            self._send(200, service.deliver(m.group("job_id"),
                                            self._claim_token(), body))

        def _do_release(self, m, q):
            self._public()
            self._send(200, service.release(m.group("job_id"),
                                            self._claim_token()))

        def _do_accept(self, m, q):
            self._need_buyer()
            self._send(200, service.accept(m.group("job_id")))

        def _do_reject(self, m, q):
            self._need_buyer()
            self._send(200, service.reject(m.group("job_id"),
                                           self._body().get("reason")))

        def _do_close(self, m, q):
            self._need_buyer()
            self._send(200, service.close(m.group("job_id")))

        def _do_receipt(self, m, q):
            self._public()
            job_id = m.group("job_id")
            accept = (self.headers.get("Accept") or "").lower()
            if "text/plain" in accept:
                service.receipt(job_id)   # 404s before we render anything
                self._send(200, service.receipt_text(job_id),
                           content_type="text/plain; charset=utf-8")
            else:
                self._send(200, service.receipt(job_id))

        # -- consent -----------------------------------------------------
        def _consents(self):
            if consents is None:
                raise errors.not_found("route")
            return consents

        def _wants_json(self):
            return "application/json" in (
                self.headers.get("Accept") or "").lower()

        def _do_consent_page(self, m, q):
            self._public()
            svc = self._consents()
            try:
                consent, demand = svc.view(m.group("consent_id"))
            except errors.ApiError as exc:
                if self._wants_json():
                    raise
                # 409 no_demand_yet and 503 demand_unavailable both land here.
                # The status is the same either way; what matters is that the
                # body that comes back carries no form.
                self._error_html(exc)
                return
            if self._wants_json():
                self._send(200, svc.as_json(consent, demand=_demand_summary(demand)))
                return
            self._send(200, consent_page.render(consent, demand, svc.base_url),
                       content_type="text/html; charset=utf-8")

        def _do_consent_json(self, m, q):
            self._public()
            svc = self._consents()
            consent, demand = svc.view(m.group("consent_id"))
            self._send(200, svc.as_json(consent, demand=_demand_summary(demand)))

        def _do_consent_sign(self, m, q):
            self._public()
            svc = self._consents()
            body = self._body()
            result = svc.sign(m.group("consent_id"), body)
            if self._wants_json() or not _is_form_post(self):
                self._send(200, result)
                return
            # A browser posted the form, so answer with the page it should now
            # see rather than a JSON body it cannot read.
            consent, demand = svc.view(m.group("consent_id"))
            self._send(200, consent_page.render(consent, demand, svc.base_url),
                       content_type="text/html; charset=utf-8")

        def _do_consent_revoke(self, m, q):
            self._public()
            self._send(200, self._consents().revoke(m.group("consent_id")))

        def _do_consent_create(self, m, q):
            self._need_buyer()
            self._send(201, self._consents().create(self._body()))

    return Handler


def _is_form_post(handler):
    return (handler.headers.get("Content-Type") or "").lower().startswith(
        "application/x-www-form-urlencoded")


def _demand_summary(demand):
    return {"settled": demand["settled"],
            "paid_xno_total": demand["paid_xno_total"],
            "open": demand["open"]}


def serve(service, host="127.0.0.1", port=8080, **kw):
    httpd = ThreadingHTTPServer((host, port), make_handler(service, **kw))
    return httpd


def main(argv=None):  # pragma: no cover - entry point
    import argparse
    import os

    from .clock import Clock
    from .node import FakeNode

    ap = argparse.ArgumentParser(description="Run the paid work queue.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument(
        "--fake-node",
        action="store_true",
        help="run against an in-process fake node (development only)",
    )
    args = ap.parse_args(argv)

    if not os.environ.get("DEMAND_QUEUE_BUYER_TOKEN"):
        raise SystemExit("DEMAND_QUEUE_BUYER_TOKEN is not set; refusing to start.")
    if not args.fake_node:
        raise SystemExit(
            "No Nano node configured. Set NANO_NODE_URL and supply a NanoNode "
            "implementation, or pass --fake-node for local development."
        )

    base_url = os.environ.get("DEMAND_QUEUE_PUBLIC_BASE_URL",
                              f"http://{args.host}:{args.port}")
    service = Service(FakeNode(), clock=Clock(), base_url=base_url)

    # The consent page is served by this same service, per
    # specs/unstuck/agent-tool-operator-consent-page.md: it has to read the
    # queue's totals live at render time, and an operator page that pointed at
    # a second deployment could show figures the queue no longer has.
    from .consent import ConsentService, DemandSource
    from .conversations import FileConversationStore, MemoryConversationStore

    walls_path = os.environ.get("DEMAND_QUEUE_WALLS_PATH")
    consents = ConsentService(
        DemandSource(service), service.clock, base_url=base_url,
        conversation_store=(FileConversationStore(walls_path) if walls_path
                            else MemoryConversationStore()))

    worker_stop = _start_worker(service)
    httpd = serve(service, args.host, args.port, consents=consents)
    print(f"listening on http://{args.host}:{args.port}/v1/health")
    try:
        httpd.serve_forever()
    finally:
        worker_stop.set()


def _start_worker(service, interval=1.0):  # pragma: no cover - entry point
    import threading

    from .settlement import SettlementWorker

    worker = SettlementWorker(service)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                worker.run_once()
            except Exception:
                pass
            stop.wait(interval)

    threading.Thread(target=loop, daemon=True).start()
    return stop


if __name__ == "__main__":  # pragma: no cover
    main()
