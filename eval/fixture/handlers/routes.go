package handlers

import "net/http"

// Register wires every route. The auditor extracts the route table from this
// function, so a route that is registered here but absent from the published
// spec is a finding.
func Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /v1/payouts", CreatePayout)
	mux.HandleFunc("GET /v1/payouts/{id}", GetPayout)
	mux.HandleFunc("GET /v1/balance", GetBalance)
	mux.HandleFunc("POST /v1/customers", CreateCustomer)
	mux.HandleFunc("GET /v1/customers/{id}", GetCustomer)
	mux.HandleFunc("GET /v1/transactions", ListTransactions)
	mux.HandleFunc("POST /v1/kyc/bvn", VerifyBVN)
	mux.HandleFunc("POST /v1/refunds", CreateRefund)
	mux.HandleFunc("GET /v1/banks", ListBanks)
	mux.HandleFunc("POST /v1/webhooks/test", TestWebhook)
}
