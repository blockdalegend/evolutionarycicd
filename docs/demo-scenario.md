# Demo Scenario

This walkthrough is designed to be run live at a conference in roughly
10-15 minutes. It illustrates the maturity model:

> Traditional CI/CD executes workflows.
> Agentic CI/CD reasons inside workflows.
> Evolutionary CI/CD improves the workflows themselves.

## Setup (before the talk)

```bash
make install
make demo-history   # seeds data/pipeline_history.json with ~30 runs
```

## Step-by-step

**Step 1 — Ordinary CI runs against a PR.**
Open a PR with a trivial change. `ci.yml` runs lint, typecheck, tests,
coverage, Bandit, and pip-audit. This is *Traditional CI/CD*: it executes,
it doesn't reason.

**Step 2 — Developer adds a validation branch without enough tests.**
Add a new branch to `PaymentService.validate_payment` (e.g. a new rejection
reason) but don't write a test for it.

**Step 3 — CI passes, but coverage drops.**
`pytest --cov` still passes (existing tests pass), but branch coverage on
`payment_service.py` drops because the new branch is unreached.

**Step 4 — Test Quality Agent analyzes the diff and coverage.**
`agent-test-quality.yml` runs `scripts/run_agent.py --agent test_quality`.
The agent's `observe()` inspects the diff for known gap patterns; `reason()`
flags the under-tested branch.

**Step 5 — Agent proposes/generates missing tests.**
The agent's `act()` writes a candidate test file
(`tests/test_agent_generated_candidates.py`), matching the recommended test
names from `prompts/test_quality.md`'s output format.

**Step 6 — Generated tests are executed.**
The agent runs `tools/testing/pytest_tools.run_pytest` against the generated
file *before* recommending it, then deletes the temporary file. The PR
comment reports the real passed/failed counts — never a fabricated result.

**Step 7 — Supply Chain Agent notices a pinning issue.**
Temporarily relax `requirements.txt` (drop a `==` pin) or add a workflow
step using `uses: actions/checkout@v4` (a mutable tag, not a SHA).
`agent-security.yml` runs the Supply Chain Agent, which reports the
unpinned dependency/action using `tools/security/scanners.py` — deterministic
detection, not LLM guesswork.

**Step 8 — A later branch introduces a merge conflict.**
Create two branches that both edit the same line in
`app/services/payment_service.py`, then open a PR that conflicts with `main`.

**Step 9 — Merge Conflict Agent proposes a resolution.**
Manually dispatch `agent-merge-conflict.yml` with the PR number.
`tools/git/operations.py` detects the conflict markers; the agent proposes a
resolution and validates it by running the test suite — but never commits or
merges it.

**Step 10 — Pipeline history shows a flaky test / slow stage.**
`data/pipeline_history.json` (seeded by `scripts/seed_demo_history.py`)
already contains a recurring `flaky:test_payment_gateway_timeout` failure, a
repeatedly failing `ruff` stage, a recurring dependency failure, and slow
outlier runs — by design, so this step always has something to show.

**Step 11 — Pipeline Optimizer Agent analyzes historical telemetry.**
Run `make agent-optimize` (or dispatch `agent-pipeline-analysis.yml`). The
agent's `_analyze()` method surfaces each of the patterns above as a
plain-English recommendation.

**Step 12 — Agent creates a GitHub issue with recommendations.**
`scripts/run_agent.py` turns the recommendation list into a GitHub issue
(using the `agent-recommendation` issue template) via
`tools/github/client.py`. In dry-run mode this is logged instead of created;
with a real token it opens an actual issue that a human must review and act
on — the agent never edits workflow YAML directly.

## Talking points to hit at each stage

| Maturity level | What it looks like in this demo |
|---|---|
| Traditional CI/CD | Step 1: `ci.yml` just executes. |
| AI-assisted CI/CD | Step 4/7: agents explain *what* happened (a coverage gap, an unpinned action). |
| Agentic CI/CD | Step 5-6, 9: agents take a bounded action (generate a test, propose a merge resolution) and validate it themselves. |
| Evolutionary CI/CD | Step 10-12: the system learns from its own history and proposes changes to the pipeline itself, gated by human approval. |
