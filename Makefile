.PHONY: help cases baseline agent notify score clean check

help: ## Show this help
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

cases: ## Build the 16 evaluation cases (verifies each compiles)
	@cd eval && python3 inject.py --all

baseline: cases ## Run the single-prompt baseline over every case
	@python3 baseline/run.py --cases eval/cases --out reports/runs/baseline
	@echo
	@cd eval && python3 score.py --run ../reports/runs/baseline --markdown

agent: cases ## Run the contract auditor over every case
	@python3 auditor/run.py --cases eval/cases --out reports/runs/agent
	@echo
	@cd eval && python3 score.py --run ../reports/runs/agent --markdown

routes: ## Print the route table for the clean fixture
	@python3 auditor/tools/routes.py eval/fixture --strip-prefix /v1

test-tools: cases ## Verify the auditor's deterministic tools
	@python3 auditor/tools/test_routes.py
	@echo
	@python3 auditor/tools/test_diff.py
	@echo
	@python3 auditor/test_verify.py
	@echo
	@python3 auditor/test_llm.py --offline
	@echo
	@python3 auditor/test_notify.py

deterministic: cases ## Run the no-model layer over every case and score it
	@python3 auditor/run_deterministic.py
	@echo
	@cd eval && python3 score.py --run ../reports/runs/deterministic --markdown

models: ## Show the configured models and their prices
	@python3 auditor/llm.py --models

test-llm: ## Verify the model client against the live endpoint (costs <$0.001)
	@python3 auditor/test_llm.py

verify: deterministic ## Put every deterministic finding through the verification gate
	@for c in eval/cases/D*; do python3 auditor/verify.py $$c; echo; done

notify: ## Preview the alert for the latest agent run (sends nothing)
	@python3 auditor/notify.py --run reports/runs/agent --min-severity high --dry-run

score: ## Score both runs side by side
	@cd eval && python3 score.py --run ../reports/runs/baseline --markdown
	@echo
	@cd eval && python3 score.py --run ../reports/runs/agent --markdown

check: ## Verify the fixture builds and the scorer is sound
	@cd eval/fixture && go build ./... && go vet ./... && echo "fixture: build ok"
	@cd eval && python3 inject.py --all >/dev/null && echo "cases: 16 built"
	@cd eval && python3 oracle.py >/dev/null
	@cd eval && python3 score.py --run ../reports/runs/oracle | grep -E "precision|recall|decoys"

clean: ## Remove generated cases and free runs (keeps paid runs)
	@rm -rf eval/cases reports/runs/deterministic reports/runs/oracle
	@echo "cleaned (agent and baseline runs kept - they cost money and time to produce)"

clean-all: ## Remove everything generated, including paid runs
	@rm -rf eval/cases reports/runs
	@echo "cleaned everything, including agent and baseline runs"
