import { Router } from "express";
import * as h from "./handlers.js";

// The auditor extracts the route table from this file, so a route registered
// here and absent from the published spec is a finding.
const router = Router();

router.post("/v1/payouts", h.createPayout);
router.get("/v1/payouts/:id", h.getPayout);
router.post("/v1/refunds", h.createRefund);
router.get("/v1/balance", h.getBalance);
router.get("/v1/transactions", h.listTransactions);
router.post("/v1/customers", h.createCustomer);
router.post("/v1/kyc/bvn", h.verifyBvn);
router.get("/v1/banks", h.listBanks);
router.post("/v1/webhooks/test", h.testWebhook);

export default router;
