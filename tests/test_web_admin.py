"""官網管理：欄位檢查、網址代稱、圖片處理（純函式，不連資料庫）。

執行：./venv/bin/python -m unittest tests.test_web_admin
"""
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

from api import web_admin as wa

NOW = datetime(2026, 10, 21, 15, 30, 45)


def img_bytes(fmt="PNG", size=(40, 30), mode="RGB", exif_rotate=False):
    im = Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128))
    buf = io.BytesIO()
    if exif_rotate:
        ex = Image.Exif()
        ex[0x0112] = 6          # 手機直拍：要轉 90 度
        im.save(buf, fmt, exif=ex)
    else:
        im.save(buf, fmt)
    return buf.getvalue()


class CleanPost(unittest.TestCase):
    def test_article(self):
        c = wa.clean_post("article", {"title": " 標題 ", "category": "聽力保健", "status": "published"}, NOW)
        self.assertEqual(c["title"], "標題")
        self.assertEqual(c["slug"], "a261021153045")          # 沒填代稱＝自動
        self.assertEqual(c["publish_at"], NOW)                 # 發佈沒填日期＝現在
        self.assertEqual(c["category"], "聽力保健")

    def test_article_needs_category(self):
        with self.assertRaises(wa.Invalid):
            wa.clean_post("article", {"title": "x", "category": "亂填"}, NOW)

    def test_title_required_and_length(self):
        with self.assertRaises(wa.Invalid):
            wa.clean_post("about", {"title": "  "}, NOW)
        with self.assertRaises(wa.Invalid):
            wa.clean_post("about", {"title": "長" * 121}, NOW)

    def test_slug_rules(self):
        self.assertEqual(wa.clean_post("about", {"title": "x", "slug": "Brand-Story"}, NOW)["slug"], "brand-story")
        for bad in ("中文", "a b", "../x", "-x"):
            with self.assertRaises(wa.Invalid):
                wa.clean_post("about", {"title": "x", "slug": bad}, NOW)

    def test_publish_date(self):
        c = wa.clean_post("article", {"title": "x", "category": "聽力保健", "publish_date": "2026-11-01"}, NOW)
        self.assertEqual(c["publish_at"], datetime(2026, 11, 1, 9, 0))
        with self.assertRaises(wa.Invalid):
            wa.clean_post("article", {"title": "x", "category": "聽力保健", "publish_date": "2026-13-01"}, NOW)
        self.assertIsNone(wa.clean_post("about", {"title": "x", "status": "published"}, NOW)["publish_at"])

    def test_share_needs_consent_to_publish(self):
        base = {"title": "x", "person": "王先生"}
        self.assertEqual(wa.clean_post("share", {**base, "status": "draft"}, NOW)["status"], "draft")
        with self.assertRaises(wa.Invalid):
            wa.clean_post("share", {**base, "status": "published"}, NOW)
        c = wa.clean_post("share", {**base, "status": "published", "consent": True}, NOW)
        self.assertTrue(c["extra"]["consent"])
        with self.assertRaises(wa.Invalid):                    # 一定要有分享人稱呼
            wa.clean_post("share", {"title": "x"}, NOW)

    def test_bad_kind_and_status(self):
        with self.assertRaises(wa.Invalid):
            wa.clean_post("product", {"title": "x"}, NOW)      # 產品系列不在官網管理改
        with self.assertRaises(wa.Invalid):
            wa.clean_post("about", {"title": "x", "status": "hidden"}, NOW)


class Images(unittest.TestCase):
    def test_png_to_webp(self):
        out, name = wa.process_image(img_bytes("PNG", (3000, 1500)))
        self.assertRegex(name, r"^[0-9a-f]{16}\.webp$")
        im = Image.open(io.BytesIO(out))
        self.assertEqual(im.format, "WEBP")
        self.assertEqual(max(im.size), 1600)                   # 縮到 1600px

    def test_exif_rotated_and_stripped(self):
        out, _ = wa.process_image(img_bytes("JPEG", (40, 30), exif_rotate=True))
        im = Image.open(io.BytesIO(out))
        self.assertEqual(im.size, (30, 40))                    # 直拍照片轉正
        self.assertFalse(im.getexif())                         # EXIF（含 GPS）不留

    def test_alpha_kept(self):
        out, _ = wa.process_image(img_bytes("PNG", (20, 20), mode="RGBA"))
        self.assertEqual(Image.open(io.BytesIO(out)).mode, "RGBA")

    def test_rejects_non_image(self):
        for data in (b"", b"not an image", b"%PDF-1.4 ..."):
            with self.assertRaises(wa.Invalid):
                wa.process_image(data)

    def test_save_dedup_and_paths(self):
        with tempfile.TemporaryDirectory() as d:
            a = wa.save_image(Path(d), img_bytes())
            b = wa.save_image(Path(d), img_bytes())
            self.assertEqual(a, b)
            self.assertEqual(len(list(Path(d).iterdir())), 1)
            self.assertEqual(wa.image_path(Path(d), a), Path(d) / a)
        for bad in ("../etc/passwd", "x.webp", "0123456789abcdef.png", ""):
            self.assertIsNone(wa.image_path(Path("/tmp"), bad))


if __name__ == "__main__":
    unittest.main()
