"""④自己改善: self-improving-agent-os の「検証 → 仮説 → 選択」ループを営業文面に当てはめたもの。

  validator    … 文面ごと・業種ごと・スコア帯ごとの反応率を集計し、レポートにする
  selector     … 十分な件数を送って負けがはっきりした文面を自動で引退、勝ちが確定したら採用
  hypothesizer … 結果を Claude に渡して、次に試す文面を1本提案させる（仮説として記録）
  （人の承認） … 提案はダッシュボードで大宮さんが承認したときだけ配信に使われる
"""
import json
import os
import re

from . import bandit
from .core import CHANNELS, anthropic_client, db, settings

REQUIRED = ["{greeting}", "{trial_url}", "{reply_channel}", "Menu Photo Pro"]
FACTS = ["10枚", "1,980円"]


def segment_rates(conn, column):
    return [dict(r) for r in conn.execute(f"""
        SELECT {column} AS seg, COUNT(DISTINCT t.id) AS sent,
               COUNT(DISTINCT CASE WHEN e.kind IN ('replied','trial','paid') THEN t.id END) AS positive
        FROM touches t JOIN shops s ON s.id=t.shop_id LEFT JOIN events e ON e.touch_id=t.id
        WHERE t.status='sent' GROUP BY seg HAVING sent >= 10 ORDER BY positive*1.0/sent DESC""")]


def validate(conn):
    rep = {"channels": {}, "segments": {}}
    for ch in CHANNELS:
        rep["channels"][ch] = list(bandit.p_best(conn, ch).values())
    rep["segments"]["業種"] = segment_rates(conn, "s.category")
    rep["segments"]["都道府県"] = segment_rates(conn, "s.prefecture")
    rep["segments"]["スコア帯"] = segment_rates(conn, "CAST(s.score/20 AS INT)*20")
    rep["funnel"] = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, COUNT(*) n FROM shops GROUP BY stage")}
    return rep


def select(conn, rep):
    cfg = settings()["improve"]
    notes = []
    active = conn.execute("SELECT COUNT(*) FROM variants WHERE status='active'").fetchone()[0]
    for ch, arms in rep["channels"].items():
        for a in arms:
            if a["sent"] < cfg["min_sends_to_judge"]:
                continue
            if a["p_best"] < cfg["retire_if_p_best_below"] and active > 1:
                conn.execute("UPDATE variants SET status='retired' WHERE id=?", (a["id"],))
                conn.execute("UPDATE hypotheses SET status='rejected', result=? WHERE variant_id=?",
                             (f"{ch}: 反応率 {a['rate']:.1%}（{a['sent']}件）で負け確定", a["id"]))
                active -= 1
                notes.append(f"引退: {a['name']}（{ch} 反応率 {a['rate']:.1%} / 最良の確率 {a['p_best']:.0%}）")
            elif a["p_best"] > 0.95:
                conn.execute("UPDATE hypotheses SET status='adopted', result=? WHERE variant_id=?",
                             (f"{ch}: 反応率 {a['rate']:.1%}（{a['sent']}件）で最良", a["id"]))
                notes.append(f"最良: {a['name']}（{ch} 反応率 {a['rate']:.1%}）")
    return notes


def check_variant(body, base_body):
    """提案文面の機械チェック。事実と違う約束や差し込み漏れを弾く。"""
    errs = [f"「{r}」がない" for r in REQUIRED if r not in body]
    errs += [f"事実「{f}」が消えている" for f in FACTS if f in base_body and f not in body]
    for bad in ["無料で永久", "完全無料", "必ず", "売上が.*倍", "No\\.1", "日本一"]:
        if re.search(bad, body):
            errs.append(f"誇大表現「{bad}」")
    if len(body) > len(base_body) * 1.3:
        errs.append("長すぎる")
    return errs


def hypothesize(conn, rep):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return "ANTHROPIC_API_KEY 未設定のため、文面の自動提案はスキップ"
    if conn.execute("SELECT COUNT(*) FROM variants WHERE status='proposed'").fetchone()[0] >= 2:
        return "承認待ちの提案が2本あるため、新しい提案は作りません"
    variants = [dict(r) for r in conn.execute("SELECT id,name,status,body,rationale FROM variants")]
    base = max((v for v in variants if v["status"] == "active"), key=lambda v: v["id"], default=variants[0])
    past = [dict(r) for r in conn.execute("SELECT title,proposal,status,result FROM hypotheses ORDER BY id DESC LIMIT 10")]
    prompt = f"""あなたは飲食店向けSaaS「Menu Photo Pro」の営業文面を改善する担当です。
Instagram DM・メール・LINEで飲食店オーナーに送る営業文の結果データを見て、次に試す文面を1本だけ提案してください。

# 守ること
- 事実を変えない（10枚まで無料、月額1,980円（税別）から、インストール・会員登録不要、料理・量・盛り付けは変えない）
- 差し込み {{greeting}} {{shop_name}} {{trial_url}} {{reply_channel}} {{sender_person}} {{found_via}} を使う（greeting・trial_url・reply_channel は必須）
- 誇大表現・煽り・嘘の実績は禁止。相手は忙しい個人店のオーナー
- 過去に負けた仮説と同じ方向は避ける
- 変える点は1つか2つに絞り、何を検証するのかがはっきり分かるようにする

# 現在の文面
{json.dumps(variants, ensure_ascii=False, indent=1)}

# 結果（channels: 文面ごとの送信数・前向き反応数・最良の確率 / segments: 業種・地域・スコア帯別）
{json.dumps(rep, ensure_ascii=False, indent=1, default=str)}

# 過去の仮説
{json.dumps(past, ensure_ascii=False, indent=1)}

JSONだけを返してください:
{{"title": "仮説の短い名前", "proposal": "何を変えるか", "rationale": "データ上の根拠", "expected_impact": "期待する効果",
 "verification_method": "どう判定するか", "subject": "メール件名", "body": "本文"}}"""
    msg = anthropic_client().messages.create(model=settings()["improve"]["proposal_model"], max_tokens=3000,
                                 messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        h = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError:
        return "提案のJSONを読めませんでした"
    errs = check_variant(h.get("body", ""), base["body"])
    if errs:
        return "提案を不採用（機械チェック）: " + " / ".join(errs)
    n = conn.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
    name = f"{chr(65 + n) if n < 26 else n}-{h['title'][:20]}"
    cur = conn.execute("INSERT INTO variants (name,subject,body,status,rationale,parent_id) VALUES (?,?,?,?,?,?)",
                       (name, h.get("subject"), h["body"], "proposed", h.get("rationale"), base["id"]))
    conn.execute("""INSERT INTO hypotheses (title,proposal,rationale,expected_impact,verification_method,variant_id)
                    VALUES (?,?,?,?,?,?)""", (h["title"], h["proposal"], h.get("rationale"),
                                              h.get("expected_impact"), h.get("verification_method"), cur.lastrowid))
    return f"新しい文面を提案: {name}（ダッシュボードの「改善」で承認すると配信に入ります）"


def reflect(propose=True):
    with db() as conn:
        rep = validate(conn)
        notes = select(conn, rep)
        if propose:
            notes.append(hypothesize(conn, rep))
        body = json.dumps({"notes": notes, **rep}, ensure_ascii=False, indent=1, default=str)
        conn.execute("INSERT INTO reports (kind, body) VALUES ('reflect', ?)", (body,))
    for n in notes:
        print("[improve]", n)
    return notes
