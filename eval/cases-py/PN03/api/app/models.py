"""Response shapes for the fixture API.

Plain dicts rather than Pydantic models so the fixture needs nothing installed,
for the same reason the Go fixture uses only the standard library: a judge must
be able to run the evaluation from a clean environment.
"""


def payout_response(body, payout_id="po_01HZX"):
    return {
        "id": payout_id,
        "status": "pending",
        "amount": body["amount"],
        "currency": body["currency"],
        "fee": "5000",
        "reference": body["reference"],
        "createdAt": "2026-08-29T10:00:00Z",
    }


def balance_response():
    return {"currency": "NGN", "ledger": "2500000", "available": "2450000"}


def customer_response(body, customer_id="cus_01HZX"):
    return {
        "id": customer_id,
        "email": body["email"],
        "firstName": body.get("firstName"),
        "lastName": body.get("lastName"),
        "phone": body.get("phone"),
        "kycTier": 0,
        "createdAt": "2026-08-29T10:00:00Z",
    }
