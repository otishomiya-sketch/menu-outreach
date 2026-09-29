"""ダッシュボードから重い処理（収集・解析・毎日の実行・振り返り）を裏で動かす。

同時に動くのは1つだけ。出力はメモリに残し、「実行」画面に表示する。
AUTO_DAILY_AT=09:30 のように環境変数を設定したときだけ、毎日その時刻に daily を自動実行する（既定はオフ）。
"""
import contextlib
import io
import threading
import time
import traceback
from datetime import datetime

_lock = threading.Lock()
state = {"name": None, "started": None, "finished": None, "log": "", "ok": None}


class _Tee(io.StringIO):
    def write(self, s):
        state["log"] = (state["log"] + s)[-20000:]
        return len(s)


def _daily():
    from . import outreach
    outreach.check_inbox()
    outreach.plan()
    outreach.send_emails()


def _task(name, **kw):
    from . import collect, enrich, improve
    return {
        "collect": lambda: collect.run(kw.get("areas"), kw.get("keywords"), kw.get("limit")),
        "enrich": lambda: enrich.run(kw.get("limit") or 200),
        "daily": _daily,
        "reflect": lambda: improve.reflect(),
    }[name]


def start(name, **kw):
    if not _lock.acquire(blocking=False):
        return False
    state.update(name=name, started=datetime.now().strftime("%m/%d %H:%M:%S"), finished=None, log="", ok=None)

    def run():
        buf = _Tee()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                _task(name, **kw)()
            state["ok"] = True
        except BaseException:   # SystemExit（設定不足のメッセージ）も画面に出す
            state["log"] += "\n" + traceback.format_exc(limit=3)
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
                start("daily")
                if now.weekday() == 0:
                    while _lock.locked():
                        time.sleep(30)
                    start("reflect")
            time.sleep(20)

    threading.Thread(target=loop, daemon=True).start()
