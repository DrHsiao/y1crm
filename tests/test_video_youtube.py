"""影片送 YouTube：網址解析、要貼的文字、檢查結果、存檔帶過來的欄位（不連資料庫、不連 YouTube）。

執行：./venv/bin/python -m unittest tests.test_video_youtube
"""
import json
import unittest
from contextlib import contextmanager
from unittest import mock

import config
from api import fb_drafts, video_youtube as vy, web_admin, youtube

SITE = "https://y1ear.com"
STORE = {"name": "韻譯助聽器 睿聲中正門市", "brand": "韻譯助聽器", "address": "台北市中正區羅斯福路一段105號",
         "phone": "02-2341-5968", "hours": [{"days": "週一至週六", "time": "09:00–18:00"}, {"days": "週日", "time": "公休"}],
         "transit": [{"lines": ["red"], "text": "捷運紅線 - 中正紀念堂站 3號出口"}]}
VID = "dQw4w9WgXcQ"


def post(**k):
    extra = {"keywords": ["聽力警訊", "長輩聽不清楚", "聽力警訊"],
             "video": {"file": "videos/0123456789abcdef.mp4",
                       "plan": [{"say": "聽力多半是慢慢變差的。", "sec": 5}, {"say": "第一個警訊：<音量>越開越大。", "sec": 5}]}}
    return {"id": 42, "kind": "article", "slug": "signs-of-hearing-loss", "title": "聽力變差的警訊",
            "summary": "家人的觀察很重要。", "category": "聽力保健", "status": "published",
            "extra": json.dumps(extra, ensure_ascii=False), **k}


class VideoId(unittest.TestCase):
    def test_forms(self):
        for s in (VID, f"https://www.youtube.com/watch?v={VID}&t=3s", f"youtube.com/watch?v={VID}",
                  f"https://youtu.be/{VID}?si=abc", f"https://m.youtube.com/watch?v={VID}",
                  f"https://www.youtube.com/shorts/{VID}", f"https://www.youtube.com/embed/{VID}",
                  f"https://www.youtube-nocookie.com/embed/{VID}?rel=0", f"https://studio.youtube.com/video/{VID}/edit",
                  f"  https://youtu.be/{VID}\n"):
            self.assertEqual(vy.video_id(s), VID, s)

    def test_bad(self):
        for s in ("", "hello", f"https://evil.example/watch?v={VID}", f"https://youtube.com.evil.example/watch?v={VID}",
                  "https://www.youtube.com/watch?v=short", f"https://www.youtube.com/channel/{VID}",
                  f"https://www.youtube.com/watch?v={VID}x", "javascript:alert(1)"):
            self.assertIsNone(vy.video_id(s), s)


class Pack(unittest.TestCase):
    def test_pack(self):
        p = vy.pack(post(), STORE, SITE + "/")
        self.assertEqual(p["title"], "聽力變差的警訊｜韻譯助聽器")
        d = p["description"]
        self.assertTrue(d.startswith("家人的觀察很重要。\n▶ 完整文章：https://y1ear.com/column/signs-of-hearing-loss\n\n"))
        for _, label, path in fb_drafts.FOOTER_LINKS:
            self.assertIn(f"{label}：{SITE}{path}", d)
        self.assertIn("【影片內容】\n聽力多半是慢慢變差的。\n第一個警訊：音量越開越大。", d)
        self.assertIn("📍 台北市中正區羅斯福路一段105號", d)
        self.assertIn("🕘 週一至週六 09:00–18:00、週日 公休", d)
        self.assertIn(f"💬 LINE 諮詢：{SITE}/line?src=youtube", d)
        self.assertIn(vy.DISCLAIMER, d)
        self.assertTrue(d.endswith("#助聽器 #聽力保健 #台北助聽器"))
        self.assertNotRegex(d, "[<>]")
        self.assertEqual(p["tags"][:3], ["聽力警訊", "長輩聽不清楚", "聽力保健"])      # 重複的只留一個
        self.assertIn("韻譯助聽器睿聲中正門市", p["tags"])
        self.assertEqual(p["tags_text"], ",".join(p["tags"]))

    def test_summary_wording(self):
        d = vy.pack(post(summary="本文說明可能原因。"), STORE, SITE)["description"]
        self.assertTrue(d.startswith("這支影片說明可能原因。\n"))

    def test_limits(self):
        p = vy.pack(post(title="長" * 95, category="助聽器知識"), STORE, SITE)
        self.assertEqual(p["title"], "長" * 95)                     # 加品牌會超過 100 字就不加
        self.assertTrue(p["description"].endswith("#助聽器 #助聽器知識 #聽力保健"))
        many = json.dumps({"keywords": [f"關鍵字{i:03d}" for i in range(200)]})
        tags = vy.pack(post(extra=many), STORE, SITE)["tags"]
        self.assertLessEqual(len(",".join(tags)), vy.TAGS_MAX)

    def test_no_site_or_video(self):
        p = vy.pack(post(extra="{}", summary=""), {}, "")
        self.assertEqual(p["title"], "聽力變差的警訊")
        self.assertNotIn("http", p["description"])
        self.assertNotIn("影片內容", p["description"])

    def test_channel_template(self):
        t = vy.channel_template(STORE, SITE)
        self.assertNotIn("完整文章", t)
        self.assertIn("【韻譯助聽器 睿聲中正門市】", t)
        self.assertIn(vy.DISCLAIMER, t)


def item(**st):
    return {"id": VID, "snippet": {"title": "影片", "channelId": "UCours"},
            "status": {"privacyStatus": "public", "embeddable": True, "uploadStatus": "processed", **st}}


class Judge(unittest.TestCase):
    def test_ok(self):
        j = vy.judge(item(), "UCours")
        self.assertEqual((j["ok"], j["problem"], j["note"], j["title"]), (True, "", "", "影片"))

    def test_problems(self):
        self.assertFalse(vy.judge(None, "UCours")["ok"])
        self.assertIn("不是門市頻道", vy.judge(item(), "UCother")["problem"])
        self.assertIn("私人", vy.judge(item(privacyStatus="private"), "UCours")["problem"])
        self.assertIn("允許嵌入", vy.judge(item(embeddable=False), "UCours")["problem"])
        self.assertFalse(vy.judge(item(uploadStatus="rejected"), "UCours")["ok"])

    def test_notes(self):
        j = vy.judge(item(privacyStatus="unlisted"), "UCours")
        self.assertTrue(j["ok"])
        self.assertIn("搜尋不到", j["note"])
        self.assertIn("處理", vy.judge(item(uploadStatus="uploaded"), "UCours")["note"])


class FakeCur:
    def __init__(self, row):
        self.row, self.sql = row, []

    def execute(self, sql, args=()):
        self.sql.append((sql, args))

    def fetchone(self):
        return self.row

    def fetchall(self):
        return [self.row] if self.row else []


class Link(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(config.YOUTUBE, client_id="i", client_secret="s", refresh_token="r", channel_id="UCours")
        p.start()
        self.addCleanup(p.stop)

    def test_saves_after_check(self):
        cur = FakeCur(post())
        with mock.patch.object(vy, "fetch", return_value={VID: item()}) as f:
            info = vy.link(cur, 42, f"https://youtu.be/{VID}", "john")
        f.assert_called_once_with([VID])
        self.assertEqual((info["id"], info["ok"], info["by"], info["for"]), (VID, True, "john", "videos/0123456789abcdef.mp4"))
        sql, args = cur.sql[-1]
        self.assertIn("JSON_SET(extra, '$.video.youtube'", sql)
        self.assertEqual(json.loads(args[0])["id"], VID)

    def test_problem_still_saved(self):
        cur = FakeCur(post())
        with mock.patch.object(vy, "fetch", return_value={VID: item(privacyStatus="private")}):
            info = vy.link(cur, 42, VID, "john")
        self.assertFalse(info["ok"])
        self.assertFalse(json.loads(cur.sql[-1][1][0])["ok"])

    def test_refuses(self):
        with self.assertRaises(vy.Invalid):
            vy.link(FakeCur(post()), 42, "https://example.com/x", "john")
        with self.assertRaises(vy.Invalid):                               # 還沒做影片
            vy.link(FakeCur(post(extra='{"video": ""}')), 42, VID, "john")
        with self.assertRaises(vy.Invalid):
            vy.link(FakeCur(None), 42, VID, "john")
        with mock.patch.dict(config.YOUTUBE, refresh_token=""), self.assertRaises(vy.Invalid):
            vy.link(FakeCur(post()), 42, VID, "john")
        cur = FakeCur(post())
        with mock.patch.object(vy, "fetch", side_effect=youtube.YouTubeError("HTTP 500")), self.assertRaises(vy.Invalid):
            vy.link(cur, 42, VID, "john")
        self.assertFalse(any("UPDATE" in s for s, _ in cur.sql))         # 連不上就不存


class Recheck(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(config.YOUTUBE, client_id="i", client_secret="s", refresh_token="r", channel_id="UCours")
        p.start()
        self.addCleanup(p.stop)

    def run_with(self, saved, fetch, tall=None):
        class Cur(FakeCur):                       # 橫式、直式（Shorts）各查一次
            def fetchall(self):
                sql = self.sql[-1][0]
                if "tall.youtube" in sql:
                    return [{"id": 42, "yt": json.dumps(tall)}] if tall else []
                return [self.row] if self.row else []
        cur = Cur({"id": 42, "yt": json.dumps(saved)})

        @contextmanager
        def conn():
            class Db:
                def cursor(self):
                    @contextmanager
                    def c():
                        yield cur
                    return c()
            yield Db()
        with mock.patch.object(vy.review_art, "_conn", conn), mock.patch.object(vy, "fetch", fetch):
            n = vy.recheck()
        return n, [json.loads(a[0]) for s, a in cur.sql if s.startswith("UPDATE")]

    def test_goes_private(self):
        n, saved = self.run_with({"id": VID, "ok": True, "problem": "", "by": "john"},
                                 mock.Mock(return_value={VID: item(privacyStatus="private")}))
        self.assertEqual(n, 1)
        self.assertEqual((saved[0]["ok"], saved[0]["by"]), (False, "john"))

    def test_unchanged(self):
        n, saved = self.run_with({"id": VID, "ok": True, "problem": ""}, mock.Mock(return_value={VID: item()}))
        self.assertEqual((n, saved[0]["ok"]), (0, True))

    def test_short_checked_too(self):
        n, saved = self.run_with({"id": VID, "ok": True, "problem": ""}, mock.Mock(return_value={VID: item()}),
                                 tall={"id": "Shorts12345", "ok": True, "problem": ""})
        self.assertEqual(n, 1)                                     # Shorts 那支 YouTube 上找不到
        self.assertEqual([x["ok"] for x in saved], [True, False])

    def test_network_error_keeps(self):
        n, saved = self.run_with({"id": VID, "ok": True}, mock.Mock(side_effect=youtube.YouTubeError("x")))
        self.assertEqual((n, saved), (0, []))


class Carried(unittest.TestCase):
    def test_video_kinds(self):
        gen = {"file": "videos/0123456789abcdef.mp4", "youtube": {"id": VID}}
        self.assertEqual(web_admin.carried_extra({"video": gen, "cover_src": "stock", "en": "x"}),
                         {"video": gen, "cover_src": "stock"})
        self.assertEqual(web_admin.carried_extra({"video": "https://old.example/v"}), {})   # 小教室的影片連結用表單的值


class SyncMeta(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(config.YOUTUBE, client_id="i", client_secret="s", refresh_token="r", channel_id="UCours")
        p.start()
        self.addCleanup(p.stop)

    def linked(self, **yt):
        x = json.loads(post()["extra"])
        x["video"]["youtube"] = {"id": VID, "ok": True, **yt}
        return post(extra=json.dumps(x, ensure_ascii=False))

    def test_body_keeps_title_category(self):
        it = {"id": VID, "snippet": {"title": "John 的標題", "categoryId": "26", "defaultLanguage": "zh",
                                     "description": "舊的", "tags": [], "channelId": "UCours", "thumbnails": {}}}
        b = vy.meta_body(it, "新說明", ["a", "b"])
        self.assertEqual(b, {"id": VID, "snippet": {"title": "John 的標題", "categoryId": "26", "description": "新說明",
                                                    "tags": ["a", "b"], "defaultLanguage": "zh"}})

    def test_clean_meta(self):
        self.assertEqual(vy.clean_meta(" 說明<b> ", "聽力, 助聽器,聽力,,<x>"), ("說明b", ["聽力", "助聽器", "x"]))
        for bad in (("", "a"), ("x" * 5001, "a"), ("ok", ",".join(f"標籤{i:03d}號碼很長" for i in range(60)))):
            with self.assertRaises(vy.Invalid):
                vy.clean_meta(*bad)

    def test_sync(self):
        cur = FakeCur(self.linked())
        with mock.patch.object(vy, "fetch", return_value={VID: item()}), mock.patch.object(vy.youtube, "put") as put:
            r = vy.sync_meta(cur, 42, "john", "改過的說明", "聽力,助聽器")
        params, body = put.call_args[0][1], put.call_args[0][2]
        self.assertEqual(params, {"part": "snippet"})
        self.assertEqual((body["snippet"]["description"], body["snippet"]["tags"]), ("改過的說明", ["聽力", "助聽器"]))
        self.assertEqual(body["snippet"]["title"], "影片")                           # 標題照 YouTube 上的
        self.assertEqual((r["synced_by"], r["tags"]), ("john", 2))
        self.assertIn("synced_at", cur.sql[-1][0])

    def test_sync_default_pack(self):
        cur = FakeCur(self.linked())
        with mock.patch.object(vy, "fetch", return_value={VID: item()}), mock.patch.object(vy.youtube, "put") as put, \
                mock.patch.object(vy.fb_drafts, "store_info", return_value=STORE), \
                mock.patch.object(vy.fb_drafts, "site_url", return_value=SITE):
            vy.sync_meta(cur, 42, "john")
        self.assertIn("▶ 完整文章：https://y1ear.com/column/signs-of-hearing-loss", put.call_args[0][2]["snippet"]["description"])

    def test_sync_refuses(self):
        with self.assertRaises(vy.Invalid):                                     # 還沒貼網址
            vy.sync_meta(FakeCur(post()), 42, "john", "x", "")
        other = {**item(), "snippet": {"title": "t", "channelId": "UCother"}}
        with mock.patch.object(vy, "fetch", return_value={VID: other}), mock.patch.object(vy.youtube, "put") as put, \
                self.assertRaises(vy.Invalid):
            vy.sync_meta(FakeCur(self.linked()), 42, "john", "x", "")
        put.assert_not_called()
        err = youtube.YouTubeError("HTTP 403 Request had insufficient authentication scopes. insufficientPermissions")
        with mock.patch.object(vy, "fetch", return_value={VID: item()}), \
                mock.patch.object(vy.youtube, "put", side_effect=err), self.assertRaises(vy.Invalid) as cm:
            vy.sync_meta(FakeCur(self.linked()), 42, "john", "x", "")
        self.assertIn("force-ssl", str(cm.exception))


class Channel(unittest.TestCase):
    def test_set_channel(self):
        cur = FakeCur(None)
        url = web_admin.set_youtube_channel(cur, "UCNS7Tf0XQSBN_kiJXjj9GRg", "t")
        self.assertEqual(url, "https://www.youtube.com/channel/UCNS7Tf0XQSBN_kiJXjj9GRg")
        self.assertEqual(json.loads(cur.sql[0][1][0])["url"], url)
        for bad in ("", "UC123", "@rosund", "UCNS7Tf0XQSBN_kiJXjj9GR/"):
            with self.assertRaises(web_admin.Invalid):
                web_admin.set_youtube_channel(FakeCur(None), bad, "t")


if __name__ == "__main__":
    unittest.main()
