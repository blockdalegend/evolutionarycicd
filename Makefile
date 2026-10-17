.PHONY: agents-all-dry-run agents-all-live install lint typecheck test security ci demo-history agent-test agent-optimize demo-test-quality-issue demo-security-issue demo-failure-issue demo-evolution-issue demo-flaky-test demo-build-performance demo-test-impact demo-documentation-drift demo-agent-effectiveness

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

demo-test-quality-issue:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent test_quality

demo-security-issue:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent supply_chain

demo-failure-issue:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent failure_analysis

demo-evolution-issue:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent pipeline_optimizer

demo-flaky-test:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent flaky_test --context-file tests/fixtures/agent_demos/flaky_test.json --demo

demo-build-performance:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent build_performance --context-file tests/fixtures/agent_demos/build_performance.json --demo

demo-test-impact:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent test_impact --context-file tests/fixtures/agent_demos/test_impact.json --demo

demo-documentation-drift:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent documentation_drift --context-file tests/fixtures/agent_demos/documentation_drift.json --demo

demo-agent-effectiveness:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent agent_effectiveness --context-file tests/fixtures/agent_demos/agent_effectiveness.json --demo

agents-all-dry-run:
	AGENT_DRY_RUN=true python scripts/run_agent.py --agent all --demo

# Requires GITHUB_TOKEN, GITHUB_REPOSITORY (and COPILOT_TOKEN for assignment)
agents-all-live:
	AGENT_ALLOW_LIVE=true python scripts/run_agent.py --agent all --live
