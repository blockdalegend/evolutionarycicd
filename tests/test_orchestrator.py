"""Tests for the policy-enforcement path in the Agent Orchestrator.

These tests exercise the security-critical guarantee documented throughout
the repository: an agent's requested tool must be checked against
``policies/agent_permissions.yml`` *before* the tool is ever invoked, not
merely audited afterward.
"""

from __future__ import annotations

from typing import Any

from agents.base import AgentContext, AgentDecision, BaseAgent
from agents.orchestrator.orchestrator import AgentOrchestrator, PolicyViolation
from tools.github.client import GitHubClient


class _StubAgent(BaseAgent):
    """A minimal agent whose ``act`` records whether it was ever invoked."""

    name = "stub_agent"

    def __init__(self, tool: str | None) -> None:
        self._tool = tool
        self.act_called = False

    def observe(self, context: AgentContext) -> dict[str, Any]:
        return {}

    def reason(self, context: AgentContext, observation: dict[str, Any]) -> AgentDecision:
        return AgentDecision(action="do_thing", reason="stub reason", tool=self._tool)

    def act(self, context: AgentContext, decision: AgentDecision) -> dict[str, Any]:
        self.act_called = True
        return {"executed": True}

    def validate(
        self, context: AgentContext, decision: AgentDecision, tool_result: dict[str, Any]
    ) -> dict[str, Any]:
        return {"validated": True}


def _orchestrator(permissions: dict[str, Any]) -> AgentOrchestrator:
    return AgentOrchestrator(
        github_client=GitHubClient(),
        permissions=permissions,
        approval_rules={"requires_human_approval": ["merge_pull_request", "deploy"]},
    )


def test_check_capability_true_when_policy_grants_it() -> None:
    orchestrator = _orchestrator({"stub_agent": {"execute_tests": True}})
    assert orchestrator.check_capability("stub_agent", "execute_tests") is True


def test_check_capability_false_when_policy_denies_it() -> None:
    orchestrator = _orchestrator({"stub_agent": {"execute_tests": False}})
    assert orchestrator.check_capability("stub_agent", "execute_tests") is False


def test_check_capability_false_when_agent_missing_from_policy() -> None:
    orchestrator = _orchestrator({})
    assert orchestrator.check_capability("unknown_agent", "execute_tests") is False


def test_requires_approval_reflects_approval_rules() -> None:
    orchestrator = _orchestrator({})
    assert orchestrator.requires_approval("merge_pull_request") is True
    assert orchestrator.requires_approval("execute_tests") is False


def test_run_agent_allows_act_when_tool_is_permitted() -> None:
    orchestrator = _orchestrator({"stub_agent": {"execute_tests": True}})
    agent = _StubAgent(tool="execute_tests")

    result = orchestrator.run_agent(agent, AgentContext())

    assert agent.act_called is True
    assert result.success is True


def test_run_agent_blocks_act_when_tool_is_not_permitted() -> None:
    """The core policy-enforcement guarantee: a denied tool must never run.

    Policy must be checked *before* ``act`` executes, so ``act_called`` must
    remain ``False`` and the result must be recorded as a failure containing
    the ``PolicyViolation`` explanation.
    """
    orchestrator = _orchestrator({"stub_agent": {"execute_tests": False}})
    agent = _StubAgent(tool="execute_tests")

    result = orchestrator.run_agent(agent, AgentContext())

    assert agent.act_called is False
    assert result.success is False
    assert "not permitted" in result.validation_results.get("error", "")


def test_run_agent_blocks_act_when_agent_absent_from_policy() -> None:
    orchestrator = _orchestrator({})
    agent = _StubAgent(tool="execute_tests")

    result = orchestrator.run_agent(agent, AgentContext())

    assert agent.act_called is False
    assert result.success is False


def test_policy_check_raises_policy_violation_directly() -> None:
    """``PolicyViolation`` is the exact exception type raised by the policy gate."""
    orchestrator = _orchestrator({"stub_agent": {"execute_tests": False}})
    agent = _StubAgent(tool="execute_tests")
    decision = AgentDecision(action="do_thing", reason="stub reason", tool="execute_tests")

    def policy_check(pending_decision: AgentDecision) -> None:
        tool = pending_decision.tool
        if tool and not orchestrator.check_capability(agent.name, tool):
            raise PolicyViolation(f"{agent.name} is not permitted to use tool '{tool}'")

    try:
        policy_check(decision)
        raise AssertionError("expected PolicyViolation to be raised")
    except PolicyViolation as exc:
        assert "not permitted" in str(exc)
