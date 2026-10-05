"""The error vocabulary, one place, so a status and a code never drift apart.

`message` is written for a non-human reader: what failed, that nothing was
stored, and which call to make next.
"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, field=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field

    def body(self) -> dict:
        out = {"error": self.code, "message": self.message}
        if self.field:
            out["field"] = self.field
        return out


def not_found(what="job"):
    where = "GET /v1/jobs" if what == "job" else "the link you were sent"
    return ApiError(404, "not_found", f"No such {what}. Check the id from {where}.")


def invalid_state(state, wanted):
    return ApiError(
        409,
        "invalid_state",
        f"Job is {state!r}; this call needs {wanted!r}. Nothing changed. "
        "Re-read GET /v1/jobs/{id} for the current state.",
    )


def invalid_address(detail="payout_address checksum does not match"):
    return ApiError(
        400,
        "invalid_address",
        f"{detail}. Nothing was stored. Generate an address with the wallet "
        "tool and retry the claim.",
        field="payout_address",
    )


def payout_address_required():
    return ApiError(
        400,
        "payout_address_required",
        "No payout address was given at claim time, so deliver must carry one. "
        "Nothing was stored. Retry deliver with payout_address set.",
        field="payout_address",
    )


def payout_address_conflict():
    return ApiError(
        409,
        "payout_address_conflict",
        "The payout address given at deliver differs from the one given at "
        "claim. Nothing changed. Resend deliver with the claimed address, or "
        "release the claim and claim again.",
        field="payout_address",
    )


# Every message in this module ends with the one thing the caller should do
# next, because an agent's only recovery path is the text it reads back. The
# remedy therefore has to match the credential that is missing: `what` was
# parameterised but the remedy sentence was not, so a buyer whose
# `X-Buyer-Token` was missing or wrong was told to "send it as
# 'Authorization: Bearer <claim_token>' from the claim response" - the seller's
# header, the seller's credential, and a claim response a buyer never has.
# Following it fails again, and posting work is the first call a buyer makes.
_REMEDIES = {
    "claim token": (
        "Send it as 'Authorization: Bearer <claim_token>' from the claim "
        "response."
    ),
    "buyer token": (
        "Send it as the 'X-Buyer-Token' header, holding the operator's "
        "DEMAND_QUEUE_BUYER_TOKEN. A claim token will not do: this is the "
        "buyer's own credential and no claim response carries it."
    ),
}


def unauthorized(what="claim token"):
    remedy = _REMEDIES.get(what, _REMEDIES["claim token"])
    return ApiError(
        401,
        "unauthorized",
        f"Missing or invalid {what}. Nothing changed. {remedy}",
    )


def forbidden():
    return ApiError(
        403,
        "forbidden",
        "That claim token belongs to a different job. Nothing changed. "
        "Use the token issued for this job id.",
    )


def claim_expired():
    return ApiError(
        409,
        "claim_expired",
        "The claim TTL elapsed and the job returned to 'open'. Nothing was "
        "stored. Claim it again with POST /v1/jobs/{id}/claim.",
    )


def price_out_of_range(detail):
    return ApiError(
        400,
        "price_out_of_range",
        f"{detail} Nothing was stored. price_xno is a decimal string between "
        '"0.000001" and "100".',
        field="price_xno",
    )


def insufficient_funds(available: str):
    return ApiError(
        409,
        "insufficient_funds",
        f"The funded account holds {available} XNO, which cannot cover this "
        "job. Nothing was stored. Post a lower price_xno or fund the account. "
        "Refusing to post is deliberate: we never owe a delivery we cannot pay.",
        field="price_xno",
    )


def reason_required():
    return ApiError(
        400,
        "reason_required",
        "A rejection must carry a reason, and it is published on the receipt. "
        "Nothing changed. Resend with {\"reason\": \"...\"}.",
        field="reason",
    )


def invalid_payload(detail):
    return ApiError(
        400,
        "invalid_payload",
        f"{detail} Nothing was stored. Match the job's 'deliverable' field.",
        field="payload",
    )


def payload_too_large():
    return ApiError(
        413,
        "payload_too_large",
        "The request body is over 64 KiB. Nothing was stored. Deliver a URL "
        "instead of an inline payload.",
    )


def rate_limited():
    return ApiError(
        429,
        "rate_limited",
        "More than 30 requests in a minute from this address. Nothing "
        "changed. Wait and retry.",
    )


def bad_request(detail, field=None):
    return ApiError(400, "bad_request", f"{detail} Nothing was stored.", field=field)


# -- operator consent ---------------------------------------------------------
#
# `no_demand_yet` and `demand_unavailable` are both deliberately hard failures
# rather than a page rendered without figures. A consent page with no payer on
# it proves in one screen that there is nobody paying, and asking for a
# signature anyway is the exact failure the consent spec exists to prevent.

NO_DEMAND_SENTENCE = "No jobs have been paid yet."


def no_demand_yet():
    return ApiError(
        409,
        "no_demand_yet",
        f"{NO_DEMAND_SENTENCE} There is no consent page to show and no "
        "signature to take until the queue has settled one. Post a job, have "
        "it delivered and accepted, and this page will carry the receipt.",
    )


def demand_unavailable():
    return ApiError(
        503,
        "demand_unavailable",
        "The work queue could not be read, so the demand figures on this page "
        "would be missing or stale. Nothing was rendered and nothing changed. "
        "Retry once GET /v1/health is ok.",
    )


def invalid_consent_state(state):
    return ApiError(
        409,
        "invalid_state",
        f"This consent is {state!r}; only a 'pending' consent can be signed "
        "or declined. Nothing changed. A changed scope is a new page and a "
        "new signature.",
    )


def invalid_signer(maximum):
    return ApiError(
        400,
        "invalid_signer",
        f"signed_by must be the signer's name, 1..{maximum} characters. "
        "Nothing was stored.",
        field="signed_by",
    )


def reason_required_for_decline():
    return ApiError(
        400,
        "reason_required",
        "A decline must carry a reason. Nothing changed. The reason is the "
        "single most useful thing this page collects, which is why it is not "
        "optional.",
        field="reason",
    )


def scope_conflict():
    return ApiError(
        400,
        "scope_conflict",
        "A receive_only consent cannot carry max_send_xno: it grants no send "
        "authority at all, so a ceiling on it would be meaningless. Nothing "
        "was stored. Use scope 'receive_and_send' if a send limit is meant.",
        field="max_send_xno",
    )


def expiry_not_settable():
    return ApiError(
        400,
        "expiry_not_settable",
        "expires_at is computed server-side as 90 days from creation and "
        "cannot be supplied. Nothing was stored. Resend without it.",
        field="expires_at",
    )
