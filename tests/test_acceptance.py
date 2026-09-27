"""The 13 numbered tests from the spec, in order, plus the error paths.

No test opens a socket: the node is always `FakeNode` and the clock is
always `FakeClock`, so the TTL test advances time instead of sleeping.
"""

import unittest

from helpers import BAD_ADDRESS, GOOD_ADDRESS, build, full_cycle, post

from nano_work_queue import amounts, errors, store
from nano_work_queue.address import normalise
from nano_work_queue.node import FakeNode
from nano_work_queue.settlement import SettlementWorker


class Numbered(unittest.TestCase):
    # 1 -------------------------------------------------------------------
    def test_claim_rejects_bad_checksum_address(self):
        """The eddie_researcher test. A one-character-off address must not be
        stored and must not be paid to."""
        service, _node, _clock = build()
        job = post(service)

        with self.assertRaises(errors.ApiError) as ctx:
            service.claim(job["id"], {"seller": "eddie",
                                      "payout_address": BAD_ADDRESS})

        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "invalid_address")
        stored = service.store.get(job["id"])
        self.assertEqual(stored.state, store.OPEN)
        self.assertIsNone(stored.payout_address)
        self.assertIsNone(stored.claim_token_hash)

    # 2 -------------------------------------------------------------------
    def test_claim_accepts_known_good_address(self):
        service, _node, _clock = build()
        job = post(service)

        out = service.claim(job["id"], {"seller": "arion",
                                        "payout_address": GOOD_ADDRESS})

        self.assertEqual(out["state"], store.CLAIMED)
        self.assertTrue(out["claim_token"].startswith("clm_"))
        # Echoed byte for byte as normalised, with no stray whitespace.
        self.assertEqual(out["payout_address"], normalise(GOOD_ADDRESS))
        self.assertEqual(out["payout_address"], GOOD_ADDRESS)

    # 3 -------------------------------------------------------------------
    def test_seller_never_pays(self):
        """Nothing on the seller's path may ask for money or return 402."""
        service, _node, _clock = build()
        job = post(service)
        claim = service.claim(job["id"], {"seller": "arion",
                                          "payout_address": GOOD_ADDRESS})
        delivered = service.deliver(job["id"], claim["claim_token"],
                                    {"payload": [{"vendor": "a"}]})
        seller_surfaces = [
            claim, delivered,
            service.list_jobs(state=None),
            service.get_job(job["id"]),
            service.receipt(job["id"]),
            service.receipt_text(job["id"]),
        ]
        blob = repr(seller_surfaces).lower()
        for forbidden in ("payment_required", "pay to", "deposit", "escrow",
                          "x-402", "402"):
            self.assertNotIn(forbidden, blob, f"seller path mentions {forbidden!r}")

        # And no service method is capable of returning a 402 at all.
        codes = [
            getattr(errors, name)
            for name in dir(errors)
            if callable(getattr(errors, name)) and not name.startswith("_")
        ]
        for factory in codes:
            try:
                err = factory() if factory is not errors.ApiError else None
            except TypeError:
                continue
            if err is not None:
                self.assertNotEqual(getattr(err, "status", None), 402)

    # 4 -------------------------------------------------------------------
    def test_full_happy_path_produces_receipt_with_block_hash(self):
        service, node, _clock = build()
        job_id, _token = full_cycle(service, price_xno="0.05")

        SettlementWorker(service).run_once()
        receipt = service.receipt(job_id)

        self.assertEqual(receipt["state"], store.SETTLED)
        self.assertTrue(store.is_block_hash(receipt["block_hash"]))
        self.assertEqual(len(receipt["block_hash"]), 64)
        self.assertTrue(receipt["confirmed"])
        self.assertEqual(receipt["price_xno"], "0.050000")
        self.assertEqual(receipt["payout_address"], GOOD_ADDRESS)
        # The send carried exactly the price, to exactly that address.
        self.assertEqual(len(node.sends), 1)
        self.assertEqual(node.sends[0]["amount_raw"], amounts.parse_xno("0.05"))
        self.assertEqual(node.sends[0]["to"], GOOD_ADDRESS)

    # 5 -------------------------------------------------------------------
    def test_accept_does_not_block_on_node(self):
        """The node fake sleeps 5s on confirmation; accept must not wait."""
        import time

        service, _node, _clock = build(delay=5.0)
        job = post(service)
        claim = service.claim(job["id"], {"seller": "arion",
                                          "payout_address": GOOD_ADDRESS})
        service.deliver(job["id"], claim["claim_token"],
                        {"payload": [{"vendor": "a"}]})

        started = time.monotonic()
        out = service.accept(job["id"])
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.5, f"accept blocked for {elapsed:.3f}s")
        self.assertEqual(out["state"], store.ACCEPTED)
        self.assertEqual(out["settlement"], "pending")

    # 6 -------------------------------------------------------------------
    def test_settlement_is_idempotent(self):
        service, node, _clock = build()
        job_id, _token = full_cycle(service)
        worker = SettlementWorker(service)

        worker.run_once()
        worker.run_once()          # a second pass over the same job

        self.assertEqual(len(node.sends), 1, "the job was paid twice")
        self.assertEqual(service.store.get(job_id).state, store.SETTLED)

        # Even a direct re-settle adopts the existing block rather than resend.
        worker.settle(service.store.get(job_id))
        self.assertEqual(len(node.sends), 1)

    # 7 -------------------------------------------------------------------
    def test_no_settled_without_block_hash(self):
        service, _node, _clock = build(confirms=False)
        job_id, _token = full_cycle(service)

        SettlementWorker(service, confirm_timeout_s=0).run_once()

        job = service.store.get(job_id)
        self.assertEqual(job.state, store.ACCEPTED)
        self.assertEqual(service.health()["settlement_backlog"], 1)
        self.assertFalse(service.receipt(job_id)["confirmed"])

        # Asserted against the store directly: no code path can set settled
        # with a null block hash, whatever the API does.
        job.block_hash = None
        with self.assertRaises(store.IllegalTransition):
            service.store.transition(job, store.SETTLED)
        with self.assertRaises(store.IllegalTransition):
            service.store.mark_settled(job, None)
        with self.assertRaises(store.IllegalTransition):
            service.store.mark_settled(job, "not-a-hash")

    # 8 -------------------------------------------------------------------
    def test_claim_ttl_returns_job_to_open(self):
        service, _node, clock = build()
        job = post(service, claim_ttl_s=60)
        claim = service.claim(job["id"], {"seller": "arion",
                                          "payout_address": GOOD_ADDRESS})

        clock.advance(61)
        moved = service.store.reap_expired_claims()

        self.assertEqual(moved, [job["id"]])
        self.assertEqual(service.store.get(job["id"]).state, store.OPEN)
        with self.assertRaises(errors.ApiError) as ctx:
            service.deliver(job["id"], claim["claim_token"], {"payload": [{}]})
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "claim_expired")

    # 9 -------------------------------------------------------------------
    def test_rejected_job_reopens_and_reason_is_public(self):
        service, _node, _clock = build()
        job = post(service)
        claim = service.claim(job["id"], {"seller": "arion",
                                          "payout_address": GOOD_ADDRESS})
        service.deliver(job["id"], claim["claim_token"],
                        {"payload": [{"vendor": "a"}]})

        service.reject(job["id"], "Only two vendors, and no monthly price.")

        self.assertEqual(service.store.get(job["id"]).state, store.OPEN)
        receipt = service.receipt(job["id"])
        self.assertEqual(receipt["rejected_reason"],
                         "Only two vendors, and no monthly price.")

    # 10 ------------------------------------------------------------------
    def test_totals_are_the_demand_proof(self):
        service, _node, _clock = build()
        worker = SettlementWorker(service)
        for price in ("0.05", "0.10"):
            full_cycle(service, price_xno=price)
        worker.run_all()

        totals = service.list_jobs(state=None)["totals"]

        self.assertEqual(totals["settled"], 2)
        self.assertEqual(totals["paid_xno_total"], "0.150000")

    # 11 ------------------------------------------------------------------
    def test_claim_token_not_leaked(self):
        service, _node, _clock = build()
        job = post(service)
        claim = service.claim(job["id"], {"seller": "arion",
                                          "payout_address": GOOD_ADDRESS})
        token = claim["claim_token"]
        service.deliver(job["id"], token, {"payload": [{"vendor": "a"}]})
        service.accept(job["id"])
        SettlementWorker(service).run_once()

        log = "\n".join(service.log_lines)
        self.assertNotIn(token, log, "the full claim token reached a log line")
        self.assertIn(token[:8], log, "the 8-char stub should be logged")
        # Stored hashed, never in the clear.
        self.assertEqual(service.store.get(job["id"]).claim_token_hash,
                         store.hash_token(token))
        self.assertNotIn(token, repr(vars(service.store.get(job["id"]))
                                     if hasattr(service.store.get(job["id"]),
                                                "__dict__") else
                                     [getattr(service.store.get(job["id"]), s)
                                      for s in store.Job.__slots__]))
        # And in no public response other than the 201 that issued it.
        for surface in (service.get_job(job["id"]),
                        service.receipt(job["id"]),
                        service.list_jobs(state=None)):
            self.assertNotIn(token, repr(surface))

    # 12 ------------------------------------------------------------------
    def test_insufficient_funds_refuses_to_post(self):
        service, _node, _clock = build(balance_xno="0.01")

        with self.assertRaises(errors.ApiError) as ctx:
            post(service, price_xno="0.05")

        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "insufficient_funds")
        self.assertIn("0.010000", ctx.exception.message)

    # 13 ------------------------------------------------------------------
    def test_price_is_decimal_string_end_to_end(self):
        service, _node, _clock = build()
        job = post(service, price_xno="0.000001")
        self.assertEqual(job["price_xno"], "0.000001")
        self.assertIsInstance(job["price_xno"], str)

        claim = service.claim(job["id"], {"seller": "arion",
                                          "payout_address": GOOD_ADDRESS})
        self.assertEqual(claim["price_xno"], "0.000001")
        service.deliver(job["id"], claim["claim_token"],
                        {"payload": [{"vendor": "a"}]})
        service.accept(job["id"])
        SettlementWorker(service).run_once()
        receipt = service.receipt(job["id"])
        self.assertEqual(receipt["price_xno"], "0.000001")
        self.assertIsInstance(receipt["price_xno"], str)

        # 0.1 + 0.2 drift cannot occur: the sum is integer raw arithmetic.
        service2, _n2, _c2 = build()
        for price in ("0.1", "0.2"):
            full_cycle(service2, price_xno=price)
        SettlementWorker(service2).run_all()
        self.assertEqual(
            service2.list_jobs(state=None)["totals"]["paid_xno_total"],
            "0.300000",
        )


class ErrorPaths(unittest.TestCase):
    def test_unknown_job_is_404(self):
        service, _n, _c = build()
        for call in (
            lambda: service.get_job("job_0000000000000000"),
            lambda: service.receipt("job_0000000000000000"),
            lambda: service.claim("job_0000000000000000", {"seller": "a"}),
        ):
            with self.assertRaises(errors.ApiError) as ctx:
                call()
            self.assertEqual(ctx.exception.status, 404)
            self.assertEqual(ctx.exception.code, "not_found")

    def test_claim_on_a_job_not_open_is_409(self):
        service, _n, _c = build()
        job = post(service)
        service.claim(job["id"], {"seller": "a", "payout_address": GOOD_ADDRESS})
        with self.assertRaises(errors.ApiError) as ctx:
            service.claim(job["id"], {"seller": "b",
                                      "payout_address": GOOD_ADDRESS})
        self.assertEqual(ctx.exception.code, "invalid_state")
        self.assertEqual(ctx.exception.status, 409)

    def test_accept_from_the_wrong_state_is_409(self):
        service, _n, _c = build()
        job = post(service)
        with self.assertRaises(errors.ApiError) as ctx:
            service.accept(job["id"])
        self.assertEqual(ctx.exception.code, "invalid_state")

    def test_payout_address_required_at_deliver_when_absent_at_claim(self):
        service, _n, _c = build()
        job = post(service)
        claim = service.claim(job["id"], {"seller": "arion"})   # no address yet
        self.assertNotIn("payout_address", claim)
        with self.assertRaises(errors.ApiError) as ctx:
            service.deliver(job["id"], claim["claim_token"],
                            {"payload": [{"vendor": "a"}]})
        self.assertEqual(ctx.exception.code, "payout_address_required")
        self.assertEqual(ctx.exception.status, 400)

        # Supplying it at deliver works: claim with no wallet, generate later.
        out = service.deliver(job["id"], claim["claim_token"],
                              {"payload": [{"vendor": "a"}],
                               "payout_address": GOOD_ADDRESS})
        self.assertEqual(out["state"], store.DELIVERED)

    def test_payout_address_conflict_between_claim_and_deliver(self):
        service, _n, _c = build()
        job = post(service)
        claim = service.claim(job["id"], {"seller": "arion",
                                          "payout_address": GOOD_ADDRESS})
        other = "nano_3t6k35gi95xu6tergt6p69ck76ogmitsa8mnijtpxm9fkcm736xtoncuohr3"
        with self.assertRaises(errors.ApiError) as ctx:
            service.deliver(job["id"], claim["claim_token"],
                            {"payload": [{"v": 1}], "payout_address": other})
        self.assertEqual(ctx.exception.code, "payout_address_conflict")
        self.assertEqual(ctx.exception.status, 409)

    def test_missing_or_wrong_claim_token(self):
        service, _n, _c = build()
        job_a = post(service)
        job_b = post(service)
        claim_a = service.claim(job_a["id"], {"seller": "a",
                                              "payout_address": GOOD_ADDRESS})
        service.claim(job_b["id"], {"seller": "b",
                                    "payout_address": GOOD_ADDRESS})

        with self.assertRaises(errors.ApiError) as ctx:
            service.deliver(job_b["id"], "nonsense", {"payload": "x"})
        self.assertEqual(ctx.exception.status, 401)

        # A's valid token used on B's job is forbidden, not merely unauthorized.
        with self.assertRaises(errors.ApiError) as ctx:
            service.deliver(job_b["id"], claim_a["claim_token"],
                            {"payload": [{"v": 1}]})
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.code, "forbidden")

    def test_reject_without_reason(self):
        service, _n, _c = build()
        job = post(service)
        claim = service.claim(job["id"], {"seller": "a",
                                          "payout_address": GOOD_ADDRESS})
        service.deliver(job["id"], claim["claim_token"], {"payload": [{"v": 1}]})
        for bad in (None, "", "   ", 5):
            with self.assertRaises(errors.ApiError) as ctx:
                service.reject(job["id"], bad)
            self.assertEqual(ctx.exception.code, "reason_required")

    def test_price_out_of_range_and_non_decimal(self):
        service, _n, _c = build()
        for bad in ("0.0000001", "101", "-1", "abc", "1e-3", 0.05, None):
            with self.assertRaises(errors.ApiError) as ctx:
                post(service, price_xno=bad)
            self.assertEqual(ctx.exception.code, "price_out_of_range", bad)

    def test_invalid_payload_for_each_deliverable(self):
        cases = [("json", "a string"), ("text", 5), ("url", "http://insecure"),
                 ("text", "   "), ("json", None)]
        for deliverable, payload in cases:
            service, _n, _c = build()
            job = post(service, deliverable=deliverable)
            claim = service.claim(job["id"], {"seller": "a",
                                              "payout_address": GOOD_ADDRESS})
            with self.assertRaises(errors.ApiError) as ctx:
                service.deliver(job["id"], claim["claim_token"],
                                {"payload": payload})
            self.assertEqual(ctx.exception.code, "invalid_payload",
                             (deliverable, payload))

    def test_illegal_transitions_are_refused_by_the_store(self):
        service, _n, _c = build()
        job = post(service)
        j = service.store.get(job["id"])
        for target in (store.DELIVERED, store.ACCEPTED, store.SETTLED,
                       store.REJECTED):
            with self.assertRaises(store.IllegalTransition):
                service.store.transition(j, target)

    def test_settlement_survives_an_unreachable_node(self):
        service, node, _c = build()
        job_id, _t = full_cycle(service)
        node.up = False

        result = SettlementWorker(service).run_once()

        self.assertIsNone(result)
        self.assertEqual(service.store.get(job_id).state, store.ACCEPTED)
        self.assertEqual(service.store.get(job_id).settle_attempts, 1)
        self.assertEqual(service.health()["node"], "unreachable")
        self.assertFalse(service.health()["ok"])

    def test_health_reports_funds_and_backlog(self):
        service, _n, _c = build(balance_xno="2")
        post(service)
        health = service.health()
        self.assertEqual(health["funded_xno"], "2.000000")
        self.assertEqual(health["open_jobs"], 1)
        self.assertEqual(health["settlement_backlog"], 0)
        self.assertEqual(health["node"], "reachable")

    def test_amounts_refuse_floats_and_keep_thirty_places(self):
        self.assertEqual(amounts.parse_xno("1"), amounts.RAW_PER_XNO)
        with self.assertRaises(amounts.AmountError):
            amounts.parse_xno(1.0)
        with self.assertRaises(amounts.AmountError):
            amounts.parse_xno("0." + "0" * 30 + "1")     # 31 dp
        # One raw is representable and survives the round trip.
        one_raw = amounts.parse_xno("0." + "0" * 29 + "1")
        self.assertEqual(one_raw, 1)
        self.assertEqual(amounts.format_xno(1), "0." + "0" * 29 + "1")

    def test_amounts_do_not_round_at_the_decimal_context_precision(self):
        """An exact XNO amount can need 31 significant digits; 28 is not enough.

        Scaling with ``Decimal.scaleb`` ran under the default 28-digit
        context and rounded anything longer to fit, so these amounts came
        back as a different amount than was sent. Every case below is inside
        the accepted price range, so none of them was caught by a range check.
        """
        one_over_one_xno = "1." + "0" * 29 + "1"          # 10**30 + 1 raw
        self.assertEqual(
            amounts.parse_xno(one_over_one_xno), amounts.RAW_PER_XNO + 1
        )
        # The worst shape: rounding *up*, so the rail reads a larger amount
        # than the caller wrote - here all the way to a round 100 XNO.
        just_under_100 = "99." + "9" * 30
        self.assertEqual(
            amounts.parse_xno(just_under_100), 100 * amounts.RAW_PER_XNO - 1
        )
        self.assertLess(
            amounts.parse_xno(just_under_100), 100 * amounts.RAW_PER_XNO
        )
        self.assertEqual(
            amounts.parse_xno("12.345678901234567890123456789012"),
            12345678901234567890123456789012,
        )

    def test_amounts_round_trip_at_full_precision(self):
        """format_xno then parse_xno returns the same raw, to the last digit."""
        for raw in (
            amounts.RAW_PER_XNO + 1,
            amounts.RAW_PER_XNO + 10**24 + 7,
            42 * amounts.RAW_PER_XNO + 123456789012345678901234567890,
            100 * amounts.RAW_PER_XNO - 1,
            1,
        ):
            with self.subTest(raw=raw):
                self.assertEqual(amounts.parse_xno(amounts.format_xno(raw)), raw)

    def test_amounts_still_refuse_what_is_not_a_decimal_string(self):
        """The exact scaling must not have widened what parse_xno accepts."""
        for bad in (".", "", "   ", "+1", "-1", "1e3", "Inf", "NaN", "1.2.3",
                    "0x10", "1 000", None, 1, True, 1.0):
            with self.subTest(bad=bad):
                with self.assertRaises(amounts.AmountError):
                    amounts.parse_xno(bad)

    def test_listing_paginates_and_reports_totals(self):
        service, _n, _c = build()
        ids = [post(service)["id"] for _ in range(3)]
        first = service.list_jobs(limit=2)
        self.assertEqual(len(first["jobs"]), 2)
        self.assertIsNotNone(first["next_cursor"])
        second = service.list_jobs(limit=2, cursor=first["next_cursor"])
        self.assertEqual(len(second["jobs"]), 1)
        self.assertEqual(
            {j["id"] for j in first["jobs"] + second["jobs"]}, set(ids)
        )

    def test_job_detail_never_exposes_the_payout_address(self):
        service, _n, _c = build()
        job = post(service)
        service.claim(job["id"], {"seller": "a", "payout_address": GOOD_ADDRESS})
        self.assertNotIn("payout_address", service.get_job(job["id"]))
        # It IS on the settled receipt, by design, so a third party can check.
        job_id, _t = full_cycle(service)
        SettlementWorker(service).run_once()
        self.assertEqual(service.receipt(job_id)["payout_address"], GOOD_ADDRESS)
        self.assertIn("verify", service.receipt(job_id))

    def test_fake_node_is_the_only_network_seam(self):
        """A node that is never called cannot have opened a socket."""
        service, node, _c = build()
        self.assertIsInstance(node, FakeNode)
        post(service)
        self.assertEqual(node.sends, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
