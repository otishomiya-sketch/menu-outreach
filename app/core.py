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
    for key in ("person", "company", "address", "email", "contact"):
        if os.environ.get(f"SENDER_{key.upper()}"):
            cfg["sender"][key] = os.environ[f"SENDER_{key.upper()}"]
    if os.environ.get("EMAIL_LIVE"):
        cfg["channels"]["email_live"] = os.environ["EMAIL_LIVE"].lower() in ("1", "true", "yes")
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


PREFS = ("北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 "
         "新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 "
         "奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 "
         "長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県").split()


def prefecture_of(address):
    for p in PREFS:
        if p in (address or ""):
            return p
    return None


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
