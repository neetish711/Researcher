"""Pre-deploy smoke test — MUST pass before any deploy (see CLAUDE.md).

Covers the paths a compile check can't: the background _advance thread (this is
what caught the _flow_steps NameError), gates, retries, uploads, files, and the
security invariants. Run:  python -m pytest tests/test_smoke.py -q
(or plain:  python tests/test_smoke.py)
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["RUNS_DIR"] = tempfile.mkdtemp()
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["CRED_SECRET"] = "smoke-test-secret"
os.environ.pop("CONSOLE_TOKEN", None)

from fastapi.testclient import TestClient  # noqa: E402
from src.server.app import app  # noqa: E402


def test_full_run_lifecycle_reaches_agent_code():
    """POST /runs must drive _advance through _flow_steps into the first agent.
    With no LLM key the run must land in a CLASSIFIED error — never hang in
    running:* (which is what a NameError in the thread looks like)."""
    c = TestClient(app)
    rid = c.post("/runs", json={"problem": "smoke", "title": "smoke",
                                "model": "smoke-model"}).json()["run_id"]
    status = ""
    for _ in range(60):
        status = c.get(f"/runs/{rid}").json()["summary"]["status"]
        if status.startswith(("error", "awaiting")):
            break
        time.sleep(0.5)
    assert status.startswith("error"), (
        f"run stuck in {status!r} — the _advance thread likely crashed "
        "before persisting state (check the traceback above)")
    ev = c.get(f"/runs/{rid}/events").json()["events"]
    types = {e["type"] for e in ev}
    assert {"run_created", "agent_start", "error"} <= types
    err = next(e for e in ev if e["type"] == "error")
    assert err.get("error_class"), "errors must be classified with a suggested fix"


def test_core_endpoints_respond():
    c = TestClient(app)
    assert c.get("/health").json() == {"status": "ok"}
    assert c.get("/api").json()["version"]
    assert [s["agent"] for s in c.get("/config/flow").json()["flow"]] \
        == ["discovery", "mapping", "research", "suitability"]
    assert len(c.get("/research-sources").json()["providers"]) == 6
    assert c.post("/forecast").json()["providers"]
    assert isinstance(c.get("/runs").json(), list)
    assert c.get("/providers").json()


def test_security_invariants():
    c = TestClient(app)
    r = c.post("/providers", json={"name": "smoke-prov", "type": "anthropic",
                                   "api_key": "sk-ant-SMOKE-000000000000ZZZZ"})
    assert r.status_code == 201
    assert "sk-ant-SMOKE" not in r.text and r.json()["key_fingerprint"].endswith("ZZZZ")
    assert "sk-ant-SMOKE" not in json.dumps(c.get("/providers").json())
    r = c.post("/runs", json={"problem": "x", "model": "sk-ant-LOOKSLIKEAKEY0000"})
    assert r.status_code == 422 and "looks like an API key" in r.text
    c.delete("/providers/smoke-prov")


def test_tavily_research_endpoint_blocked():
    from src.server.adapters import BlockedEndpoint, tavily_search
    try:
        tavily_search("x", endpoint="research")
        raise AssertionError("/research callable!")
    except BlockedEndpoint:
        pass


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"PASS {name}")
    print("\nSMOKE OK — safe to deploy")
