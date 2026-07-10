"""Vercel serverless entrypoint. All routes rewrite here (vercel.json); the Python
runtime serves the ASGI `app`. The bundle is read-only on Vercel, so run state goes
to /tmp — per-instance and ephemeral; see README for what that implies.
"""
import os

os.environ.setdefault("RUNS_DIR", "/tmp/runs")

from src.server.app import app  # noqa: E402,F401
