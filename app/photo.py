"""写真の「素人っぽさ」を数値化する。

Pillow だけで測れる4指標（暗さ・黄ばみ・メリハリのなさ・ピンボケ）を 0〜1 の弱点スコアにまとめる。
1 に近いほど「Menu Photo Pro で良くなる余地が大きい」写真。
use_vision が true なら Claude にも見せて補正する。
"""
import base64
import io
import json
import os

import requests
from PIL import Image, ImageFilter, ImageStat

from .core import clamp01


def fetch(url, timeout=15):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    img = Image.open(io.BytesIO(r.content)).convert("RGB")
    img.thumbnail((640, 640))
    return img


def metrics(img):
    gray = img.convert("L")
    stat = ImageStat.Stat(gray)
    brightness, contrast = stat.mean[0], stat.stddev[0]
    r, g, b = ImageStat.Stat(img).mean
    warmth = (r + g) / 2 - b                      # 大きいほど黄〜オレンジかぶり
    edges = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES))
    sharpness = edges.stddev[0]
    return {"brightness": brightness, "contrast": contrast, "warmth": warmth, "sharpness": sharpness}


def weakness(m):
    dark = clamp01((135 - m["brightness"]) / 60)
    yellow = clamp01((m["warmth"] - 18) / 35)
    flat = clamp01((52 - m["contrast"]) / 30)
    blur = clamp01((22 - m["sharpness"]) / 16)
    return 0.40 * dark + 0.25 * yellow + 0.15 * flat + 0.20 * blur


def analyze(urls):
    """複数画像の平均。1枚も取れなければ None。"""
    ms = []
    for u in urls:
        try:
            ms.append(metrics(fetch(u)))
        except Exception:
            continue
    if not ms:
        return None
    avg = {k: sum(m[k] for m in ms) / len(ms) for k in ms[0]}
    avg["weakness"] = sum(weakness(m) for m in ms) / len(ms)
    avg["n"] = len(ms)
    return avg


VISION_PROMPT = """これは飲食店のInstagramの直近投稿画像です。料理写真として、プロが撮影したメニュー写真と比べて
どれだけ改善の余地があるかを採点してください（暗い・黄ばみ・生活感のある背景・ピンボケ・構図の悪さ など）。
料理が写っていない画像は無視してください。
JSONだけを返してください: {"weakness": 0〜1の数値（1=素人感が強く改善余地が大きい）, "food_ratio": 料理写真の割合0〜1, "note": "日本語で20字以内の所見"}"""


def vision_score(urls, model):
    """Claude に最大4枚まとめて見せて採点させる。"""
    import anthropic

    content = []
    for u in urls[:4]:
        try:
            img = fetch(u)
        except Exception:
            continue
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(buf.getvalue()).decode()}})
    if not content:
        return None
    content.append({"type": "text", "text": VISION_PROMPT})
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    msg = client.messages.create(model=model, max_tokens=300, messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if b.type == "text")
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
        return {"weakness": clamp01(float(data["weakness"])), "food_ratio": float(data.get("food_ratio", 1)),
                "note": str(data.get("note", ""))[:40]}
    except (ValueError, KeyError):
        return None
