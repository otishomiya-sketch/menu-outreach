"""文面の出し分け（トンプソン・サンプリング）。

「前向きな反応」= 送った店から 返信 / 無料体験 / 有料契約 のいずれかが記録されたこと。
反応が良い文面ほど多く選ばれ、まだ件数の少ない文面にも一定の確率で出番が回る。
"""
import random

POSITIVE = ("replied", "trial", "paid")


def stats(conn, channel=None):
    q = f"""
      SELECT v.id, v.name, v.status, t.channel,
             COUNT(DISTINCT t.id) AS sent,
             COUNT(DISTINCT CASE WHEN e.kind IN ('replied','trial','paid') THEN t.id END) AS positive,
             COUNT(DISTINCT CASE WHEN e.kind IN ('trial','paid') THEN t.id END) AS trials,
             COUNT(DISTINCT CASE WHEN e.kind='paid' THEN t.id END) AS paid,
             COUNT(DISTINCT CASE WHEN e.kind IN ('declined','unsubscribed') THEN t.id END) AS negative
      FROM variants v
      LEFT JOIN touches t ON t.variant_id=v.id AND t.status='sent' {"AND t.channel=?" if channel else ""}
      LEFT JOIN events e ON e.touch_id=t.id
      GROUP BY v.id, t.channel
    """
    return [dict(r) for r in conn.execute(q, (channel,) if channel else ())]


def _by_variant(conn, channel):
    out = {}
    for r in conn.execute("SELECT id, name, status FROM variants WHERE status='active'"):
        out[r["id"]] = {"id": r["id"], "name": r["name"], "sent": 0, "positive": 0}
    for s in stats(conn, channel):
        if s["id"] in out and (s["channel"] == channel):
            out[s["id"]].update(sent=s["sent"], positive=s["positive"])
    return out


def choose(conn, channel):
    arms = _by_variant(conn, channel)
    if not arms:
        return None
    best = max(arms.values(), key=lambda a: random.betavariate(1 + a["positive"], 1 + a["sent"] - a["positive"]))
    return conn.execute("SELECT * FROM variants WHERE id=?", (best["id"],)).fetchone()


def p_best(conn, channel, draws=4000):
    """各文面が「いちばん良い」確率。"""
    arms = list(_by_variant(conn, channel).values())
    if not arms:
        return {}
    wins = {a["id"]: 0 for a in arms}
    for _ in range(draws):
        samples = {a["id"]: random.betavariate(1 + a["positive"], 1 + a["sent"] - a["positive"]) for a in arms}
        wins[max(samples, key=samples.get)] += 1
    return {a["id"]: {**a, "rate": a["positive"] / a["sent"] if a["sent"] else None,
                      "p_best": wins[a["id"]] / draws} for a in arms}
