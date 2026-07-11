# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An "Opportunity-to-Solution Copilot": four agents (Discovery → Mapping → Research → Suitability)
that take a business problem to a cited, costed decision brief, plus a React operator console.
`AGENTS.md` is the behavioral spec and source of truth; `BUILD_PLAN.md` §6 holds the CaseFile
schema; `RESEARCH_AGENT_SPEC.md` details Agent 3. When code and AGENTS.md disagree, AGENTS.md wins.

## Commands

```bash
pip install -r requirements.txt

# CLI pipeline (needs a model id — nothing is pinned; key via vault or LLM_API_KEY env)
python -m src.orchestrator.runner --flow config/flow.yaml --model <model-id> --problem "…"
python -m src.agents.research --model <model-id> --input runs/<id>/casefile.json --budget 4h
python -m src.orchestrator.runner --resume runs/<id> --model <model-id>   # after gate/budget pause

# Server + operator console
uvicorn src.server.app:app --port 8000        # serves API + built UI from ui/dist
cd ui && npm install && npm run dev           # UI dev server (vite proxies API to :8000)
cd ui && npm run build                        # REQUIRED before deploy — dist/ is committed

# Checks (no pytest suite; verification = compile + TestClient smoke scripts)
python -m compileall -q src api

# Deploy (Vercel project researcher1/research; entry api/index.py; Python 3.12 pinned)
vercel --prod --yes
```

CLI agents are interactive (`input()` gates); don't run them in a non-interactive shell without
`--no-gates`. Env vars that matter: `CRED_SECRET` (vault encryption — required on serverless or
saved keys become undecryptable), `LLM_COST_PER_MTOK_INPUT/OUTPUT` (cost accounting; no model is
pinned so price is an assumption), `CONTACT_EMAIL` (OpenAlex/Crossref polite pool), `RUNS_DIR`/
`DATA_DIR` (state roots; `api/index.py` points both at /tmp on Vercel).

## Architecture

**One CaseFile, one run() per agent, three frontends.** All state flows through `CaseFile`
(`src/state/casefile.py`, pydantic v2) saved at `runs/<id>/casefile.json`. Each agent exposes
`run(case, ctx) -> CaseFile` wrapped by (a) its own `python -m` CLI, (b) the orchestrator
(`src/orchestrator/runner.py`, a loop over `config/flow.yaml`), and (c) the server
(`src/server/app.py:_advance`), which reuses `runner.call_agent`/`gate_satisfied` in a background
thread. Gates are booleans on the CaseFile set only by real humans — `input()` in CLI mode,
`POST /runs/{id}/approve` in server mode; unattended mode proceeds with flags honestly false.
Never set `*_by_human` flags programmatically.

**Models are per-call, never pinned.** `src/tools/models.py:llm()` resolves:
call arg → `ctx.role_models[role]` → `ctx.model` → `config/run.yaml` → `llm.yaml defaults.model`,
else raises. Roles carry only temperature/max_tokens. Providers come from the credential vault
first (`src/server/credstore.py`), then `llm.yaml` env-keyed entries; wire format (Anthropic vs
OpenAI chat) is inferred from base_url/type. `validate_model_id` rejects key-shaped strings —
keep that on any new model-accepting field.

**Security invariant: keys never leave the server.** The vault (Fernet, `data/credentials.json`)
stores LLM provider keys and research-source keys; API responses carry fingerprints only.
`credstore.redact()` scrubs key patterns AND current secret values from everything written to
events — all event payloads pass through `events._scrub`. `config/sources.yaml` holds env-var
*names* and limits, never keys. A repo key-scan is part of the acceptance tests.

**Observability: events.jsonl is the Run Console's source of truth.** `src/server/events.py`
appends one scrubbed JSON line per action (llm_call with full prompt/response, source_call,
finding_created, citation_*, round_complete, gate_waiting, error with auto-classification +
suggested fix, retry, checkpoint_saved). The UI replays from seq 0 (survives refresh) and tails
via SSE with polling fallback. `ctx.emit()` never raises. If you add an event field carrying
text, it's scrubbed automatically; new event *types* need an icon in `ui/src/pages/RunConsole.jsx`.

**Research source stack (Agent 3's retrieval), strictly layered:**
`research.py` workers → `RouterSession` (`src/server/router.py`) → `QuotaManager`
(`src/server/quota.py`) → adapters (`src/server/adapters.py`). Never call an adapter directly.
- QuotaManager: SQLite ledger (`data/quota.db`) in each provider's own unit, pre-flight refusal
  *before* any HTTP when a call would exceed the free tier, `free_tier_only` hard block (default
  on; override lives in `data/sources_override.json`), token-bucket rps/rpm, per-run reservations.
- Router: curated per-worker chains (`SEARCH_ROUTES`/`READ_ROUTES`) behind `search()`/`read()` —
  the LLM never sees provider names. Keyless primaries first, transparent fallback on
  quota/auth/error, cross-run query+page cache in SQLite (repeat = zero quota), in-run dedup.
- Adapters: Tavily is search-only, `depth=basic` — any other endpoint raises `BlockedEndpoint`
  (its /research burns ~250 credits); Jina is the default extractor (token units ≈ chars/4);
  Firecrawl on Jina failure / saas worker; TinyFish only on saas pricing pages.
- User-registered custom HTTP APIs (`src/server/sources.py`, dot-path response mapping) ride
  along for every worker. To add a provider: adapter fn → `SEARCH_ADAPTERS`/`READ_ADAPTERS` →
  `sources.yaml` entry (unit + rate limit + quota) → router chain placement.

**Citations are enforced in five layers:** a `Finding` cannot be constructed without a `Source`
URL (pydantic); Agent 3's `_merge` silently discards unsourced worker output; `_verify_citations`
checks reachability (cache counts) and demotes/drops; `_claim_support_pass` re-reads cached pages
and LLM-verifies the page supports the claim; `_sole_support_flags` marks options that rest only
on vendor pages (`vendor_only`) or community anecdote (`community_only`, weight 0.4). Suitability
may only cite existing finding IDs.

**Frontend** (`ui/`, React + Tailwind v4 + Vite, hash-routed SPA, no router lib): pages in
`ui/src/pages/`, shared primitives in `lib.jsx`, API client + polling/SSE hooks in `api.js`.
FastAPI mounts `ui/dist` at `/` (mounted last so API routes win). **`ui/dist` is committed** —
Vercel's Python service can't run npm, so rebuild before committing UI changes.

**Serverless (Vercel) constraints:** bundle is read-only — `api/index.py` redirects RUNS_DIR and
DATA_DIR to /tmp (runs, vault, quota ledger, cache are per-instance and ephemeral there); prompt
editing returns 409; long research runs are capped by function duration. The always-on path
(local uvicorn or Railway via `railway.json`/`Procfile`) has none of these limits.

## Conventions

- Prompts (`src/prompts/*.md`) declare strict-JSON contracts parsed by `llm_json()` (fence-
  tolerant, one corrective retry). Changing a prompt's JSON shape requires updating the paired
  `_apply`/`_merge` parser in the agent.
- Behavior changes go in YAML (`config/`): flow order/gates in `flow.yaml`, Agent 3 depth in
  `research.yaml`, provider limits in `sources.yaml`, keys/roles/cost caps in `llm.yaml`.
- Adding a gate or agent means updating all of: `runner.gate_satisfied`, the server's approve
  endpoint flag-mapping, `AGENT_META` in `app.py`, and `GATE_OWNER`/`GATE_LABEL` in the UI.
- Reports (`src/tools/reports.py`) and the SPA stay self-contained (no CDN); every number is
  labeled fact / estimate / assumption.
