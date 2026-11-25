"""粉專集點的純函式（不連資料庫）。資料庫那一段用本機測試伺服器實測（Utest_ 假資料，測完刪掉）。

執行：./venv/bin/python -m unittest tests.test_fb_points
"""
import hashlib
import unittest
from unittest import mock

from api import fb_page, fb_points


class FindCode(unittest.TestCase):
    def test_explicit(self):
        for t in ["綁定碼 123456", "綁定123456", "綁定：123456", "FB123456", "fb 123456", "我的綁定碼是123456喔"]:
            self.assertEqual(fb_points.find_code(t), ("123456", True), t)

    def test_bare(self):
        self.assertEqual(fb_points.find_code("123456"), ("123456", False))
        self.assertEqual(fb_points.find_code("  123456 \n"), ("123456", False))

    def test_fullwidth(self):
        self.assertEqual(fb_points.find_code("１２３４５６"), ("123456", False))
        self.assertEqual(fb_points.find_code("綁定碼：１２３４５６"), ("123456", True))

    def test_not_code(self):
        for t in ["", "你好", "12345", "1234567", "電話 0912345678", "型號 123456X 多少錢", "綁定碼 1234567"]:
            self.assertEqual(fb_points.find_code(t)[0], None, t)


class Pure(unittest.TestCase):
    def test_hash(self):
        self.assertEqual(fb_points.person_hash("2468"), hashlib.sha256(b"2468").hexdigest())
        self.assertEqual(len(fb_points.code_hash("123456")), 64)

    def test_points_of(self):
        self.assertEqual(fb_points.points_of({"status": "counted", "points": 1}), 1)
        for st in ("repeat", "removed", "void"):
            self.assertEqual(fb_points.points_of({"status": st, "points": 1}), 0, st)

    def test_reply(self):
        self.assertIn("王先生 您好", fb_points.bound_reply("王先生", 3))
        self.assertIn("目前 3 點", fb_points.bound_reply("王先生", 3))
        self.assertTrue(fb_points.bound_reply("", 0).startswith("Facebook"))


class TryBind(unittest.TestCase):
    def setUp(self):
        fb_points._fails.clear()

    def test_bare_digits_not_found_is_normal_message(self):
        with mock.patch.object(fb_points, "bind_code", return_value=None):
            self.assertIsNone(fb_points.try_bind("p1", "123456"))
        self.assertEqual(fb_points._fails.get("p1"), [])     # 沒寫「綁定」不算打錯

    def test_explicit_not_found_and_lockout(self):
        with mock.patch.object(fb_points, "bind_code", return_value=None) as bc:
            for _ in range(fb_points.CODE_FAIL_MAX):
                self.assertEqual(fb_points.try_bind("p2", "綁定碼 111111"), fb_points.FAIL_REPLY)
            self.assertEqual(fb_points.try_bind("p2", "綁定碼 111111"), fb_points.LOCK_REPLY)
            self.assertIsNone(fb_points.try_bind("p2", "222222"))        # 鎖住時光傳數字當一般訊息
            self.assertEqual(bc.call_count, fb_points.CODE_FAIL_MAX)    # 鎖住後不再查資料庫

    def test_success(self):
        with mock.patch.object(fb_points, "bind_code",
                               return_value={"member_id": 1, "display_name": "王先生", "total": 2}) as bc:
            msg = fb_points.try_bind("p3", "123456", "王大明")
        bc.assert_called_once_with("p3", "123456", "王大明")
        self.assertIn("綁定好了", msg)


class HandleHooks(unittest.TestCase):
    """fb_page.handle 和集點的接點：資料庫、Graph API 都換成假的。"""

    def _ev(self, **kw):
        ev = {"op": "add", "kind": "comment", "ext_id": "c1", "post_id": "P_1", "person_id": "u1",
              "person_name": "客人", "direction": "in", "source": "customer", "mid": "c1",
              "text": "好文", "plain": True}
        ev.update(kw)
        return ev

    def _run(self, ev, bind_reply=None):
        cur = mock.MagicMock()
        conn = mock.MagicMock()
        conn.__enter__.return_value = conn
        conn.cursor.return_value.__enter__.return_value = cur
        with mock.patch.object(fb_page, "_conn", return_value=conn), \
             mock.patch.object(fb_page, "_thread", return_value=7), \
             mock.patch.object(fb_page, "log_msg", return_value=(1, True)), \
             mock.patch.object(fb_page, "get_thread", return_value={"person_name": "客人"}), \
             mock.patch.object(fb_page, "_mode", return_value="bot"), \
             mock.patch.object(fb_page.ai_chat, "enabled", return_value=False), \
             mock.patch.object(fb_points, "record_comment") as rec, \
             mock.patch.object(fb_points, "remove_comment") as rem, \
             mock.patch.object(fb_points, "try_bind", return_value=bind_reply) as tb, \
             mock.patch.object(fb_page, "_system_reply") as sr:
            fb_page.handle(ev)
        return rec, rem, tb, sr

    def test_comment_records_points(self):
        rec, rem, tb, sr = self._run(self._ev())
        rec.assert_called_once_with("c1", "P_1", "u1", "客人")
        tb.assert_not_called()

    def test_page_own_comment_not_counted(self):
        rec, *_ = self._run(self._ev(person_id=None, direction="out", source="staff"))
        rec.assert_not_called()

    def test_remove(self):
        rec, rem, *_ = self._run(self._ev(op="remove"))
        rem.assert_called_once_with("c1")
        rec.assert_not_called()

    def test_messenger_code_replies(self):
        ev = self._ev(kind="messenger", ext_id="psid9", post_id=None, person_id="psid9", text="綁定碼 123456")
        rec, rem, tb, sr = self._run(ev, bind_reply="好了")
        tb.assert_called_once()
        sr.assert_called_once_with(7, "psid9", "好了")
        rec.assert_not_called()

    def test_messenger_plain_text_skips_bind(self):
        ev = self._ev(kind="messenger", ext_id="psid9", post_id=None, person_id="psid9", text="請問營業時間")
        rec, rem, tb, sr = self._run(ev)
        tb.assert_not_called()
        sr.assert_not_called()

    def test_points_error_does_not_break(self):
        with mock.patch.object(fb_points, "record_comment", side_effect=RuntimeError("no table")):
            self.assertIsNone(fb_page._points(fb_points.record_comment, "c", "p", "u", "n"))


if __name__ == "__main__":
    unittest.main()
