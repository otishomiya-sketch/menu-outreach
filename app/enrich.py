"""②調べる: 各店の Instagram の更新頻度と写真の質を測り、見込み度スコアを付ける。

取得元（.env で自動選択）:
  1. Instagram Graph API の Business Discovery（公式）
     IG_BD_USER_ID / IG_BD_ACCESS_TOKEN が必要。Facebookログイン方式のトークンでのみ使える点に注意
     （insta-autopost-kit の「Instagram業務用ログイン」トークンでは使えない）。
     対象はビジネス / クリエイターアカウントのみ。1時間あたり約200件まで。
  2. Apify の instagram-profile-scraper（APIFY_API_TOKEN。公式APIが無いとき）
"""
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from . import photo
from .collect import OWNER_RE, dataset_id
from .core import clamp01, db, settings

GRAPH = "https://graph.facebook.com/" + os.environ.get("GRAPH_API_VERSION", "v26.0")


def _parse_ts(s):
    s = str(s).replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":   # 2024-01-01T00:00:00+0000 形式
        s = s[:-2] + ":" + s[-2:]
    return datetime.fromisoformat(s)


class GraphFatal(Exception):
    """トークン切れ・権限不足・回数制限など、続けても全部失敗するエラー。公式APIをその回は使わない。"""


class GraphSkip(Exception):
    """この1件だけ取れない（個人アカウント・非公開・存在しない等）。Apifyで代わりに取る。"""


FATAL_CODES = {190, 102, 10, 4, 17, 32, 613}   # トークン無効・権限・回数制限


def _graph_error(err):
    code = err.get("code")
    msg = f"{err.get('message', 'graph error')}（code {code}）"
    if code in FATAL_CODES or (isinstance(code, int) and 200 <= code < 300):
        return GraphFatal(msg)
    return GraphSkip(msg)


def check_graph_token():
    """公式APIのトークンが使えるか・期限が近くないかを確かめる。問題があれば理由の文字列、なければ None。"""
    token = os.environ["IG_BD_ACCESS_TOKEN"].strip()
    try:
        r = requests.get(f"{GRAPH}/debug_token", params={"input_token": token, "access_token": token}, timeout=20)
        d = r.json().get("data", {})
    except Exception as e:
        return f"トークンを確認できませんでした（{e}）"
    if not d.get("is_valid"):
        return "トークンが無効です（期限切れ・取り消しなど）"
    exp = d.get("expires_at") or 0          # 0 = 無期限（システムユーザー）
    if exp and exp - time.time() < 10 * 86400:
        days = max(0, int((exp - time.time()) // 86400))
        send_notice(f"Instagram公式APIのトークンが、あと{days}日で期限切れになります。作り直してRailwayの IG_BD_ACCESS_TOKEN を更新してください。")
    return None


def send_notice(text):
    try:
        from . import notify
        notify.send("【Menu Photo Pro 営業】" + text)
    except Exception as e:
        print(f"  通知に失敗: {e}")


def via_graph(username, n_media):
    fields = (f"business_discovery.username({username})"
              f"{{username,name,biography,website,followers_count,media_count,"
              f"media.limit({n_media}){{timestamp,media_type,media_url,thumbnail_url,permalink}}}}")
    r = requests.get(f"{GRAPH}/{os.environ['IG_BD_USER_ID']}",
                     params={"fields": fields, "access_token": os.environ["IG_BD_ACCESS_TOKEN"]}, timeout=20)
    data = r.json()
    if "error" in data:
        raise _graph_error(data["error"])
    bd = data.get("business_discovery")
    if not bd:
        raise GraphSkip("business_discovery が空")
    media = bd.get("media", {}).get("data", [])
    return {
        "followers": bd.get("followers_count"), "media_count": bd.get("media_count"),
        "biography": bd.get("biography") or "", "full_name": bd.get("name") or "", "external_url": bd.get("website") or "",
        "timestamps": [_parse_ts(m["timestamp"]) for m in media],
        "images": [m.get("thumbnail_url") if m.get("media_type") == "VIDEO" else m.get("media_url")
                   for m in media if m.get("media_url") or m.get("thumbnail_url")],
        "source": "graph",
    }


def via_apify(usernames, n_media):
    from apify_client import ApifyClient

    client = ApifyClient(os.environ["APIFY_API_TOKEN"])
    run = client.actor("apify/instagram-profile-scraper").call(logger=None, run_input={"usernames": usernames})
    out = {}
    for it in client.dataset(dataset_id(run)).iterate_items():
        posts = (it.get("latestPosts") or [])[:n_media]
        out[(it.get("username") or "").lower()] = {
            "followers": it.get("followersCount"), "media_count": it.get("postsCount"),
            "biography": it.get("biography") or "", "full_name": it.get("fullName") or "",
            "external_url": it.get("externalUrl") or "",
            "timestamps": [_parse_ts(p["timestamp"]) for p in posts if p.get("timestamp")],
            "images": [p.get("displayUrl") for p in posts if p.get("displayUrl")],
            "source": "apify",
        }
    return out


GENERIC_WORDS = ("株式会社", "有限会社", "本店", "支店", "店舗", "公式", "official", "の店", "店", "レストラン", "restaurant",
                 "カフェ", "cafe", "居酒屋", "食堂", "ラーメン", "焼肉", "バー", "bar", "キッチン", "kitchen")


def _norm(text):
    """比較用に、全角半角・大文字小文字・ひらがなカタカナ・記号の違いをそろえる。"""
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = "".join(chr(ord(ch) + 0x60) if "ぁ" <= ch <= "ゖ" else ch for ch in t)   # ひらがな → カタカナ
    return re.sub(r"[\s\W_]+", "", t)


def _host(url):
    h = urlparse(url if "//" in (url or "") else "//" + (url or "")).netloc.lower()
    return h[4:] if h.startswith("www.") else h


# グルメサイト・地域情報誌・まとめサービスなど、たくさんの店を紹介する側のアカウント
PORTAL_WORDS = ("ヒトサラ", "hitosara", "食べログ", "tabelog", "ぐるなび", "gnavi", "ホットペッパー", "hotpepper", "retty",
                "一休", "ikyu", "ozmall", "じゃらん", "jalan", "スナカラ", "snakara", "タウン情報", "情報誌", "グルメ情報",
                "グルメガイド", "magazine", "マガジン", "フリーペーパー", "s-style", "エススタイル", "ぐるめ", "gourmet_",
                "_gourmet", "グルメ部", "まとめ", "公式メディア", "web media", "webマガジン")
# 店のサイトとしては扱わないドメイン（店ごとのページが同じドメインに並ぶサービス）
PORTAL_HOSTS = ("hitosara.com", "tabelog.com", "gnavi.co.jp", "hotpepper.jp", "retty.me", "ikyu.com", "ozmall.co.jp",
                "jalan.net", "snakara.jp", "instagram.com", "facebook.com", "linktr.ee", "lit.link", "ameblo.jp",
                "machinavi.com", "favy.jp", "yelp.com", "tripadvisor.jp", "google.com", "goo.gl", "x.com", "twitter.com")
FACILITY_EN = ("park", "hotel", "resort", "museum", "zoo", "station", "airport", "onsen", "michinoeki", "mall", "aquarium")


def match_score(shop, prof, site_shared=False):
    """Instagramアカウントがその店のものか（0〜1）と、判定の理由。0.6以上を一致とみなす。
    site_shared: 店の「公式サイト」と同じドメインを、ほかの店も使っている（グルメサイトの掲載ページなど）"""
    from .collect import NOT_FOOD_WORDS
    fname = (prof.get("full_name") or "").lower()
    uname = (prof.get("username") or "").lower()
    # グルメサイト・情報誌など、紹介する側のアカウント
    portal = next((w for w in PORTAL_WORDS if w.lower() in fname or w.lower() in uname), None)
    if portal:
        return 0.2, f"紹介サイト・情報誌のアカウント（「{portal}」）"
    # 道の駅・ホテル・公園など、施設全体のアカウント（中の飲食店のものではない）
    facility = next((w for w in NOT_FOOD_WORDS + FACILITY_EN if w.lower() in fname or w.lower() in uname), None)
    if facility:
        return 0.3, f"施設のアカウント（「{facility}」）"
    site, link = _host(shop["website"] or ""), _host(prof.get("external_url") or "")
    site_is_shop = site and not site_shared and not any(site == h or site.endswith("." + h) for h in PORTAL_HOSTS)
    if site_is_shop and link and (site == link or site.endswith("." + link) or link.endswith("." + site)):
        return 1.0, "プロフィールのリンクが公式サイトと同じ"
    def core(text):
        n = _norm(text)
        for w in GENERIC_WORDS:
            n = n.replace(_norm(w), "")
        return n if len(n) >= 2 else _norm(text)

    # 「らーめん山頭火 旭川本店」のような支店名つきは、空白より前（ブランド名）でも比べる
    first = re.split(r"[\s　]+", (shop["name"] or "").strip())[0]
    candidates = [c for c in dict.fromkeys([core(shop["name"]), core(first)]) if len(c) >= 2]
    name = candidates[0] if candidates else ""
    target = _norm(prof.get("full_name")) + _norm(prof.get("biography")) + _norm(prof.get("username"))
    if candidates and candidates[0] in target:
        return 0.9, "名前か自己紹介に店名がある"
    if any(c in target for c in candidates[1:]):
        # 支店名を除いたブランド名だけが一致。本部・ブランド全体の公式アカウントのことがある
        return 0.65, "ブランド名だけ一致（本部・ブランドのアカウントの可能性。支店のアカウントか確認）"
    grams = {name[i:i + 2] for i in range(len(name) - 1)}
    if grams:
        ratio = sum(1 for g in grams if g in target) / len(grams)
        if ratio >= 0.6:
            return 0.7, f"店名の大部分が一致（{ratio:.0%}）"
        return round(ratio * 0.5, 2), f"店名と一致しない（{ratio:.0%}）"
    return 0.0, "比べられる店名がない"


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


_vision = {"off": False}   # 設定の問題で失敗したら、その回の残りは試さない


def _save(shop_id, username, prof, cfg):
    """写真のダウンロードと採点（時間がかかる）を先に済ませ、DBへの書き込みは最後に短く行う。
    書き込み中はDBがふさがり、ダッシュボードの操作が「database is locked」になるため。"""
    posts_30d, last = activity_of(prof["timestamps"])
    imgs = [u for u in prof["images"] if u][: cfg["images_per_shop"]]
    pq = photo.analyze(imgs) or {}
    vis = None
    if cfg.get("use_vision") and imgs and os.environ.get("ANTHROPIC_API_KEY") and not _vision["off"]:
        try:
            vis = photo.vision_score(imgs, cfg["vision_model"])
        except Exception as e:
            print(f"  写真のAI採点に失敗: {e}")
            if any(w in str(e) for w in ("workspace", "authentication", "401", "403", "credit")):
                _vision["off"] = True
                print("  → 設定の問題のため、今回の残りはAI採点をせずに進めます")
    m = OWNER_RE.search(prof["biography"] or "")
    with db() as conn:
        shop = conn.execute("SELECT name, website FROM shops WHERE id=?", (shop_id,)).fetchone()
        score, note = match_score(shop, {**prof, "username": username}, site_shared=website_shared(conn, shop["website"]))
        _write(conn, shop_id, username, prof, posts_30d, last, imgs, pq, vis, m)
        conn.execute("UPDATE ig_stats SET full_name=?, external_url=?, ig_match=?, ig_match_note=? WHERE shop_id=?",
                     (prof.get("full_name"), prof.get("external_url"), score, note, shop_id))
        if score < MATCH_OK:
            print(f"  要確認 @{username}（{shop['name']}）: {note}")


MATCH_OK = 0.6   # これ以上で「その店のアカウント」とみなし、DMの送信先にする


def website_shared(conn, website):
    """同じドメインを「公式サイト」にしている店が2店以上あるか（＝グルメサイトなどの掲載ページ）。"""
    host = _host(website or "")
    if not host:
        return False
    n = conn.execute("SELECT COUNT(*) FROM shops WHERE website LIKE ? OR website LIKE ?",
                     (f"%://{host}%", f"%://www.{host}%")).fetchone()[0]
    return n >= 2


def rescore_ig_matches():
    """保存済みのプロフィール情報で、アカウントの一致判定をやり直す（判定の基準を直したとき用。通信はしない）。"""
    changed = 0
    with db() as conn:
        rows = conn.execute("""SELECT g.shop_id, g.username, g.full_name, g.external_url, g.biography, g.ig_match,
                                      s.name, s.website
                               FROM ig_stats g JOIN shops s ON s.id=g.shop_id
                               WHERE g.error IS NULL AND g.full_name IS NOT NULL""").fetchall()
        for r in rows:
            score, note = match_score(r, {"full_name": r["full_name"], "external_url": r["external_url"],
                                          "biography": r["biography"], "username": r["username"]},
                                      site_shared=website_shared(conn, r["website"]))
            if score != r["ig_match"]:
                conn.execute("UPDATE ig_stats SET ig_match=?, ig_match_note=? WHERE shop_id=?", (score, note, r["shop_id"]))
                changed += 1
    if changed:
        print(f"[match] アカウントの一致判定を {changed}店でやり直しました")


def _write(conn, shop_id, username, prof, posts_30d, last, imgs, pq, vis, m):
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
    _vision["off"] = False
    use_graph = bool(os.environ.get("IG_BD_USER_ID") and os.environ.get("IG_BD_ACCESS_TOKEN"))
    if not use_graph and not os.environ.get("APIFY_API_TOKEN"):
        raise SystemExit("IG_BD_* か APIFY_API_TOKEN のどちらかを .env に設定してください")
    with db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.instagram FROM shops s LEFT JOIN ig_stats g ON g.shop_id=s.id
               WHERE s.instagram IS NOT NULL AND s.stage='new'
                 AND (g.shop_id IS NULL OR g.fetched_at < datetime('now', ?) OR (g.ig_match IS NULL AND g.error IS NULL))
               ORDER BY (g.shop_id IS NOT NULL AND g.ig_match IS NULL) DESC
               LIMIT ?""", (f"-{refresh_days} days", limit)).fetchall()
    print(f"[enrich] 対象 {len(rows)}店 / 取得元: {'公式API（取れない分はApify）' if use_graph else 'Apify'}")
    if use_graph and rows:
        problem = check_graph_token()
        if problem:
            use_graph = False
            print(f"[enrich] 公式APIを使えません: {problem} → 今回はApifyで取得します")
            send_notice(f"Instagram公式APIが使えません（{problem}）。今朝の分析はApifyで行いました。"
                        "トークンを確認して、Railwayの IG_BD_ACCESS_TOKEN を更新してください。")

    fallback = []      # 公式APIで取れなかった分（Apifyで取り直す）
    if use_graph:
        graph_ok = True
        for i, r in enumerate(rows, 1):
            if not graph_ok:
                fallback.append(r)
                continue
            try:
                prof = via_graph(r["instagram"], cfg["images_per_shop"])
                _save(r["id"], r["instagram"], prof, cfg)
            except GraphFatal as e:
                graph_ok = False
                fallback.append(r)
                print(f"[enrich] 公式APIが途中で使えなくなりました: {e} → 残りはApifyで取得します")
                send_notice(f"Instagram公式APIが途中で使えなくなりました（{e}）。残りはApifyで取得しました。")
            except Exception as e:     # 個人アカウント・非公開など、この1件だけ
                fallback.append(r)
                print(f"  @{r['instagram']}: 公式APIで取れず（{str(e)[:60]}）→ Apifyで取得")
            if i % 10 == 0:
                print(f"  {i}/{len(rows)}")
            time.sleep(18)   # 200件/時 の上限に収める
        print(f"[enrich] 公式APIで {len(rows) - len(fallback)}店、Apifyに回す分 {len(fallback)}店")
    else:
        fallback = list(rows)

    if fallback:
        if os.environ.get("APIFY_API_TOKEN"):
            _apify_batch(fallback, cfg)
        else:
            print(f"[enrich] APIFY_API_TOKEN が無いため {len(fallback)}店は次回に持ち越し")

    score_all()


def _apify_batch(rows, cfg):
    for i in range(0, len(rows), 50):
        chunk = rows[i:i + 50]
        profs = via_apify([r["instagram"] for r in chunk], cfg["images_per_shop"])
        for r in chunk:
            prof = profs.get(r["instagram"])
            if prof:
                _save(r["id"], r["instagram"], prof, cfg)
                continue
            with db() as conn:
                conn.execute("INSERT OR REPLACE INTO ig_stats (shop_id,username,error) VALUES (?,?,?)",
                             (r["id"], r["instagram"], "取得できず（非公開・存在しない等）"))
        print(f"  Apify {min(i + 50, len(rows))}/{len(rows)}")


def score_all():
    with db() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM shops")]
        for sid in ids:
            rescore(conn, sid)
    print(f"[score] {len(ids)}店のスコアを更新")
