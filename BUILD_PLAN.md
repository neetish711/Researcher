# BUILD_PLAN.md

Repo structure, tech stack, and build phases for the Opportunity-to-Solution Copilot.
Behavioral spec lives in `AGENTS.md`; Agent 3 detail in `RESEARCH_AGENT_SPEC.md`.

---

## 1. Goals

Four agents (Discovery, Mapping, Research, Suitability) that run standalone and orchestrated,
share one typed `CaseFile`, enforce citations in code, pause at human gates, and never pin a model.

## 2. Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | stdlib `argparse` CLIs, `concurrent.futures` for worker fan-out |
| State / validation | pydantic v2 | `Finding` cannot be constructed without a `Source` — enforced by the type system |
| Config | PyYAML | everything behavioral is YAML |
| LLM access | raw HTTP via `requests` | provider-agnostic: Anthropic messages API or any OpenAI-compatible endpoint, decided by `base_url` |
| Web search | `ddgs` (DuckDuckGo), fallback HTML scrape | free sources, no key required |
| Page reading | `requests` + BeautifulSoup | read full official docs, not snippets |
| Reports | self-contained HTML template + `python-pptx` | interactive detailed analysis + PPT overview |

No agent framework: the orchestrator is a small explicit loop over `flow.yaml`, which keeps
gates, budgets, and resume behavior inspectable.

## 3. Repo structure

```
research/
├── AGENTS.md                  # behavioral spec (source of truth)
├── BUILD_PLAN.md              # this file
├── RESEARCH_AGENT_SPEC.md     # Agent 3 deep spec
├── CLAUDE.md
├── requirements.txt
├── .env.example
├── config/
│   ├── llm.yaml               # providers (keys), roles (temperature), defaults, cost limits
│   ├── flow.yaml              # agent order + gates
│   ├── research.yaml          # Agent 3 knobs
│   └── run.yaml               # optional per-run role → model mapping
├── runs/                      # one dir per run: runs/<id>/casefile.json + reports/
└── src/
    ├── state/casefile.py      # CaseFile + all typed sections (§6)
    ├── tools/
    │   ├── models.py          # llm() — role/provider/model resolution, retries, cost tracking
    │   ├── costs.py           # CostTracker, BudgetExceeded, checkpoint_and_pause
    │   ├── search.py          # web_search(), fetch_page(), check_url()
    │   └── reports.py         # render_html(), render_ppt()
    ├── prompts/               # one versioned .md prompt per agent/worker
    ├── agents/
    │   ├── discovery.py       # Agent 1
    │   ├── mapping.py         # Agent 2
    │   ├── research.py        # Agent 3 (lead + parallel category workers)
    │   └── suitability.py     # Agent 5
    └── orchestrator/
        └── runner.py          # flow.yaml executor: gates, checkpoints, --resume
```

Every agent module exposes the same two entry points:
- `run(casefile, ctx) -> CaseFile` — called by the orchestrator
- `python -m src.agents.<name> ...` — standalone CLI wrapping the same `run()`

`ctx` is a `RunContext` (model/provider overrides, run dir, cost tracker, interactive flag).
Identical behavior in both modes is guaranteed because both paths call the same `run()`.

## 4. Build phases

1. **Foundation** — `casefile.py`, `models.py`, `costs.py`, configs. Definition of done:
   `llm()` resolves model per call (call arg → run.yaml → defaults.model → fail fast),
   a `Finding` without a `Source` raises, budget breach raises `BudgetExceeded`.
2. **Agents 1 + 2** — interview loop with `confirmed | assumption | missing` tagging;
   mapping with future-state labels and the hard validation gate.
3. **Agent 3** — lead plan → approval gate → parallel category workers → similarity →
   costs → synthesis → coverage loop → citation verification (see `RESEARCH_AGENT_SPEC.md`).
4. **Agent 5 + reports** — evidence-bound verdict; HTML + PPT rendering.
5. **Orchestrator** — flow execution, gate enforcement, checkpoint/pause on budget, `--resume`.

## 5. Verification

- `python -m py_compile` over `src/` (no test suite yet; agents are exercised end-to-end).
- Citation test: after any research run, every finding's source URL must be reachable —
  `src/tools/search.py:check_url` is re-run by the verification pass; unverified findings
  are demoted to `assumption` or dropped.
- Gate test: with `human_gates: true`, the pipeline must stop after discovery, mapping,
  and the research plan until the human answers.

## 6. CaseFile schema

One typed object (`src/state/casefile.py`, pydantic v2) flows through all agents. Persisted as
`runs/<id>/casefile.json`; `CaseFile.load()/save()` round-trips it. Key rule enforced by the
types: **a `Finding` cannot be constructed without a `Source`**.

```
Source
  url: str (required, non-empty)     title: str
  publisher: str                     accessed: ISO date str
  source_type: official_docs | vendor | community | news | academic | internal
  reliability: high | medium | low   verified: bool (set by the citation pass)

Finding
  id: str (auto "F-<n>")             claim: str
  kind: fact | estimate | assumption
  category: no_code | low_code | full_code | saas | general
  source: Source (REQUIRED — construction fails without it)
  confidence: float 0–1              vendor_claim: bool (labeled + down-weighted)

CapturedItem            # Agent 1 interview coverage
  field, value, status: confirmed | assumption | missing

DataInventoryItem
  name, description, format, location,
  sensitivity: public | internal | confidential | regulated,
  status: confirmed | assumption | missing

WorkflowStep
  id, name, actor, system, time_estimate, pain_points[], decision_points[]
  label: AI-assist | deterministic-automation | human-owned | redesign-first  (future-state only)
  rationale

SimilarityResult        # per tool option, see RESEARCH_AGENT_SPEC §4
  index: int 0–100      matched[]: capabilities covered
  missing[]: capabilities not covered
  existing_solution: bool            existing_solution_url

CostEstimate            # per tool option, see RESEARCH_AGENT_SPEC §5
  build_cost_usd_low/high            per_run_cost_usd
  monthly_operation_usd              method: str (shown in reports)
  assumptions[]: labeled assumptions

ToolOption
  name, category, vendor, url, summary
  scores: {criterion: 0–10}          finding_ids[] (evidence backing this option)
  similarity: SimilarityResult       costs: CostEstimate

ResearchPlan
  questions[], categories[], source_criteria[], target_profile: str
  approved_by_human: bool

Suitability
  verdict: one of the fixed list in AGENTS.md §7
  scores: {value, data_readiness, error_tolerance, verifiability, privacy, integration, scale}
  rationale: str                     cited_finding_ids[] (must resolve to real findings)

CaseFile
  run_id, created_at, updated_at, status
  problem_statement, stated_vs_real: {stated, real, evidence}
  captured[]: CapturedItem           data_inventory[]: DataInventoryItem
  problem_confirmed_by_human: bool
  current_workflow[]: WorkflowStep   future_workflow[]: WorkflowStep
  map_validated_by_human: bool
  research_plan: ResearchPlan        findings[]: Finding
  tool_landscape: {category: ToolOption[]}
  open_questions[]                   suitability: Suitability
  cost_spent_usd, llm_calls          next_agent (for --resume)
```

`status` values: `in_progress`, `awaiting_gate:<name>`, `paused_budget`, `complete`.
