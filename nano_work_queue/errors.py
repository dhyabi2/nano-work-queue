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
    return ApiError(404, "not_found", f"No such {what}. Check the id from GET /v1/jobs.")


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


def unauthorized(what="claim token"):
    return ApiError(
        401,
        "unauthorized",
        f"Missing or invalid {what}. Nothing changed. Send it as "
        "'Authorization: Bearer <claim_token>' from the claim response.",
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
