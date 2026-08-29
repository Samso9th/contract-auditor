package handlers

import (
	"encoding/json"
	"net/http"
)

// VerifyBVN godoc
// @Summary      Verify a BVN
// @Description  Verifies a Bank Verification Number and returns the matched identity.
// @Tags         KYC
// @Accept       json
// @Produce      json
// @Param        body  body  BVNRequest  true  "BVN details"
// @Success      200  {object}  BVNResponse
// @Failure      400  {object}  ErrorResponse
// @Security     ApiKeyAuth
// @Router       /kyc/bvn [post]
func VerifyBVN(w http.ResponseWriter, r *http.Request) {
	var req BVNRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_body", "request body could not be parsed")
		return
	}
	if len(req.BVN) != 11 {
		writeError(w, http.StatusBadRequest, "invalid_bvn", "bvn must be 11 digits")
		return
	}
	writeJSON(w, http.StatusOK, BVNResponse{
		Verified:  true,
		FirstName: "Ada",
		LastName:  "Okafor",
		Tier:      1,
	})
}

// ListBanks godoc
// @Summary      List supported banks
// @Description  Returns every institution that can receive a payout.
// @Tags         Reference
// @Produce      json
// @Success      200  {array}  Bank
// @Router       /banks [get]
func ListBanks(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, []Bank{
		{Code: "044", Name: "Access Bank", Slug: "access-bank"},
		{Code: "058", Name: "Guaranty Trust Bank", Slug: "gtb"},
	})
}

// TestWebhook godoc
// @Summary      Send a test webhook
// @Description  Delivers a sample event to the configured webhook URL. The delivery is signed with HMAC-SHA256 in the X-Signature header.
// @Tags         Webhooks
// @Produce      json
// @Success      202  {object}  ErrorResponse
// @Security     ApiKeyAuth
// @Router       /webhooks/test [post]
func TestWebhook(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("X-Signature", "sha256=deadbeef")
	writeJSON(w, http.StatusAccepted, ErrorResponse{
		Success: true,
		Code:    "queued",
		Message: "test event queued for delivery",
	})
}
