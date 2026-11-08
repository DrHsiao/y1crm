"""Facebook 粉專整合的純函式（不連資料庫、不打 Graph API）。

執行：./venv/bin/python -m unittest tests.test_fb_page
"""
import hashlib
import hmac
import unittest
from datetime import datetime, timedelta

from api import ai_chat, fb_page

PAGE, APP = "1000", "555"


def messaging(*items):
    return {"object": "page", "entry": [{"id": PAGE, "time": 1, "messaging": list(items)}]}


def feed(**value):
    return {"object": "page", "entry": [{"id": PAGE, "time": 1,
                                         "changes": [{"field": "feed", "value": {"item": "comment", **value}}]}]}


class Signature(unittest.TestCase):
    def test_good_and_bad(self):
        body = '{"object":"page"}'.encode()
        sig = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(fb_page.verify_signature(body, sig, "secret"))
        self.assertFalse(fb_page.verify_signature(body + b" ", sig, "secret"))
        self.assertFalse(fb_page.verify_signature(body, sig, "other"))
        self.assertFalse(fb_page.verify_signature(body, sig[7:], "secret"))     # 少了 sha256= 前綴
        self.assertFalse(fb_page.verify_signature(body, sig, ""))               # 沒設 App 密鑰一律拒絕
        self.assertFalse(fb_page.verify_signature(body, "sha256=中文", "secret"))  # 非 ASCII 不能丟例外

    def test_appsecret_proof(self):
        want = hmac.new(b"s", b"tok", hashlib.sha256).hexdigest()
        self.assertEqual(fb_page.appsecret_proof("tok", "s"), want)


class PageToken(unittest.TestCase):
    """page_token 可以貼粉專權杖，也可以貼系統使用者權杖（自動換成粉專權杖）。"""

    def setUp(self):
        self._cfg, self._call = dict(fb_page.config.FACEBOOK), fb_page._call
        fb_page.config.FACEBOOK.update(page_id=PAGE, page_token="raw", app_secret="")
        fb_page._tok_cache.clear()
        self.calls = []

    def tearDown(self):
        fb_page.config.FACEBOOK.clear()
        fb_page.config.FACEBOOK.update(self._cfg)
        fb_page._call = self._call
        fb_page._tok_cache.clear()

    def fake(self, answers):
        def call(token, path, payload, method):
            self.calls.append(path)
            return answers[path]
        fb_page._call = call

    def test_page_token_used_as_is(self):
        self.fake({"me": {"id": PAGE}})
        self.assertEqual(fb_page._page_token(), "raw")
        self.assertEqual(fb_page._page_token(), "raw")
        self.assertEqual(self.calls, ["me"])                 # 第二次用快取

    def test_system_user_token_exchanged(self):
        self.fake({"me": {"id": "999"}, PAGE: {"access_token": "pagetok"}})
        self.assertEqual(fb_page._page_token(), "pagetok")

    def test_page_not_assigned(self):
        self.fake({"me": {"id": "999"}, PAGE: {"id": PAGE}})
        with self.assertRaises(fb_page.GraphError):
            fb_page._page_token()
        self.assertEqual(fb_page._tok_cache, {})


class ParseMessenger(unittest.TestCase):
    def test_customer_text(self):
        ev, = fb_page.parse(messaging({"sender": {"id": "77"}, "recipient": {"id": PAGE},
                                       "message": {"mid": "m1", "text": "請問營業時間"}}), PAGE, APP)
        self.assertEqual((ev["kind"], ev["ext_id"], ev["direction"], ev["source"], ev["mid"], ev["text"], ev["plain"]),
                         ("messenger", "77", "in", "customer", "m1", "請問營業時間", True))

    def test_attachment_not_plain(self):
        ev, = fb_page.parse(messaging({"sender": {"id": "77"}, "recipient": {"id": PAGE},
                                       "message": {"mid": "m2", "attachments": [{"type": "image"}]}}), PAGE, APP)
        self.assertEqual(ev["text"], "[客人傳了圖片]")
        self.assertFalse(ev["plain"])     # 圖片不送 AI

    def test_echo(self):
        ours = {"sender": {"id": PAGE}, "recipient": {"id": "77"},
                "message": {"mid": "m3", "text": "您好", "is_echo": True, "app_id": int(APP)}}
        self.assertEqual(fb_page.parse(messaging(ours), PAGE, APP), [])     # 我們自己送的：送出時已記
        inbox = {"sender": {"id": PAGE}, "recipient": {"id": "77"},
                 "message": {"mid": "m4", "text": "我是店員", "is_echo": True, "app_id": 263902037430900}}
        ev, = fb_page.parse(messaging(inbox), PAGE, APP)
        self.assertEqual((ev["ext_id"], ev["direction"], ev["source"]), ("77", "out", "staff"))

    def test_ignored(self):
        self.assertEqual(fb_page.parse(messaging({"sender": {"id": "77"}, "recipient": {"id": PAGE},
                                                  "read": {"watermark": 1}}), PAGE, APP), [])
        self.assertEqual(fb_page.parse({"object": "instagram", "entry": []}, PAGE, APP), [])
        self.assertEqual(fb_page.parse({}, PAGE, APP), [])


class ParseComments(unittest.TestCase):
    def test_top_level(self):
        ev, = fb_page.parse(feed(verb="add", comment_id="P_1", post_id="1000_9", parent_id="1000_9",
                                 message="請問有到府服務嗎", **{"from": {"id": "42", "name": "王小明"}}), PAGE)
        self.assertEqual((ev["kind"], ev["ext_id"], ev["post_id"], ev["person_name"], ev["direction"]),
                         ("comment", "P_1", "1000_9", "王小明", "in"))

    def test_reply_joins_parent(self):
        ev, = fb_page.parse(feed(verb="add", comment_id="P_2", post_id="1000_9", parent_id="P_1",
                                 message="我也想問", **{"from": {"id": "43", "name": "李"}}), PAGE)
        self.assertEqual(ev["ext_id"], "P_1")

    def test_page_reply_is_staff(self):
        ev, = fb_page.parse(feed(verb="add", comment_id="P_3", post_id="1000_9", parent_id="P_1",
                                 message="有的喔", **{"from": {"id": PAGE, "name": "韻譯"}}), PAGE)
        self.assertEqual((ev["direction"], ev["source"], ev["person_name"]), ("out", "staff", None))

    def test_edit_remove_and_others(self):
        e1, = fb_page.parse(feed(verb="edited", comment_id="P_1", post_id="1000_9", message="改過"), PAGE)
        e2, = fb_page.parse(feed(verb="remove", comment_id="P_1", post_id="1000_9"), PAGE)
        self.assertEqual((e1["op"], e2["op"]), ("edit", "remove"))
        self.assertEqual(fb_page.parse(feed(verb="hide", comment_id="P_1", post_id="1000_9"), PAGE), [])
        reaction = {"object": "page", "entry": [{"changes": [{"field": "feed", "value": {"item": "reaction"}}]}]}
        self.assertEqual(fb_page.parse(reaction, PAGE), [])

    def test_photo_comment(self):
        ev, = fb_page.parse(feed(verb="add", comment_id="P_5", post_id="1000_9", photo="https://x/y.jpg",
                                 **{"from": {"id": "42"}}), PAGE)
        self.assertEqual(ev["text"], "[留言附圖片]")
        self.assertFalse(ev["plain"])


class PostText(unittest.TestCase):
    def test_price_blocked(self):
        for t in ("現在 12,000 元起", "NT$3000", "限時特價", "價格請洽門市", "$ 500"):
            errors, _ = fb_page.check_post_text(t)
            self.assertTrue(errors, t)

    def test_claims_warned(self):
        errors, warns = fb_page.check_post_text("保證改善聽力，效果最好")
        self.assertEqual(errors, [])
        self.assertEqual(len(warns), 3)

    def test_clean(self):
        self.assertEqual(fb_page.check_post_text("助聽器的日常保養\n\n每天睡前用乾布擦拭。"), ([], []))
        self.assertTrue(fb_page.check_post_text("   ")[0])
        self.assertTrue(fb_page.check_post_text("字" * (fb_page.MAX_POST + 1))[0])

    def test_post_url(self):
        self.assertEqual(fb_page.post_url("https://a.tw/", "article", "care-101"), "https://a.tw/column/care-101")
        self.assertEqual(fb_page.post_url("https://a.tw", "care", "battery"), "https://a.tw/care/battery")
        self.assertIsNone(fb_page.post_url("https://a.tw", "share", "x"))     # 好評分享不能分享（見證有限制）
        self.assertIsNone(fb_page.post_url("https://a.tw", "about", "x"))

    def test_default_text(self):
        self.assertEqual(fb_page.default_post_text("標題", "摘要"), "標題\n\n摘要")
        self.assertEqual(fb_page.default_post_text("標題", ""), "標題")


class Window(unittest.TestCase):
    def test_24h(self):
        now = datetime(2026, 10, 24, 12, 0)
        self.assertTrue(fb_page.in_window(now - timedelta(hours=23, minutes=59), now))
        self.assertFalse(fb_page.in_window(now - timedelta(hours=24), now))
        self.assertFalse(fb_page.in_window(None, now))


class Prompt(unittest.TestCase):
    def test_channel_note(self):
        line = ai_chat.system_prompt()
        self.assertNotIn("Facebook", line)                          # LINE 的提示詞完全不變
        self.assertIn("公開留言", ai_chat.system_prompt("comment"))
        self.assertIn("Messenger", ai_chat.system_prompt("messenger"))
        self.assertTrue(ai_chat.system_prompt("comment").startswith(line))


if __name__ == "__main__":
    unittest.main()
