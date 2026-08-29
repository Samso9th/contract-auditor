import { payoutResponse, balanceResponse, customerResponse, errorResponse } from "./types.js";

export function createPayout(req, res) {
  if (!req.get("Idempotency-Key")) {
    return res.status(400).json(errorResponse("missing_idempotency_key", "Idempotency-Key header is required"));
  }
  if (!req.body || !req.body.reference) {
    return res.status(400).json(errorResponse("missing_reference", "reference is required"));
  }
  return res.status(201).json(payoutResponse(req.body));
}

export function getPayout(req, res) {
  return res.status(200).json(payoutResponse(
    { amount: "150000", currency: "NGN", reference: "ref_123" }, req.params.id));
}

export function createRefund(req, res) {
  return res.status(201).json({
    id: "rf_01HZX",
    payoutId: req.body.payoutId,
    status: "pending",
    amount: req.body.amount,
  });
}

export function getBalance(_req, res) {
  return res.status(200).json(balanceResponse());
}

export function listTransactions(req, res) {
  let pageNum = Number(req.query.page);
  if (!Number.isFinite(pageNum) || pageNum < 1) pageNum = 1;
  let page = pageNum;
  let perPage = Number(req.query.perPage);
  if (!Number.isFinite(perPage) || perPage < 1) perPage = 25;
  if (perPage > 100) perPage = 100;
  return res.status(200).json({ data: [], page, perPage, total: 0 });
}

export function createCustomer(req, res) {
  if (!req.body || !req.body.email) {
    return res.status(400).json(errorResponse("missing_email", "email is required"));
  }
  return res.status(201).json(customerResponse(req.body));
}

export function verifyBvn(req, res) {
  if (!req.body || String(req.body.bvn || "").length !== 11) {
    return res.status(400).json(errorResponse("invalid_bvn", "bvn must be 11 digits"));
  }
  return res.status(200).json({ verified: true, firstName: "Ada", lastName: "Okafor", tier: 1 });
}

export function listBanks(_req, res) {
  return res.status(200).json([
    { code: "044", name: "Access Bank", slug: "access-bank" },
    { code: "058", name: "Guaranty Trust Bank", slug: "gtb" },
  ]);
}

export function testWebhook(_req, res) {
  res.set("X-Signature", "sha256=deadbeef");
  return res.status(202).json({ success: true, code: "queued", message: "test event queued" });
}
