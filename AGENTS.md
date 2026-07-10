# AGENTS.md — Final Documentation

The definitive reference for the 4-agent Opportunity-to-Solution Copilot. Pairs with:
- `BUILD_PLAN.md` — repo structure, tech stack, build phases
- `RESEARCH_AGENT_SPEC.md` — Agent 3's deep-research pipeline, cost/similarity methods, report specs

Scope: **4 agents** — Discovery (1), Workflow Mapping (2), Research (3), AI Suitability (5). Each runs **standalone** and as an **orchestrated pipeline**. Everything is **config-driven**.

---

## 1. System at a glance

```
Business problem
   → Agent 1 Discovery       → [human confirms problem + data]
   → Agent 2 Mapping         → [human validates the map]
   → Agent 3 Research (deep) → [human approves research plan] → cited landscape + costs
   → Agent 5 Suitability     → cited decision brief (+ interactive report + PPT)
```

Core guarantees, enforced in code across every agent:
- **No claim without a source** — findings can't exist without a citation.
- **Human gates** after Agent 1, Agent 2, and Agent 3's plan.
- **Cost caps** — a run checkpoints and pauses before exceeding budget.
- **Config, not code** — flow order, gates, budgets, and *which LLM each agent uses* are all YAML.

---

## 2. LLM configuration — `config/llm.yaml`

No model is pinned anywhere. You configure **keys** only; the **model is supplied at call time** and can be changed on any call (CLI flag, function argument, or run config). Roles carry only behavior params like temperature — never a fixed model.

```yaml
# config/llm.yaml
# Configure KEYS only. Models are NOT fixed here.
# Pass the model per call: CLI --model / function arg / run config.
# Any call can override both model and provider.

# ── Providers: keys + endpoints only. No models listed. ──
providers:
  default:                          # used unless a call overrides it
    api_key_env: LLM_API_KEY        # your key goes in this env var
    base_url: https://api.anthropic.com   # any Anthropic or OpenAI-compatible endpoint
  # Optional extra providers to switch to per call:
  # openai:     { api_key_env: OPENAI_API_KEY,     base_url: https://api.openai.com/v1 }
  # openrouter: { api_key_env: OPENROUTER_API_KEY, base_url: https://openrouter.ai/api/v1 }
  # local:      { api_key_env: LOCAL_API_KEY,      base_url: http://localhost:11434/v1 }

# ── Roles: behavior only (temperature, tokens). NO model pinned. ──
roles:
  lead:     { temperature: 0.2, max_tokens: 8000 }
  worker:   { temperature: 0.3, max_tokens: 4000 }
  classify: { temperature: 0.0, max_tokens: 1000 }
  report:   { temperature: 0.5, max_tokens: 16000 }

# ── Defaults: provider + optional fallback model. ──
defaults:
  provider: default
  model: null            # null = model MUST be given at call time; or set one here as a fallback
  top_p: 0.95
  timeout_seconds: 120
  max_retries: 4

limits:
  max_cost_usd_per_run: 25.0
  on_limit: checkpoint_and_pause
```

**Change the model on any call** — resolution order is `call arg → run config → defaults.model`:

```bash
# CLI: pick the model (and optionally provider) at runtime
python -m src.agents.research --input runs/<id>/casefile.json --model <any-model-id>
python -m src.orchestrator.runner --flow config/flow.yaml --problem "..." \
       --model <any-model-id> --provider openai
```

```python
# In code: model is a per-call parameter, changeable every call
llm(role="worker", model="any-model-id")                  # default provider + role temperature
llm(role="lead",   model="another-id", provider="openai") # switch provider for this call only
```

```yaml
# Optional per-run override (config/run.yaml): set models without touching code,
# still overridable by a --model flag on the command line.
models:
  lead:   <any-model-id>
  worker: <any-model-id>
```

Implement in `src/tools/models.py`: a **role** supplies only temperature + limits; a **provider** supplies the key from its `api_key_env`; the **model comes from the call** (falling back to run config, then `defaults.model`). If no model resolves, fail fast and ask for one. Nothing about a specific model is hardcoded — temperature is config, model is per-call.

---

## 3. Shared state — the `CaseFile`

One typed object flows through all agents; each reads what it needs and writes its section. This is what makes orchestrated and standalone runs identical. Full schema in `BUILD_PLAN.md §6`. Key rule: a `Finding` cannot be constructed without a `Source`.

---

## 4. Agent 1 — Stakeholder Discovery

| | |
|---|---|
| **Purpose** | Run a structured interview; separate the *stated request* from the *real problem*; capture what data is available. |
| **Order** | First. Gate after: human confirms problem + data. |
| **LLM role** | `lead` (model chosen at call time) |
| **Inputs** | Free-text problem + interactive Q&A turns |
| **Outputs** | `problem_statement`, `stated_vs_real`, `data_inventory` |
| **Standalone** | `python -m src.agents.discovery` |

**Logic:** ask follow-ups *only on gaps*. Every captured item is tagged `confirmed | assumption | missing`. Never accept the stated request at face value. Required coverage before it finishes: objective, current workflow + tools, volume/frequency, time spent + error rate, data sources + sensitivity, non-negotiable human-judgment points, error tolerance, baseline metric. Also produces a **data inventory** (what data exists, format, sensitivity).

**Prompt** (`src/prompts/discovery.md`):
```
Role: senior discovery consultant. Find the REAL business problem, not the stated request.
Classify each answer: confirmed fact / assumption / missing info.
Ask a follow-up ONLY when a required field is missing or ambiguous.
Required: objective, workflow, tools, volume, time, error rate, data sources,
  data sensitivity, human-judgment points, error tolerance, baseline.
Produce a data inventory. Output strict JSON. Do NOT propose solutions.
```

**Guardrails:** does not recommend tools or solutions; blocks the pipeline until the human confirms.

---

## 5. Agent 2 — Workflow Mapping

| | |
|---|---|
| **Purpose** | Map current-state, propose future-state, and **route to a human to validate before anything downstream runs**. |
| **Order** | Second. Gate after: human validates the map. |
| **LLM role** | `lead` (model chosen at call time) |
| **Inputs** | Agent 1 output (or a CaseFile stub for standalone) |
| **Outputs** | `current_workflow`, `future_workflow`, `map_validated_by_human` |
| **Standalone** | `python -m src.agents.mapping --input runs/<id>/casefile.json` |

**Logic:** map current workflow step by step (actor, system, time, pain, decision points). Propose future-state, labeling each step `AI-assist | deterministic-automation | human-owned | redesign-first`. Then **pause (interrupt)** and present the map for sign-off. `map_validated_by_human` must be `true` before Agent 3 starts. Does **not** name specific tools/vendors — that's Agent 3.

**Prompt** (`src/prompts/mapping.md`):
```
Role: process/operations analyst.
Map the CURRENT workflow: actor, system, time, pain, decision points.
Propose a future-state; mark each step [AI-assist | deterministic-automation | human-owned | redesign-first].
Do NOT name tools or vendors. End by requesting human validation. Output strict JSON.
```

**Guardrails:** technology-agnostic; hard human gate before the expensive research runs.

---

## 6. Agent 3 — Research (deep analysis engine)

The core. An **orchestrator-worker system inside one agent** that runs a long internal loop. Full pipeline, cost/similarity methods, and report specs are in `RESEARCH_AGENT_SPEC.md`; summary here.

| | |
|---|---|
| **Purpose** | Deeply evaluate every viable option (no-code / low-code / full-code / SaaS) for the mapped workflow, from **complete documentation**, with real cited sources, costs, and similarity to existing solutions. Not a quick report — it loops for hours until coverage is met. |
| **Order** | Third. Gate: human approves the research plan before workers run. |
| **LLM roles** | `lead` + parallel `worker`s; model(s) passed at call time (pass different models to workers to diversify) |
| **Inputs** | Validated `future_workflow` + `problem_statement` + `data_inventory` |
| **Outputs** | `research_plan`, `findings` (each cited), `tool_landscape` (options per category, scored, costed, with similarity), `open_questions` |
| **Standalone** | `python -m src.agents.research --input runs/<id>/casefile.json --budget 4h` |

**Internal flow (loops until coverage or budget):**
1. **Lead plans** the research (questions, categories, reliable-source criteria), writes plan to memory → **human approves**.
2. **Fan out to 4 category workers** (own context windows, high tool budgets): no-code, low-code, full-code, SaaS.
3. Workers **generate structured queries** from each capability × angle, search across many free sources, and **read full official docs** (not snippets).
4. **Similarity + existing-solution detection** — 0–100 index with matched/missing breakdown; flag anything that already exists (link + gaps).
5. **SaaS deep analysis** — relevance, competitors, case studies, pricing.
6. **Cost estimation** — build cost + per-run + monthly operation cost (method + assumptions shown, labeled estimates).
7. **Synthesize + score** against the target solution profile — decision matrix.
8. **Coverage check** — if gaps remain and budget/`min_rounds` allow, run another targeted round; else exit.
9. **Citation + verification** — drop/demote any unsupported claim.
10. **Reports** — interactive HTML detailed analysis + PPT overview.

**Config that shapes it:** `research.yaml` (budget, coverage, categories, queries, sources, reliability, cost, similarity, outputs) — see `RESEARCH_AGENT_SPEC.md §2`.

**Guardrails:** hard budget caps (max rounds/workers/tool-calls/wall-clock); dedicated citation pass; permission-scoped internal retrieval; vendor claims labeled and down-weighted; estimates always labeled.

---

## 7. Agent 5 — AI Suitability

| | |
|---|---|
| **Purpose** | Decide whether AI is the right answer, and if so which kind — using **only** Agent 3's cited evidence. |
| **Order** | Last. No gate (produces the brief). |
| **LLM role** | `lead` (model chosen at call time) |
| **Inputs** | `future_workflow` + `findings` + `tool_landscape` |
| **Outputs** | `suitability` verdict + rationale citing finding IDs |
| **Standalone** | `python -m src.agents.suitability --input runs/<id>/casefile.json` |

**Logic:** score value, data readiness, error tolerance, verifiability, privacy, integration, scale. Verdict from a fixed list: `don't automate / improve process first / deterministic automation / analytics / generative for a subtask / RAG / single agent / multi-agent / AI with mandatory human review / controlled experiment only / reject`. Must cite the findings justifying the verdict; may not introduce new claims.

**Prompt** (`src/prompts/suitability.md`):
```
Role: pragmatic AI strategist. Decide if AI fits THIS workflow.
Use ONLY the provided cited findings — introduce no new claims.
Score: value, data readiness, error tolerance, verifiability, privacy, integration, scale.
Output one verdict from the allowed list + rationale citing finding IDs.
If AI is not appropriate, say so plainly and recommend the better path.
```

**Guardrails:** evidence-bound (no new claims); refuses false precision; honest "don't use AI" is a valid output.

---

## 8. Orchestration & running

Driven by `config/flow.yaml` (order + gates) and `config/llm.yaml` (models). Same graph + CaseFile back every mode.

```bash
# Full pipeline
python -m src.orchestrator.runner --flow config/flow.yaml --problem "…"

# Single agent (individual flow)
python -m src.agents.research --input runs/<id>/casefile.json --budget 4h

# Custom flow (e.g. skip discovery, start from an existing map)
python -m src.orchestrator.runner --flow config/custom.yaml --input runs/<id>/casefile.json

# Resume a paused deep run
python -m src.orchestrator.runner --resume runs/<id>
```

`flow.yaml` example:
```yaml
flow:
  - { agent: discovery,   gate: confirm_problem }
  - { agent: mapping,     gate: validate_map }
  - { agent: research,    gate: approve_plan }
  - { agent: suitability, gate: none }
human_gates: true          # false = unattended (not recommended)
```

Customization = editing YAML: reorder agents, drop a gate, change budgets/thresholds, or **change the model on any call/agent** via `--model`, a call arg, or `config/run.yaml`.

---

## 9. Config file index

| File | Controls |
|------|----------|
| `config/llm.yaml` | Providers (keys), roles (temperature + limits), defaults, cost limits |
| `config/flow.yaml` | Agent order, human gates, orchestrated vs unattended |
| `config/research.yaml` | Agent 3 depth, coverage, categories, sources, cost, similarity, outputs |
| `config/run.yaml` | Optional per-run role → model mapping |
| `src/prompts/*.md` | One versioned prompt per agent/worker |

---

## 10. Cross-agent guarantees (definition of done)

- All four agents run standalone **and** orchestrated.
- Every finding in the final brief resolves to a real, reachable source (citation test passes).
- Human gates fire after Discovery, Mapping, and Agent 3's plan.
- Agent 3 sustains a multi-round loop within budget and returns a cited, costed landscape across all four categories, with a similarity call on existing solutions.
- Any agent/call can be pointed at any model at runtime (via `--model`, a call arg, or run config); keys and temperature stay in config.
- A full run outputs an interactive HTML report + a PPT overview, both separating fact / estimate / assumption.
