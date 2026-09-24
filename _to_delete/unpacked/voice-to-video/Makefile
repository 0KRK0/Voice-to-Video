.DEFAULT_GOAL := help
PY ?= python3
export PYTHONPATH := src

.PHONY: help
help: ## Show the available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: install
install: ## Install the package and development tooling
	$(PY) -m pip install -e ".[dev]"

.PHONY: test
test: ## Run the test suite (pytest if available, stdlib unittest otherwise)
	@if $(PY) -c "import pytest" 2>/dev/null; then \
		$(PY) -m pytest; \
	else \
		echo "pytest not installed; falling back to unittest"; \
		$(PY) -m unittest discover -s tests; \
	fi

.PHONY: lint
lint: ## Check formatting and lint rules
	ruff check .

.PHONY: format
format: ## Apply safe automatic fixes
	ruff check --fix .

.PHONY: types
types: ## Strict type check
	mypy

.PHONY: schemas
schemas: ## Regenerate the exported JSON Schemas
	$(PY) -m vtv.schema_export

.PHONY: schemas-check
schemas-check: ## Fail if the committed schemas have drifted from the code
	$(PY) -m vtv.schema_export --check

.PHONY: example
example: ## Print a summary of the worked example, end to end
	@$(PY) -c "from vtv.examples import transistor_project; \
e = transistor_project(); \
print('segments', len(e.transcript.segments), '-> units', len(e.understanding.units), '-> scenes', len(e.scene_graph.scenes)); \
print('strategy mix', e.visual_plan.strategy_mix()); \
print('expected cost  \$$%.3f' % e.visual_plan.expected_cost_usd); \
print('renderable', e.timeline.is_renderable())"

.PHONY: evaluate
evaluate: ## Score the intelligence stages against the evaluation corpus
	$(PY) -m vtv.evaluation.harness

.PHONY: serve
serve: ## Run the API and web UI on http://localhost:8000
	$(PY) -m uvicorn --factory vtv.api.app:create_app --host 0.0.0.0 --port 8000

.PHONY: demo
demo: ## Run the golden path end to end and write a real MP4 to ./var/demo
	$(PY) -m vtv.demo

.PHONY: check
check: lint types schemas-check test evaluate ## Everything CI runs
	@echo "all checks passed"
