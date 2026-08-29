package handlers

// PayoutRequest is the body accepted by POST /v1/payouts.
type PayoutRequest struct {
	AccountNumber string `json:"accountNumber"`
	BankCode      string `json:"bankCode"`
	// Amount is a decimal string in minor units to avoid float drift.
	Amount    string `json:"amount"`
	Currency  string `json:"currency"`
	Narration string `json:"narration,omitempty"`
	Reference string `json:"reference"`
}

// PayoutResponse is returned by the payout endpoints.
type PayoutResponse struct {
	ID        string `json:"id"`
	Status    string `json:"status"`
	Amount    string `json:"amount"`
	Currency  string `json:"currency"`
	Fee       string `json:"fee"`
	Reference string `json:"reference"`
	CreatedAt string `json:"createdAt"`
}

// BalanceResponse is returned by GET /v1/balance.
type BalanceResponse struct {
	Available string `json:"available"`
	Ledger    string `json:"ledger"`
	Currency  string `json:"currency"`
}

// CustomerRequest is the body accepted by POST /v1/customers.
type CustomerRequest struct {
	Email     string `json:"email"`
	FirstName string `json:"firstName"`
	LastName  string `json:"lastName"`
	Phone     string `json:"phone"`
}

// CustomerResponse describes a customer record.
type CustomerResponse struct {
	ID        string `json:"id"`
	Email     string `json:"email"`
	FirstName string `json:"firstName"`
	LastName  string `json:"lastName"`
	Phone     string `json:"phone"`
	KYCTier   int    `json:"kycTier"`
	CreatedAt string `json:"createdAt"`
}

// TransactionsResponse is a paginated transaction list.
type TransactionsResponse struct {
	Data    []PayoutResponse `json:"data"`
	Page    int              `json:"page"`
	PerPage int              `json:"perPage"`
	Total   int              `json:"total"`
}

// BVNRequest is the body accepted by POST /v1/kyc/bvn.
type BVNRequest struct {
	BVN         string `json:"bvn"`
	DateOfBirth string `json:"dateOfBirth"`
}

// BVNResponse is the verification outcome.
type BVNResponse struct {
	Verified  bool   `json:"verified"`
	FirstName string `json:"firstName"`
	LastName  string `json:"lastName"`
	Tier      int    `json:"tier"`
}

// RefundRequest is the body accepted by POST /v1/refunds.
type RefundRequest struct {
	PayoutID string `json:"payoutId"`
	Amount   string `json:"amount"`
	Reason   string `json:"reason"`
}

// RefundResponse describes a refund.
type RefundResponse struct {
	ID       string `json:"id"`
	PayoutID string `json:"payoutId"`
	Status   string `json:"status"`
	Amount   string `json:"amount"`
}

// Bank is a supported destination institution.
type Bank struct {
	Code string `json:"code"`
	Name string `json:"name"`
	Slug string `json:"slug"`
}

// ErrorResponse is the shape returned for every non-2xx status.
type ErrorResponse struct {
	Success bool   `json:"success"`
	Code    string `json:"code"`
	Message string `json:"message"`
}
