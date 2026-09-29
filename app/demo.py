"""動作確認用のデモデータ。APIキーなしでダッシュボードを一通り触れる。

data/demo.db に架空の飲食店30件を作る（本番の data/outreach.db には触らない）。
写真はその場で描いたダミー画像で、店ごとに暗さ・黄ばみ・ボケを変えてあり、実際の採点ロジックで弱点度を測る。
"""
import random
import secrets
from datetime import datetime, timedelta

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from . import enrich, outreach, photo
from .core import DB_PATH, ROOT, db, init_db, prefecture_of

IMG_DIR = ROOT / "data" / "demo"

SHOPS = [
    ("炭火焼鳥 とりまる", "居酒屋", "東京都渋谷区", "田中 一郎"), ("カフェ ひだまり", "カフェ", "東京都世田谷区", None),
    ("麺屋 こがね", "ラーメン", "大阪府大阪市北区", None), ("焼肉 牛若", "焼肉", "大阪府大阪市中央区", "佐藤 健"),
    ("トラットリア ソーレ", "イタリアン", "福岡県福岡市中央区", None), ("和食処 さくら", "和食", "京都府京都市中京区", "山本 和子"),
    ("中華料理 龍門", "中華料理", "神奈川県横浜市中区", None), ("洋食屋 ポムポム", "洋食", "愛知県名古屋市中区", None),
    ("定食 まんぷく亭", "定食", "北海道札幌市中央区", None), ("パン工房 こむぎ", "ベーカリー", "兵庫県神戸市中央区", "中村 美咲"),
    ("甘味処 あんず", "スイーツ", "東京都台東区", None), ("バー ルナ", "バー", "東京都港区", None),
    ("海鮮居酒屋 波平", "居酒屋", "宮城県仙台市青葉区", None), ("喫茶 しろくま", "カフェ", "広島県広島市中区", None),
    ("豚骨ラーメン 一心", "ラーメン", "福岡県福岡市博多区", "吉田 大輔"), ("ホルモン 炎", "焼肉", "東京都新宿区", None),
    ("ピッツェリア ナポリ", "イタリアン", "東京都目黒区", None), ("割烹 みやび", "和食", "石川県金沢市", None),
    ("餃子の福来", "中華料理", "栃木県宇都宮市", None), ("キッチン グリル", "洋食", "東京都杉並区", None),
    ("ごはん処 ひなた", "定食", "静岡県静岡市葵区", None), ("ブーランジェリー 麦", "ベーカリー", "大阪府大阪市西区", None),
    ("パティスリー 花", "スイーツ", "埼玉県さいたま市大宮区", "小林 花子"), ("酒場 ともしび", "居酒屋", "沖縄県那覇市", None),
    ("珈琲店 まめ", "カフェ", "千葉県千葉市中央区", None), ("つけ麺 いぶき", "ラーメン", "東京都豊島区", None),
    ("焼肉 よつば", "焼肉", "熊本県熊本市中央区", None), ("ビストロ ル・ポン", "洋食", "東京都渋谷区", None),
    ("そば処 けやき", "和食", "長野県松本市", None), ("台湾料理 美味館", "中華料理", "大阪府大阪市浪速区", None),
]


def _food_image(path, dark, yellow, blur, seed):
    rnd = random.Random(seed)
    img = Image.new("RGB", (480, 480), (rnd.randint(170, 235),) * 3)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 300, 480, 480], fill=(rnd.randint(120, 180), rnd.randint(90, 130), rnd.randint(60, 90)))
    d.ellipse([70, 110, 410, 390], fill=(245, 245, 240), outline=(200, 200, 200), width=4)
    for _ in range(9):
        x, y, r = rnd.randint(150, 330), rnd.randint(180, 320), rnd.randint(22, 50)
        d.ellipse([x - r, y - r, x + r, y + r],
                  fill=rnd.choice([(200, 70, 40), (230, 170, 60), (90, 150, 60), (160, 90, 50), (240, 220, 180)]))
    img = ImageEnhance.Brightness(img).enhance(1.15 - 0.65 * dark)
    if yellow:
        overlay = Image.new("RGB", img.size, (255, 190, 60))
        img = Image.blend(img, overlay, 0.35 * yellow)
    if blur:
        img = img.filter(ImageFilter.GaussianBlur(4 * blur))
    img.save(path, "JPEG", quality=85)
    return photo.metrics(img)


def seed():
    if DB_PATH.exists():
        DB_PATH.unlink()
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    init_db()
    outreach.seed_variants()
    rnd = random.Random(42)
    now = datetime.now()
    with db() as c:
        for i, (name, cat, area, owner) in enumerate(SHOPS):
            ig = f"demo_shop{i:02d}" if i % 7 != 6 else None
            email = f"info@shop{i:02d}.example" if i % 3 != 1 else None
            line = f"@demo{i:02d}" if i % 4 == 0 or not ig else None
            cur = c.execute(
                """INSERT INTO shops (place_url,name,owner_name,category,address,prefecture,website,email,instagram,
                     line_id,rating,reviews,search_keyword,search_location,ref_code)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"demo:{i}", name, owner, cat, area + "1-2-3", prefecture_of(area),
                 None, email, ig, line, round(rnd.uniform(3.2, 4.6), 1), rnd.randint(8, 400), cat, area,
                 secrets.token_hex(4)))
            sid = cur.lastrowid
            if not ig:
                continue
            # 店ごとの「写真の弱さ」と「更新頻度」をばらつかせる
            dark, yellow, blur = rnd.random(), rnd.random() * 0.9, rnd.random() * 0.7
            ms, thumbs = [], []
            for k in range(4):
                fn = f"s{i:02d}_{k}.jpg"
                ms.append(_food_image(IMG_DIR / fn, dark, yellow, blur, i * 10 + k))
                thumbs.append(f"/demo-img/{fn}")
            avg = {key: sum(m[key] for m in ms) / len(ms) for key in ms[0]}
            posts = [now - timedelta(days=rnd.expovariate(1 / rnd.choice([2, 4, 8, 25]))) for _ in range(12)]
            posts_30d = sum(1 for p in posts if (now - p).days <= 30)
            last = min((now - p).days for p in posts)
            c.execute(
                """INSERT INTO ig_stats (shop_id,username,followers,media_count,posts_30d,days_since_last,brightness,
                     contrast,warmth,sharpness,photo_weakness,thumbs,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'demo')""",
                (sid, ig, rnd.randint(150, 6000), rnd.randint(40, 900), posts_30d, last, avg["brightness"],
                 avg["contrast"], avg["warmth"], avg["sharpness"],
                 sum(photo.weakness(m) for m in ms) / len(ms), "\n".join(thumbs)))
    enrich.score_all()

    # 過去の送信実績（「改善」画面の集計を見るため）: 下位の店に20日前に送った体で作る
    with db() as c:
        c.execute("UPDATE variants SET status='active'")
        low = [r[0] for r in c.execute("SELECT id FROM shops WHERE instagram IS NOT NULL ORDER BY score LIMIT 10")]
        vids = [r[0] for r in c.execute("SELECT id FROM variants ORDER BY id")]
        for n, sid in enumerate(low):
            vid = vids[n % len(vids)]
            sent = (now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
            cur = c.execute("""INSERT INTO touches (shop_id,channel,variant_id,status,planned_on,sent_at,message)
                               VALUES (?,?,?,'sent',?,?,'（デモ）')""", (sid, "instagram", vid, sent[:10], sent))
            c.execute("UPDATE shops SET stage='contacted' WHERE id=?", (sid,))
            kind = {0: "replied", 3: "trial", 4: "replied", 6: "declined", 8: "paid"}.get(n)
            if kind:
                outreach.record_outcome(c, sid, kind, "デモ", cur.lastrowid)
        c.execute("UPDATE variants SET status='proposed' WHERE name LIKE 'B-%'")
    outreach.plan()
    outreach.send_emails(live=False)
    print(f"[demo] デモデータを作成しました: {DB_PATH}")
