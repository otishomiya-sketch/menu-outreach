"""ダッシュボードから重い処理（収集・解析・毎日の実行・振り返り）を裏で動かす。

同時に動くのは1つだけ。出力はメモリに残し、「実行」画面に表示する。
AUTO_DAILY_AT=09:30 のように環境変数を設定したときだけ、毎日その時刻に auto（返信取り込み→収集→解析→送信リスト→メール→通知）を実行する。
"""
import contextlib
import io
import re
import sys
import threading
import time
import traceback
from datetime import datetime

_lock = threading.Lock()
state = {"name": None, "started": None, "finished": None, "log": "", "ok": None, "error": None}
ANSI = re.compile(r"\x1b\[[0-9;]*m|\[\d+m")


class _Tee(io.StringIO):
    """画面用に保存しつつ、Railway のログ（本来の標準出力）にも出す。"""
    def write(self, s):
        s = ANSI.sub("", s)
        state["log"] = (state["log"] + s)[-20000:]
        try:
            sys.__stdout__.write(s)
            sys.__stdout__.flush()
        except Exception:
            pass
        return len(s)


def _step(name, fn, report):
    try:
        fn()
        report.append(f"✓ {name}")
    except BaseException as e:   # 1つ失敗しても残りは続ける
        print(f"[daily] {name} 失敗: {e}")
        report.append(f"✗ {name}（{str(e)[:80]}）")


def run_daily(full=False):
    """毎朝の処理。full=True（自動実行）では収集と解析も行う。"""
    import os

    from . import collect, enrich, notify, outreach
    from .core import db, settings
    report = []
    _step("返信の取り込み", outreach.check_inbox, report)
    if full:
        _step("店舗の収集", collect.run_auto, report)
        _step("Instagram解析", lambda: enrich.run(int(os.environ.get("AUTO_ENRICH_LIMIT") or 150)), report)
    _step("送信リスト作成", outreach.plan, report)
    _step("メール送信", outreach.send_emails, report)
    if full:
        with db() as c:
            q = {r["channel"]: r["n"] for r in c.execute(
                "SELECT channel, COUNT(*) n FROM touches WHERE status='queued' GROUP BY channel")}
            total = c.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
            mails = c.execute("""SELECT COUNT(*) FROM touches WHERE channel='email' AND status IN ('sent','dryrun')
                                 AND date(sent_at)=date('now','localtime')""").fetchone()[0]
            funnel = {r["stage"]: r["n"] for r in c.execute("SELECT stage, COUNT(*) n FROM shops GROUP BY stage")}
        live = settings()["channels"]["email_live"]
        url = os.environ.get("PUBLIC_URL") or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
                                                if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
        notify.send("\n".join([
            "【Menu Photo Pro 営業】今朝の自動実行",
            f"📷 Instagram DM：{q.get('instagram', 0)}件 準備できました",
            f"💬 LINE：{q.get('line', 0)}件",
            f"✉️ メール：{mails}件 {'送信' if live else '（ドライラン）'}",
            f"収集済み {total}店 / 返信 {funnel.get('replied', 0)}・体験 {funnel.get('trial', 0)}・有料 {funnel.get('paid', 0)}",
            " ".join(report),
            f"{url}/queue/instagram" if url else "",
        ]))


def _task(name, **kw):
    from . import collect, enrich, improve
    return {
        "collect": lambda: collect.run(kw.get("areas"), kw.get("keywords"), kw.get("limit")),
        "enrich": lambda: enrich.run(kw.get("limit") or 200),
        "daily": run_daily,
        "auto": lambda: run_daily(full=True),
        "reflect": lambda: improve.reflect(),
    }[name]


def start(name, **kw):
    if not _lock.acquire(blocking=False):
        return False
    state.update(name=name, started=datetime.now().strftime("%m/%d %H:%M:%S"), finished=None, log="", ok=None, error=None)

    def run():
        buf = _Tee()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                _task(name, **kw)()
            state["ok"] = True
        except BaseException as e:   # SystemExit（設定不足のメッセージ）も画面に出す
            tb = traceback.format_exc(limit=4)
            state["error"] = f"{type(e).__name__}: {e}"
            state["log"] += "\n" + tb
            sys.__stdout__.write(f"[job {name}] 失敗\n{tb}\n")
            sys.__stdout__.flush()
            state["ok"] = False
        finally:
            state["finished"] = datetime.now().strftime("%m/%d %H:%M:%S")
            _lock.release()

    threading.Thread(target=run, daemon=True).start()
    return True


def start_scheduler(at):
    """at = "HH:MM"（サーバーのTZ。Railway では TZ=Asia/Tokyo を設定する）"""
    def loop():
        last = None
        while True:
            now = datetime.now()
            if now.strftime("%H:%M") == at and last != now.date():
                last = now.date()
                start("auto")
                if now.weekday() == 0:
                    while _lock.locked():
                        time.sleep(30)
                    start("reflect")
            time.sleep(20)

    threading.Thread(target=loop, daemon=True).start()
