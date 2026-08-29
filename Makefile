.PHONY: help cases baseline agent score clean check

help: ## Show this help
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

cases: ## Build the 16 evaluation cases (verifies each compiles)
	@cd eval && python3 inject.py --all

baseline: cases ## Run the single-prompt baseline over every case
	@python3 baseline/run.py --cases eval/cases --out reports/runs/baseline

agent: cases ## Run the contract auditor over every case
	@python3 auditor/run.py --cases eval/cases --out reports/runs/agent

routes: ## Print the route table for the clean fixture
	@python3 auditor/tools/routes.py eval/fixture --strip-prefix /v1

test-tools: cases ## Verify the auditor's deterministic tools
	@python3 auditor/tools/test_routes.py

score: ## Score both runs side by side
	@cd eval && python3 score.py --run ../reports/runs/baseline --markdown
	@echo
	@cd eval && python3 score.py --run ../reports/runs/agent --markdown

check: ## Verify the fixture builds and the scorer is sound
	@cd eval/fixture && go build ./... && go vet ./... && echo "fixture: build ok"
	@cd eval && python3 inject.py --all >/dev/null && echo "cases: 16 built"
	@cd eval && python3 oracle.py >/dev/null
	@cd eval && python3 score.py --run ../reports/runs/oracle | grep -E "precision|recall|decoys"

clean: ## Remove generated cases and runs
	@rm -rf eval/cases reports/runs
	@echo "cleaned"
