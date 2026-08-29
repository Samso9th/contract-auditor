package handlers

import (
	"encoding/json"
	"net/http"
)

// CreatePayout godoc
// @Summary      Create a payout
// @Description  Initiates a payout to a bank account. Requires an Idempotency-Key header.
// @Tags         Payouts
// @Accept       json
// @Produce      json
// @Param        Idempotency-Key  header  string         true  "Unique key to make the request idempotent"
// @Param        body             body    PayoutRequest  true  "Payout details"
// @Success      201  {object}  PayoutResponse
// @Failure      400  {object}  ErrorResponse
// @Failure      409  {object}  ErrorResponse
// @Security     ApiKeyAuth
// @Router       /payouts [post]
func CreatePayout(w http.ResponseWriter, r *http.Request) {
	if r.Header.Get("Idempotency-Key") == "" {
		writeError(w, http.StatusBadRequest, "missing_idempotency_key", "Idempotency-Key header is required")
		return
	}
	var req PayoutRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_body", "request body could not be parsed")
		return
	}
	if req.Reference == "" {
		writeError(w, http.StatusBadRequest, "missing_reference", "reference is required")
		return
	}
	writeJSON(w, http.StatusCreated, PayoutResponse{
		ID:        "po_01HZX",
		Status:    "pending",
		Amount:    req.Amount,
		Currency:  req.Currency,
		Fee:       "5000",
		Reference: req.Reference,
		CreatedAt: "2026-08-29T10:00:00Z",
	})
}

// GetPayout godoc
// @Summary      Retrieve a payout
// @Description  Fetches a single payout by its identifier.
// @Tags         Payouts
// @Produce      json
// @Param        id   path  string  true  "Payout ID"
// @Success      200  {object}  PayoutResponse
// @Failure      404  {object}  ErrorResponse
// @Security     ApiKeyAuth
// @Router       /payouts/{id} [get]
func GetPayout(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		writeError(w, http.StatusNotFound, "not_found", "payout not found")
		return
	}
	writeJSON(w, http.StatusOK, PayoutResponse{
		ID:        id,
		Status:    "successful",
		Amount:    "150000",
		Currency:  "NGN",
		Fee:       "5000",
		Reference: "ref_123",
		CreatedAt: "2026-08-29T10:00:00Z",
	})
}

// CreateRefund godoc
// @Summary      Create a refund
// @Description  Refunds a previously successful payout, in full or in part.
// @Tags         Payouts
// @Accept       json
// @Produce      json
// @Param        body  body  RefundRequest  true  "Refund details"
// @Success      201  {object}  RefundResponse
// @Failure      400  {object}  ErrorResponse
// @Failure      422  {object}  ErrorResponse
// @Security     ApiKeyAuth
// @Router       /refunds [post]
func CreateRefund(w http.ResponseWriter, r *http.Request) {
	var req RefundRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_body", "request body could not be parsed")
		return
	}
	writeJSON(w, http.StatusCreated, RefundResponse{
		ID:       "rf_01HZX",
		PayoutID: req.PayoutID,
		Status:   "pending",
		Amount:   req.Amount,
	})
}
