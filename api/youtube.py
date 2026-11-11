"""
youtube.py — YouTube 頻道串接（YouTube Data API v3）：OAuth 授權、換存取權杖、查頻道。

John 2026-11-08：文章短影音（api/article_video.py）先送 YouTube，再嵌入官網文章。
- 用戶端是 Google Cloud 專案 y1-youtube 的「電腦版應用程式」，回呼網址用本機迴路位址 REDIRECT_URI：
  瀏覽器通常在別台電腦，跳轉後頁面打不開沒關係，把網址列整串貼回 scripts/youtube_auth.py（授權只做一次）。
- 長期授權（refresh_token）和頻道 id 存在 config.toml [youtube]，程式不印出來；錯誤訊息也只帶 Google 回的錯誤代碼。
- ⚠ 專案沒通過 YouTube API 稽核前，程式上傳的影片一律被鎖成私人（不能改公開、不能申訴），
  所以稽核過之前影片由 John 在 YouTube 工作室手動上傳。
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Sequence, Tuple

import config

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/youtube/v3"
REDIRECT_URI = "http://127.0.0.1:8765/"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",     # 上傳影片、縮圖
          "https://www.googleapis.com/auth/youtube.readonly",   # 查頻道、影片處理狀態（有沒有被鎖）
          # 改影片說明、標籤（videos.update 只認這個或 youtube）。John 11-08 同意加；這個權限也能刪影片，程式只用來改說明和標籤
          "https://www.googleapis.com/auth/youtube.force-ssl")
TIMEOUT = 30

_token: Dict[str, Any] = {}     # 存取權杖快取 {token, exp}（約一小時有效）


class YouTubeError(RuntimeError):
    pass


def configured() -> bool:
    return bool(config.YOUTUBE.get("client_id") and config.YOUTUBE.get("client_secret"))


def authorized() -> bool:
    return configured() and bool(config.YOUTUBE.get("refresh_token"))


def pkce() -> Tuple[str, str]:
    """PKCE：(verifier, challenge)。授權碼就算外流，沒有這次的 verifier 也換不到權杖。"""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(state: str, challenge: str) -> str:
    """給人用瀏覽器打開的 Google 登入網址。prompt=consent＋offline：每次都拿得到 refresh_token。"""
    q = {"client_id": config.YOUTUBE["client_id"], "redirect_uri": REDIRECT_URI, "response_type": "code",
         "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    return f"{AUTH_URL}?{urllib.parse.urlencode(q)}"


def parse_redirect(pasted: str, state: str) -> str:
    """跳轉後網址列的整串（有沒有 http:// 都可以）→ 授權碼。按了拒絕、state 不是這次的、沒有 code 都丟 YouTubeError。"""
    s = (pasted or "").strip().split("#", 1)[0]
    q = urllib.parse.parse_qs(s.split("?", 1)[1] if "?" in s else s)
    if q.get("error"):
        raise YouTubeError(f"Google 回覆：{q['error'][0]}（access_denied＝按了取消）")
    if q.get("state", [""])[0] != state:
        raise YouTubeError("網址不是這次登入產生的（state 對不上），請用這次印出的網址重新登入")
    code = q.get("code", [""])[0]
    if not code:
        raise YouTubeError("貼上的網址裡沒有授權碼（code），請複製網址列的整串")
    return code


def missing_scopes(granted: str) -> Sequence[str]:
    """Google 現在讓使用者逐項勾權限：回傳沒勾到的。"""
    got = set((granted or "").split())
    return [s for s in SCOPES if s not in got]


def _err(e: urllib.error.HTTPError) -> str:
    try:
        j = json.loads(e.read() or b"{}")
    except ValueError:
        j = {}
    err = j.get("error")
    if isinstance(err, dict):          # API：{"error": {"code", "message", "errors": [{"reason"}]}}
        reasons = ",".join(x.get("reason", "") for x in err.get("errors") or [] if isinstance(x, dict))
        return f"HTTP {e.code} {err.get('message', '')} {reasons}".strip()
    return f"HTTP {e.code} {err or ''} {j.get('error_description', '')}".strip()   # OAuth：invalid_grant 之類


def _open(req: urllib.request.Request) -> Dict[str, Any]:
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        raise YouTubeError(_err(e)) from None
    except urllib.error.URLError as e:
        raise YouTubeError(f"連不上 Google：{e.reason}") from None
    return json.loads(body or b"{}")


def _post(url: str, data: Dict[str, str]) -> Dict[str, Any]:
    return _open(urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST",
                                        headers={"Content-Type": "application/x-www-form-urlencoded"}))


def exchange(code: str, verifier: str) -> Dict[str, Any]:
    """授權碼 → {access_token, refresh_token, scope, expires_in, …}。"""
    return _post(TOKEN_URL, {"code": code, "client_id": config.YOUTUBE["client_id"],
                             "client_secret": config.YOUTUBE["client_secret"], "redirect_uri": REDIRECT_URI,
                             "grant_type": "authorization_code", "code_verifier": verifier})


def access_token() -> str:
    """用 config 的 refresh_token 換存取權杖（快取到快過期）。授權被撤銷或半年沒用 → invalid_grant，要重跑 login。"""
    if _token.get("exp", 0) > time.time() + 60:
        return _token["token"]
    if not authorized():
        raise YouTubeError("還沒授權 YouTube：sudo venv/bin/python scripts/youtube_auth.py login")
    j = _post(TOKEN_URL, {"client_id": config.YOUTUBE["client_id"], "client_secret": config.YOUTUBE["client_secret"],
                          "refresh_token": config.YOUTUBE["refresh_token"], "grant_type": "refresh_token"})
    _token.update(token=j["access_token"], exp=time.time() + int(j.get("expires_in") or 3600))
    return _token["token"]


def revoke(token: str) -> None:
    """撤銷授權（選錯頻道時用）；失敗不管，使用者也可以到 Google 帳戶的「第三方存取權」移除。"""
    try:
        _post(REVOKE_URL, {"token": token})
    except YouTubeError:
        pass


def granted() -> Sequence[str]:
    """目前這份授權實際拿到的範圍（加了新範圍要重跑 login 才會有）。"""
    return (_post(TOKENINFO_URL, {"access_token": access_token()}).get("scope") or "").split()


def put(path: str, params: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
    req = urllib.request.Request(f"{API}/{path}?{urllib.parse.urlencode(params)}", method="PUT",
                                 data=json.dumps(body, ensure_ascii=False).encode(),
                                 headers={"Authorization": f"Bearer {access_token()}",
                                          "Content-Type": "application/json; charset=utf-8"})
    return _open(req)


def get(path: str, params: Dict[str, str], token: Optional[str] = None) -> Dict[str, Any]:
    req = urllib.request.Request(f"{API}/{path}?{urllib.parse.urlencode(params)}",
                                 headers={"Authorization": f"Bearer {token or access_token()}"})
    return _open(req)


def my_channel(token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """授權的是哪個頻道：{id, title, handle, url, long_uploads, made_for_kids}；帳號沒有頻道回 None。
    long_uploads＝"allowed" 表示做過電話驗證；made_for_kids 是頻道層級的「為兒童打造」設定。"""
    items = get("channels", {"part": "snippet,status", "mine": "true"}, token).get("items") or []
    if not items:
        return None
    c = items[0]
    sn, st = c.get("snippet") or {}, c.get("status") or {}
    return {"id": c.get("id"), "title": sn.get("title"), "handle": sn.get("customUrl"),
            "url": f"https://www.youtube.com/channel/{c.get('id')}",
            "long_uploads": st.get("longUploadsStatus"),
            "made_for_kids": st.get("selfDeclaredMadeForKids", st.get("madeForKids"))}
