.PHONY: install lint typecheck test security ci demo-history agent-test agent-optimize

install:
	pip install -r requirements.txt

lint:
	ruff check .

typecheck:
	mypy app agents tools llm telemetry scripts

test:
	pytest --cov=app --cov-report=term-missing

security:
	bandit -r app agents tools llm telemetry scripts -q
	pip-audit -r requirements.txt || true

ci: lint typecheck test security

demo-history:
	python scripts/seed_demo_history.py

agent-test:
	python scripts/run_agent.py --agent test_quality

agent-optimize:
	python scripts/run_agent.py --agent pipeline_optimizer
