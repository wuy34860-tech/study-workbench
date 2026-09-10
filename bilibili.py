"""Bilibili 视频搜索（WBI 签名）。"""

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]


def _get(url: str, timeout: int = 8):
    headers = {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}
    req = urllib.request.Request(url, headers=headers)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))


def _mixin_key(orig: str) -> str:
    return "".join(orig[i] for i in _MIXIN_KEY_ENC_TAB)[:32]


def _enc_wbi(params: dict, img_key: str, sub_key: str) -> str:
    mixin = _mixin_key(img_key + sub_key)
    params = dict(params)
    params["wts"] = int(time.time())
    params = dict(sorted(params.items()))
    filtered = {k: "".join(c for c in str(v) if c not in "!'()*") for k, v in params.items()}
    query = urllib.parse.urlencode(filtered)
    return hashlib.md5((query + mixin).encode()).hexdigest()


def bilibili_search(keyword: str, max_results: int = 8) -> list:
    """搜索 B 站视频，按播放量倒序返回 top N。失败返回空列表。"""
    try:
        nav = _get("https://api.bilibili.com/x/web-interface/nav")
        wbi = nav.get("data", {}).get("wbi_img", {})
        img_key = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
        sub_key = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
        if not img_key or not sub_key:
            return []
    except Exception:
        return []

    params = {"search_type": "video", "keyword": keyword, "page": 1, "page_size": 30}
    w_rid = _enc_wbi(params, img_key, sub_key)
    params["w_rid"] = w_rid
    url = "https://api.bilibili.com/x/web-interface/wbi/search/type?" + urllib.parse.urlencode(params)
    try:
        data = _get(url)
    except Exception:
        return []
    if data.get("code") != 0:
        return []

    results = []
    for item in data.get("data", {}).get("result", [])[:20]:
        title = re.sub(r"<[^>]+>", "", item.get("title", ""))
        bvid = item.get("bvid", "")
        results.append({
            "title": title,
            "author": item.get("author", ""),
            "play": item.get("play", 0),
            "duration": item.get("duration", ""),
            "url": f"https://www.bilibili.com/video/{bvid}" if bvid else item.get("arcurl", ""),
        })
    results.sort(key=lambda r: r.get("play", 0), reverse=True)
    return results[:max_results]
