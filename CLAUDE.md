# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A 4-agent "Opportunity-to-Solution Copilot": Discovery → Mapping → Research → Suitability,
taking a business problem to a cited, costed decision brief. `AGENTS.md` is the behavioral
spec and source of truth; `BUILD_PLAN.md` §6 holds the CaseFile schema; `RESEARCH_AGENT_SPEC.md`
details Agent 3. When code and AGENTS.md disagree, AGENTS.md wins.

## Commands

```bash
pip install -r requirements.txt          # pydantic v2, PyYAML, requests, bs4, ddgs, python-pptx

# Full pipeline (needs LLM_API_KEY env var and a --model; nothing is pinned)
python -m src.orchestrator.runner --flow config/flow.yaml --model <model-id> --problem "…"

# Any single agent, same run() as orchestrated mode
python -m src.agents.discovery   --model <model-id> --problem "…"
python -m src.agents.mapping     --model <model-id> --input runs/<id>/casefile.json
python -m src.agents.research    --model <model-id> --input runs/<id>/casefile.json --budget 4h
python -m src.agents.suitability --model <model-id> --input runs/<id>/casefile.json

# Resume after a gate stop or budget pause
python -m src.orchestrator.runner --resume runs/<id> --model <model-id>

# Checks (no test suite yet; smoke = compile + import)
python -m compileall -q src
```

Agents are interactive by design (`input()` gates and interview turns) — don't run them
without `--no-gates` in a non-interactive shell, and treat `--no-gates` as a last resort:
it leaves the `*_by_human` flags honestly false.

## Architecture

**One CaseFile, two entry points per agent.** All state flows through `CaseFile`
(`src/state/casefile.py`, pydantic v2), persisted at `runs/<id>/casefile.json`. Every agent
module exposes `run(case, ctx) -> CaseFile` plus a `python -m` CLI that wraps the same
`run()` — that identity is what makes standalone and orchestrated behavior the same. The
orchestrator (`src/orchestrator/runner.py`) is just a loop over `config/flow.yaml` calling
those `run()` functions, checking gate flags, and checkpointing after every agent.

**Model resolution is per-call, never pinned.** `src/tools/models.py:llm()` resolves
`call arg → config/run.yaml models[role] → llm.yaml defaults.model`, else raises
`ModelNotSpecified`. Roles (`lead`/`worker`/`classify`/`report` in `config/llm.yaml`) carry
only temperature/max_tokens; providers carry only `api_key_env` + `base_url`. The wire format
(Anthropic messages vs OpenAI chat completions) is inferred from `base_url` (override with
`api_style` on a provider). Never hardcode a model id anywhere — that's the repo's core rule.

**Gates are flags, set only by real humans.** `problem_confirmed_by_human`,
`map_validated_by_human`, `research_plan.approved_by_human` are set inside the agents via
`_common.gate()`, and the runner stops the flow when the flag for a step's declared gate is
false. Unattended mode proceeds but leaves flags false — don't "fix" that by setting them
programmatically.

**Citations are enforced structurally, twice.** A `Finding` cannot be constructed without a
`Source` with a non-empty URL (pydantic validator). Agent 3's merge step silently discards
worker output lacking a URL, and `_verify_citations()` re-checks reachability at the end,
demoting unreachable evidence to `assumption` or dropping it. Suitability (Agent 5) may only
cite existing finding IDs — invalid IDs trigger a corrective retry.

**Agent 3 is an orchestrator-worker loop inside one process.** Per round: 4 category workers
(no_code/low_code/full_code/saas from `config/research.yaml`) run in a `ThreadPoolExecutor` —
each generates queries (capability × angle), searches free sources (`src/tools/search.py`,
ddgs with an HTML-scrape fallback), reads full pages, and extracts findings; then the lead
synthesizes similarity (matched/missing vs the plan's `target_profile`), cost estimates
(method + assumptions always attached), and scores; coverage gaps feed the next round. The
CaseFile is saved every round, so a `BudgetExceeded` (cost cap from `llm.yaml limits`, or
wall clock via `Deadline`) loses at most one round — the `checkpoint_and_exit` path prints
the `--resume` command and exits with code 3.

**Cost accounting spans resumed sessions.** `CostTracker` (thread-safe; price-per-Mtok from
`LLM_COST_PER_MTOK_INPUT/OUTPUT` env vars since no model is pinned) counts the current
session; `ctx.prior_cost_usd` carries forward spend from before a pause so
`case.cost_spent_usd` stays cumulative.

## Conventions

- Prompts live in `src/prompts/*.md`, one per agent/worker, loaded by name via
  `load_prompt()`; each declares its strict-JSON output contract, parsed with `llm_json()`
  (fence-tolerant, one corrective retry). If you change a prompt's JSON shape, update the
  corresponding `_apply`/`_merge` parser in the agent.
- Behavior changes go in YAML (`config/`), not code: flow order/gates in `flow.yaml`,
  Agent 3 depth/coverage/sources in `research.yaml`, keys/roles/limits in `llm.yaml`.
- Reports (`src/tools/reports.py`) must stay self-contained (inline CSS/JS, no CDN) and must
  label every number fact / estimate / assumption.
