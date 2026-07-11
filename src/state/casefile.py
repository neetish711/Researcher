"""Shared state for all agents. One CaseFile flows through the whole pipeline;
each agent reads what it needs and writes its section (schema: BUILD_PLAN.md §6).

Key rule, enforced by the types: a Finding cannot be constructed without a Source.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

VERDICTS = [
    "don't automate",
    "improve process first",
    "deterministic automation",
    "analytics",
    "generative for a subtask",
    "RAG",
    "single agent",
    "multi-agent",
    "AI with mandatory human review",
    "controlled experiment only",
    "reject",
]

CATEGORIES = ["no_code", "low_code", "full_code", "saas"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Source(BaseModel):
    url: str
    title: str = ""
    publisher: str = ""
    accessed: str = Field(default_factory=_now)
    source_type: str = "community"  # official_docs | vendor | community | news | academic | internal
    reliability: str = "low"        # high | medium | low
    verified: bool = False          # set by the citation-verification pass

    @field_validator("url")
    @classmethod
    def _url_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a Source requires a non-empty url — no claim without a source")
        return v.strip()


class Finding(BaseModel):
    id: str = ""                    # assigned by CaseFile.add_finding: "F-<n>"
    claim: str
    kind: str = "fact"              # fact | estimate | assumption
    category: str = "general"       # no_code | low_code | full_code | saas | general
    source: Source                  # REQUIRED — a Finding cannot exist without a Source
    confidence: float = 0.8
    vendor_claim: bool = False      # labeled and down-weighted in scoring
    option: Optional[str] = None    # tool option this finding supports

    @field_validator("claim")
    @classmethod
    def _claim_required(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("a Finding requires a claim")
        return v.strip()


class CapturedItem(BaseModel):
    field: str
    value: str = ""
    status: str = "missing"         # confirmed | assumption | missing


class DataInventoryItem(BaseModel):
    name: str
    description: str = ""
    format: str = ""
    location: str = ""
    sensitivity: str = "internal"   # public | internal | confidential | regulated
    status: str = "assumption"      # confirmed | assumption | missing


class WorkflowStep(BaseModel):
    id: str
    name: str
    actor: str = ""
    system: str = ""
    time_estimate: str = ""
    pain_points: List[str] = Field(default_factory=list)
    decision_points: List[str] = Field(default_factory=list)
    label: Optional[str] = None     # future-state only: AI-assist | deterministic-automation | human-owned | redesign-first
    rationale: str = ""


class SimilarityResult(BaseModel):
    index: int = 0                  # 0–100
    matched: List[str] = Field(default_factory=list)
    missing: List[str] = Field(default_factory=list)
    existing_solution: bool = False
    existing_solution_url: Optional[str] = None


class CostEstimate(BaseModel):
    build_cost_usd_low: float = 0.0
    build_cost_usd_high: float = 0.0
    per_run_cost_usd: float = 0.0
    monthly_operation_usd: float = 0.0
    method: str = ""
    assumptions: List[str] = Field(default_factory=list)


class ToolOption(BaseModel):
    name: str
    category: str = "saas"
    vendor: str = ""
    url: str = ""
    summary: str = ""
    capability_notes: str = ""
    scores: Dict[str, float] = Field(default_factory=dict)
    similarity: SimilarityResult = Field(default_factory=SimilarityResult)
    costs: CostEstimate = Field(default_factory=CostEstimate)
    finding_ids: List[str] = Field(default_factory=list)
    vendor_only: bool = False      # sole support is the vendor — "vendor, unverified"
    community_only: bool = False   # anecdote-only evidence — never a sole basis


class ResearchPlan(BaseModel):
    target_profile: str = ""
    capabilities: List[str] = Field(default_factory=list)
    questions: List[str] = Field(default_factory=list)
    categories: List[str] = Field(default_factory=lambda: list(CATEGORIES))
    source_criteria: List[str] = Field(default_factory=list)
    approved_by_human: bool = False


class Suitability(BaseModel):
    verdict: str
    scores: Dict[str, float] = Field(default_factory=dict)
    rationale: str = ""
    cited_finding_ids: List[str] = Field(default_factory=list)
    better_path: Optional[str] = None

    @field_validator("verdict")
    @classmethod
    def _verdict_allowed(cls, v: str) -> str:
        if v not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}, got {v!r}")
        return v


class CaseFile(BaseModel):
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    status: str = "in_progress"     # in_progress | awaiting_gate:<name> | paused_budget | complete
    next_agent: Optional[str] = None  # where --resume picks up

    # Agent 1
    problem_statement: str = ""
    stated_vs_real: Dict[str, str] = Field(default_factory=dict)
    captured: List[CapturedItem] = Field(default_factory=list)
    data_inventory: List[DataInventoryItem] = Field(default_factory=list)
    problem_confirmed_by_human: bool = False

    # Agent 2
    current_workflow: List[WorkflowStep] = Field(default_factory=list)
    future_workflow: List[WorkflowStep] = Field(default_factory=list)
    map_validated_by_human: bool = False

    # Agent 3
    research_plan: Optional[ResearchPlan] = None
    findings: List[Finding] = Field(default_factory=list)
    tool_landscape: Dict[str, List[ToolOption]] = Field(default_factory=dict)
    open_questions: List[str] = Field(default_factory=list)
    research_rounds_done: int = 0

    # Agent 5
    suitability: Optional[Suitability] = None

    # accounting
    cost_spent_usd: float = 0.0
    llm_calls: int = 0

    # ── helpers ──────────────────────────────────────────────────────────
    _finding_counter: int = 0

    def add_finding(self, finding: Finding) -> Finding:
        """Only path to add a finding — assigns the id and appends."""
        n = len(self.findings) + 1
        finding.id = f"F-{n}"
        self.findings.append(finding)
        return finding

    def get_finding(self, fid: str) -> Optional[Finding]:
        return next((f for f in self.findings if f.id == fid), None)

    def touch(self) -> None:
        self.updated_at = _now()

    # ── persistence ──────────────────────────────────────────────────────
    def save(self, run_dir: Path | str) -> Path:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.touch()
        path = run_dir / "casefile.json"
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | str) -> "CaseFile":
        path = Path(path)
        if path.is_dir():
            path = path / "casefile.json"
        return cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
