"""Route registrations. The auditor extracts its table from this file."""

from fastapi import APIRouter

from . import handlers

router = APIRouter()


@router.post("/payouts", status_code=201)
async def create_payout(body: dict, idempotency_key: str = None):
    return handlers.create_payout(body, {"Idempotency-Key": idempotency_key})


@router.get("/payouts/{id}")
async def get_payout(id: str):
    return handlers.get_payout(id)


@router.post("/refunds", status_code=201)
async def create_refund(body: dict):
    return handlers.create_refund(body)


@router.get("/balance")
async def get_balance():
    return handlers.get_balance()


@router.get("/transactions")
async def list_transactions(page: int = 1, perPage: int = 50):
    return handlers.list_transactions(page, perPage)


@router.post("/customers", status_code=201)
async def create_customer(body: dict):
    return handlers.create_customer(body)


@router.post("/kyc/bvn")
async def verify_bvn(body: dict):
    return handlers.verify_bvn(body)


@router.get("/banks")
async def list_banks():
    return handlers.list_banks()
