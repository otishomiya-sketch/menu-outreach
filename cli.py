#!/usr/bin/env python3
"""Menu Photo Pro 営業アプリ コマンド一覧

  python cli.py init                 DB作成・初期文面の取り込み
  python cli.py collect [--area 東京都渋谷区 --keyword カフェ --limit 40]
                                     ①Googleマップから飲食店を集める（Apify）
  python cli.py enrich [--limit 200] ②Instagramの更新頻度と写真の質を測ってスコア付け
  python cli.py score                スコアだけ再計算（設定の重みを変えたとき）
  python cli.py plan                 ③今日の送信リストを作る（上限・重複・停止リストを考慮）
  python cli.py email [--live]       ③キューのメールを送る（既定はドライラン）
  python cli.py inbox                返信メールを取り込む（返信あり / 配信停止）
  python cli.py reflect [--no-propose] ④振り返り（集計・文面の引退判定・新しい文面の提案）
  python cli.py daily                inbox → plan → email を順に実行
  python cli.py serve                ダッシュボードを開く http://127.0.0.1:8765
  python cli.py demo                 APIキーなしで動作確認（架空の30店で data/demo.db を作り、ダッシュボードを開く）
  python cli.py serve --demo         作成済みのデモデータでダッシュボードを開く
"""
import argparse
import os
import sys
from pathlib import Path

if "demo" in sys.argv or "--demo" in sys.argv:
    os.environ["MENU_OUTREACH_DB"] = str(Path(__file__).resolve().parent / "data" / "demo.db")

from app.core import init_db, load_env


def main():
    load_env()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("collect")
    p.add_argument("--area", action="append")
    p.add_argument("--keyword", action="append")
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true", help="取得済みの組み合わせも再取得")
    p = sub.add_parser("enrich")
    p.add_argument("--limit", type=int, default=200)
    sub.add_parser("score")
    sub.add_parser("plan")
    p = sub.add_parser("email")
    p.add_argument("--live", action="store_true", help="本当に送信する")
    sub.add_parser("inbox")
    p = sub.add_parser("reflect")
    p.add_argument("--no-propose", action="store_true")
    sub.add_parser("daily")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--demo", action="store_true", help="デモデータで開く")
    p = sub.add_parser("demo")
    p.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    init_db()
    from app import collect, enrich, improve, outreach
    if a.cmd == "init":
        outreach.seed_variants()
        print("初期化しました。次は `python cli.py collect`")
    elif a.cmd == "collect":
        collect.run(a.area, a.keyword, a.limit, a.force)
    elif a.cmd == "enrich":
        enrich.run(a.limit)
    elif a.cmd == "score":
        enrich.score_all()
    elif a.cmd == "plan":
        outreach.plan()
    elif a.cmd == "email":
        outreach.send_emails(live=True if a.live else None)
    elif a.cmd == "inbox":
        outreach.check_inbox()
    elif a.cmd == "reflect":
        improve.reflect(propose=not a.no_propose)
    elif a.cmd == "daily":
        outreach.check_inbox()
        outreach.plan()
        outreach.send_emails()
    elif a.cmd == "demo":
        from app import demo
        from app.web import serve
        demo.seed()
        serve(a.port)
    elif a.cmd == "serve":
        from app.web import serve
        serve(a.port)


if __name__ == "__main__":
    main()
