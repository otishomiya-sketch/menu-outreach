"""設定・DB・共通ユーティリティ。"""
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get("MENU_OUTREACH_DB", ROOT / "data" / "outreach.db"))

# 顧客ステージ。数字が大きいほど前進。
STAGES = ["new", "contacted", "replied", "trial", "paid"]
TERMINAL = {"declined", "unsubscribed", "unreachable"}
CHANNELS = ["instagram", "email", "line"]


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def settings():
    """settings.yaml を読み、環境変数で上書きする。
    リポジトリは公開なので、住所などの個人情報は Railway の環境変数に置く。"""
    cfg = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    for key in ("person", "brand", "company", "address", "email", "contact"):
        if os.environ.get(f"SENDER_{key.upper()}"):
            cfg["sender"][key] = os.environ[f"SENDER_{key.upper()}"]
    if os.environ.get("EMAIL_LIVE"):
        cfg["channels"]["email_live"] = os.environ["EMAIL_LIVE"].lower() in ("1", "true", "yes")
    for ch in ("instagram", "line", "email"):   # 例: EMAIL_DAILY_LIMIT=20（新しいアドレスは少なめから始める）
        if os.environ.get(f"{ch.upper()}_DAILY_LIMIT"):
            cfg["channels"]["daily_limit"][ch] = int(os.environ[f"{ch.upper()}_DAILY_LIMIT"])
    if os.environ.get("USE_VISION"):   # 写真のAI採点（Claude）
        cfg["scoring"]["use_vision"] = os.environ["USE_VISION"].lower() in ("1", "true", "yes")
    if os.environ.get("COLLECT_AREAS"):
        cfg["collect"]["areas"] = [a.strip() for a in os.environ["COLLECT_AREAS"].split(",") if a.strip()]
    return cfg


SCHEMA = """
CREATE TABLE IF NOT EXISTS shops (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  place_url TEXT UNIQUE,
  name TEXT NOT NULL,
  owner_name TEXT,
  category TEXT,
  address TEXT,
  prefecture TEXT,
  phone TEXT,
  website TEXT,
  email TEXT,
  instagram TEXT,
  line_id TEXT,
  rating REAL,
  reviews INTEGER,
  email_refused INTEGER DEFAULT 0,      -- サイトに「営業メールお断り」等の表示あり
  search_keyword TEXT,
  search_location TEXT,
  crawled_at TEXT,
  -- 見込み度
  score REAL,
  activity REAL,
  weakness REAL,
  reach REAL,
  -- 進捗
  stage TEXT DEFAULT 'new',
  ref_code TEXT UNIQUE,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_shops_score ON shops(score DESC);

CREATE TABLE IF NOT EXISTS ig_stats (
  shop_id INTEGER PRIMARY KEY REFERENCES shops(id),
  username TEXT,
  followers INTEGER,
  media_count INTEGER,
  biography TEXT,
  posts_30d INTEGER,
  days_since_last INTEGER,
  brightness REAL, contrast REAL, warmth REAL, sharpness REAL,
  photo_weakness REAL,
  vision_weakness REAL,
  vision_note TEXT,
  thumbs TEXT,                          -- 直近画像URL（改行区切り）ダッシュボード表示用
  source TEXT,
  error TEXT,
  fetched_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS variants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE,
  subject TEXT,
  body TEXT NOT NULL,
  status TEXT DEFAULT 'proposed' CHECK (status IN ('active','proposed','retired','rejected')),
  rationale TEXT,
  parent_id INTEGER,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id INTEGER NOT NULL REFERENCES shops(id),
  channel TEXT NOT NULL,
  variant_id INTEGER REFERENCES variants(id),
  status TEXT DEFAULT 'queued' CHECK (status IN ('queued','sent','skipped','failed','dryrun')),
  planned_on TEXT DEFAULT (date('now','localtime')),
  sent_at TEXT,
  message TEXT,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_touches_shop ON touches(shop_id);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id INTEGER REFERENCES shops(id),
  touch_id INTEGER,
  kind TEXT NOT NULL,        -- replied / trial / paid / declined / unsubscribed / unreachable / note
  note TEXT,
  at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS suppression (
  value TEXT PRIMARY KEY,    -- メールアドレス / ig:ユーザー名 / line:ID / shop:ID
  reason TEXT,
  at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS collect_runs (
  keyword TEXT, location TEXT, total INTEGER,
  at TEXT DEFAULT (datetime('now','localtime')),
  PRIMARY KEY (keyword, location)
);

-- self-improving-agent-os の hypotheses を簡略化したもの
CREATE TABLE IF NOT EXISTS hypotheses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  proposal TEXT NOT NULL,
  rationale TEXT,
  expected_impact TEXT,
  verification_method TEXT,
  variant_id INTEGER REFERENCES variants(id),
  status TEXT DEFAULT 'proposed',  -- proposed / testing / adopted / rejected
  result TEXT,
  created_at TEXT DEFAULT (datetime('now','localtime'))
);

-- Menu Photo Pro のデモコードと店の対応（無料体験の時点で記録し、後日の有料契約を同じ店に結び付ける）
CREATE TABLE IF NOT EXISTS app_codes (
  code TEXT PRIMARY KEY,
  shop_id INTEGER REFERENCES shops(id),
  ref TEXT,
  at TEXT DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, body TEXT,
  at TEXT DEFAULT (datetime('now','localtime'))
);
"""


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
        # 後から足した列（既存のDBにも追加する）
        have = {r[1] for r in c.execute("PRAGMA table_info(ig_stats)")}
        for col, typ in (("full_name", "TEXT"), ("external_url", "TEXT"), ("ig_match", "REAL"), ("ig_match_note", "TEXT")):
            if col not in have:
                c.execute(f"ALTER TABLE ig_stats ADD COLUMN {col} {typ}")


def restore_sent_emails():
    """2026-10-01 の掃除で、送信済みの店から消してしまったアドレスを戻す（返信の取り込みに必要）。"""
    fixes = {"%まぶりっと%": "maburitto@wave.plala.or.jp", "%よりみち%": "shoko_kanko-kakari@town.taiki.hokkaido.jp",
             "%来よ乃%": "shimokita@kasamai-shimokita.or.jp"}
    with db() as c:
        for like, addr in fixes.items():
            rows = c.execute("""SELECT id, name FROM shops WHERE email IS NULL AND name LIKE ? AND id IN
                                (SELECT shop_id FROM touches WHERE channel='email' AND status='sent')""", (like,)).fetchall()
            if len(rows) == 1:
                c.execute("UPDATE shops SET email=? WHERE id=?", (addr, rows[0]["id"]))
                print(f"[restore] {rows[0]['name']} に送信済みのアドレスを戻しました")


def restore_lost_email():
    """2026-09-30 の宛先検査の不具合（gmail.com を mail.com と誤判定）で消した1件を戻す。
    候補がちょうど1店のときだけ戻し、それ以外は候補を記録するだけ（別の店に付けないため）。"""
    addr = "world.tea.labo.tempo@gmail.com"
    with db() as c:
        if c.execute("SELECT 1 FROM shops WHERE email=?", (addr,)).fetchone():
            return
        like = ["%tea%labo%", "%tealabo%", "%ティーラボ%", "%ティー ラボ%", "%tea labo%"]
        cond = " OR ".join(["lower(name) LIKE ?", "lower(website) LIKE ?"] * len(like))
        args = [x for p in like for x in (p, p)]
        rows = c.execute(f"SELECT id, name, website FROM shops WHERE email IS NULL AND ({cond})", args).fetchall()
        if len(rows) == 1:
            c.execute("UPDATE shops SET email=? WHERE id=?", (addr, rows[0]["id"]))
            print(f"[restore] {rows[0]['name']} にメールアドレスを戻しました")
        else:
            wide = c.execute("""SELECT name, website FROM shops WHERE email IS NULL AND
                                (lower(name) LIKE '%tea%' OR name LIKE '%ティー%' OR lower(website) LIKE '%tea%') LIMIT 10""").fetchall()
            print(f"[restore] 候補 {len(rows)}店のため戻していません。参考: " +
                  " / ".join(f"{r['name']} {r['website'] or ''}" for r in wide))


def clean_emails():
    """保存済みの宛先を検査し、送ってはいけないアドレスを消す（送信待ちのメールも取り消す）。何度実行しても同じ結果。"""
    from .collect import valid_email
    with db() as c:
        # すでにメールを送った店のアドレスは残す（返信を取り込むときに店を特定するため）
        bad = [r for r in c.execute("""SELECT id, email FROM shops WHERE email IS NOT NULL AND id NOT IN
                                       (SELECT shop_id FROM touches WHERE channel='email' AND status='sent')""")
               if not valid_email(r["email"])]
        for r in bad:
            c.execute("UPDATE shops SET email=NULL WHERE id=?", (r["id"],))
            c.execute("DELETE FROM touches WHERE shop_id=? AND channel='email' AND status IN ('queued','dryrun')", (r["id"],))
    if bad:
        print(f"[clean] 送信できない宛先 {len(bad)}件を削除: " + ", ".join(r["email"][:40] for r in bad[:10]))
    # Instagramアカウントがその店のものと確認できていないDMの送信待ちを取り消す（翌朝の解析で確認し直す）
    with db() as c:
        unverified = c.execute("""SELECT t.id FROM touches t LEFT JOIN ig_stats g ON g.shop_id=t.shop_id
                                  WHERE t.channel='instagram' AND t.status='queued'
                                    AND (g.shop_id IS NULL OR g.error IS NOT NULL OR COALESCE(g.ig_match, 0) < 0.6)""").fetchall()
        for r in unverified:
            c.execute("DELETE FROM touches WHERE id=?", (r["id"],))
    if unverified:
        print(f"[clean] アカウント未確認のDM送信待ち {len(unverified)}件を取り消し（翌朝の解析で確認後、確認できた店だけ戻します）")
    # 飲食店ではない店（ホテル・キャンプ場・道の駅など）の送信待ちを取り消す
    from .collect import is_food_shop
    with db() as c:
        non_food = [r for r in c.execute("""SELECT DISTINCT s.id, s.name, s.category FROM shops s JOIN touches t ON t.shop_id=s.id
                                           WHERE t.status IN ('queued','dryrun')""") if not is_food_shop(r["category"])]
        for r in non_food:
            c.execute("DELETE FROM touches WHERE shop_id=? AND status IN ('queued','dryrun')", (r["id"],))
    if non_food:
        print(f"[clean] 飲食店ではない {len(non_food)}店の送信待ちを取り消し: " +
              ", ".join(f"{r['name']}（{r['category']}）" for r in non_food[:10]))
    return len(bad)


PREFS = ("北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 "
         "新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 "
         "奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 "
         "長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県").split()


def prefecture_of(address):
    for p in PREFS:
        if p in (address or ""):
            return p
    return None


def anthropic_client():
    """Claude のクライアント。ワークスペースに属さないキーのときは ANTHROPIC_WORKSPACE_ID を付けて送る。"""
    import anthropic
    headers = {}
    if os.environ.get("ANTHROPIC_WORKSPACE_ID"):
        headers["anthropic-workspace-id"] = os.environ["ANTHROPIC_WORKSPACE_ID"].strip()
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(), default_headers=headers or None)


def clamp01(x):
    return max(0.0, min(1.0, x))


def is_suppressed(conn, *values):
    vals = [v for v in values if v]
    if not vals:
        return False
    q = "SELECT 1 FROM suppression WHERE value IN (%s)" % ",".join("?" * len(vals))
    return conn.execute(q, vals).fetchone() is not None


def suppression_keys(shop):
    keys = [f"shop:{shop['id']}"]
    if shop["email"]:
        keys.append(shop["email"].lower())
    if shop["instagram"]:
        keys.append("ig:" + shop["instagram"].lower())
    if shop["line_id"]:
        keys.append("line:" + shop["line_id"].lower())
    return keys


IG_RESERVED = {"p", "reel", "reels", "explore", "stories", "accounts", "tv", "about", "developer", "legal"}


def normalize_ig(value):
    """URL / @handle どちらでもユーザー名にそろえる。"""
    if not value:
        return None
    v = value.strip()
    m = re.search(r"instagram\.com/([A-Za-z0-9_.]+)", v)
    if m:
        v = m.group(1)
    v = v.lstrip("@").strip("/").lower()
    if not re.fullmatch(r"[a-z0-9_.]{1,30}", v) or v in IG_RESERVED:
        return None
    return v
