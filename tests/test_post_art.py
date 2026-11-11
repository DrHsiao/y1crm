"""貼文配圖（api/post_art.py）、相片貼文（fb_drafts.photo_*、fb_page.publish_photo）。
不連資料庫、不跑模型、不打 Graph API。

執行：./venv/bin/python -m unittest tests.test_post_art
"""
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from PIL import Image

from api import fb_drafts as fd
from api import fb_page, post_art, review_art

SITE = "https://y1ear.com"


class FakeCur:
    """SELECT 依序回 answers；其他 SQL 只記下來。"""
    def __init__(self, *answers):
        self.answers, self.sql = list(answers), []
        self._last = []

    def execute(self, sql, args=None):
        self.sql.append((sql, args))
        if sql.lstrip().upper().startswith("SELECT"):
            self._last = self.answers.pop(0) if self.answers else []

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


class Ctx:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return Ctx2(self.cur)


class Ctx2(Ctx):
    def __enter__(self):
        return self.cur


class Scene(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(post_art.plain("## 標題\n\n看 [官網](https://x.y/a) 和 ![圖](/m/a.webp)\n\n\n\n**粗體**"),
                         "標題\n\n看 官網 和 圖\n\n粗體")


class Targets(unittest.TestCase):
    MSG = f"冬天到了，記得除濕。\n\n{fd.footer(SITE)}\n\n{fd.STORE_TAG}"

    def draft(self, i, image=None, art=None, web_cover=None, web_extra=None):
        return {"id": i, "topic": f"題目{i}", "message": self.MSG, "image": image,
                "web_article_id": art, "web_cover": web_cover, "web_extra": json.dumps(web_extra or {})}

    def test_drafts(self):
        rows = [self.draft(1), self.draft(2, image="a.webp"), self.draft(3, art=106),
                self.draft(4, art=107, web_cover="own.webp"), self.draft(5, image="b.webp", art=108, web_cover="b.webp")]
        out = post_art.draft_targets(FakeCur(rows), [1, 2, 3, 4, 5], False)
        self.assertEqual([t["id"] for t in out], [1, 3, 4])               # 有圖的不換
        self.assertEqual([t["article"] for t in out], [None, 106, None])  # 店員自己放的專欄封面不動
        self.assertEqual(out[0]["text"], "冬天到了，記得除濕。")           # 官網宣傳、hashtag 不拿來比對主題
        out = post_art.draft_targets(FakeCur(rows[4:]), [5], True)       # SQL 只撈 id=5
        self.assertEqual((out[0]["id"], out[0]["article"]), (5, 108))     # 換一張：專欄封面是同一張 → 跟著換
        self.assertEqual(out[0]["current"], "b.webp")
        self.assertEqual(post_art.draft_targets(FakeCur(rows), [], False), [])

    def test_articles(self):
        rows = [{"id": 40, "kind": "article", "title": "第一次戴", "summary": "摘要", "body_md": "## 內文", "cover": None,
                 "extra": "{}", "category": "聽力保健"},
                {"id": 41, "kind": "article", "title": "怎麼選", "summary": "", "body_md": "x", "cover": "own.webp",
                 "extra": "{}", "category": "助聽器知識"},
                {"id": 81, "kind": "care", "title": "換電池", "summary": None, "body_md": "y", "cover": "ai.webp",
                 "extra": json.dumps({"cover_ai": True}), "category": None}]
        out = post_art.article_targets(FakeCur(rows), None, False)
        self.assertEqual([t["id"] for t in out], [40])                    # 沒封面的
        self.assertEqual(out[0]["text"], "摘要\n\n內文")
        out = post_art.article_targets(FakeCur(rows), [41, 81], True)
        self.assertEqual([t["id"] for t in out], [81])                    # 換一張只動 AI 畫的、圖庫的；原圖不動
        self.assertEqual(post_art.article_targets(FakeCur(rows), [], False), [])
        self.assertEqual(post_art.article_targets(FakeCur(rows), None, False, ("share",)), [])


class Photo(unittest.TestCase):
    MSG = f"今天的新聞。\n\n{fd.footer(SITE)}\n\n{fd.STORE_TAG}"

    def test_message(self):
        out = fd.photo_message(self.MSG, "https://n.example/1", "news")
        self.assertIn("今天的新聞。\n\n📰 新聞原文：https://n.example/1\n\n" + fd.FOOTER_HEAD, out)
        self.assertTrue(out.endswith(fd.STORE_TAG))
        self.assertIn("👉 全文：https://y1ear.com/care/battery", fd.photo_message(self.MSG, "https://y1ear.com/care/battery", "web"))
        self.assertEqual(fd.photo_message(self.MSG, None, "care"), self.MSG)
        self.assertEqual(fd.photo_message("看 https://a.b/c", "https://a.b/c"), "看 https://a.b/c")   # 已經在文字裡
        self.assertEqual(fd.photo_message("沒有宣傳段", "https://a.b/c"), "沒有宣傳段\n\n🔗 https://a.b/c")

    def test_jpeg(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.BytesIO()
            Image.new("RGB", (60, 40), "orange").save(buf, "WEBP")
            (Path(d) / "0123456789abcdef.webp").write_bytes(buf.getvalue())
            with mock.patch.dict(fd.config.WEB, {"media_dir": d}):
                jpg = fd.photo_jpeg("0123456789abcdef.webp")
                self.assertEqual(Image.open(io.BytesIO(jpg)).format, "JPEG")
                self.assertIsNone(fd.photo_jpeg("fedcba9876543210.webp"))    # 檔案不見：照舊發文字
                self.assertIsNone(fd.photo_jpeg("../x.webp"))
                self.assertIsNone(fd.photo_jpeg(None))

    def test_multipart(self):
        body = fb_page.multipart({"message": "哈囉", "published": "true"}, {"source": ("a.jpg", b"\xff\xd8JPEG", "image/jpeg")}, "BND")
        self.assertTrue(body.startswith(b'--BND\r\nContent-Disposition: form-data; name="message"\r\n\r\n'
                                        + "哈囉".encode() + b"\r\n"))
        self.assertIn(b'name="source"; filename="a.jpg"\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8JPEG\r\n', body)
        self.assertTrue(body.endswith(b"--BND--\r\n"))

    def test_publish_photo_dev(self):
        with mock.patch.object(fb_page, "dev_mode", return_value=True), mock.patch.object(fb_page, "_call") as call:
            self.assertIsNone(fb_page.publish_photo("文字", b"jpg"))
            call.assert_not_called()

    def test_publish_video(self):
        with mock.patch.object(fb_page, "dev_mode", return_value=True), mock.patch.object(fb_page, "_call") as call:
            self.assertIsNone(fb_page.publish_video("文字", "標題", b"mp4"))
            call.assert_not_called()
        with mock.patch.object(fb_page, "dev_mode", return_value=False), \
                mock.patch.object(fb_page, "_page_token", return_value="tok"), \
                mock.patch.object(fb_page, "_call", return_value={"id": "vid1"}) as call:
            self.assertEqual(fb_page.publish_video("文字", "標題", b"mp4", b"jpg"), "vid1")
        args, kw = call.call_args
        self.assertTrue(args[1].endswith("/videos"))
        self.assertEqual(args[2], {"description": "文字", "title": "標題", "published": "true"})
        self.assertEqual(sorted(kw["files"]), ["source", "thumb"])
        self.assertEqual((kw["host"], kw["timeout"]), ("https://graph-video.facebook.com", 300))

    def test_publish_photo_returns_post_id(self):
        with mock.patch.object(fb_page, "dev_mode", return_value=False), \
                mock.patch.object(fb_page, "_page_token", return_value="tok"), \
                mock.patch.object(fb_page, "_call", return_value={"id": "photo1", "post_id": "page_post1"}) as call:
            self.assertEqual(fb_page.publish_photo("文字", b"jpg"), "page_post1")
        args, kw = call.call_args
        self.assertTrue(args[1].endswith("/photos"))
        self.assertEqual(args[2]["message"], "文字")
        self.assertEqual(kw["files"]["source"][1], b"jpg")


class Publish(unittest.TestCase):
    MSG = f"保養提醒。\n\n{fd.footer(SITE)}\n\n{fd.STORE_TAG}"

    def row(self, i, image=None, link=None, cat="care"):
        return {"id": i, "day": date(2026, 11, 9), "category": cat, "message": self.MSG, "link": link, "image": image,
                "image_meta": None, "web_sync": 0, "web_post_id": None, "seo": None, "status": "new"}

    def test_photo_when_image(self):
        cur = FakeCur([self.row(1, "0123456789abcdef.webp", "https://n.example/1", "news"), self.row(2)])
        with mock.patch.object(fd, "_conn", return_value=Ctx(cur)), \
                mock.patch.object(fd, "photo_jpeg", side_effect=lambda n: b"JPG" if n else None), \
                mock.patch.object(fd.fb_page, "publish_photo", return_value="p_1") as photo, \
                mock.patch.object(fd.fb_page, "publish_post", return_value="p_2") as post:
            r = fd.publish([1, 2], False, "測試")
        self.assertTrue(r["ok"])
        self.assertEqual([(x["id"], x["fb_post_id"]) for x in r["results"]], [(1, "p_1"), (2, "p_2")])
        msg, img = photo.call_args[0]
        self.assertEqual(img, b"JPG")
        self.assertIn("📰 新聞原文：https://n.example/1", msg)              # 相片貼文：連結寫進文字
        post.assert_called_once_with(self.MSG, None)                       # 沒圖：照舊


class Source(unittest.TestCase):
    """每張圖都標原圖還是 AI 生成（John 2026-11-08：以後才能批次替換）。"""

    def test_set_cover_marks_ai(self):
        cur = FakeCur([{"cover": None}])
        with mock.patch.object(review_art.web_admin, "get_post", return_value={"cover": None}):
            review_art.set_cover(cur, Path("/tmp"), 40, "0123456789abcdef.webp", "prompt", "畫面", "測試")
        sql, args = cur.sql[-1]
        self.assertIn("'$.cover_src', 'ai'", sql)
        self.assertEqual(args[2], review_art.ART_MODEL)                    # 模型版本
        self.assertRegex(args[3], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")      # 畫的時間
        cur = FakeCur()
        with mock.patch.object(review_art.web_admin, "get_post", return_value=None):
            review_art.set_cover(cur, Path("/tmp"), 106, "a.webp", "p", "s", "測試", "舊模型/v0", "2026-11-08 06:01")
        self.assertEqual(cur.sql[-1][1][2:4], ("舊模型/v0", "2026-11-08 06:01"))   # 草稿的圖同步到專欄：沿用原本的紀錄

    def test_draft_meta(self):
        cur = FakeCur([{"image": None, "card": None}])
        post_art.set_draft_image(cur, Path("/tmp"), 1, "a.webp", {"src": "stock", "scene": "照片"})
        meta = json.loads(cur.sql[-1][1][1])
        self.assertEqual((meta["src"], meta["scene"]), ("stock", "照片"))
        cur = FakeCur([{"image": None, "card": None}])
        post_art.set_draft_image(cur, Path("/tmp"), 1, None)
        self.assertIsNone(cur.sql[-1][1][1])

    def test_illustrate_uses_stock(self):
        from api import stock_art
        rows = [{"id": 2, "topic": "換電池", "message": "電池快沒電", "image": None, "web_article_id": 108,
                 "web_cover": None, "web_extra": "{}"}]
        cur = FakeCur(rows, [])
        with mock.patch.object(review_art, "_conn", return_value=Ctx(cur)), \
                mock.patch.object(stock_art, "available", return_value=Stock.ITEMS), \
                mock.patch.object(stock_art, "usage", return_value={}), \
                mock.patch.object(stock_art, "set_cover") as cover, \
                mock.patch.object(post_art, "set_draft_image") as setimg:
            r = post_art.illustrate([2], [], "測試")
        self.assertEqual((r["total"], r["done"], r["errors"]), (1, 1, []))
        name, meta = setimg.call_args[0][3], setimg.call_args[0][4]
        self.assertEqual((name, meta["src"]), ("aaaaaaaaaaaaaaaa.webp", "stock"))      # 粉專不挑要標作者的電池照
        self.assertEqual(cover.call_args[0][2:4], (108, "aaaaaaaaaaaaaaaa.webp"))      # 同步的專欄封面跟著換

    def test_upload_is_original(self):
        from api import web_admin
        self.assertIn("'$.cover_src', 'upload'", web_admin.UPLOAD_COVER_KEYS)
        for k in ("cover_src", "cover_model", "cover_at"):
            self.assertIn(k, web_admin.COVER_KEYS)                         # 存檔時保留、換圖時清掉
            self.assertIn(f"'$.{k}'", web_admin.CLEAR_COVER_KEYS)

    def test_backfill_mark(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("image_sources", Path(__file__).resolve().parent.parent
                                                      / "scripts" / "illustrate" / "image_sources.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from datetime import datetime
        t = datetime(2026, 11, 8, 14, 16)
        self.assertEqual(mod.mark({"cover_ai": True}, t),
                         {"cover_ai": True, "cover_src": "ai", "cover_model": review_art.ART_MODEL, "cover_at": "2026-11-08 14:16"})
        self.assertEqual(mod.mark({"format": "quote"}, t), {"format": "quote", "cover_src": "upload"})
        self.assertEqual(mod.mark({"cover_ai": False}, t)["cover_src"], "upload")
        self.assertIsNone(mod.mark({"cover_src": "upload"}, t))                  # 已經標過的不動


class Stock(unittest.TestCase):
    """「助聽器知識」用圖庫的真實照片，不用 AI 插圖（John 2026-11-08）。"""
    ITEMS = [{"id": "senior", "file": "aaaaaaaaaaaaaaaa.webp", "tags": ["第一次", "適應", "配戴", "通用"]},
             {"id": "fit", "file": "bbbbbbbbbbbbbbbb.webp", "tags": ["驗配", "調整", "選購"]},
             {"id": "otoscope", "file": "cccccccccccccccc.webp", "tags": ["回饋", "嗶嗶", "耳垢"]},
             {"id": "battery", "file": "dddddddddddddddd.webp", "tags": ["電池", "換電池"], "credit_required": True,
              "author": "Laserlicht", "source": "Wikimedia Commons", "license": "CC BY-SA 4.0"}]

    def test_pick_by_topic(self):
        from api import stock_art
        self.assertEqual(stock_art.pick(self.ITEMS, "助聽器一直嗶嗶叫，可能是耳垢")["id"], "otoscope")
        self.assertEqual(stock_art.pick(self.ITEMS, "換電池的小技巧")["id"], "battery")
        self.assertEqual(stock_art.pick(self.ITEMS, "換電池的小技巧", allow_credit=False)["id"], "senior")  # 粉專不挑要標作者的
        self.assertEqual(stock_art.pick(self.ITEMS, "第一次戴", avoid=["aaaaaaaaaaaaaaaa.webp"])["id"], "fit")   # 換一張
        used = {"aaaaaaaaaaaaaaaa.webp": 2}
        self.assertEqual(stock_art.pick(self.ITEMS, "第一次戴", used)["id"], "senior")   # 只有它相關：常用也不換成不相關的
        self.assertEqual(stock_art.pick(self.ITEMS, "第一次戴助聽器要調整", used)["id"], "fit")  # 都相關：常用的讓一讓
        self.assertEqual(stock_art.pick(self.ITEMS, "充電座怎麼用")["id"], "senior")     # 都不相關：用「通用」的
        self.assertIsNone(stock_art.pick([], "x"))

    def test_credit(self):
        from api import stock_art
        c = stock_art.credit(self.ITEMS[3])
        self.assertEqual(stock_art.credit_line(c), "圖：Laserlicht／Wikimedia Commons（CC BY-SA 4.0），經裁切調色")
        self.assertEqual(stock_art.credit_line(stock_art.credit(self.ITEMS[0])), "")

    def test_process(self):
        from api import stock_art
        for size, pad in (((1920, 1003), "#000000"), ((1000, 1500), "#000000"), ((1000, 1500), None), ((3000, 2000), None)):
            buf = io.BytesIO()
            Image.new("RGB", size, "white").save(buf, "PNG")
            im = Image.open(io.BytesIO(stock_art.process(buf.getvalue(), (0.5, 0.4), pad)))
            self.assertEqual(im.size, (stock_art.OUT_W, stock_art.OUT_H))
        buf = io.BytesIO()                                   # 補邊不能把圓形拉成橢圓：中間的方塊還是方的
        im = Image.new("RGB", (1920, 1003), "black")
        im.paste(Image.new("RGB", (400, 400), "white"), (760, 301))
        im.save(buf, "PNG")
        out = Image.open(io.BytesIO(stock_art.process(buf.getvalue(), pad="#000000"))).convert("L")
        box = out.point(lambda v: 255 if v > 128 else 0).getbbox()
        self.assertAlmostEqual((box[2] - box[0]) / (box[3] - box[1]), 1.0, delta=0.02)

    def test_replaceable_covers(self):
        arts = [{"id": 108, "kind": "article", "title": "電池", "summary": "", "body_md": "y", "cover": "ai.webp",
                 "extra": json.dumps({"cover_src": "ai"})}]
        t = post_art.article_targets(FakeCur(arts), [108], True)
        self.assertEqual(t[0]["current"], "ai.webp")                                     # 以前 AI 畫的換成照片
        arts[0]["extra"] = json.dumps({"cover_src": "stock"})
        self.assertEqual(len(post_art.article_targets(FakeCur(arts), [108], True)), 1)     # 圖庫照片可以換一張
        arts[0]["extra"] = json.dumps({"cover_src": "upload"})
        self.assertEqual(post_art.article_targets(FakeCur(arts), [108], True), [])          # 原圖不動

    def test_ensure_stock_before_publish(self):
        from api import stock_art
        r = {"id": 2, "topic": "換電池", "message": "電池", "image": "ai.webp", "image_meta": json.dumps({"src": "ai"}),
             "seo": json.dumps({"category": "助聽器知識"})}
        with mock.patch.object(stock_art, "available", return_value=self.ITEMS), \
                mock.patch.object(stock_art, "usage", return_value={}), \
                mock.patch.object(post_art, "set_draft_image") as setimg:
            post_art.ensure_stock(FakeCur(), r)
            self.assertEqual(r["image"], "aaaaaaaaaaaaaaaa.webp")          # 換成不用標作者的
            self.assertEqual(json.loads(r["image_meta"])["src"], "stock")
            setimg.assert_called_once()
            setimg.reset_mock()                                            # John 11-08：好評以外的分類也一律換
            post_art.ensure_stock(FakeCur(), {**r, "seo": json.dumps({"category": "生活樂活"}), "image": "ai.webp",
                                              "image_meta": json.dumps({"src": "ai"})})
            setimg.assert_called_once()
            setimg.reset_mock()
            post_art.ensure_stock(FakeCur(), {**r, "image": None})          # 店員拿掉圖的：不補
            setimg.assert_not_called()


if __name__ == "__main__":
    unittest.main()
