"""本番（Railway）用の入口。起動コマンドは Procfile（gunicorn app.wsgi:app、ワーカーは1つ）。

ワーカーを増やすと、実行中の処理と毎朝の自動実行がワーカーごとに重複するので1つのままにする。
"""
import os

from .core import clean_emails, init_db, load_env, restore_lost_email, restore_sent_emails

load_env()
init_db()
clean_emails()   # 役所・見本アドレスなど、送ってはいけない宛先を起動時に除去
restore_lost_email()
restore_sent_emails()

from . import jobs, outreach  # noqa: E402
from .web import app  # noqa: E402

outreach.seed_variants()
if os.environ.get("AUTO_DAILY_AT"):
    jobs.start_scheduler(os.environ["AUTO_DAILY_AT"])

__all__ = ["app"]   # gunicorn app.wsgi:app
