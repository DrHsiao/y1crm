"""文章短影音的純函式（不打本機模型、不打語音服務、不跑 ffmpeg）。

執行：./venv/bin/python -m unittest tests.test_article_video
"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from api import article_video as av

ITEMS = [{"id": "tv", "file": "aaaaaaaaaaaaaaaa.webp", "tags": ["電視", "音量"]},
         {"id": "talk", "file": "bbbbbbbbbbbbbbbb.webp", "tags": ["聊天", "對話", "家人", "通用"]},
         {"id": "phone", "file": "cccccccccccccccc.webp", "tags": ["手機"]},
         {"id": "fit", "file": "dddddddddddddddd.webp", "tags": ["聽不清楚", "調整"]},
         {"id": "family", "file": "eeeeeeeeeeeeeeee.webp", "tags": ["家人", "孫子"]},
         {"id": "credit", "file": "ffffffffffffffff.webp", "tags": ["預約"], "credit_required": True}]


class Script(unittest.TestCase):
    def test_clean(self):
        ok = {"lines": [{"say": f"第 {i} 句 旁白", "caption": "重點" * 10, "topic": "看電視"} for i in range(6)]}
        out = av.clean_script(ok)
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0]["say"], "第0句旁白")                       # 空白拿掉（字幕、語音都不需要）
        self.assertLessEqual(len(out[0]["caption"]), 14)
        self.assertEqual(av.clean_script({"lines": ok["lines"][:3]}), [])   # 太少句
        bad = {"lines": ok["lines"][:5] + [{"say": "現在只要 9999 元", "caption": "", "topic": ""}]}
        self.assertEqual(av.clean_script(bad), [])                          # 提到價格：不能用
        self.assertEqual(av.clean_script("不是 JSON 物件"), [])

    def test_list_script(self):
        body = "前言\n\n1. 電視越開越大聲。\n2. 常說「什麼？」\n\n## 可以怎麼做？\n\n- 先聊聊。"
        out = av.list_script("聽力變差的警訊：家人可以留意的事", body)
        self.assertEqual(out[0]["say"], "聽力變差的警訊，家人可以留意的事")
        self.assertEqual([x["say"] for x in out[1:4]], ["電視越開越大聲。", "常說「什麼？」", "先聊聊。"])
        self.assertIn("線上聽力測驗", out[-1]["say"])


class Photos(unittest.TestCase):
    def test_topic_first_and_no_repeat(self):
        lines = [{"say": "家人先發現", "caption": "", "topic": "家人 聊天"},
                 {"say": "電視越開越大聲", "caption": "", "topic": "看電視"},
                 {"say": "講電話常聽不清楚", "caption": "", "topic": "講電話 手機"},
                 {"say": "先做線上測驗", "caption": "", "topic": "預約"}]
        got = [e["id"] for e in av.photos(ITEMS, lines)]
        self.assertEqual(got[1:3], ["tv", "phone"])          # 旁白裡的「聽不清楚」不會蓋過主題「手機」
        self.assertNotIn(got[0], got[1:3])                   # 片頭不和中間重複
        self.assertNotIn("credit", got)                      # 影片裡不放要標作者的照片


class Text(unittest.TestCase):
    def test_wrap(self):
        f = av.font(54)
        rows = av.wrap("如果是突然聽不到、單耳變差，或有耳鳴、暈眩，請盡快到耳鼻喉科就醫。", f, 1500)
        self.assertEqual("".join(rows), "如果是突然聽不到、單耳變差，或有耳鳴、暈眩，請盡快到耳鼻喉科就醫。")
        self.assertTrue(all(f.getlength(r) <= 1500 + 60 for r in rows))
        self.assertFalse(any(r[0] in "，。、！？" for r in rows))   # 標點不放行首



class Tall(unittest.TestCase):
    """直式 9:16：字幕不能跑到 Reels 下方說明文字、右側按鈕蓋住的地方。"""
    def layer(self, say, label="警訊 1", header="聽力變差的警訊"):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x.png"
            av.overlay_tall(say, label, header, out)
            return Image.open(out).copy()

    def test_safe_zones(self):
        for say in ("短句。", "如果是突然聽不到、單耳變差，或有耳鳴、暈眩，請盡快到耳鼻喉科就醫，不要拖。"):
            im = self.layer(say)
            self.assertEqual(im.size, (av.TW, av.TH))
            box = im.getchannel("A").crop((0, av.T_PHOTO_Y + av.T_PHOTO_H - 60, av.TW, av.TH)).getbbox()
            self.assertIsNotNone(box)
            self.assertLessEqual(box[3] - 1 + av.T_PHOTO_Y + av.T_PHOTO_H - 60, av.T_SUB_BOTTOM)   # 最後一列（getbbox 下緣不含）在字幕區內
            self.assertLessEqual(box[2], av.TW - 100)                                         # 右邊留給按鈕

    def test_header_optional(self):
        top = (0, 150, av.TW, av.T_PHOTO_Y)
        self.assertIsNotNone(self.layer("一句。").getchannel("A").crop(top).getbbox())
        self.assertIsNone(self.layer("一句。", "", None).getchannel("A").crop(top).getbbox())

    def test_card_tall(self):
        with tempfile.TemporaryDirectory() as d:
            bg, out = Path(d) / "bg.jpg", Path(d) / "c.jpg"
            Image.new("RGB", (1600, 1067), (120, 140, 160)).save(bg)
            av.card(bg, ["一個很長很長很長很長的標題會自動換行"], [av.BRAND], out, (av.TW, av.TH))
            self.assertEqual(Image.open(out).size, (av.TW * 2, av.TH * 2))

if __name__ == "__main__":
    unittest.main()
