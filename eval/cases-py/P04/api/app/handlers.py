"""Handlers for the fixture payments API.

Each returns the response body only, and the status is declared on the route
decorator - the idiomatic FastAPI shape. Errors raise HttpError, which carries
the status the caller would see.
"""

from .models import payout_response, balance_response, customer_response


class HttpError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def create_payout(body, headers=None):
    headers = headers or {}
    if not headers.get("Idempotency-Key"):
        raise HttpError(400, "missing_idempotency_key", "Idempotency-Key header is required")
    if not body.get("reference"):
        raise HttpError(400, "missing_reference", "reference is required")
    return payout_response(body)


def get_payout(payout_id):
    return payout_response(
        {"amount": "150000", "currency": "NGN", "reference": "ref_123"}, payout_id)


def create_refund(body):
    return {
        "id": "rf_01HZX",
        "payoutId": body.get("payoutId"),
        "status": "pending",
        "amount": body.get("amount"),
    }


def get_balance():
    return balance_response()


def list_transactions(page=1, perPage=25):
    page = max(int(page or 1), 1)
    perPage = int(perPage or 25)
    if perPage < 1:
        perPage = 25
    if perPage > 100:
        perPage = 100
    return {"data": [], "page": page, "perPage": perPage, "total": 0}


def create_customer(body):
    if not body.get("email"):
        raise HttpError(400, "missing_email", "email is required")
    return customer_response(body)


def verify_bvn(body):
    if len(str(body.get("bvn", ""))) != 11:
        raise HttpError(400, "invalid_bvn", "bvn must be 11 digits")
    return {"verified": True, "firstName": "Ada", "lastName": "Okafor", "tier": 1}


def list_banks():
    return [
        {"code": "044", "name": "Access Bank", "slug": "access-bank"},
        {"code": "058", "name": "Guaranty Trust Bank", "slug": "gtb"},
    ]
