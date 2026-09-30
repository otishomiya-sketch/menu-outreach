"""本番（Railway）用の入口。起動コマンドは Procfile（gunicorn app.wsgi:app、ワーカーは1つ）。

ワーカーを増やすと、実行中の処理と毎朝の自動実行がワーカーごとに重複するので1つのままにする。
"""
import os

from .core import init_db, load_env

load_env()
init_db()

from . import jobs, outreach  # noqa: E402
from .web import app  # noqa: E402

outreach.seed_variants()
if os.environ.get("AUTO_DAILY_AT"):
    jobs.start_scheduler(os.environ["AUTO_DAILY_AT"])

__all__ = ["app"]   # gunicorn app.wsgi:app
