"""示意插圖的純函式（不連資料庫、不跑模型）。

執行：./venv/bin/python -m unittest tests.test_review_art
"""
import unittest
from unittest import mock

from api import review_art as ra


class Prompt(unittest.TestCase):
    def test_clean(self):
        self.assertEqual(ra.clean_prompt("An elderly man in his 70s, 微笑 smiling with his daughter <b>at home</b>!"),
                         "An elderly man in his 70s, smiling with his daughter b at home b")
        self.assertEqual(ra.clean_prompt("太短 short"), "")            # 太短的不用，改用通用場景
        self.assertEqual(len(ra.clean_prompt("word " * 300).split()), ra.MAX_WORDS)

    def test_scene_fallback(self):
        with mock.patch.object(ra.urllib.request, "urlopen", side_effect=OSError("down")):
            d = ra.scene("服務很好")
            self.assertEqual(d["prompt"], ra.FALLBACK)
            self.assertEqual(ra.scene("服務很好", variant=3), dict(zip(("people", "prompt"), ra.DEFAULT_SCENES[3])))

    def test_no_real_faces(self):
        # 畫面描述只從評論文字來：提示詞要求虛構人物、不畫文字和助聽器特寫
        self.assertIn("虛構", ra.SCENE_PROMPT)
        self.assertIn("不要特寫耳朵或助聽器", ra.SCENE_PROMPT)


if __name__ == "__main__":
    unittest.main()
