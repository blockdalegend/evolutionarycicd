# Agent Prompts

Each Markdown file in this directory is the system prompt for one specialized
agent. Prompts constrain LLM reasoning to the evidence already collected by
the deterministic agent implementation; they do not grant capabilities beyond
`policies/agent_permissions.yml`.

Agents must preserve the shared `AgentDecision` contract and use `no_action`
when evidence thresholds are not met. Findings are advisory and remain subject
to orchestrator policy, confidence thresholds, and human review.
