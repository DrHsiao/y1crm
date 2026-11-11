"""粉專圖卡（api/post_card.py）：標題、排版、做圖卡記到草稿、發佈用圖卡。
不連資料庫、不跑模型、不打 Graph API。

執行：./venv/bin/python -m unittest tests.test_post_card
"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

from api import fb_drafts as fd
from api import post_art, post_card, web_admin
from tests.test_post_art import Ctx, FakeCur

SITE = "https://y1ear.com"
MSG = f"電視音量總是讓全家傷腦筋嗎？其實可以先從簡單方法開始。\n\n・開字幕\n\n{fd.footer(SITE)}\n\n{fd.STORE_TAG}"


class Title(unittest.TestCase):
    def test_fallback(self):
        self.assertEqual(post_card.fallback_title(MSG), ("電視音量總是讓全家傷腦筋嗎？", "其實可以先從簡單方法開始"))
        t, s = post_card.fallback_title("每天幫耳內型助聽器做簡單清潔，比較不容易出狀況。")
        self.assertEqual((t, s), ("每天幫耳內型助聽器做簡單清潔", ""))               # 太長：在逗號斷
        self.assertLessEqual(len(post_card.fallback_title("一" * 30)[0]), post_card.TITLE_MAX)
        self.assertEqual(post_card.fallback_title("每天幫耳內型助聽器做簡單清潔，比較不容易出狀況，也比較安心。")[0],
                         "每天幫耳內型助聽器做簡單清潔")
        self.assertEqual(post_card.fallback_title("", "題目")[0], "題目")

    def test_ai(self):
        def ai(reply):
            return mock.patch.object(fd, "_chat", return_value=json.dumps(reply, ensure_ascii=False))
        with ai({"title": "電視越開越大聲？", "sub": "先試試字幕和坐近一點 📺"}):
            self.assertEqual(post_card.make_title(MSG), {"title": "電視越開越大聲？", "sub": "先試試字幕和坐近一點",
                                                         "src": "ai"})            # 表情符號拿掉
        for bad in ({"title": "一" * 16, "sub": ""},                            # 太長
                    {"title": "戴了最清楚", "sub": ""},                          # 最高級
                    {"title": "藍牙耳機也能用", "sub": ""},                       # 助聽器叫耳機
                    {"title": "", "sub": "x"}):
            with ai(bad):
                self.assertEqual(post_card.make_title(MSG)["src"], "text", bad)
        with mock.patch.object(fd, "_chat", side_effect=TimeoutError):
            self.assertEqual(post_card.make_title(MSG)["title"], "電視音量總是讓全家傷腦筋嗎？")

    def test_staff_title(self):
        self.assertEqual(post_card.check_title(" 電視 越開越大聲？", "坐近一點"), ("電視越開越大聲？", "坐近一點"))
        for t, s in (("", "x"), ("一" * 19, ""), ("戴了保證聽得清楚", ""), ("限時免費", "")):
            with self.assertRaises(ValueError):
                post_card.check_title(t, s)


class Layout(unittest.TestCase):
    def test_wrap_balanced(self):
        d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        f = post_card._font(72)
        self.assertEqual(post_card._wrap(d, "陪長輩打疫苗前，先檢查助聽器", f, 936),
                         ["陪長輩打疫苗前", "先檢查助聽器"])                       # 從逗號斷，不會剩一個字
        rows = post_card._wrap(d, "一二三四五六七八九十一二三四五", f, 936)
        self.assertEqual(len(rows), 2)
        self.assertLessEqual(abs(len(rows[0]) - len(rows[1])), 1)
        self.assertEqual(post_card._wrap(d, "短標題", f, 936), ["短標題"])

    def test_render(self):
        photo = Image.new("RGB", (1600, 1067), (120, 140, 160))
        for args in (("電視越開越大聲？", "先試試字幕", "家人溝通"), ("陪長輩打疫苗前，先檢查助聽器電量", "", ""),
                     ("短", "第二行也可以比較長一點點", "時事話題")):
            im = post_card.render(photo, *args, site=SITE)
            self.assertEqual(im.size, (post_card.W, post_card.H))
            self.assertEqual(im.getpixel((20, 1300)), post_card.NAVY)             # 字卡底色
            self.assertNotEqual(im.getpixel((540, 300)), post_card.NAVY)          # 上面是照片


class Build(unittest.TestCase):
    def row(self, **kw):
        return {"id": 5, "category": "family", "topic": "看電視", "message": MSG, "image": "0123456789abcdef.webp",
                "image_meta": json.dumps({"src": "stock", "credit": {"id": "px-1"}}), "card": None, "card_meta": None,
                "status": "new", **kw}

    def run_build(self, row, **kw):
        with tempfile.TemporaryDirectory() as d:
            Image.new("RGB", (1600, 1067), (90, 100, 110)).save(Path(d) / "0123456789abcdef.webp")
            cur = FakeCur([row])
            with mock.patch.dict(post_card.config.WEB, media_dir=d), \
                    mock.patch.object(fd, "site_url", return_value=SITE), \
                    mock.patch.object(post_card, "make_title", return_value={"title": "AI 擬的", "sub": "第二行", "src": "ai"}) as mt, \
                    mock.patch.object(web_admin, "remove_if_unused") as rm:
                name = post_card.build(cur, 5, **kw)
                saved = (Path(d) / name).is_file() if name else None
        upd = [a for s, a in cur.sql if s.startswith("UPDATE")]
        return name, saved, json.loads(upd[-1][1]) if upd and upd[-1][1] else None, mt, rm

    def test_first_time_asks_ai(self):
        name, saved, meta, mt, rm = self.run_build(self.row())
        self.assertTrue(web_admin.IMAGE_NAME_RE.match(name) and saved)
        self.assertEqual((meta["title"], meta["sub"], meta["src"], meta["image"]), ("AI 擬的", "第二行", "ai",
                                                                                  "0123456789abcdef.webp"))
        mt.assert_called_once()
        rm.assert_not_called()

    def test_reuse_and_staff_title(self):
        old = json.dumps({"title": "上次的", "sub": "", "src": "ai"})
        _, _, meta, mt, rm = self.run_build(self.row(card="fedcba9876543210.webp", card_meta=old))
        self.assertEqual(meta["title"], "上次的")                                # 換照片重做：標題沿用
        mt.assert_not_called()
        rm.assert_called_once()                                                  # 舊圖卡沒人用就刪
        _, _, meta, mt, _ = self.run_build(self.row(card_meta=old), title="店員改的", sub="小字")
        self.assertEqual((meta["title"], meta["sub"], meta["src"]), ("店員改的", "小字", "staff"))
        mt.assert_not_called()
        _, _, meta, mt, _ = self.run_build(self.row(card_meta=old), retitle=True)
        mt.assert_called_once()

    def test_no_image_or_sent(self):
        name, _, _, mt, _ = self.run_build(self.row(image=None, card="fedcba9876543210.webp"))
        self.assertIsNone(name)
        mt.assert_not_called()
        name, _, meta, mt, _ = self.run_build(self.row(status="published", card="fedcba9876543210.webp"))
        self.assertEqual(name, "fedcba9876543210.webp")                         # 發出去的不動
        self.assertIsNone(meta)

    def test_photo_change_drops_card(self):
        cur = FakeCur([{"image": "0123456789abcdef.webp", "card": "fedcba9876543210.webp"}])
        with mock.patch.object(web_admin, "remove_if_unused") as rm:
            post_art.set_draft_image(cur, Path("/tmp"), 5, "aaaaaaaaaaaaaaaa.webp", {"src": "stock"})
        self.assertIn("card=NULL", cur.sql[-1][0])
        self.assertEqual([c[0][2] for c in rm.call_args_list], ["0123456789abcdef.webp", "fedcba9876543210.webp"])


class Publish(unittest.TestCase):
    def row(self, i, image=None, card=None):
        return {"id": i, "day": date(2026, 11, 9), "category": "family", "message": MSG, "link": None, "image": image,
                "image_meta": None, "card": card, "web_sync": 0, "web_post_id": None, "seo": None, "status": "new"}

    def test_card_is_posted(self):
        cur = FakeCur([self.row(1, "0123456789abcdef.webp", "fedcba9876543210.webp"),
                       self.row(2, "0123456789abcdef.webp"), self.row(3)])
        with mock.patch.object(fd, "_conn", return_value=Ctx(cur)), \
                mock.patch.object(fd, "photo_jpeg", side_effect=lambda n: f"JPG:{n}".encode() if n else None), \
                mock.patch.object(post_art, "make_card", side_effect=[None]) as mk, \
                mock.patch.object(fd.fb_page, "publish_photo", return_value="p") as photo, \
                mock.patch.object(fd.fb_page, "publish_post", return_value="q"):
            r = fd.publish([1, 2, 3], False, "測試")
        self.assertTrue(r["ok"])
        self.assertEqual([c[0][1] for c in photo.call_args_list],
                         [b"JPG:fedcba9876543210.webp",                      # 有圖卡：發圖卡
                          b"JPG:0123456789abcdef.webp"])                     # 圖卡做不出來：照樣發照片
        mk.assert_called_once_with(cur, 2)                                   # 沒有圖卡的先補做；沒照片的不做


if __name__ == "__main__":
    unittest.main()
