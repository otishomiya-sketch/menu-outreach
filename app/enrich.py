"""②調べる: 各店の Instagram の更新頻度と写真の質を測り、見込み度スコアを付ける。

取得元（.env で自動選択）:
  1. Instagram Graph API の Business Discovery（公式）
     IG_BD_USER_ID / IG_BD_ACCESS_TOKEN が必要。Facebookログイン方式のトークンでのみ使える点に注意
     （insta-autopost-kit の「Instagram業務用ログイン」トークンでは使えない）。
     対象はビジネス / クリエイターアカウントのみ。1時間あたり約200件まで。
  2. Apify の instagram-profile-scraper（APIFY_API_TOKEN。公式APIが無いとき）
"""
import os
import time
from datetime import datetime, timezone

import requests

from . import photo
from .collect import OWNER_RE
from .core import clamp01, db, settings

GRAPH = "https://graph.facebook.com/" + os.environ.get("GRAPH_API_VERSION", "v21.0")


def _parse_ts(s):
    s = str(s).replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":   # 2024-01-01T00:00:00+0000 形式
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


def via_graph(username, n_media):
    fields = (f"business_discovery.username({username})"
              f"{{username,name,biography,followers_count,media_count,"
              f"media.limit({n_media}){{timestamp,media_type,media_url,thumbnail_url,permalink}}}}")
    r = requests.get(f"{GRAPH}/{os.environ['IG_BD_USER_ID']}",
                     params={"fields": fields, "access_token": os.environ["IG_BD_ACCESS_TOKEN"]}, timeout=20)
    data = r.json()
    if "error" in data:
        raise RuntimeError(data["error"].get("message", "graph error"))
    bd = data["business_discovery"]
    media = bd.get("media", {}).get("data", [])
    return {
        "followers": bd.get("followers_count"), "media_count": bd.get("media_count"),
        "biography": bd.get("biography") or "",
        "timestamps": [_parse_ts(m["timestamp"]) for m in media],
        "images": [m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url")
                   for m in media if m.get("media_url") or m.get("thumbnail_url")],
        "source": "graph",
    }


def via_apify(usernames, n_media):
    from apify_client import ApifyClient

    client = ApifyClient(os.environ["APIFY_API_TOKEN"])
    run = client.actor("apify/instagram-profile-scraper").call(run_input={"usernames": usernames})
    out = {}
    for it in client.dataset(run["defaultDatasetId"]).iterate_items():
        posts = (it.get("latestPosts") or [])[:n_media]
        out[(it.get("username") or "").lower()] = {
            "followers": it.get("followersCount"), "media_count": it.get("postsCount"),
            "biography": it.get("biography") or "",
            "timestamps": [_parse_ts(p["timestamp"]) for p in posts if p.get("timestamp")],
            "images": [p.get("displayUrl") for p in posts if p.get("displayUrl")],
            "source": "apify",
        }
    return out


def activity_score(posts_30d, last, cfg):
    """更新頻度（30日の投稿数）× 鮮度（最終投稿からの日数）。"""
    if last is None:
        return 0.0
    freq = clamp01(posts_30d / cfg["target_posts_30d"])
    stale = cfg["stale_after_days"]
    recency = 1.0 if last <= stale else clamp01(1 - (last - stale) / 45)
    return freq * recency


def activity_of(timestamps):
    if not timestamps:
        return 0, None
    now = datetime.now(timezone.utc)
    days = [(now - t).days for t in timestamps]
    return sum(1 for d in days if d <= 30), min(days)


def reach_of(shop):
    return (0.5 if shop["instagram"] else 0) + \
           (0.3 if shop["email"] and not shop["email_refused"] else 0) + \
           (0.2 if shop["line_id"] else 0)


def rescore(conn, shop_id):
    cfg = settings()["scoring"]
    w = cfg["weights"]
    shop = conn.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    ig = conn.execute("SELECT * FROM ig_stats WHERE shop_id=?", (shop_id,)).fetchone()
    activity = weak = 0.0
    if ig and not ig["error"]:
        activity = activity_score(ig["posts_30d"] or 0, ig["days_since_last"], cfg)
        weak = ig["photo_weakness"] or 0
        if ig["vision_weakness"] is not None:
            weak = 0.4 * weak + 0.6 * ig["vision_weakness"]
    reach = reach_of(shop)
    score = 100 * (w["activity"] * activity + w["weakness"] * weak + w["reach"] * reach)
    if activity < cfg["min_activity"]:
        score *= 0.3
    conn.execute("UPDATE shops SET score=?, activity=?, weakness=?, reach=? WHERE id=?",
                 (round(score, 1), activity, weak, reach, shop_id))


def _save(conn, shop_id, username, prof, cfg):
    posts_30d, last = activity_of(prof["timestamps"])
    imgs = [u for u in prof["images"] if u][: cfg["images_per_shop"]]
    pq = photo.analyze(imgs) or {}
    vis = None
    if cfg.get("use_vision") and imgs and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            vis = photo.vision_score(imgs, cfg["vision_model"])
        except Exception as e:
            print(f"  vision失敗: {e}")
    m = OWNER_RE.search(prof["biography"] or "")
    if m:
        conn.execute("UPDATE shops SET owner_name=COALESCE(owner_name, ?) WHERE id=?", (m.group(1).strip(), shop_id))
    conn.execute(
        """INSERT OR REPLACE INTO ig_stats (shop_id,username,followers,media_count,biography,posts_30d,
             days_since_last,brightness,contrast,warmth,sharpness,photo_weakness,vision_weakness,vision_note,
             thumbs,source,error,fetched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,datetime('now','localtime'))""",
        (shop_id, username, prof["followers"], prof["media_count"], prof["biography"], posts_30d, last,
         pq.get("brightness"), pq.get("contrast"), pq.get("warmth"), pq.get("sharpness"), pq.get("weakness"),
         vis and vis["weakness"], vis and vis["note"], "\n".join(imgs[:6]), prof["source"]))


def run(limit=200, refresh_days=30):
    cfg = settings()["scoring"]
    use_graph = bool(os.environ.get("IG_BD_USER_ID") and os.environ.get("IG_BD_ACCESS_TOKEN"))
    if not use_graph and not os.environ.get("APIFY_API_TOKEN"):
        raise SystemExit("IG_BD_* か APIFY_API_TOKEN のどちらかを .env に設定してください")
    with db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.instagram FROM shops s LEFT JOIN ig_stats g ON g.shop_id=s.id
               WHERE s.instagram IS NOT NULL AND s.stage='new'
                 AND (g.shop_id IS NULL OR g.fetched_at < datetime('now', ?))
               LIMIT ?""", (f"-{refresh_days} days", limit)).fetchall()
    print(f"[enrich] 対象 {len(rows)}店 / 取得元: {'Graph API' if use_graph else 'Apify'}")

    if use_graph:
        for i, r in enumerate(rows, 1):
            try:
                prof = via_graph(r["instagram"], cfg["images_per_shop"])
                with db() as conn:
                    _save(conn, r["id"], r["instagram"], prof, cfg)
            except Exception as e:
                with db() as conn:
                    conn.execute("INSERT OR REPLACE INTO ig_stats (shop_id,username,error) VALUES (?,?,?)",
                                 (r["id"], r["instagram"], str(e)[:200]))
            print(f"  {i}/{len(rows)} @{r['instagram']}")
            time.sleep(18)   # 200件/時 の上限に収める
    else:
        for i in range(0, len(rows), 50):
            chunk = rows[i:i + 50]
            profs = via_apify([r["instagram"] for r in chunk], cfg["images_per_shop"])
            with db() as conn:
                for r in chunk:
                    prof = profs.get(r["instagram"])
                    if prof:
                        _save(conn, r["id"], r["instagram"], prof, cfg)
                    else:
                        conn.execute("INSERT OR REPLACE INTO ig_stats (shop_id,username,error) VALUES (?,?,?)",
                                     (r["id"], r["instagram"], "取得できず（非公開・存在しない等）"))
            print(f"  {min(i + 50, len(rows))}/{len(rows)}")

    score_all()


def score_all():
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM shops")]
        for sid in ids:
            rescore(conn, sid)
    print(f"[score] {len(ids)}店のスコアを更新")
