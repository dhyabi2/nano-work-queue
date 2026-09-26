"""The A2A agent card: one skill, and its examples are a real exchange."""

from .mcp_server import FIRST_SENTENCE
from .service import SELLER_INSTRUCTIONS

GOOD_ADDRESS_EXAMPLE = (
    "nano_1111111111111111111111111111111111111111111111111111hifc8npp"
)

SKILL_ID = "nano.work.sell"


def agent_card(base_url="https://queue.example"):
    base = base_url.rstrip("/")
    return {
        "name": "Nano work queue",
        "description": FIRST_SENTENCE,
        "url": base,
        "skills": [
            {
                "id": SKILL_ID,
                "name": "Sell work, get paid in XNO",
                "description": FIRST_SENTENCE,
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "examples": [
                    {
                        "title": "Claim, deliver, get a receipt",
                        "exchange": [
                            {
                                "request": {
                                    "method": "POST",
                                    "url": f"{base}/v1/jobs/job_4f2a91c0d3b58e17/claim",
                                    "body": {
                                        "seller": "arion",
                                        "payout_address": GOOD_ADDRESS_EXAMPLE,
                                    },
                                },
                                "response": {
                                    "status": 201,
                                    "body": {
                                        "job_id": "job_4f2a91c0d3b58e17",
                                        "claim_token": "clm_9b1c77e0f4a2d85639ce0117a4bb2d6e",
                                        "state": "claimed",
                                        "price_xno": "0.050000",
                                        "payout_address": GOOD_ADDRESS_EXAMPLE,
                                        "instructions": SELLER_INSTRUCTIONS,
                                    },
                                },
                            },
                            {
                                "request": {
                                    "method": "POST",
                                    "url": f"{base}/v1/jobs/job_4f2a91c0d3b58e17/deliver",
                                    "headers": {
                                        "Authorization": "Bearer clm_9b1c77e0f4a2d85639ce0117a4bb2d6e"
                                    },
                                    "body": {
                                        "payload": [
                                            {"vendor": "acme", "monthly": 49}
                                        ]
                                    },
                                },
                                "response": {
                                    "status": 200,
                                    "body": {
                                        "job_id": "job_4f2a91c0d3b58e17",
                                        "state": "delivered",
                                        "receipt_url": f"{base}/v1/receipts/job_4f2a91c0d3b58e17",
                                    },
                                },
                            },
                            {
                                "request": {
                                    "method": "GET",
                                    "url": f"{base}/v1/receipts/job_4f2a91c0d3b58e17",
                                },
                                "response": {
                                    "status": 200,
                                    "body": {
                                        "state": "settled",
                                        "price_xno": "0.050000",
                                        "payout_address": GOOD_ADDRESS_EXAMPLE,
                                        "block_hash": "B1B2C3D4E5F60718293A4B5C6D7E8F901A2B3C4D5E6F708192A3B4C5D6E7F801",
                                        "confirmed": True,
                                    },
                                },
                            },
                        ],
                    }
                ],
            }
        ],
    }
