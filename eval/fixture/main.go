// Package main is a synthetic payments API used as the evaluation target for
// the contract auditor. It is deliberately small enough to read in one sitting
// and deliberately shaped like a real fintech API: money endpoints, KYC,
// webhooks, pagination, idempotency.
//
// It builds with the standard library only, so a judge can run the evaluation
// from a clean environment with no network access.
//
// @title        Fixture Payments API
// @version      1.0.0
// @description  Synthetic payments API used as an evaluation target.
// @host         api.fixture.test
// @BasePath     /v1
package main

import (
	"log"
	"net/http"

	"github.com/micro1-hackathon/fixture/handlers"
)

func main() {
	mux := http.NewServeMux()
	handlers.Register(mux)
	log.Println("fixture api listening on :8080")
	if err := http.ListenAndServe(":8080", mux); err != nil {
		log.Fatal(err)
	}
}
