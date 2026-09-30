"""③送る: 文面の作成・送信計画・メール自動送信・返信の取り込み・結果の記録。

チャネルごとの方針
  instagram … ダッシュボードで「コピーしてDMを開く」→ 大宮さんが送信 → 「送信した」を押す（手動送信）
  line      … 同上。店のLINE公式アカウントを友だち追加して送る（手動送信）
  email     … 公開メールアドレスに自動送信。特定電子メール法の表示（送信者名・住所・配信停止方法）を必ず付ける
"""
import email.utils
import imaplib
import poplib
import os
import random
import re
import smtplib
import time
from email import message_from_bytes
from email.header import decode_header, make_header
from email.mime.text import MIMEText

import yaml

from . import bandit
from .core import (CHANNELS, ROOT, STAGES, TERMINAL, db, is_suppressed, settings,
                   suppression_keys)

REPLY_CHANNEL = {"instagram": "このDM", "line": "このLINE", "email": "このメールへのご返信"}
OPTOUT_RE = re.compile(r"配信停止|停止|不要|結構です|お断り|送らないで|unsubscribe", re.I)


# ---------- 文面 ----------

class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def trial_url(cfg, shop, variant_id, channel):
    url = cfg["service"]["trial_url"]
    if cfg["service"].get("append_ref"):
        sep = "&" if "?" in url else "?"
        url += f"{sep}ref={shop['ref_code']}-{channel[:2]}{variant_id}"
    return url


def _col(row, key):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def render(variant, shop, channel, cfg=None):
    cfg = cfg or settings()
    owner = (shop["owner_name"] or "").strip()
    vals = _Safe(
        greeting=f"{shop['name']} {owner}さま" if owner else f"{shop['name']} ご担当者さま",
        shop_name=shop["name"],
        trial_url=trial_url(cfg, shop, variant["id"], channel),
        reply_channel=REPLY_CHANNEL[channel],
        sender_person=cfg["sender"]["person"],
        found_via="Instagram" if _col(shop, "instagram") else "お店の情報",
    )
    body = variant["body"].format_map(vals).strip()
    subject = (variant["subject"] or "").format_map(vals)
    if channel == "email":
        body += "\n\n" + email_footer(cfg)
    return subject, body


def email_footer(cfg):
    s = cfg["sender"]
    lines = ["――――――――――――――――",
             f"送信者：{s['company']}　{s['person']}",
             f"住所：{s['address']}"]
    if s.get("contact"):
        lines.append(f"連絡先：{s['contact']}")
    lines += ["今後このようなご案内が不要な場合は、本文に「配信停止」とだけ書いてご返信ください。",
              "以後お送りいたしません。"]
    return "\n".join(lines)


def seed_variants():
    data = yaml.safe_load((ROOT / "templates" / "variants.yaml").read_text(encoding="utf-8"))
    with db() as conn:
        for v in data["variants"]:
            conn.execute("""INSERT OR IGNORE INTO variants (name,subject,body,status,rationale)
                            VALUES (?,?,?,?,?)""", (v["name"], v.get("subject"), v["body"], v["status"],
                                                   v.get("rationale")))


# ---------- 計画 ----------

def _available(shop):
    return {
        "instagram": bool(shop["instagram"]),
        "email": bool(shop["email"]) and not shop["email_refused"],
        "line": bool(shop["line_id"]),
    }


def plan():
    """今日送る分をキューに積む。上限・重複・配信停止・再アプローチ間隔を守る。"""
    cfg = settings()
    ch = cfg["channels"]
    with db() as conn:
        queued = {c: conn.execute(
            """SELECT COUNT(*) FROM touches WHERE channel=? AND planned_on=date('now','localtime')
               AND status IN ('queued','sent')""", (c,)).fetchone()[0] for c in CHANNELS}
        room = {c: max(0, ch["daily_limit"][c] - queued[c]) for c in CHANNELS}
        cands = conn.execute(
            """SELECT s.*,
                  (SELECT COUNT(*) FROM touches t WHERE t.shop_id=s.id AND t.status IN ('sent','queued')) AS n_touch,
                  (SELECT MAX(t.sent_at) FROM touches t WHERE t.shop_id=s.id AND t.status='sent') AS last_touch,
                  (SELECT GROUP_CONCAT(t.channel) FROM touches t WHERE t.shop_id=s.id AND t.status IN ('sent','queued')) AS used
                FROM shops s
                WHERE s.stage IN ('new','contacted') AND s.score IS NOT NULL
                ORDER BY s.score DESC""").fetchall()
        added = {c: 0 for c in CHANNELS}
        for s in cands:
            if not any(room.values()):
                break
            if is_suppressed(conn, *suppression_keys(s)):
                continue
            used = set((s["used"] or "").split(",")) - {""}
            if s["n_touch"] >= ch["max_touches_per_shop"]:
                continue
            if s["n_touch"] and conn.execute(
                    "SELECT julianday('now','localtime') - julianday(?) < ?",
                    (s["last_touch"], ch["followup_after_days"])).fetchone()[0]:
                continue
            if conn.execute("SELECT 1 FROM touches WHERE shop_id=? AND status='queued'", (s["id"],)).fetchone():
                continue
            avail = _available(s)
            channel = next((c for c in ch["priority"] if avail[c] and c not in used and room[c] > 0), None)
            if not channel:
                continue
            variant = bandit.choose(conn, channel)
            if not variant:
                raise SystemExit("有効(active)な文面がありません。ダッシュボードの「改善」で承認してください")
            _, body = render(variant, s, channel, cfg)
            conn.execute("INSERT INTO touches (shop_id,channel,variant_id,message) VALUES (?,?,?,?)",
                         (s["id"], channel, variant["id"], body))
            room[channel] -= 1
            added[channel] += 1
    print("[plan] 本日のキューに追加: " + " / ".join(f"{c} {n}件" for c, n in added.items()))
    return added


# ---------- 記録 ----------

def mark_sent(conn, touch_id, status="sent", error=None):
    t = conn.execute("SELECT * FROM touches WHERE id=?", (touch_id,)).fetchone()
    conn.execute("UPDATE touches SET status=?, sent_at=datetime('now','localtime'), error=? WHERE id=?",
                 (status, error, touch_id))
    if status == "sent":   # ドライランは送信扱いにしない（本番に切り替えたら改めて送られる）
        conn.execute("UPDATE shops SET stage='contacted' WHERE id=? AND stage='new'", (t["shop_id"],))


def record_outcome(conn, shop_id, kind, note=None, touch_id=None):
    """replied / trial / paid / declined / unsubscribed / unreachable / note"""
    if touch_id is None:
        row = conn.execute("""SELECT id FROM touches WHERE shop_id=? AND status='sent'
                              ORDER BY sent_at DESC LIMIT 1""", (shop_id,)).fetchone()
        touch_id = row and row["id"]
    conn.execute("INSERT INTO events (shop_id,touch_id,kind,note) VALUES (?,?,?,?)",
                 (shop_id, touch_id, kind, note))
    shop = conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    if kind in STAGES:
        cur = STAGES.index(shop["stage"]) if shop["stage"] in STAGES else -1
        if STAGES.index(kind) > cur:
            conn.execute("UPDATE shops SET stage=? WHERE id=?", (kind, shop_id))
    elif kind in TERMINAL:
        conn.execute("UPDATE shops SET stage=? WHERE id=?", (kind, shop_id))
        conn.execute("DELETE FROM touches WHERE shop_id=? AND status='queued'", (shop_id,))
        if kind in ("declined", "unsubscribed"):
            for key in suppression_keys(shop):
                conn.execute("INSERT OR IGNORE INTO suppression (value,reason) VALUES (?,?)", (key, kind))


# ---------- メール送信 ----------

def _check_sender(cfg):
    s = cfg["sender"]
    missing = [k for k in ("company", "address", "email") if not s.get(k)]
    if missing:
        raise SystemExit(f"config/settings.yaml の sender.{', sender.'.join(missing)} が空です。"
                         "特定電子メール法の表示義務のため、メールは送信しません。")
    for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"):
        if not os.environ.get(k):
            raise SystemExit(f".env の {k} が空です")
    if os.environ["SMTP_USER"].lower() != s["email"].lower():
        raise SystemExit(f"送信元アドレス（SENDER_EMAIL={s['email']}）とSMTPのユーザー（SMTP_USER={os.environ['SMTP_USER']}）が"
                         "違います。なりすまし判定で届かなくなるため、同じアドレスにそろえてください。")


def send_emails(live=None):
    cfg = settings()
    live = cfg["channels"]["email_live"] if live is None else live
    if live:
        _check_sender(cfg)
    lo, hi = cfg["channels"]["email_interval_sec"]
    with db() as conn:
        rows = conn.execute(
            """SELECT t.id AS touch_id, t.variant_id, s.* FROM touches t JOIN shops s ON s.id=t.shop_id
               WHERE t.channel='email' AND t.status='queued' ORDER BY s.score DESC""").fetchall()
    print(f"[email] 送信対象 {len(rows)}件（{'本番' if live else 'ドライラン：実際には送りません'}）")
    smtp = None
    try:
        for i, r in enumerate(rows):
            with db() as conn:
                if is_suppressed(conn, *suppression_keys(r)):
                    mark_sent(conn, r["touch_id"], "skipped", "配信停止リスト")
                    continue
                v = conn.execute("SELECT * FROM variants WHERE id=?", (r["variant_id"],)).fetchone()
            subject, body = render(v, r, "email", cfg)
            if not live:
                with db() as conn:
                    conn.execute("UPDATE touches SET message=? WHERE id=?", (body, r["touch_id"]))
                    mark_sent(conn, r["touch_id"], "dryrun")
                print(f"  (dry) {r['name']} <{r['email']}> 件名: {subject}")
                continue
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = subject
            msg["From"] = email.utils.formataddr((f"{cfg['sender']['company']} {cfg['sender']['person']}",
                                                  cfg["sender"]["email"]))
            msg["To"] = r["email"]
            msg["Date"] = email.utils.formatdate(localtime=True)
            msg["Message-ID"] = email.utils.make_msgid()
            msg["List-Unsubscribe"] = f"<mailto:{cfg['sender']['email']}?subject=配信停止>"
            try:
                if smtp is None:
                    smtp = smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 465)))
                    smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                smtp.send_message(msg)
                with db() as conn:
                    conn.execute("UPDATE touches SET message=? WHERE id=?", (body, r["touch_id"]))
                    mark_sent(conn, r["touch_id"])
                print(f"  送信 {r['name']} <{r['email']}>")
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError) as e:
                with db() as conn:
                    mark_sent(conn, r["touch_id"], "failed", str(e)[:200])
            if i < len(rows) - 1:
                time.sleep(random.uniform(lo, hi))
    finally:
        if smtp:
            smtp.quit()


# ---------- 返信の取り込み（IMAP） ----------

def _text_of(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                return part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
        return ""
    return msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")


QUOTE_START_RE = re.compile(r"^(On .+wrote:|.+のメッセージ:|.+wrote:|-----Original Message|――――)", re.M)


def _own_part(text):
    """引用部分（こちらの元メール。フッターに「配信停止」を含む）を除いた、相手が書いた部分だけ。"""
    m = QUOTE_START_RE.search(text)
    if m:
        text = text[:m.start()]
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))


def _imap_messages(days):
    im = imaplib.IMAP4_SSL(os.environ["IMAP_HOST"], int(os.environ.get("IMAP_PORT", 993)))
    im.login(os.environ.get("IMAP_USER") or os.environ["SMTP_USER"],
             os.environ.get("IMAP_PASSWORD") or os.environ["SMTP_PASSWORD"])
    im.select("INBOX", readonly=True)
    since = time.strftime("%d-%b-%Y", time.localtime(time.time() - days * 86400))
    _, ids = im.search(None, f'(SINCE "{since}")')
    try:
        for num in ids[0].split():
            _, data = im.fetch(num, "(BODY.PEEK[])")
            yield message_from_bytes(data[0][1])
    finally:
        im.logout()


def _pop3_messages(limit=300):
    """POP3（ヘテムル等）。サーバー上のメールは削除せず、新しい順に最大 limit 通だけ読む。"""
    pop = poplib.POP3_SSL(os.environ["POP3_HOST"], int(os.environ.get("POP3_PORT", 995)))
    pop.user(os.environ.get("POP3_USER") or os.environ["SMTP_USER"])
    pop.pass_(os.environ.get("POP3_PASSWORD") or os.environ["SMTP_PASSWORD"])
    try:
        count = len(pop.list()[1])
        for i in range(count, max(0, count - limit), -1):
            _, lines, _ = pop.retr(i)
            yield message_from_bytes(b"\r\n".join(lines))
    finally:
        pop.quit()   # DELE していないので、メールはサーバーに残る


def check_inbox(days=14):
    """送信先からの返信を見つけて「返信あり」に、配信停止の依頼なら停止リストへ。IMAP か POP3 のどちらか。"""
    if os.environ.get("IMAP_HOST"):
        messages = _imap_messages(days)
    elif os.environ.get("POP3_HOST"):
        messages = _pop3_messages()
    else:
        print("[inbox] IMAP_HOST / POP3_HOST 未設定のためスキップ")
        return
    if not (os.environ.get("IMAP_PASSWORD") or os.environ.get("POP3_PASSWORD") or os.environ.get("SMTP_PASSWORD")):
        print("[inbox] メールのパスワード未設定のためスキップ")
        return
    n_reply = n_stop = 0
    with db() as conn:
        emails = {r["email"].lower(): r for r in conn.execute(
            "SELECT id, email, stage FROM shops WHERE email IS NOT NULL AND stage NOT IN ('new')")}
        seen_ids = {r[0] for r in conn.execute("SELECT note FROM events WHERE kind IN ('replied','unsubscribed')")}
        for msg in messages:
            sender = email.utils.parseaddr(msg.get("From", ""))[1].lower()
            shop = emails.get(sender)
            mid = msg.get("Message-ID", "")
            if not shop or f"mail:{mid}" in seen_ids:
                continue
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            text = subject + "\n" + _own_part(_text_of(msg))[:500]
            if OPTOUT_RE.search(text):
                record_outcome(conn, shop["id"], "unsubscribed", f"mail:{mid}")
                n_stop += 1
            else:
                record_outcome(conn, shop["id"], "replied", f"mail:{mid}")
                n_reply += 1
    print(f"[inbox] 返信 {n_reply}件 / 配信停止 {n_stop}件 を記録")
