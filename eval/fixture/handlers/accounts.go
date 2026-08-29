package handlers

import (
	"encoding/json"
	"net/http"
	"strconv"
)

// GetBalance godoc
// @Summary      Retrieve wallet balance
// @Description  Returns the available and ledger balance for the authenticated account.
// @Tags         Accounts
// @Produce      json
// @Success      200  {object}  BalanceResponse
// @Security     ApiKeyAuth
// @Router       /balance [get]
func GetBalance(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, BalanceResponse{
		Available: "2450000",
		Ledger:    "2500000",
		Currency:  "NGN",
	})
}

// ListTransactions godoc
// @Summary      List transactions
// @Description  Returns a paginated list of transactions, newest first.
// @Tags         Accounts
// @Produce      json
// @Param        page     query  int  false  "Page number, 1-indexed"   default(1)
// @Param        perPage  query  int  false  "Results per page, max 100"  default(25)
// @Success      200  {object}  TransactionsResponse
// @Security     ApiKeyAuth
// @Router       /transactions [get]
func ListTransactions(w http.ResponseWriter, r *http.Request) {
	page, _ := strconv.Atoi(r.URL.Query().Get("page"))
	if page < 1 {
		page = 1
	}
	perPage, _ := strconv.Atoi(r.URL.Query().Get("perPage"))
	if perPage < 1 {
		perPage = 25
	}
	if perPage > 100 {
		perPage = 100
	}
	writeJSON(w, http.StatusOK, TransactionsResponse{
		Data:    []PayoutResponse{},
		Page:    page,
		PerPage: perPage,
		Total:   0,
	})
}

// CreateCustomer godoc
// @Summary      Create a customer
// @Description  Creates a customer record.
// @Tags         Customers
// @Accept       json
// @Produce      json
// @Param        body  body  CustomerRequest  true  "Customer details"
// @Success      201  {object}  CustomerResponse
// @Failure      400  {object}  ErrorResponse
// @Security     ApiKeyAuth
// @Router       /customers [post]
func CreateCustomer(w http.ResponseWriter, r *http.Request) {
	var req CustomerRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid_body", "request body could not be parsed")
		return
	}
	if req.Email == "" {
		writeError(w, http.StatusBadRequest, "missing_email", "email is required")
		return
	}
	writeJSON(w, http.StatusCreated, CustomerResponse{
		ID:        "cus_01HZX",
		Email:     req.Email,
		FirstName: req.FirstName,
		LastName:  req.LastName,
		Phone:     req.Phone,
		KYCTier:   0,
		CreatedAt: "2026-08-29T10:00:00Z",
	})
}

// GetCustomer godoc
// @Summary      Retrieve a customer
// @Description  Fetches a single customer by identifier.
// @Tags         Customers
// @Produce      json
// @Param        id   path  string  true  "Customer ID"
// @Success      200  {object}  CustomerResponse
// @Failure      404  {object}  ErrorResponse
// @Security     ApiKeyAuth
// @Router       /customers/{id} [get]
func GetCustomer(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, CustomerResponse{
		ID:        r.PathValue("id"),
		Email:     "ada@example.test",
		FirstName: "Ada",
		LastName:  "Okafor",
		Phone:     "+2348000000000",
		KYCTier:   1,
		CreatedAt: "2026-08-29T10:00:00Z",
	})
}
