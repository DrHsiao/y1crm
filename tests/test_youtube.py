"""YouTube 授權的純函式與 config.toml 寫入（不連 Google）。

執行：./venv/bin/python -m unittest tests.test_youtube
"""
import base64
import hashlib
import io
import os
import stat
import tempfile
import tomllib
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

import config
from api import youtube as yt

CFG = """# 組態
[facebook]
page_id = "123"
refresh_token = "fb-不要動"

[youtube]
# YouTube 頻道
client_id     = "id.apps.googleusercontent.com"
client_secret = "s"

[web]
media_dir = "/x"
"""


class Pkce(unittest.TestCase):
    def test_challenge(self):
        v, c = yt.pkce()
        self.assertTrue(43 <= len(v) <= 128)
        want = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(c, want)
        self.assertNotEqual(yt.pkce()[0], v)

    def test_auth_url(self):
        with mock.patch.dict(config.YOUTUBE, client_id="test-id"):
            u = urllib.parse.urlparse(yt.auth_url("st", "ch"))
        q = dict(urllib.parse.parse_qsl(u.query))
        self.assertEqual(f"{u.scheme}://{u.netloc}{u.path}", yt.AUTH_URL)
        self.assertEqual(q["client_id"], "test-id")
        self.assertEqual(q["redirect_uri"], yt.REDIRECT_URI)
        self.assertEqual(q["scope"].split(), list(yt.SCOPES))
        self.assertEqual((q["access_type"], q["prompt"], q["state"]), ("offline", "consent", "st"))
        self.assertEqual((q["code_challenge"], q["code_challenge_method"]), ("ch", "S256"))
        self.assertNotIn("client_secret", q)


class Redirect(unittest.TestCase):
    def test_full_url(self):
        u = "http://127.0.0.1:8765/?state=abc&code=4/0Ab-cd&scope=x%20y"
        self.assertEqual(yt.parse_redirect(u, "abc"), "4/0Ab-cd")
        self.assertEqual(yt.parse_redirect("  127.0.0.1:8765/?state=abc&code=k#frag \n", "abc"), "k")

    def test_bad(self):
        for pasted, msg in (("http://127.0.0.1:8765/?state=old&code=k", "state"),
                            ("http://127.0.0.1:8765/?error=access_denied&state=abc", "access_denied"),
                            ("http://127.0.0.1:8765/?state=abc", "code"),
                            ("", "state")):
            with self.assertRaises(yt.YouTubeError) as cm:
                yt.parse_redirect(pasted, "abc")
            self.assertIn(msg, str(cm.exception))

    def test_missing_scopes(self):
        self.assertEqual(yt.missing_scopes(" ".join(yt.SCOPES)), [])
        self.assertEqual(yt.missing_scopes(" ".join(yt.SCOPES[:2])), [yt.SCOPES[2]])     # 舊授權沒有 force-ssl
        self.assertIn("https://www.googleapis.com/auth/youtube.force-ssl", yt.SCOPES)
        self.assertEqual(yt.missing_scopes(""), list(yt.SCOPES))


class Api(unittest.TestCase):
    def test_my_channel(self):
        resp = {"items": [{"id": "UCx", "snippet": {"title": "門市", "customUrl": "@y1ear"},
                           "status": {"longUploadsStatus": "allowed", "selfDeclaredMadeForKids": False}}]}
        with mock.patch.object(yt, "_open", return_value=resp) as op:
            ch = yt.my_channel("tok")
        req = op.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertIn("mine=true", req.full_url)
        self.assertEqual(ch, {"id": "UCx", "title": "門市", "handle": "@y1ear",
                              "url": "https://www.youtube.com/channel/UCx", "long_uploads": "allowed",
                              "made_for_kids": False})
        with mock.patch.object(yt, "_open", return_value={}):
            self.assertIsNone(yt.my_channel("tok"))

    def test_access_token_cache(self):
        with mock.patch.dict(config.YOUTUBE, client_id="i", client_secret="s", refresh_token="r"), \
                mock.patch.dict(yt._token, clear=True), \
                mock.patch.object(yt, "_post", return_value={"access_token": "a1", "expires_in": 3599}) as post:
            self.assertEqual(yt.access_token(), "a1")
            self.assertEqual(yt.access_token(), "a1")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args[0][1]["grant_type"], "refresh_token")

    def test_not_authorized(self):
        with mock.patch.dict(config.YOUTUBE, refresh_token=""), mock.patch.dict(yt._token, clear=True):
            with self.assertRaises(yt.YouTubeError):
                yt.access_token()

    def test_error_text(self):
        def http(body):
            return urllib.error.HTTPError("u", 400, "Bad", {}, io.BytesIO(body))
        self.assertEqual(yt._err(http(b'{"error":"invalid_grant","error_description":"Bad Request"}')),
                         "HTTP 400 invalid_grant Bad Request")
        self.assertEqual(yt._err(http(b'{"error":{"message":"Quota","errors":[{"reason":"quotaExceeded"}]}}')),
                         "HTTP 400 Quota quotaExceeded")
        self.assertEqual(yt._err(http(b"<html>")), "HTTP 400")


class TomlWrite(unittest.TestCase):
    def test_insert_and_replace(self):
        out = config.set_keys(CFG, "youtube", {"refresh_token": "1//r", "channel_id": "UCx"})
        d = tomllib.loads(out)
        self.assertEqual(d["youtube"]["refresh_token"], "1//r")
        self.assertEqual(d["youtube"]["channel_id"], "UCx")
        self.assertEqual(d["facebook"]["refresh_token"], "fb-不要動")
        self.assertEqual(d["web"]["media_dir"], "/x")
        self.assertIn("# YouTube 頻道", out)
        self.assertLess(out.index("channel_id"), out.index("[web]"))     # 加在 [youtube] 裡，不是檔尾
        again = config.set_keys(out, "youtube", {"refresh_token": "2//r"})
        self.assertEqual(again.count("refresh_token ="), 2)              # facebook 的一行＋youtube 的一行
        self.assertEqual(tomllib.loads(again)["youtube"]["refresh_token"], "2//r")
        self.assertEqual(again.replace("2//r", "1//r"), out)              # 只改那一行

    def test_new_section_and_escape(self):
        out = config.set_keys("[web]\nmedia_dir = \"/x\"\n\n", "youtube", {"refresh_token": 'a"b\\c'})
        self.assertTrue(out.endswith('\n\n[youtube]\nrefresh_token = "a\\"b\\\\c"\n'))
        self.assertEqual(tomllib.loads(out)["youtube"]["refresh_token"], 'a"b\\c')
        self.assertEqual(tomllib.loads(config.set_keys("", "youtube", {"k": "v"})), {"youtube": {"k": "v"}})

    def test_save_keeps_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.toml"
            p.write_text(CFG, encoding="utf-8")
            os.chmod(p, 0o600)
            with mock.patch.object(config, "_CONFIG_PATH", p), mock.patch.dict(config.YOUTUBE):
                config.save_keys("youtube", {"refresh_token": "1//r"})
                self.assertEqual(config.YOUTUBE["refresh_token"], "1//r")
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
            self.assertEqual(tomllib.loads(p.read_text(encoding="utf-8"))["youtube"]["refresh_token"], "1//r")
            self.assertEqual(sorted(x.name for x in Path(d).iterdir()), ["config.toml"])   # 暫存檔沒留下


if __name__ == "__main__":
    unittest.main()
