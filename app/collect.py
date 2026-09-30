"""①集める: Googleマップ（Apify）で飲食店を集め、公式サイトから メール / Instagram / LINE を拾う。

maps-outreach の gmaps-lead-scraper を飲食店向けに改修したもの。
- scrapeContacts を有効にして、Apify 側でもメール・SNS を取得
- 取れなかった分は公式サイトを自前でクロールして補完
- 「営業メールお断り」等の表示がある店はメール送信対象から外す
"""
import os
import re
import secrets
import sys
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .core import PREFS, db, normalize_ig, prefecture_of, settings

ACTOR_ID = "compass/crawler-google-places"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_FULL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")
LINE_RE = re.compile(r"(?:line\.me/R/ti/p/|page\.line\.me/)(@?[A-Za-z0-9_.-]+)|lin\.ee/([A-Za-z0-9]+)")
REFUSAL_RE = re.compile(r"(営業|勧誘|セールス|広告)[^。\n]{0,20}(お断り|ご遠慮|禁止|控え)")
OWNER_RE = re.compile(r"(?:オーナー|店主|代表(?:取締役)?|料理長|シェフ)[\s　:：]*([一-龥々]{1,4}(?:[\s　][一-龥々]{1,4})?)(?![一-龥々])")
BAD_EMAIL_PARTS = ("example.", "sentry", "wixpress", ".png", ".jpg", ".gif", "@2x", "domain.")
# 飲食店ではない宛先（役所・学校など）と、ホームページのひな形に入っている見本アドレス
BAD_EMAIL_DOMAINS = (".lg.jp", ".go.jp", ".ed.jp", ".ac.jp", "example.com", "example.jp", "mail.com", "test.com")
BAD_EMAIL_LOCALS = ("simple", "sample", "test", "dummy", "xxx", "your", "yourname", "name", "user", "noreply", "no-reply")
FREE_MAIL = ("gmail.com", "yahoo.co.jp", "icloud.com", "outlook.jp", "outlook.com", "hotmail.com", "ezweb.ne.jp",
             "docomo.ne.jp", "softbank.ne.jp", "i.softbank.jp", "au.com", "me.com", "ymail.ne.jp")


def valid_email(e):
    """送ってよい宛先か。形式が正しく、役所・見本アドレスでないこと。"""
    e = (e or "").strip().lower()
    if not EMAIL_FULL_RE.fullmatch(e) or any(b in e for b in BAD_EMAIL_PARTS):
        return False
    local, domain = e.rsplit("@", 1)
    if local in BAD_EMAIL_LOCALS or any(domain == d.lstrip(".") or domain.endswith(d) for d in BAD_EMAIL_DOMAINS):
        return False
    return True


def best_email(candidates, website=""):
    """候補から1つ選ぶ。公式サイトと同じドメイン > 独自ドメイン > フリーメール。"""
    good = sorted({e.strip().lower() for e in candidates if valid_email(e)})
    if not good:
        return None
    host = urlparse(website or "").netloc.lower().removeprefix("www.") if website else ""

    def rank(e):
        domain = e.rsplit("@", 1)[1]
        if host and (domain == host or host.endswith("." + domain) or domain.endswith("." + host)):
            return 0
        return 2 if domain in FREE_MAIL else 1
    return min(good, key=lambda e: (rank(e), e))


def dataset_id(run):
    """apify-client 1.x は dict、2.x はモデルオブジェクトを返すので両方に対応。"""
    if not run:
        raise RuntimeError("Apify の実行結果が空です")
    if isinstance(run, dict):
        ds = run.get("defaultDatasetId")
    else:
        ds = getattr(run, "default_dataset_id", None) or getattr(run, "defaultDatasetId", None)
    status = run.get("status") if isinstance(run, dict) else getattr(run, "status", None)
    if not ds:
        raise RuntimeError(f"Apify の実行に失敗しました（status={status}）")
    return ds


def _first(items):
    return items[0] if items else None


def crawl_site(url, timeout=12):
    """公式サイトのトップ＋問い合わせ系ページからメール・SNS・お断り表示・オーナー名を拾う。"""
    out = {"emails": set(), "instagram": None, "line_id": None, "refused": False, "owner": None}
    if not url:
        return out
    seen, queue = set(), [url]
    host = urlparse(url).netloc
    while queue and len(seen) < 4:
        u = queue.pop(0)
        if u in seen:
            continue
        seen.add(u)
        try:
            r = requests.get(u, headers=UA, timeout=timeout)
            if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
                continue
            r.encoding = r.apparent_encoding or r.encoding
            html = r.text
        except requests.RequestException:
            continue
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ", strip=True)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("mailto:"):
                out["emails"].add(href[7:].split("?")[0])
            elif "instagram.com" in href and not out["instagram"]:
                out["instagram"] = normalize_ig(href)
            elif not out["line_id"] and LINE_RE.search(href):
                m = LINE_RE.search(href)
                out["line_id"] = m.group(1) or ("lin.ee/" + m.group(2))
            label = (a.get_text() or "") + href
            if (len(seen) + len(queue) < 4 and urlparse(urljoin(u, href)).netloc == host
                    and re.search(r"contact|inquiry|toiawase|about|company|shop|access|問い合わせ|会社|店舗", label, re.I)):
                queue.append(urljoin(u, href))
        out["emails"].update(EMAIL_RE.findall(text))
        if REFUSAL_RE.search(text):
            out["refused"] = True
        if not out["owner"]:
            m = OWNER_RE.search(text)
            if m:
                out["owner"] = m.group(1).strip()
    out["emails"] = {e.lower() for e in out["emails"] if not any(b in e.lower() for b in BAD_EMAIL_PARTS)}
    return out


def _upsert(conn, rec):
    row = conn.execute("SELECT id FROM shops WHERE place_url=?", (rec["place_url"],)).fetchone()
    if row:
        # 既存店は空欄だけ埋める
        sets = ", ".join(f"{k}=COALESCE({k}, ?)" for k in ("email", "instagram", "line_id", "owner_name", "website"))
        conn.execute(f"UPDATE shops SET {sets} WHERE id=?",
                     (rec["email"], rec["instagram"], rec["line_id"], rec["owner_name"], rec["website"], row["id"]))
        return False
    conn.execute(
        """INSERT INTO shops (place_url,name,owner_name,category,address,prefecture,phone,website,email,
             instagram,line_id,rating,reviews,email_refused,search_keyword,search_location,crawled_at,ref_code)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now','localtime'),?)""",
        (rec["place_url"], rec["name"], rec["owner_name"], rec["category"], rec["address"],
         prefecture_of(rec["address"]), rec["phone"], rec["website"], rec["email"], rec["instagram"],
         rec["line_id"], rec["rating"], rec["reviews"], int(rec["refused"]), rec["keyword"],
         rec["location"], secrets.token_hex(4)))
    return True


def run_search(keyword, location, limit, force=False):
    from apify_client import ApifyClient

    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        sys.exit("APIFY_API_TOKEN が .env にありません")
    with db() as conn:
        if not force and conn.execute("SELECT 1 FROM collect_runs WHERE keyword=? AND location=?",
                                      (keyword, location)).fetchone():
            print(f"[collect] スキップ（取得済み）: {keyword} @ {location}")
            return 0
    client = ApifyClient(token)
    print(f"[collect] Apify: {keyword} @ {location} (max {limit})")
    run = client.actor(ACTOR_ID).call(logger=None, run_input={
        "searchStringsArray": [keyword], "locationQuery": location,
        "maxCrawledPlacesPerSearch": limit, "language": "ja", "countryCode": "jp",
        "skipClosedPlaces": True, "scrapeContacts": True,
    })
    added = total = 0
    for item in client.dataset(dataset_id(run)).iterate_items():
        if item.get("permanentlyClosed") or item.get("temporarilyClosed"):
            continue
        website = (item.get("website") or "").strip()
        ig = normalize_ig(_first(item.get("instagrams") or []))
        emails = [e.lower() for e in (item.get("emails") or [])]
        site = crawl_site(website)
        # Googleマップのサイト欄が Instagram のことも多い
        if not ig and "instagram.com" in website:
            ig = normalize_ig(website)
        rec = {
            "place_url": item.get("url") or f"{item.get('title')}|{item.get('address')}",
            "name": item.get("title") or "",
            "owner_name": site["owner"],
            "category": item.get("categoryName") or keyword,
            "address": item.get("address") or "",
            "phone": item.get("phone") or "",
            "website": website,
            "email": best_email(set(emails) | site["emails"], website),
            "instagram": ig or site["instagram"],
            "line_id": site["line_id"],
            "rating": item.get("totalScore"),
            "reviews": item.get("reviewsCount"),
            "refused": site["refused"],
            "keyword": keyword, "location": location,
        }
        with db() as conn:
            added += _upsert(conn, rec)
        total += 1
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO collect_runs (keyword,location,total) VALUES (?,?,?)",
                     (keyword, location, total))
    print(f"[collect] {total}件取得 / 新規 {added}件")
    return added


def run(areas=None, keywords=None, limit=None, force=False):
    cfg = settings()["collect"]
    areas = areas or cfg.get("areas") or PREFS
    keywords = keywords or cfg["keywords"]
    limit = limit or cfg["limit_per_search"]
    n = 0
    for area in areas:
        for kw in keywords:
            n += run_search(kw, area, limit, force)
    print(f"[collect] 完了: 新規 {n}件")


def auto_areas():
    """毎朝の自動収集で回るエリア。collect.auto_scope が nationwide なら47都道府県、areas なら settings の areas。"""
    cfg = settings()["collect"]
    if cfg.get("auto_scope", "nationwide") == "areas" and cfg.get("areas"):
        return cfg["areas"]
    return PREFS


def run_auto(searches=None):
    """まだ集めていない「エリア×業種」を、1日 searches 件だけ順番に集める。全部回ったら古い順に取り直す。"""
    cfg = settings()["collect"]
    searches = searches or int(os.environ.get("AUTO_SEARCHES_PER_DAY") or cfg.get("auto_searches_per_day", 5))
    with db() as conn:
        done = {(r["keyword"], r["location"]): r["at"] for r in conn.execute("SELECT * FROM collect_runs")}
    combos = [(kw, area) for area in auto_areas() for kw in cfg["keywords"]]
    todo = [c for c in combos if c not in done]
    if len(todo) < searches:   # 一巡したら、いちばん古いものから取り直す（新店・閉店の反映）
        todo += sorted((c for c in combos if c in done), key=lambda c: done[c])
    todo = todo[:searches]
    print(f"[collect] 自動収集: 残り {len([c for c in combos if c not in done])}/{len(combos)} 組のうち {len(todo)} 組を実行")
    n = fails = 0
    for kw, area in todo:
        try:
            n += run_search(kw, area, cfg["limit_per_search"], force=True)
        except Exception as e:
            fails += 1
            print(f"[collect] 失敗 {kw} @ {area}: {e}")
    print(f"[collect] 自動収集 完了: 新規 {n}件")
    if todo and fails == len(todo):
        raise RuntimeError("すべての検索が失敗しました（APIFY_API_TOKEN・残高を確認）")
    return n
