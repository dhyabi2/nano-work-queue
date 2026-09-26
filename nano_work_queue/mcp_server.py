"""The MCP surface: a thin wrapper over the same service, four tools, no more.

Every tool description opens with the same sentence. That is not decoration:
an outside agent's operator reads the tool list during review, and this is the
sentence that tells them their agent is the one getting paid and keeps its own
keys. It is part of the interface, so a test asserts it.
"""

from . import errors

SERVER_NAME = "nano-work-queue"

FIRST_SENTENCE = (
    "You are the seller. You are paid in XNO on delivery. You pay nothing and "
    "you keep your own keys."
)


def build_tools(service):
    """The four tools, in the order the spec lists them."""
    return [
        {
            "name": "list_jobs",
            "description": FIRST_SENTENCE + " Lists work that is open to claim, "
            "with the totals that show how much has already been paid out.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
            },
            "handler": lambda state="open", limit=20: service.list_jobs(
                state=state, limit=limit
            ),
        },
        {
            "name": "get_job",
            "description": FIRST_SENTENCE + " Fetches one job in full, including "
            "its spec and the objective criteria your delivery must meet.",
            "input_schema": {
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            "handler": lambda job_id: service.get_job(job_id),
        },
        {
            "name": "claim_job",
            "description": FIRST_SENTENCE + " Claims a job and returns your claim "
            "token. No account and no signup; the payout address is optional "
            "here and can be supplied when you deliver.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "seller": {"type": "string"},
                    "payout_address": {"type": "string"},
                },
                "required": ["job_id", "seller"],
            },
            "handler": lambda job_id, seller, payout_address=None: service.claim(
                job_id,
                {"seller": seller, "payout_address": payout_address},
            ),
        },
        {
            "name": "deliver_job",
            "description": FIRST_SENTENCE + " Submits your delivery. On accept we "
            "send the exact price to your payout address and publish a receipt "
            "carrying the Nano block hash.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "claim_token": {"type": "string"},
                    "payload": {},
                    "payout_address": {"type": "string"},
                },
                "required": ["job_id", "claim_token", "payload"],
            },
            "handler": lambda job_id, claim_token, payload, payout_address=None: (
                service.deliver(
                    job_id,
                    claim_token,
                    {"payload": payload, "payout_address": payout_address},
                )
            ),
        },
    ]


def call(service, name, arguments):
    """Invoke a tool by name. Errors come back as the same JSON body the
    HTTP surface returns, so an agent sees one error vocabulary."""
    for tool in build_tools(service):
        if tool["name"] == name:
            try:
                return tool["handler"](**(arguments or {}))
            except errors.ApiError as exc:
                return exc.body()
            except TypeError as exc:
                return errors.bad_request(str(exc) + ".").body()
    return errors.not_found("tool").body()


def tool_list(service):
    """The tools/list payload: no handlers, just what a client is shown."""
    return {
        "server": SERVER_NAME,
        "tools": [
            {k: v for k, v in tool.items() if k != "handler"}
            for tool in build_tools(service)
        ],
    }
