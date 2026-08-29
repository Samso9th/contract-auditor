// Shared shapes for the fixture API. Plain JS with JSDoc rather than TypeScript
// so the fixture runs under `node --test` with no build step and no dependency
// on a compiler being present — the same reason the Go fixture uses only the
// standard library.

/** @typedef {{accountNumber:string, bankCode:string, amount:string, currency:string, reference:string, narration?:string}} PayoutRequest */

export function payoutResponse(req, id = "po_01HZX") {
  return {
    id,
    status: "pending",
    amount: req.amount,
    currency: req.currency,
    fee: "5000",
    reference: req.reference,
    createdAt: "2026-08-29T10:00:00Z",
  };
}

export function balanceResponse() {
  return { available: 2450000, ledger: 2500000, currency: "NGN" };
}

export function customerResponse(body, id = "cus_01HZX") {
  return {
    id,
    email: body.email,
    firstName: body.firstName,
    lastName: body.lastName,
    phone: body.phone,
    kycTier: 0,
    createdAt: "2026-08-29T10:00:00Z",
  };
}

export function errorResponse(code, message) {
  return { success: false, code, message };
}
