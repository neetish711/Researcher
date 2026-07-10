# Opportunity-to-Solution Copilot

Four config-driven agents that take a raw business problem to a cited, costed decision brief:

```
Business problem
   → Agent 1 Discovery       → [you confirm problem + data]
   → Agent 2 Mapping         → [you validate the map]
   → Agent 3 Research (deep) → [you approve the plan] → cited landscape + costs
   → Agent 5 Suitability     → decision brief (+ interactive HTML report + PPT)
```

Docs: `AGENTS.md` (behavioral spec, source of truth) · `BUILD_PLAN.md` (structure + CaseFile
schema) · `RESEARCH_AGENT_SPEC.md` (Agent 3 deep pipeline).

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # set LLM_API_KEY (see config/llm.yaml providers)
```

No model is pinned anywhere — pass one at call time (`--model <any-model-id>`) or set
`models:` in `config/run.yaml`. Without either, the run fails fast and asks for one.

## Run

```bash
# Full pipeline
python -m src.orchestrator.runner --flow config/flow.yaml --model <model-id> --problem "…"

# Any agent standalone (same behavior, same CaseFile)
python -m src.agents.discovery   --model <model-id> --problem "…"
python -m src.agents.mapping     --model <model-id> --input runs/<id>/casefile.json
python -m src.agents.research    --model <model-id> --input runs/<id>/casefile.json --budget 4h
python -m src.agents.suitability --model <model-id> --input runs/<id>/casefile.json

# Resume a run that paused at a gate or on budget
python -m src.orchestrator.runner --resume runs/<id> --model <model-id>
```

Outputs land in `runs/<id>/`: `casefile.json` (all state) and `reports/`
(`detailed_analysis.html`, `overview.pptx`).

## Server mode / deploy (Railway)

`src/server/app.py` wraps the same pipeline in HTTP — the CLI's human gates become API
approvals. Runs execute in the background and stop at each gate until you approve.

```bash
uvicorn src.server.app:app --host 0.0.0.0 --port 8000   # local; Railway uses railway.json

curl -X POST /runs -d '{"problem": "…", "model": "<model-id>", "budget": "1h"}'
curl /runs/<id>                    # status + full casefile; shows which gate it awaits
curl -X POST /runs/<id>/approve    # confirm_problem → validate_map → approve_plan
curl /runs/<id>/report             # interactive HTML report
```

Interactive API docs at `/docs`. Deploy: connect the repo on Railway (or `railway up`) and set
`LLM_API_KEY`. Note: discovery's follow-up interview is skipped in server mode — put
everything you know in `problem`. `runs/` is ephemeral without a volume.

## Guarantees (enforced in code)

- **No claim without a source** — a `Finding` cannot be constructed without a `Source`;
  a citation-verification pass demotes/drops unreachable evidence.
- **Human gates** after discovery, mapping, and the research plan.
- **Cost caps** — `config/llm.yaml limits`; a breach checkpoints and pauses, `--resume` continues.
- **Config, not code** — flow order, gates, budgets, and models are all YAML/flags.
