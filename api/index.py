"""Vercel serverless entrypoint. All routes rewrite here (vercel.json); the Python
runtime serves the ASGI `app`. The bundle is read-only on Vercel, so run state goes
to /tmp — per-instance and ephemeral; see README for what that implies.
"""
import os

os.environ.setdefault("RUNS_DIR", "/tmp/runs")
os.environ.setdefault("DATA_DIR", "/tmp/data")   # credential vault + source registry
# NOTE: set CRED_SECRET in the Vercel env so vault entries survive instance recycling
# (with the generated key file, /tmp loss would orphan encrypted credentials).

from src.server.app import app  # noqa: E402,F401
