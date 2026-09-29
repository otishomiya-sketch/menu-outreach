"""ダッシュボード（ローカル専用 http://127.0.0.1:8765）。

- 送信キュー（Instagram / LINE）: 1件ずつ「コピーして開く」→ 送る → 「送信した」
- 店舗リスト: 見込み度順。店ごとに反応（返信・無料体験・有料・お断り）を記録
- 改善: 文面ごとの成績、提案された文面の承認
"""
import json

from flask import Flask, abort, redirect, render_template_string, request, url_for

from . import bandit, improve, outreach
from .core import CHANNELS, db, settings

app = Flask(__name__)

BASE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Menu Photo Pro 営業</title>
<style>
:root{--bg:#f7f6f3;--fg:#1d1d1f;--mut:#6b6b70;--card:#fff;--line:#e4e2dd;--acc:#c2410c;--ok:#15803d;--ng:#b91c1c}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#eee;--mut:#9a9a9f;--card:#222;--line:#333;--acc:#fb923c;--ok:#4ade80;--ng:#f87171}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,"Hiragino Sans",sans-serif}
nav{display:flex;gap:16px;padding:12px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}nav a{color:var(--fg);text-decoration:none;font-weight:600}
nav a.brand{color:var(--acc)}main{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.num{font-size:28px;font-weight:700}.mut{color:var(--mut);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.tw{overflow-x:auto}button,.btn{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:9px 14px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block;font-size:14px}
button.sub,.btn.sub{background:transparent;color:var(--fg);border:1px solid var(--line)}button.ok{background:var(--ok)}button.ng{background:var(--ng)}
textarea{width:100%;min-height:340px;font-family:inherit;font-size:14px;line-height:1.6;padding:10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--fg)}
.thumbs{display:flex;gap:6px;flex-wrap:wrap}.thumbs img{width:96px;height:96px;object-fit:cover;border-radius:6px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.pill{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}
input,select{padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg)}
pre{white-space:pre-wrap;font-size:13px}
</style></head><body>
<nav><a class="brand" href="/">Menu Photo Pro 営業</a><a href="/queue/instagram">Instagram DM</a><a href="/queue/line">LINE</a>
<a href="/leads">店舗リスト</a><a href="/improve">改善</a></nav><main>{% block c %}{% endblock %}</main></body></html>"""


def page(body, **kw):
    return render_template_string(BASE.replace("{% block c %}{% endblock %}", body), **kw)


@app.route("/")
def home():
    cfg = settings()
    with db() as c:
        funnel = {r["stage"]: r["n"] for r in c.execute("SELECT stage, COUNT(*) n FROM shops GROUP BY stage")}
        today = {r["channel"]: dict(r) for r in c.execute(
            """SELECT channel, SUM(status='queued') q, SUM(status='sent') s, SUM(status='dryrun') d FROM touches
               WHERE planned_on=date('now','localtime') GROUP BY channel""")}
        total = c.execute("SELECT COUNT(*) FROM shops").fetchone()[0]
        scored = c.execute("SELECT COUNT(*) FROM shops WHERE score IS NOT NULL").fetchone()[0]
    return page("""
<div class="card"><h2 style="margin-top:0">今日の送信</h2><div class="grid">
{% for ch,label in [('instagram','Instagram DM（手動）'),('line','LINE（手動）'),('email','メール（自動）')] %}
 {% set t = today.get(ch, {}) %}<div><div class="mut">{{label}}</div>
 <div class="num">{{ t.s or 0 }} <span class="mut">/ {{ limits[ch] }}</span></div>
 <div class="mut">残りキュー {{ t.q or 0 }}件{% if t.d %}・ドライラン {{t.d}}件{% endif %}</div>
 {% if ch != 'email' and t.q %}<a class="btn" href="/queue/{{ch}}">送信を始める</a>{% endif %}</div>
{% endfor %}</div>
<form method="post" action="/plan" style="margin-top:12px"><button>今日の送信リストを作る</button>
<span class="mut">見込み度の高い順に、上限まで積みます（{{ 'メールは本番送信' if live else 'メールはドライラン' }}）</span></form></div>
<div class="card"><h2 style="margin-top:0">全体</h2><div class="grid">
<div><div class="mut">収集した店</div><div class="num">{{total}}</div><div class="mut">スコア済み {{scored}}</div></div>
{% for st,label in [('contacted','送信済み'),('replied','返信あり'),('trial','無料体験'),('paid','有料契約'),('declined','お断り'),('unsubscribed','配信停止')] %}
<div><div class="mut">{{label}}</div><div class="num">{{ funnel.get(st,0) }}</div></div>{% endfor %}
</div></div>""", funnel=funnel, today=today, total=total, scored=scored,
                limits=cfg["channels"]["daily_limit"], live=cfg["channels"]["email_live"])


@app.post("/plan")
def do_plan():
    outreach.plan()
    return redirect("/")


def open_url(channel, shop):
    if channel == "instagram":
        return f"https://ig.me/m/{shop['instagram']}"
    lid = shop["line_id"] or ""
    return "https://" + lid if lid.startswith("lin.ee/") else f"https://line.me/R/ti/p/{lid if lid.startswith('@') else '@' + lid}"


@app.route("/queue/<channel>")
def queue(channel):
    if channel not in ("instagram", "line"):
        abort(404)
    with db() as c:
        t = c.execute("""SELECT t.*, s.name, s.instagram, s.line_id, s.category, s.prefecture, s.score, s.owner_name,
                                s.id AS sid, g.posts_30d, g.days_since_last, g.photo_weakness, g.thumbs, g.followers,
                                g.vision_note, v.name AS vname
                         FROM touches t JOIN shops s ON s.id=t.shop_id LEFT JOIN ig_stats g ON g.shop_id=s.id
                         LEFT JOIN variants v ON v.id=t.variant_id
                         WHERE t.channel=? AND t.status='queued' ORDER BY s.score DESC LIMIT 1""", (channel,)).fetchone()
        left = c.execute("SELECT COUNT(*) FROM touches WHERE channel=? AND status='queued'", (channel,)).fetchone()[0]
        done = c.execute("""SELECT COUNT(*) FROM touches WHERE channel=? AND status='sent'
                            AND date(sent_at)=date('now','localtime')""", (channel,)).fetchone()[0]
    limit = settings()["channels"]["daily_limit"][channel]
    if not t:
        return page("""<div class="card"><h2>キューは空です</h2><p>今日の送信 {{done}} / {{limit}} 件。
<form method="post" action="/plan"><button>今日の送信リストを作る</button></form></p></div>""", done=done, limit=limit)
    return page("""
<div class="row" style="justify-content:space-between"><h2>{{ '📷 Instagram DM' if ch=='instagram' else '💬 LINE' }}</h2>
<span class="mut">今日 {{done}} / {{limit}} 件送信・残り {{left}} 件</span></div>
{% if done >= limit %}<div class="card" style="border-color:var(--ng)">今日の上限に達しました。アカウント保護のため、続きは明日にしてください。</div>{% endif %}
<div class="card"><div class="row" style="justify-content:space-between"><div>
<h3 style="margin:0">{{t.name}} <a href="/shop/{{t.sid}}" class="mut">詳細</a></h3>
<div class="mut">{{t.prefecture or ''}} ・ {{t.category or ''}} ・ 見込み度 {{t.score}} ・ 文面 {{t.vname}}</div>
{% if t.posts_30d is not none %}<div class="mut">30日の投稿 {{t.posts_30d}}本・最終投稿 {{t.days_since_last}}日前・写真の弱点度 {{ '%.2f'|format(t.photo_weakness or 0) }}
{% if t.followers %}・フォロワー {{t.followers}}{% endif %} {% if t.vision_note %}・{{t.vision_note}}{% endif %}</div>{% endif %}
</div>{% if ch=='instagram' %}<a class="btn sub" target="_blank" href="https://www.instagram.com/{{t.instagram}}/">プロフィールを見る</a>{% endif %}</div>
{% if t.thumbs %}<div class="thumbs" style="margin-top:10px">{% for u in t.thumbs.split('\n') %}<img src="{{u}}" referrerpolicy="no-referrer" loading="lazy">{% endfor %}</div>{% endif %}
</div>
<form method="post" action="/touch/{{t.id}}" class="card">
<div class="row" style="margin-bottom:8px"><label class="mut">オーナー名（分かれば。宛名に入ります）</label>
<input name="owner" value="{{t.owner_name or ''}}" placeholder="例: 山田 太郎"><button class="sub" name="action" value="owner">宛名を更新</button></div>
<textarea id="msg" name="message">{{t.message}}</textarea>
<div class="row" style="margin-top:10px">
<button type="button" onclick="copyOpen()">① コピーして{{ 'DM' if ch=='instagram' else 'LINE' }}を開く</button>
<button class="ok" name="action" value="sent">② 送信した → 次へ</button>
<button class="sub" name="action" value="skip">スキップ</button>
<button class="sub" name="action" value="unreachable">送れない（DM不可・アカウントなし）</button>
</div><p class="mut">開いた画面にメッセージを貼り付けて、内容を確認してから送信してください。キーボード: C = コピーして開く / S = 送信した</p></form>
<script>
function copyOpen(){const m=document.getElementById('msg');navigator.clipboard.writeText(m.value).then(()=>{window.open({{ url|tojson }},'_blank')});}
document.addEventListener('keydown',e=>{if(e.target.tagName==='TEXTAREA'||e.target.tagName==='INPUT')return;
 if(e.key==='c')copyOpen(); if(e.key==='s')document.querySelector('button[value=sent]').click();});
</script>""", t=t, ch=channel, left=left, done=done, limit=limit, url=open_url(channel, t))


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


@app.route("/leads")
def leads():
    pref, q, stage = request.args.get("pref", ""), request.args.get("q", ""), request.args.get("stage", "")
    sql, args = "SELECT * FROM shops WHERE 1=1", []
    if pref:
        sql += " AND prefecture=?"; args.append(pref)
    if stage:
        sql += " AND stage=?"; args.append(stage)
    if q:
        sql += " AND (name LIKE ? OR category LIKE ?)"; args += [f"%{q}%"] * 2
    sql += " ORDER BY score DESC NULLS LAST LIMIT 300"
    with db() as c:
        rows = c.execute(sql, args).fetchall()
        prefs = [r[0] for r in c.execute("SELECT DISTINCT prefecture FROM shops WHERE prefecture IS NOT NULL ORDER BY 1")]
    return page("""<form class="row card"><input name="q" value="{{q}}" placeholder="店名・業種">
<select name="pref"><option value="">都道府県</option>{% for p in prefs %}<option {{'selected' if p==pref}}>{{p}}</option>{% endfor %}</select>
<select name="stage"><option value="">ステージ</option>{% for s in ['new','contacted','replied','trial','paid','declined','unsubscribed','unreachable'] %}<option {{'selected' if s==stage}}>{{s}}</option>{% endfor %}</select>
<button>絞り込み</button></form>
<div class="card tw"><table><tr><th>見込み度</th><th>店名</th><th>地域・業種</th><th>活発度</th><th>写真の弱点</th><th>連絡先</th><th>ステージ</th></tr>
{% for s in rows %}<tr><td><b>{{s.score if s.score is not none else '-'}}</b></td><td><a href="/shop/{{s.id}}">{{s.name}}</a></td>
<td class="mut">{{s.prefecture or ''}} {{s.category or ''}}</td><td>{{ '%.2f'|format(s.activity or 0) }}</td><td>{{ '%.2f'|format(s.weakness or 0) }}</td>
<td>{% if s.instagram %}<span class="pill">IG</span>{% endif %}{% if s.email and not s.email_refused %}<span class="pill">メール</span>{% endif %}{% if s.line_id %}<span class="pill">LINE</span>{% endif %}</td>
<td>{{s.stage}}</td></tr>{% endfor %}</table></div>""", rows=rows, prefs=prefs, pref=pref, q=q, stage=stage)


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
    return page("""<div class="card"><h2 style="margin-top:0">{{s.name}}</h2>
<div class="mut">{{s.address}} ・ {{s.category}} ・ ★{{s.rating}}（{{s.reviews}}件） ・ 見込み度 {{s.score}} ・ <b>{{s.stage}}</b></div>
<div class="row" style="margin-top:8px">{% if s.website %}<a class="btn sub" target="_blank" href="{{s.website}}">サイト</a>{% endif %}
{% if s.instagram %}<a class="btn sub" target="_blank" href="https://www.instagram.com/{{s.instagram}}/">Instagram</a>{% endif %}
{% if s.place_url and s.place_url.startswith('http') %}<a class="btn sub" target="_blank" href="{{s.place_url}}">Googleマップ</a>{% endif %}</div>
{% if g and g.thumbs %}<div class="thumbs" style="margin-top:10px">{% for u in g.thumbs.split('\n') %}<img src="{{u}}" referrerpolicy="no-referrer">{% endfor %}</div>{% endif %}
{% if g %}<p class="mut">30日の投稿 {{g.posts_30d}}本・最終投稿 {{g.days_since_last}}日前・明るさ {{ '%.0f'|format(g.brightness or 0) }}・黄ばみ {{ '%.0f'|format(g.warmth or 0) }}・シャープさ {{ '%.0f'|format(g.sharpness or 0) }}
{% if g.error %}・取得エラー: {{g.error}}{% endif %}</p>{% endif %}</div>
<form method="post" class="card"><h3 style="margin-top:0">反応を記録</h3><div class="row">
<input name="note" placeholder="メモ（任意）" style="flex:1">
<button class="ok" name="kind" value="replied">返信あり</button><button class="ok" name="kind" value="trial">無料体験した</button>
<button class="ok" name="kind" value="paid">有料契約</button><button class="ng" name="kind" value="declined">お断り</button>
<button class="ng" name="kind" value="unsubscribed">停止希望</button><button class="sub" name="kind" value="note">メモだけ</button></div></form>
<form method="post" class="card"><h3 style="margin-top:0">連絡先</h3><div class="grid">
{% for k,label in [('owner_name','オーナー名'),('instagram','Instagram'),('line_id','LINE ID'),('email','メール')] %}
<label class="mut">{{label}}<br><input name="{{k}}" value="{{s[k] or ''}}"></label>{% endfor %}</div>
{% if s.email_refused %}<p class="mut">⚠ サイトに営業お断りの表示があるため、メールは送りません</p>{% endif %}<button class="sub">保存</button></form>
<div class="card tw"><h3 style="margin-top:0">履歴</h3><table>{% for t in touches %}<tr><td>{{t.sent_at or t.planned_on}}</td><td>{{t.channel}}</td><td>{{t.vname}}</td><td>{{t.status}} {{t.error or ''}}</td></tr>{% endfor %}
{% for e in events %}<tr><td>{{e.at}}</td><td colspan="2"><b>{{e.kind}}</b></td><td>{{e.note or ''}}</td></tr>{% endfor %}</table></div>""",
                s=s, g=g, touches=touches, events=events)


@app.route("/improve", methods=["GET", "POST"])
def improve_page():
    with db() as c:
        if request.method == "POST":
            vid, act = request.form.get("vid"), request.form["action"]
            if act == "reflect":
                improve.reflect()
            elif act in ("active", "rejected", "retired"):
                c.execute("UPDATE variants SET status=? WHERE id=?", (act, vid))
                hs = {"active": "testing", "rejected": "rejected", "retired": "rejected"}[act]
                c.execute("UPDATE hypotheses SET status=? WHERE variant_id=?", (hs, vid))
            return redirect("/improve")
        variants = c.execute("SELECT * FROM variants ORDER BY status='active' DESC, status='proposed' DESC, id").fetchall()
        perf = {ch: bandit.p_best(c, ch) for ch in CHANNELS}
        hyps = c.execute("SELECT * FROM hypotheses ORDER BY id DESC LIMIT 20").fetchall()
        rep = c.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
    notes = json.loads(rep["body"])["notes"] if rep else []
    return page("""<div class="card"><div class="row" style="justify-content:space-between"><h2 style="margin:0">文面の成績</h2>
<form method="post"><button name="action" value="reflect">今すぐ振り返る（集計・引退判定・新しい文面の提案）</button></form></div>
{% if rep %}<p class="mut">前回 {{rep.at}}: {{ notes|join(' / ') }}</p>{% endif %}
<div class="tw"><table><tr><th>文面</th><th>状態</th>{% for ch in chs %}<th>{{ch}}<br><span class="mut">送信・反応率・最良の確率</span></th>{% endfor %}<th></th></tr>
{% for v in variants %}<tr><td><b>{{v.name}}</b></td><td>{{v.status}}</td>
{% for ch in chs %}{% set p = perf[ch].get(v.id) %}<td>{% if p %}{{p.sent}}件・{{ '%.1f%%'|format(100*p.rate) if p.rate is not none else '-' }}・{{ '%.0f%%'|format(100*p.p_best) }}{% else %}-{% endif %}</td>{% endfor %}
<td><form method="post" class="row"><input type="hidden" name="vid" value="{{v.id}}">
{% if v.status=='proposed' %}<button class="ok" name="action" value="active">承認して配信</button><button class="sub" name="action" value="rejected">却下</button>
{% elif v.status=='active' %}<button class="sub" name="action" value="retired">停止</button>
{% else %}<button class="sub" name="action" value="active">再開</button>{% endif %}</form></td></tr>
<tr><td colspan="{{ 3 + chs|length }}"><details><summary class="mut">本文と意図を見る</summary><p class="mut">{{v.rationale or ''}}</p><pre>{{v.body}}</pre></details></td></tr>{% endfor %}
</table></div></div>
<div class="card tw"><h2 style="margin-top:0">仮説</h2><table><tr><th>日付</th><th>仮説</th><th>状態</th><th>結果</th></tr>
{% for h in hyps %}<tr><td>{{h.created_at[:10]}}</td><td><b>{{h.title}}</b><br><span class="mut">{{h.proposal}}</span></td><td>{{h.status}}</td><td>{{h.result or ''}}</td></tr>{% endfor %}</table></div>""",
                variants=variants, perf=perf, chs=CHANNELS, hyps=hyps, rep=rep, notes=notes)


def serve(port=8765):
    app.run(host="127.0.0.1", port=port, debug=False)
