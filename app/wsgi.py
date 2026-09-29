"""本番（Railway）用の入口: gunicorn app.wsgi:app"""
import os

from .core import init_db, load_env

load_env()
init_db()

from . import jobs, outreach  # noqa: E402
from .web import app  # noqa: E402,F401

outreach.seed_variants()
if os.environ.get("AUTO_DAILY_AT"):
    jobs.start_scheduler(os.environ["AUTO_DAILY_AT"])
