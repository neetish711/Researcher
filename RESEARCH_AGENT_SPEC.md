# RESEARCH_AGENT_SPEC.md

Deep spec for **Agent 3 — Research**: an orchestrator-worker system inside one agent
(`src/agents/research.py`). Behavioral summary in `AGENTS.md §6`; this file is the detail.

---

## 1. Pipeline

```
lead plan ──[human approves]──▶ round loop:
    fan out: 4 category workers (no-code / low-code / full-code / SaaS), in parallel
        generate queries (capability × angle) → search free sources → read full docs
        → extract findings (each with a Source) + tool options
    similarity pass        (every option vs the target profile, 0–100)
    SaaS deep analysis     (pricing, competitors, case studies)
    cost estimation        (build + per-run + monthly, method + assumptions shown)
    synthesis + scoring    (lead builds/updates decision matrix)
    coverage check         (gaps + budget + min_rounds decide: loop again or exit)
citation verification pass (reachability; drop/demote unsupported claims)
reports                    (interactive HTML + PPT overview)
```

Workers run in a `ThreadPoolExecutor` with their own conversation context and tool budgets.
Pass different `--model` values per invocation (or set `models.worker` in `config/run.yaml`)
to diversify workers across runs.

## 2. `config/research.yaml`

Every knob that shapes the loop. The shipped file is the reference copy; the schema:

```yaml
budget:
  max_wall_clock: 4h            # parse: <n>h | <n>m | <n>s
  max_rounds: 6                 # hard cap on loop iterations
  min_rounds: 2                 # loop at least this many rounds if budget allows
  max_workers: 4                # parallel category workers
  max_tool_calls_per_worker: 40 # search + fetch budget per worker per round
  # cost cap comes from config/llm.yaml limits.max_cost_usd_per_run

coverage:                       # the loop exits early only when ALL are met
  min_options_per_category: 3
  min_findings_per_option: 3
  require_similarity_on_all: true
  require_costs_on_all: true

categories:                     # one worker each; prompt = src/prompts/research_worker.md
  no_code:   "No-code platforms an ops team can configure without engineers"
  low_code:  "Low-code platforms needing light scripting/config by a technical user"
  full_code: "Frameworks/libraries/APIs for a custom engineered build"
  saas:      "Off-the-shelf SaaS products solving this workflow directly"

queries:
  angles: [capabilities, integrations, pricing, limits, security, case studies, alternatives, reviews]
  max_queries_per_round: 12     # per worker
  results_per_query: 6

sources:
  reliability:                  # substring match on URL/domain → tier
    high:   [docs., developer., learn., .org/docs, readthedocs]
    medium: [github.com, stackoverflow.com, wikipedia.org]
    # everything else: low
  prefer_official_docs: true    # workers must fetch and read full doc pages, not snippets
  down_weight_vendor_claims: true   # vendor_claim findings are labeled and scored lower
  denylist: []                  # domains never used as sources

similarity:                     # §4
  existing_solution_threshold: 75   # index ≥ this ⇒ flagged as "already exists"

cost:                           # §5
  hourly_rate_usd: 120          # build-effort assumption, always surfaced in reports
  label_estimates: true         # every number that is not from a source is kind=estimate

outputs:
  html: reports/detailed_analysis.html    # relative to runs/<id>/
  ppt:  reports/overview.pptx
```

## 3. Query generation

Workers do not free-associate searches. For each **capability** in the validated
`future_workflow` (steps labeled `AI-assist` or `deterministic-automation`) crossed with each
**angle** in `queries.angles`, the worker LLM emits a structured query list (capped at
`max_queries_per_round`), executes them via `src/tools/search.py:web_search`, then fetches the
most reliable hits (`sources.reliability` tiers) and reads the full page text.

## 4. Similarity + existing-solution detection

For every tool option, the lead computes a similarity index against the **target profile**
(the plan's one-paragraph description of the ideal solution, derived from the future workflow):

- Decompose the target profile into required capabilities.
- The LLM matches each capability against the option's *cited* findings only.
- `index = round(100 * weighted_matched / total_required)` with matched/missing lists kept.
- `index ≥ similarity.existing_solution_threshold` ⇒ `existing_solution: true`, with the
  product URL and the `missing` list presented as gaps.

The matched/missing breakdown is always shown; the raw number is never presented alone.

## 5. Cost estimation

Three numbers per option, each labeled `estimate` and carrying `method` + `assumptions`:

| Number | Method |
|---|---|
| **Build cost** (low–high USD) | effort-class per category (config/setup vs integration vs custom build) × `cost.hourly_rate_usd`; ranges widen with uncertainty |
| **Per-run cost** | pricing findings (cited) where available; else LLM-token/API-call arithmetic with stated assumptions |
| **Monthly operation** | subscription tiers from cited pricing pages + per-run × stated volume from discovery |

Numbers taken directly from a cited pricing page stay `kind=fact`; anything derived is
`kind=estimate`. The report renders `method` and `assumptions` next to every figure.

## 6. Citation verification pass

Runs once after the loop exits, before reports:

1. Every `Finding.source.url` is checked for reachability (`check_url`, HEAD then GET).
2. Reachable → `source.verified = true`.
3. Unreachable → the finding is **demoted** to `kind=assumption` (and flagged) or **dropped**
   if nothing else cites it; tool options lose the demoted evidence from their scores.
4. Vendor-sourced claims keep their `vendor_claim` label and reduced weight regardless.

A finding with no source can never exist — that is enforced at the type level (`BUILD_PLAN.md §6`).

## 7. Reports

- **Interactive HTML** (`reports/detailed_analysis.html`): self-contained (inline CSS/JS, no CDN).
  Sections: problem + workflow, decision matrix (sortable), per-category option cards with
  similarity matched/missing, cost tables with method/assumptions, full findings table
  filterable by fact / estimate / assumption, open questions.
- **PPT overview** (`reports/overview.pptx`, python-pptx): title, problem, future workflow,
  landscape per category, top options + costs, existing-solution flags, suitability verdict
  (if Agent 5 has run), next steps.

Both reports visually separate **fact / estimate / assumption** everywhere a number appears.

## 8. Budgets & guardrails

- Hard caps: `max_rounds`, `max_workers`, `max_tool_calls_per_worker`, `max_wall_clock`,
  and the global `limits.max_cost_usd_per_run` (from `config/llm.yaml`).
- On any breach: **checkpoint and pause** — the CaseFile is saved with `status=paused_budget`
  and the run resumes with `python -m src.orchestrator.runner --resume runs/<id>`.
- The plan-approval gate fires before any worker spends a tool call.
- Estimates are always labeled; vendor claims always down-weighted; no finding without a source.
