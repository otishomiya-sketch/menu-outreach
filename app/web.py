"""ダッシュボード。画面のHTMLは app/templates/ にある。

- 送信キュー（Instagram / LINE）: 1件ずつ「コピーして開く」→ 送る → 「送信した」
- メール: 自動送信の設定チェックと送信状況
- 店舗リスト: 見込み度順。店ごとに反応（返信・無料体験・有料・お断り）を記録
- 改善: 文面ごとの成績、提案された文面の承認
- 実行: 収集・解析・送信・振り返りを裏で動かす

本番（PORT あり）では DASHBOARD_PASSWORD のベーシック認証が必須。
"""
import hmac
import json
import os

from flask import Flask, abort, redirect, render_template, request, send_from_directory, url_for

from . import bandit, jobs, outreach
from .core import CHANNELS, DB_PATH, ROOT, db, settings

app = Flask(__name__)

# 画面に出す英語の状態名 → 日本語
JA = {
    # 店舗のステージ
    "new": "未送信", "contacted": "送信済み", "replied": "返信あり", "trial": "無料体験", "paid": "有料契約",
    "declined": "お断り", "unsubscribed": "配信停止", "unreachable": "送信不可",
    # 送信の状態
    "queued": "送信待ち", "sent": "送信済み", "skipped": "スキップ", "failed": "失敗", "dryrun": "ドライラン",
    # チャネル
    "instagram": "Instagram DM", "email": "メール", "line": "LINE",
    # 記録の種類
    "note": "メモ",
    # 文面・仮説の状態
    "active": "配信中", "proposed": "承認待ち", "retired": "停止", "rejected": "却下",
    "testing": "検証中", "adopted": "採用",
    # 実行
    "collect": "①収集", "enrich": "②Instagram解析", "daily": "③送信だけ実行", "auto": "全自動", "reflect": "④振り返り",
}
STAGE_FILTER = ["new", "contacted", "replied", "trial", "paid", "declined", "unsubscribed", "unreachable"]
MAIL_STATUSES = ["queued", "dryrun", "sent", "failed", "skipped"]


@app.template_filter("ja")
def ja(value):
    return JA.get(value, value)


@app.context_processor
def _demo_flag():
    return {"is_demo": DB_PATH.name == "demo.db"}


@app.before_request
def _auth():
    if request.path in ("/healthz", "/api/track"):   # /api/track は独自に検証する
        return None
    pw = os.environ.get("DASHBOARD_PASSWORD")
    if not pw:
        if os.environ.get("PORT") and request.remote_addr not in ("127.0.0.1", "::1"):
            return ("DASHBOARD_PASSWORD が未設定のため、ダッシュボードを公開していません。", 503)
        return None
    a = request.authorization
    if a and hmac.compare_digest(a.username or "", os.environ.get("DASHBOARD_USER", "otis")) \
            and hmac.compare_digest(a.password or "", pw):
        return None
    return ("ログインが必要です", 401, {"WWW-Authenticate": 'Basic realm="menu-outreach"'})


@app.post("/api/track")
def api_track():
    """Menu Photo Pro から「無料体験を始めた」「有料契約した」を受け取る。
    body: {"ref": "a1b2c3d4-in1", "event": "trial" | "paid", "code": "デモコード", "shop_name": "任意"}
    有料契約は ref がなくても、無料体験のときに送られたデモコードで店を特定する。
    TRACK_TOKEN を設定した場合は、ヘッダー X-Track-Token が一致しないと受け付けない。"""
    token = os.environ.get("TRACK_TOKEN")
    if token and not hmac.compare_digest(request.headers.get("X-Track-Token", ""), token):
        return {"ok": False, "error": "token"}, 403
    data = request.get_json(silent=True) or request.form
    result = outreach.track_event(data.get("ref", ""), data.get("event", ""), data.get("shop_name"), data.get("code"))
    return result, (200 if result["ok"] else 400)


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/demo-img/<path:name>")
def demo_img(name):
    return send_from_directory(ROOT / "data" / "demo", name)


@app.route("/")
def home():
    cfg = settings()
    with db() as c:
        funnel = {r["stage"]: r["n"] for r in c.execute("SELECT stage, COUNT(*) n FROM shops GROUP BY stage")}
        # 送信数は「今日送ったもの」（いつリストに入ったかは問わない）、残りは送信待ちの全件
        today = {r["channel"]: dict(r) for r in c.execute(
            """SELECT channel,
                      SUM(status='queued') q,
                      SUM(status='sent' AND date(sent_at)=date('now','localtime')) s,
                      SUM(status='dryrun' AND date(sent_at)=date('now','localtime')) d
               FROM touches GROUP BY channel""")}
        total = c.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
        scored = c.execute("SELECT COUNT(*) FROM shops WHERE score IS NOT NULL").fetchone()[0]
    return render_template("home.html", funnel=funnel, today=today, total=total, scored=scored,
                           limits=cfg["channels"]["daily_limit"], live=cfg["channels"]["email_live"])


@app.post("/plan")
def do_plan():
    outreach.plan()
    return redirect("/")


def open_url(channel, shop):
    if channel == "instagram":
        return f"https://ig.me/m/{shop['instagram']}"
    lid = shop["line_id"] or ""
    if lid.startswith("lin.ee/"):
        return "https://" + lid
    return "https://line.me/R/ti/p/" + (lid if lid.startswith("@") else "@" + lid)


@app.route("/queue/<channel>")
def queue(channel):
    if channel not in ("instagram", "line"):
        abort(404)
    with db() as c:
        t = c.execute("""SELECT t.*, s.name, s.instagram, s.line_id, s.category, s.prefecture, s.score, s.owner_name,
                                s.id AS sid, s.website, g.posts_30d, g.days_since_last, g.photo_weakness, g.thumbs, g.followers,
                                g.vision_note, g.full_name, g.external_url, g.ig_match_note, v.name AS vname
                         FROM touches t JOIN shops s ON s.id=t.shop_id LEFT JOIN ig_stats g ON g.shop_id=s.id
                         LEFT JOIN variants v ON v.id=t.variant_id
                         WHERE t.channel=? AND t.status='queued' ORDER BY s.score DESC LIMIT 1""", (channel,)).fetchone()
        left = c.execute("SELECT COUNT(*) FROM touches WHERE channel=? AND status='queued'", (channel,)).fetchone()[0]
        done = c.execute("""SELECT COUNT(*) FROM touches WHERE channel=? AND status='sent'
                            AND date(sent_at)=date('now','localtime')""", (channel,)).fetchone()[0]
    limit = settings()["channels"]["daily_limit"][channel]
    if not t:
        return render_template("queue_empty.html", done=done, limit=limit)
    sender_handle = os.environ.get("IG_SENDER_HANDLE") if channel == "instagram" else os.environ.get("LINE_SENDER_NAME")
    return render_template("queue.html", t=t, ch=channel, left=left, done=done, limit=limit, url=open_url(channel, t),
                           sender_handle=sender_handle)


@app.post("/touch/<int:tid>")
def touch_action(tid):
    action = request.form["action"]
    with db() as c:
        t = c.execute("SELECT * FROM touches WHERE id=?", (tid,)).fetchone()
        if not t:
            abort(404)
        if action == "owner":
            c.execute("UPDATE shops SET owner_name=? WHERE id=?", (request.form.get("owner") or None, t["shop_id"]))
            shop = c.execute("SELECT * FROM shops WHERE id=?", (t["shop_id"],)).fetchone()
            v = c.execute("SELECT * FROM variants WHERE id=?", (t["variant_id"],)).fetchone()
            c.execute("UPDATE touches SET message=? WHERE id=?", (outreach.render(v, shop, t["channel"])[1], tid))
        elif action == "sent":
            c.execute("UPDATE touches SET message=? WHERE id=?", (request.form.get("message"), tid))
            outreach.mark_sent(c, tid)
        elif action == "skip":
            outreach.mark_sent(c, tid, "skipped")
        elif action == "unreachable":
            outreach.mark_sent(c, tid, "failed", "送信不可")
            # このチャネルは使えないので連絡先を消し、次回は別チャネルへ
            col = "instagram" if t["channel"] == "instagram" else "line_id"
            c.execute(f"UPDATE shops SET {col}=NULL WHERE id=?", (t["shop_id"],))
    return redirect(url_for("queue", channel=t["channel"]))


@app.route("/mail")
def mail_page():
    cfg = settings()
    status = request.args.get("status", "")
    sql = """SELECT t.*, s.name, s.email, s.id AS sid, v.name AS vname, v.subject
             FROM touches t JOIN shops s ON s.id=t.shop_id LEFT JOIN variants v ON v.id=t.variant_id
             WHERE t.channel='email'"""
    args = []
    if status:
        sql += " AND t.status=?"
        args.append(status)
    sql += " ORDER BY t.id DESC LIMIT 200"
    with db() as c:
        rows = c.execute(sql, args).fetchall()
        counts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM touches WHERE channel='email' GROUP BY status")}
        today = c.execute("""SELECT COUNT(*) FROM touches WHERE channel='email' AND status='sent'
                             AND date(sent_at)=date('now','localtime')""").fetchone()[0]
    sender = cfg["sender"]
    checks = [("送信者の会社名・住所・メール", all(sender.get(k) for k in ("company", "address", "email")))]
    if outreach.mail_mode() == "resend":
        checks.append(("送信サービス Resend（RESEND_API_KEY）", True))
    else:
        smtp_user = (os.environ.get("SMTP_USER") or "").lower()
        checks += [
            ("SMTPサーバー（SMTP_HOST / SMTP_USER）", bool(os.environ.get("SMTP_HOST") and smtp_user)),
            (f"送信元とSMTPユーザーが同じ（{sender.get('email') or '未設定'}）",
             bool(sender.get("email")) and smtp_user == sender["email"].lower()),
            ("メールのパスワード（SMTP_PASSWORD）", bool(os.environ.get("SMTP_PASSWORD"))),
        ]
    checks.append(("本番送信（EMAIL_LIVE=true）", cfg["channels"]["email_live"]))
    return render_template("mail.html", rows=rows, counts=counts, today=today, checks=checks, status=status,
                           statuses=MAIL_STATUSES, limit=cfg["channels"]["daily_limit"]["email"],
                           auto=os.environ.get("AUTO_DAILY_AT"), conn_result=_last_mail_test)


_last_mail_test = []


@app.post("/mail/test")
def mail_test():
    _last_mail_test[:] = outreach.test_connection()
    return redirect("/mail")


@app.post("/mail/test-send")
def mail_test_send():
    try:
        _last_mail_test[:] = [outreach.send_test_email()]
    except SystemExit as e:   # 送信者情報の不足など
        _last_mail_test[:] = [f"✗ {e}"]
    return redirect("/mail")


@app.route("/leads")
def leads():
    pref, q, stage = request.args.get("pref", ""), request.args.get("q", ""), request.args.get("stage", "")
    sql, args = "SELECT * FROM shops WHERE 1=1", []
    if pref:
        sql += " AND prefecture=?"
        args.append(pref)
    if stage:
        sql += " AND stage=?"
        args.append(stage)
    if q:
        sql += " AND (name LIKE ? OR category LIKE ?)"
        args += [f"%{q}%"] * 2
    sql += " ORDER BY score DESC NULLS LAST LIMIT 300"
    with db() as c:
        rows = c.execute(sql, args).fetchall()
        prefs = [r[0] for r in c.execute("SELECT DISTINCT prefecture FROM shops WHERE prefecture IS NOT NULL ORDER BY 1")]
    return render_template("leads.html", rows=rows, prefs=prefs, pref=pref, q=q, stage=stage, stages=STAGE_FILTER)


@app.route("/shop/<int:sid>", methods=["GET", "POST"])
def shop(sid):
    with db() as c:
        if request.method == "POST":
            if request.form.get("kind"):
                outreach.record_outcome(c, sid, request.form["kind"], request.form.get("note") or None)
            else:
                c.execute("UPDATE shops SET owner_name=?, instagram=?, line_id=?, email=? WHERE id=?",
                          tuple(request.form.get(k) or None for k in ("owner_name", "instagram", "line_id", "email")) + (sid,))
            return redirect(url_for("shop", sid=sid))
        s = c.execute("SELECT * FROM shops WHERE id=?", (sid,)).fetchone() or abort(404)
        g = c.execute("SELECT * FROM ig_stats WHERE shop_id=?", (sid,)).fetchone()
        touches = c.execute("""SELECT t.*, v.name vname FROM touches t LEFT JOIN variants v ON v.id=t.variant_id
                               WHERE shop_id=? ORDER BY t.id DESC""", (sid,)).fetchall()
        events = c.execute("SELECT * FROM events WHERE shop_id=? ORDER BY id DESC", (sid,)).fetchall()
    return render_template("shop.html", s=s, g=g, touches=touches, events=events)


@app.route("/improve", methods=["GET", "POST"])
def improve_page():
    with db() as c:
        if request.method == "POST":
            vid, act = request.form.get("vid"), request.form["action"]
            if act in ("active", "rejected", "retired"):
                c.execute("UPDATE variants SET status=? WHERE id=?", (act, vid))
                hs = {"active": "testing", "rejected": "rejected", "retired": "rejected"}[act]
                c.execute("UPDATE hypotheses SET status=? WHERE variant_id=?", (hs, vid))
            return redirect("/improve")
        variants = c.execute("SELECT * FROM variants ORDER BY status='active' DESC, status='proposed' DESC, id").fetchall()
        perf = {ch: bandit.p_best(c, ch) for ch in CHANNELS}
        hyps = c.execute("SELECT * FROM hypotheses ORDER BY id DESC LIMIT 20").fetchall()
        rep = c.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
    notes = json.loads(rep["body"])["notes"] if rep else []
    return render_template("improve.html", variants=variants, perf=perf, chs=CHANNELS, hyps=hyps, rep=rep, notes=notes)


def _set(*names):
    return all(os.environ.get(n) for n in names)


@app.route("/settings")
def settings_page():
    """設定状況の一覧（値は表示しない）。"""
    cfg = settings()
    s = cfg["sender"]
    groups = [
        ("基本（必須）", [
            ("ダッシュボードのパスワード", _set("DASHBOARD_PASSWORD"), "DASHBOARD_PASSWORD"),
            ("店舗の収集・Instagram解析（Apify）", _set("APIFY_API_TOKEN"), "APIFY_API_TOKEN"),
            ("毎朝の自動実行", _set("AUTO_DAILY_AT"), f"AUTO_DAILY_AT（{os.environ.get('AUTO_DAILY_AT') or '未設定'}）"),
        ]),
        ("メール", [
            ("送信者の会社名・住所・メール", all(s.get(k) for k in ("company", "address", "email")),
             "SENDER_COMPANY / SENDER_ADDRESS / SENDER_EMAIL"),
            ("送信サービス（Resend）", _set("RESEND_API_KEY"), "RESEND_API_KEY（Railway では SMTP が使えないため）"),
            ("返信の取り込み（ヘテムル）", (_set("POP3_HOST") or _set("IMAP_HOST")) and _set("SMTP_USER", "SMTP_PASSWORD"),
             "POP3_HOST ＋ SMTP_USER / SMTP_PASSWORD"),
            ("本番送信", cfg["channels"]["email_live"], "EMAIL_LIVE=true"),
        ]),
        ("任意機能", [
            ("Claude（文面の自動提案）", _set("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY"),
            ("写真のAI採点", _set("ANTHROPIC_API_KEY") and cfg["scoring"]["use_vision"], "ANTHROPIC_API_KEY ＋ USE_VISION=true"),
            ("通知（Discord）", _set("DISCORD_WEBHOOK_URL"), "DISCORD_WEBHOOK_URL"),
            ("通知（LINE）", _set("LINE_CHANNEL_ACCESS_TOKEN", "LINE_TO_USER_ID"), "LINE_CHANNEL_ACCESS_TOKEN / LINE_TO_USER_ID"),
            ("Instagram公式API", _set("IG_BD_USER_ID", "IG_BD_ACCESS_TOKEN"), "IG_BD_USER_ID / IG_BD_ACCESS_TOKEN"),
        ]),
    ]
    return render_template("settings.html", groups=groups, notify_result=_last_notify)


_last_notify = []


@app.post("/settings/test-notify")
def test_notify():
    from . import notify
    _last_notify[:] = notify.send("【Menu Photo Pro 営業】通知のテストです。これが届けば設定完了です。")
    return redirect("/settings")


@app.route("/jobs", methods=["GET", "POST"])
def jobs_page():
    if request.method == "POST":
        name = request.form["name"]
        kw = {}
        if name == "collect":
            kw["areas"] = [a.strip() for a in request.form.get("areas", "").split(",") if a.strip()] or None
            kw["keywords"] = [k.strip() for k in request.form.get("keywords", "").split(",") if k.strip()] or None
            kw["limit"] = int(request.form.get("limit") or 0) or None
        elif name == "enrich":
            kw["limit"] = int(request.form.get("limit") or 200)
        jobs.start(name, **kw)
        return redirect("/jobs")
    cfg = settings()
    st = jobs.state
    return render_template("jobs.html", st=st, running=bool(st["name"] and not st["finished"]),
                           auto=os.environ.get("AUTO_DAILY_AT"), live=cfg["channels"]["email_live"],
                           areas=",".join(cfg["collect"].get("areas") or []))


def serve(port=8765):
    app.run(host="127.0.0.1", port=port, debug=False)
