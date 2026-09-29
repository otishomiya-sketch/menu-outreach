"""通知: Discord（DISCORD_WEBHOOK_URL）と LINE（LINE_CHANNEL_ACCESS_TOKEN + LINE_TO_USER_ID）。どちらも任意。"""
import os

import requests


def send(text):
    sent = False
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        try:
            requests.post(os.environ["DISCORD_WEBHOOK_URL"], json={"content": text[:1900]}, timeout=10)
            sent = True
        except requests.RequestException as e:
            print(f"[notify] Discord失敗: {e}")
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO_USER_ID"):
        try:
            requests.post("https://api.line.me/v2/bot/message/push", timeout=10,
                          headers={"Authorization": f"Bearer {os.environ['LINE_CHANNEL_ACCESS_TOKEN']}"},
                          json={"to": os.environ["LINE_TO_USER_ID"], "messages": [{"type": "text", "text": text[:4900]}]})
            sent = True
        except requests.RequestException as e:
            print(f"[notify] LINE失敗: {e}")
    if not sent:
        print("[notify] 通知先が未設定のためスキップ")
