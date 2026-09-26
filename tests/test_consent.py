"""The 13 numbered tests from
specs/unstuck/agent-tool-operator-consent-page.md, in order, plus the error
table and the rules stated in prose beside them.

No test needs a Nano node: the queue underneath runs on `FakeNode` and a
`FakeClock`, so a 90-day expiry resolves without waiting for one.
"""

import unittest
from html.parser import HTMLParser

from helpers import build, full_cycle

from nano_work_queue import conversations, errors
from nano_work_queue.consent import (
    ConsentService, DemandSource, DemandUnavailable, EXPIRY_S, RECEIVE_ONLY,
)
from nano_work_queue.consent_page import render
from nano_work_queue.settlement import SettlementWorker

BASE = "https://queue.example"


class Forms(HTMLParser):
    """Enough of an HTML parser to count what the spec forbids."""

    def __init__(self):
        super().__init__()
        self.inputs = []
        self.buttons = []
        self.forms = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.forms += 1
        elif tag == "input":
            self.inputs.append(attrs)
        elif tag == "button":
            self.buttons.append(attrs)


def parse(html):
    p = Forms()
    p.feed(html)
    return p


def make(settled_jobs=1, price="0.05"):
    """A queue with `settled_jobs` paid jobs, plus a consent service on it."""
    service, node, clock = build()
    worker = SettlementWorker(service)
    for _ in range(settled_jobs):
        full_cycle(service, price_xno=price)
        worker.run_all()
    store = conversations.MemoryConversationStore()
    consents = ConsentService(DemandSource(service), clock, base_url=BASE,
                              conversation_store=store)
    return service, consents, store, clock, worker


def page(consents, consent_id):
    consent, demand = consents.view(consent_id)
    return render(consent, demand, consents.base_url)


class Test01ZeroSettledRendersNoForm(unittest.TestCase):
    """1. test_zero_settled_jobs_renders_no_form -- the Pattern 6 test."""

    def test_empty_queue_refuses_to_render_a_form(self):
        service, node, clock = build()
        consents = ConsentService(DemandSource(service), clock, base_url=BASE)
        created_id = self._create_against_a_paid_queue_then_empty(consents,
                                                                  service)

        with self.assertRaises(errors.ApiError) as caught:
            consents.view(created_id)
        exc = caught.exception
        self.assertEqual((exc.status, exc.code), (409, "no_demand_yet"))
        self.assertIn("No jobs have been paid yet.", exc.message)

        from nano_work_queue.consent_page import render_error
        html = render_error(exc)
        self.assertIn("No jobs have been paid yet.", html)
        self.assertNotIn("<form", html)
        self.assertNotIn("<input", html)
        self.assertNotIn("<button", html)

    def _create_against_a_paid_queue_then_empty(self, consents, service):
        """Create while a job is settled, then take the settlement away.

        Creating needs demand too, so the page cannot be reached by creating
        first and hoping. This proves the check is at *render* time, not only
        at create time.
        """
        worker = SettlementWorker(service)
        full_cycle(service)
        worker.run_all()
        consent_id = consents.create({"agent": "DeskCrew"})["consent_id"]
        for job in service.store.all_jobs():
            job.state = "open"
            job.block_hash = None
            job.settled_at = None
        return consent_id

    def test_a_signature_cannot_be_taken_either(self):
        service, consents, store, clock, worker = make(settled_jobs=1)
        consent_id = consents.create({"agent": "DeskCrew"})["consent_id"]
        for job in service.store.all_jobs():
            job.state = "open"
            job.block_hash = None
        with self.assertRaises(errors.ApiError) as caught:
            consents.sign(consent_id, {"signed_by": "R. Okonkwo",
                                       "decision": "approve"})
        self.assertEqual(caught.exception.code, "no_demand_yet")

    def test_a_consent_may_be_issued_but_never_shown_without_demand(self):
        """Issuing is allowed; showing and signing are not.

        The gate is on the page and the signature, which is where the harm
        is: a consent record with no page behind it asks nobody for anything.
        """
        service, node, clock = build()
        consents = ConsentService(DemandSource(service), clock, base_url=BASE)
        created = consents.create({"agent": "DeskCrew"})
        self.assertEqual(created["state"], "pending")
        # The create body reports the queue truthfully: zero settled is a real
        # reading. `null` is kept for "we could not read the queue at all",
        # which is a different thing and must not look like a zero.
        self.assertEqual(created["demand"],
                         {"settled": 0, "paid_xno_total": "0.000000",
                          "open": 0})
        with self.assertRaises(errors.ApiError) as caught:
            consents.view(created["consent_id"])
        self.assertEqual(caught.exception.code, "no_demand_yet")

    def test_an_unreadable_queue_reports_null_not_zero(self):
        service, node, clock = build()
        consents = ConsentService(DemandSource(service), clock, base_url=BASE)

        class Unreachable(DemandSource):
            def snapshot(self):
                raise DemandUnavailable("queue is not answering")

        consents.demand = Unreachable(service)
        self.assertIsNone(consents.create({"agent": "DeskCrew"})["demand"])

    def test_revoking_works_even_with_no_demand(self):
        """An operator must always be able to withdraw, outage or not."""
        service, node, clock = build()
        consents = ConsentService(DemandSource(service), clock, base_url=BASE)
        created = consents.create({"agent": "DeskCrew"})
        self.assertEqual(consents.revoke(created["consent_id"])["state"],
                         "revoked")


class Test02DemandIsLive(unittest.TestCase):
    """2. test_demand_figures_are_live_not_cached."""

    def test_settling_another_job_moves_the_page(self):
        service, consents, store, clock, worker = make(settled_jobs=1,
                                                       price="0.05")
        consent_id = consents.create({"agent": "DeskCrew"})["consent_id"]
        first = page(consents, consent_id)
        self.assertIn("1 jobs settled, 0.050000 XNO paid.", first)

        full_cycle(service, price_xno="0.10")
        worker.run_all()
        second = page(consents, consent_id)
        self.assertIn("2 jobs settled, 0.150000 XNO paid.", second)
        self.assertNotIn("1 jobs settled", second)

    def test_the_json_body_moves_too(self):
        service, consents, store, clock, worker = make(settled_jobs=1)
        consent_id = consents.create({"agent": "DeskCrew"})["consent_id"]
        consent, demand = consents.view(consent_id)
        self.assertEqual(consents.as_json(consent)["demand"]["settled"], 1)
        full_cycle(service, price_xno="0.10")
        worker.run_all()
        self.assertEqual(consents.as_json(consent)["demand"]["settled"], 2)

    def test_the_open_count_is_live_as_well(self):
        service, consents, store, clock, worker = make(settled_jobs=1)
        consent_id = consents.create({"agent": "DeskCrew"})["consent_id"]
        self.assertIn("Open right now: 0 jobs", page(consents, consent_id))
        from helpers import post
        post(service)
        self.assertIn("Open right now: 1 jobs", page(consents, consent_id))


class Test03DefaultScope(unittest.TestCase):
    """3. test_default_scope_is_receive_only."""

    def test_no_scope_given(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        self.assertEqual(created["scope"], RECEIVE_ONLY)
        self.assertIsNone(created["max_send_xno"])
        html = page(consents, created["consent_id"])
        self.assertIn("It cannot send.", html)
        self.assertNotIn("may send up to", html)


class Test04ReceiveOnlyHasNoSendLimit(unittest.TestCase):
    """4. test_receive_only_cannot_carry_a_send_limit."""

    def test_scope_conflict(self):
        service, consents, store, clock, worker = make()
        with self.assertRaises(errors.ApiError) as caught:
            consents.create({"agent": "DeskCrew", "scope": "receive_only",
                             "max_send_xno": "1.0"})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (400, "scope_conflict"))

    def test_receive_and_send_needs_a_ceiling(self):
        service, consents, store, clock, worker = make()
        with self.assertRaises(errors.ApiError) as caught:
            consents.create({"agent": "DeskCrew",
                             "scope": "receive_and_send"})
        self.assertEqual(caught.exception.field, "max_send_xno")

    def test_a_send_ceiling_is_rendered_exactly(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew",
                                   "scope": "receive_and_send",
                                   "max_send_xno": "0.1"})
        # Exact decimal, never a float: "0.1" renders canonically.
        self.assertEqual(created["max_send_xno"], "0.100000")
        self.assertIn("may send up to 0.100000 XNO",
                      page(consents, created["consent_id"]))


class Test05FormHasExactlyTwoInputs(unittest.TestCase):
    """5. test_form_has_exactly_two_inputs."""

    FORBIDDEN = ("email", "password", "card", "account", "address", "tel")

    def test_two_inputs_two_buttons_and_nothing_else(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        parsed = parse(page(consents, created["consent_id"]))

        self.assertEqual(parsed.forms, 1)
        self.assertEqual(len(parsed.inputs), 2, parsed.inputs)
        self.assertEqual(len(parsed.buttons), 2, parsed.buttons)
        names = sorted(i.get("name") for i in parsed.inputs)
        self.assertEqual(names, ["reason", "signed_by"])
        self.assertEqual(sorted(b.get("value") for b in parsed.buttons),
                         ["approve", "decline"])

    def test_no_forbidden_field_and_no_hidden_token(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        parsed = parse(page(consents, created["consent_id"]))
        for field in parsed.inputs:
            name = (field.get("name") or "").lower()
            kind = (field.get("type") or "").lower()
            for banned in self.FORBIDDEN:
                self.assertNotIn(banned, name, field)
                self.assertNotIn(banned, kind, field)
            self.assertNotEqual(kind, "hidden", field)
            self.assertEqual(kind, "text", field)


class Test06RendersWithoutJavaScript(unittest.TestCase):
    """6. test_page_renders_without_javascript."""

    def test_stripping_every_script_changes_nothing(self):
        import re

        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        html = page(consents, created["consent_id"])
        stripped = re.sub(r"<script.*?</script>", "", html,
                          flags=re.S | re.I)
        self.assertEqual(stripped, html, "there is no script to strip")

        for required in ("1 jobs settled", "0.050000 XNO paid",
                         "It cannot send.", "No custody.",
                         "Revocable at any time", "<form", "signed_by",
                         "Approve", "Decline"):
            self.assertIn(required, stripped)

    def test_the_buttons_carry_the_decision_so_no_script_is_needed(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        parsed = parse(page(consents, created["consent_id"]))
        self.assertTrue(all(b.get("name") == "decision"
                            for b in parsed.buttons), parsed.buttons)


class Test07ExpiryIsNinetyDays(unittest.TestCase):
    """7. test_expiry_is_ninety_days_and_not_settable."""

    def test_caller_supplied_expiry_is_rejected(self):
        service, consents, store, clock, worker = make()
        with self.assertRaises(errors.ApiError) as caught:
            consents.create({"agent": "DeskCrew",
                             "expires_at": "2099-01-01T00:00:00Z"})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (400, "expiry_not_settable"))

    def test_expiry_is_exactly_ninety_days_after_creation(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        consent = consents.get(created["consent_id"])
        self.assertEqual(consent.expires_at,
                         clock.iso(clock.now() + EXPIRY_S))
        self.assertEqual(round(consent.expires_epoch - clock.now()), EXPIRY_S)

    def test_a_pending_consent_expires_on_its_own(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        clock.advance(EXPIRY_S + 1)
        self.assertEqual(consents.get(created["consent_id"]).state, "expired")
        html = page(consents, created["consent_id"])
        self.assertNotIn("<form", html)
        self.assertIn("expired", html)
        with self.assertRaises(errors.ApiError) as caught:
            consents.sign(created["consent_id"], {"signed_by": "R",
                                                  "decision": "approve"})
        self.assertEqual(caught.exception.code, "invalid_state")


class Test08DeclineIsEvidence(unittest.TestCase):
    """8. test_decline_is_recorded_as_evidence."""

    def test_a_reason_matching_a_cluster(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        result = consents.sign(created["consent_id"], {
            "signed_by": "R. Okonkwo", "decision": "decline",
            "reason": "our policy needs a named counterparty"})

        self.assertEqual(result["state"], "declined")
        self.assertEqual(result["decline_reason"],
                         "our policy needs a named counterparty")
        walls = store.walls()
        self.assertEqual(len(walls), 1)
        self.assertEqual(walls[0]["kind"], "walls")
        self.assertEqual(walls[0]["cluster"], "counterparty-identity")
        self.assertEqual(walls[0]["text"],
                         "our policy needs a named counterparty")
        self.assertEqual(walls[0]["meta"]["agent"], "DeskCrew")

    def test_a_reason_matching_no_cluster_is_kept_unclustered(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        consents.sign(created["consent_id"], {
            "signed_by": "R. Okonkwo", "decision": "decline",
            "reason": "we already built this in house last quarter"})
        self.assertEqual(store.walls()[0]["cluster"], "unclustered")

    def test_a_decline_is_a_success_not_an_error(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        result = consents.sign(created["consent_id"], {
            "signed_by": "R. Okonkwo", "decision": "decline",
            "reason": "nobody here is paying for agent work yet"})
        self.assertEqual(result["state"], "declined")
        self.assertEqual(store.walls()[0]["cluster"],
                         "demand-first-operator-gate")

    def test_the_reason_survives_a_restart_when_the_store_is_a_file(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "walls.jsonl")
            service, node, clock = build()
            worker = SettlementWorker(service)
            full_cycle(service)
            worker.run_all()
            consents = ConsentService(
                DemandSource(service), clock, base_url=BASE,
                conversation_store=conversations.FileConversationStore(path))
            created = consents.create({"agent": "DeskCrew"})
            consents.sign(created["consent_id"], {
                "signed_by": "R", "decision": "decline",
                "reason": "our legal team needs a security review first"})
            reloaded = conversations.FileConversationStore(path)
            self.assertEqual(len(reloaded.walls()), 1)
            self.assertEqual(reloaded.walls()[0]["cluster"],
                             "human-must-approve")


class Test09DeclineNeedsAReason(unittest.TestCase):
    """9. test_decline_without_reason_is_rejected."""

    def test_missing_and_blank_reasons(self):
        service, consents, store, clock, worker = make()
        for reason in (None, "", "   "):
            created = consents.create({"agent": "DeskCrew"})
            body = {"signed_by": "R. Okonkwo", "decision": "decline"}
            if reason is not None:
                body["reason"] = reason
            with self.assertRaises(errors.ApiError) as caught:
                consents.sign(created["consent_id"], body)
            self.assertEqual((caught.exception.status, caught.exception.code),
                             (400, "reason_required"))
            # and nothing was recorded or changed
            self.assertEqual(store.walls(), [])
            self.assertEqual(consents.get(created["consent_id"]).state,
                             "pending")


class Test10RevokeIsIdempotent(unittest.TestCase):
    """10. test_revoke_is_idempotent_and_needs_no_auth."""

    def test_two_revokes_then_a_refused_signature(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        first = consents.revoke(created["consent_id"])
        second = consents.revoke(created["consent_id"])
        self.assertEqual(first["state"], "revoked")
        self.assertEqual(second["state"], "revoked")

        with self.assertRaises(errors.ApiError) as caught:
            consents.sign(created["consent_id"], {"signed_by": "R",
                                                  "decision": "approve"})
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (409, "invalid_state"))

    def test_a_signed_consent_can_still_be_revoked(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        consents.sign(created["consent_id"], {"signed_by": "R",
                                              "decision": "approve"})
        self.assertEqual(consents.revoke(created["consent_id"])["state"],
                         "revoked")


class Test11SignedPageShowsNoForm(unittest.TestCase):
    """11. test_signed_page_shows_no_form."""

    def test_signer_and_date_replace_the_form(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        consents.sign(created["consent_id"], {"signed_by": "R. Okonkwo",
                                              "decision": "approve"})
        html = page(consents, created["consent_id"])
        self.assertIn("Approved by R. Okonkwo", html)
        self.assertIn(consents.get(created["consent_id"]).signed_at, html)
        self.assertNotIn("<form", html)
        self.assertEqual(parse(html).buttons, [])

    def test_a_declined_page_shows_no_form_either(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        consents.sign(created["consent_id"], {"signed_by": "R",
                                              "decision": "decline",
                                              "reason": "not right now"})
        html = page(consents, created["consent_id"])
        self.assertNotIn("<form", html)
        self.assertIn("Declined by R", html)

    def test_signing_twice_is_refused(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        consents.sign(created["consent_id"], {"signed_by": "R",
                                              "decision": "approve"})
        with self.assertRaises(errors.ApiError) as caught:
            consents.sign(created["consent_id"], {"signed_by": "R",
                                                  "decision": "approve"})
        self.assertEqual(caught.exception.code, "invalid_state")


class Test12DemandUnavailableIsHard(unittest.TestCase):
    """12. test_demand_unavailable_is_a_hard_failure."""

    def test_unreachable_queue_is_503_with_no_form(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})

        class Unreachable(DemandSource):
            def snapshot(self):
                raise DemandUnavailable("queue is not answering")

        consents.demand = Unreachable(service)
        with self.assertRaises(errors.ApiError) as caught:
            consents.view(created["consent_id"])
        exc = caught.exception
        self.assertEqual((exc.status, exc.code), (503, "demand_unavailable"))

        from nano_work_queue.consent_page import render_error
        html = render_error(exc)
        self.assertNotIn("<form", html)
        self.assertNotIn("<button", html)

    def test_a_signature_is_refused_while_demand_cannot_be_read(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})

        class Unreachable(DemandSource):
            def snapshot(self):
                raise DemandUnavailable("queue is not answering")

        consents.demand = Unreachable(service)
        with self.assertRaises(errors.ApiError) as caught:
            consents.sign(created["consent_id"], {"signed_by": "R",
                                                  "decision": "approve"})
        self.assertEqual(caught.exception.code, "demand_unavailable")
        self.assertEqual(consents.get(created["consent_id"]).state, "pending")


class Test13NoCustodyLanguage(unittest.TestCase):
    """13. test_no_custody_language_is_present_verbatim."""

    def test_the_strings_and_a_resolving_receipt_url(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        html = page(consents, created["consent_id"])
        self.assertIn("We never hold", html)
        self.assertIn("private key", html)

        # The receipt link on the page is the real one for the settled job,
        # and it resolves without auth. (The HTTP suite fetches it for real.)
        settled = [j for j in service.store.all_jobs() if j.state == "settled"]
        url = f"{service.base_url}/v1/receipts/{settled[0].id}"
        self.assertIn(url, html)
        receipt = service.receipt(settled[0].id)
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(len(receipt["block_hash"]), 64)


class TestErrorTable(unittest.TestCase):
    """Every row of the consent spec's error table."""

    def setUp(self):
        self.service, self.consents, self.store, self.clock, self.worker = make()

    def _raises(self, status, code, fn, *a, **kw):
        with self.assertRaises(errors.ApiError) as caught:
            fn(*a, **kw)
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (status, code))

    def test_unknown_consent_id(self):
        self._raises(404, "not_found", self.consents.view,
                     "csn_ffffffffffffffff")
        self._raises(404, "not_found", self.consents.revoke,
                     "csn_ffffffffffffffff")

    def test_invalid_signer(self):
        created = self.consents.create({"agent": "DeskCrew"})
        for bad in ("", "   ", "x" * 121, None, 7):
            self._raises(400, "invalid_signer", self.consents.sign,
                         created["consent_id"],
                         {"signed_by": bad, "decision": "approve"})

    def test_a_120_character_name_is_accepted(self):
        created = self.consents.create({"agent": "DeskCrew"})
        result = self.consents.sign(created["consent_id"],
                                    {"signed_by": "x" * 120,
                                     "decision": "approve"})
        self.assertEqual(result["state"], "signed")

    def test_unknown_decision(self):
        created = self.consents.create({"agent": "DeskCrew"})
        self._raises(400, "bad_request", self.consents.sign,
                     created["consent_id"],
                     {"signed_by": "R", "decision": "maybe"})

    def test_agent_bounds(self):
        self._raises(400, "bad_request", self.consents.create, {"agent": ""})
        self._raises(400, "bad_request", self.consents.create,
                     {"agent": "x" * 81})


class TestEscaping(unittest.TestCase):
    def test_an_agent_name_cannot_inject_markup(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": '<script>alert(1)</script>'})
        html = page(consents, created["consent_id"])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_a_decline_reason_cannot_inject_markup(self):
        service, consents, store, clock, worker = make()
        created = consents.create({"agent": "DeskCrew"})
        consents.sign(created["consent_id"], {
            "signed_by": '"><form action=evil>', "decision": "decline",
            "reason": "<b>no</b>"})
        html = page(consents, created["consent_id"])
        self.assertNotIn("<form", html)
        self.assertIn("&lt;b&gt;no&lt;/b&gt;", html)


if __name__ == "__main__":
    unittest.main()
