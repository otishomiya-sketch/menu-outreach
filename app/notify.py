"""通知: Discord（DISCORD_WEBHOOK_URL）と LINE（LINE_CHANNEL_ACCESS_TOKEN + LINE_TO_USER_ID）。どちらも任意。"""
import os

import requests


def _post(label, url, **kw):
    try:
        r = requests.post(url, timeout=10, **kw)
    except requests.RequestException as e:
        return f"{label}: 接続できませんでした（{e}）"
    if r.status_code >= 300:
        return f"{label}: 失敗 {r.status_code} {r.text[:200]}"
    return None


def send(text):
    """設定済みの通知先すべてに送る。結果（成功・失敗の説明）の一覧を返す。"""
    results = []
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        err = _post("Discord", os.environ["DISCORD_WEBHOOK_URL"], json={"content": text[:1900]})
        results.append(err or "Discord: 送信しました")
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO_USER_ID"):
        err = _post("LINE", "https://api.line.me/v2/bot/message/push",
                    headers={"Authorization": f"Bearer {os.environ['LINE_CHANNEL_ACCESS_TOKEN'].strip()}"},
                    json={"to": os.environ["LINE_TO_USER_ID"].strip(),
                          "messages": [{"type": "text", "text": text[:4900]}]})
        results.append(err or "LINE: 送信しました")
    if not results:
        results.append("通知先が未設定です")
    for r in results:
        print("[notify]", r)
    return results
