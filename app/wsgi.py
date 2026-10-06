"""本番（Railway）用の入口。起動コマンドは Procfile（gunicorn app.wsgi:app、ワーカーは1つ）。

ワーカーを増やすと、実行中の処理と毎朝の自動実行がワーカーごとに重複するので1つのままにする。
"""
import os

from .core import clean_emails, fix_reported_dm, init_db, load_env, restore_lost_email, restore_sent_emails

load_env()
init_db()
from .enrich import rescore_ig_matches  # noqa: E402
rescore_ig_matches()   # 判定の基準を直したときに、保存済みのアカウントを判定し直す
clean_emails()   # 役所・見本アドレスなど、送ってはいけない宛先を起動時に除去
restore_lost_email()
restore_sent_emails()
fix_reported_dm()

from . import jobs, outreach  # noqa: E402
from .web import app  # noqa: E402

outreach.seed_variants()
outreach.refresh_queued_links()
if os.environ.get("AUTO_DAILY_AT"):
    jobs.start_scheduler(os.environ["AUTO_DAILY_AT"])

__all__ = ["app"]   # gunicorn app.wsgi:app
