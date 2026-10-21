"""
聽檢輔助規則（api/hearing.py）的單元測試。純函式，不需要 DB 或 GPU。

    ./venv/bin/python -m unittest tests.test_hearing -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import hearing as H  # noqa: E402


def mk(R=None, L=None, retest=None, tymp=None, speech=None):
    """R/L：{kind: {freq: value}}，freq 可寫 int。"""
    def side(s):
        return {k: {str(f): v for f, v in (s or {}).get(k, {}).items()} for k in H.KINDS}
    return H.clean_data({"thr": {"R": side(R), "L": side(L)}, "retest1k": retest or {},
                         "tymp": tymp or {}, "speech": speech or {}})


def items(res, kind, ear=None):
    return [c for c in res["checklist"] if c["kind"] == kind and (ear is None or c["ear"] == ear)]


class CleanData(unittest.TestCase):
    def test_drops_out_of_range_and_garbage(self):
        d = mk(R={"ac": {1000: 130, 2000: "abc", 4000: "nr", 500: "-10"},
                  "bc": {1000: 90, 2000: 85, 8000: 30}})
        self.assertEqual(d["thr"]["R"]["ac"], {"4000": "NR", "500": -10})
        self.assertEqual(d["thr"]["R"]["bc"], {"2000": 85})     # 骨導上限 85、8k 不收

    def test_options_fall_back_to_defaults(self):
        self.assertEqual(H.options({"mode": "x", "transducer": "IP30", "first_ear": "L"}),
                         {"mode": "adult", "transducer": "IP30", "first_ear": "L", "talk": "ok"})


class Checklist(unittest.TestCase):
    def test_empty_suggests_start_and_first_freq(self):
        res = H.assess(mk(), talk="poor")
        self.assertIn("60 dB HL", res["start"])
        self.assertEqual((res["next"]["ear"], res["next"]["kind"], res["next"]["freq"]), ("R", "ac", 1000))

    def test_adult_order_with_retest_only_first_ear(self):
        res = H.assess(mk(), first_ear="L")
        seq = [(c["ear"], c["kind"], c["freq"]) for c in res["checklist"] if c["kind"] in ("ac", "retest")]
        self.assertEqual(seq[:7], [("L", "ac", 1000), ("L", "ac", 2000), ("L", "ac", 4000),
                                   ("L", "ac", 8000), ("L", "retest", 1000), ("L", "ac", 500),
                                   ("L", "ac", 250)])
        self.assertEqual(len(items(res, "retest")), 1)

    def test_child_order(self):
        res = H.assess(mk(), mode="child")
        self.assertEqual([c["freq"] for c in items(res, "ac", "R")], [500, 4000, 2000, 1000])

    def test_next_moves_forward(self):
        res = H.assess(mk(R={"ac": {1000: 30, 2000: 35}}))
        self.assertEqual(res["next"]["freq"], 4000)
        self.assertIsNone(res["start"])

    def test_intermediate_needed_at_20db(self):
        res = H.assess(mk(R={"ac": {500: 20, 1000: 40, 2000: 55, 4000: 70}}))
        mids = {c["freq"]: c for c in items(res, "ac", "R") if "補測" in c["text"]}
        self.assertEqual(set(mids), {750})              # 1k–2k 差 15、2k–4k 差 15 不用補
        self.assertFalse(mids[750]["done"])
        res = H.assess(mk(R={"ac": {500: 20, 750: 30, 1000: 40}}))
        self.assertTrue([c for c in items(res, "ac", "R") if c["freq"] == 750][0]["done"])


class Masking(unittest.TestCase):
    def test_bc_masking_at_abg_15(self):
        res = H.assess(mk(R={"ac": {1000: 50, 2000: 40}, "bc": {1000: 35, 2000: 30}}))
        need = items(res, "bcm", "R")
        self.assertEqual([c["freq"] for c in need], [1000])   # 2k 差 10 不用
        self.assertIn("50 − 未遮蔽骨導 35 = 15", need[0]["reason"])
        res = H.assess(mk(R={"ac": {1000: 50}, "bc": {1000: 35}, "bcm": {1000: 45}}))
        self.assertTrue(items(res, "bcm", "R")[0]["done"])

    def test_ac_masking_uses_other_ear_bc_then_ac(self):
        d = mk(R={"ac": {1000: 70, 2000: 60}}, L={"ac": {1000: 20, 2000: 25}, "bc": {1000: 25}})
        res = H.assess(d)
        need = {c["freq"]: c["reason"] for c in items(res, "acm", "R")}
        self.assertIn("左耳骨導 25 = 45", need[1000])
        self.assertNotIn(2000, need)                    # 60−25=35 < 40
        self.assertEqual(items(res, "acm", "L"), [])

    def test_ip30_skips_ac_masking_with_warning(self):
        d = mk(R={"ac": {1000: 80}}, L={"ac": {1000: 10}})
        res = H.assess(d, transducer="IP30")
        self.assertEqual(items(res, "acm"), [])
        self.assertTrue(any("IP30" in w for w in res["warnings"]))

    def test_nr_skipped_with_warning(self):
        res = H.assess(mk(R={"ac": {1000: "NR"}, "bc": {1000: 20}}))
        self.assertEqual(items(res, "bcm"), [])
        self.assertTrue(any("NR" in w for w in res["warnings"]))


class Interpret(unittest.TestCase):
    def ear(self, ac, bc=None, **kw):
        R = {"ac": dict(zip((500, 1000, 2000), ac))}
        if bc:
            R["bc"] = dict(zip((500, 1000, 2000), bc))
        R.update(kw)
        return H.assess(mk(R=R))["interp"]["R"]

    def test_degree_boundaries(self):
        for pta, want in ((25, "正常"), (26, "輕度"), (40, "輕度"), (41, "中度"), (55, "中度"),
                          (56, "中重度"), (70, "中重度"), (71, "重度"), (90, "重度"), (91, "極重度")):
            self.assertEqual(H.degree(pta), want, pta)

    def test_types(self):
        self.assertEqual(self.ear((20, 20, 20), (15, 15, 15))["type"], "聽力正常")
        self.assertEqual(self.ear((45, 45, 45), (10, 10, 10))["type"], "傳導型")
        self.assertEqual(self.ear((50, 55, 60), (45, 50, 55))["type"], "感音神經型")
        self.assertEqual(self.ear((70, 70, 70), (40, 40, 40))["type"], "混合型")
        it = self.ear((25, 25, 25), (5, 5, 5))       # AC 正常但 ABG 20：講義表外
        self.assertEqual(it["type"], "表外")

    def test_masked_values_override(self):
        it = self.ear((60, 60, 60), (20, 20, 20), bcm={500: 55, 1000: 55, 2000: 55})
        self.assertEqual((it["pta_bc"], it["type"]), (55, "感音神經型"))

    def test_open_masking_marks_provisional(self):
        it = self.ear((60, 60, 60), (20, 20, 20))
        self.assertTrue(any("暫定" in n for n in it["notes"]))

    def test_missing_and_nr(self):
        self.assertIsNone(self.ear((30, 30))["pta_ac"])
        self.assertIn("無反應", self.ear((30, 30, "NR"))["notes"][0])
        it = self.ear((40, 40, 40))
        self.assertEqual((it["degree"], it["type"]), ("輕度", None))

    def test_summary(self):
        d = mk(R={"ac": {500: 50, 1000: 55, 2000: 60}, "bc": {500: 45, 1000: 50, 2000: 55}},
               L={"ac": {500: 10, 1000: 10, 2000: 15}, "bc": {500: 5, 1000: 5, 2000: 10}})
        self.assertEqual(H.assess(d)["summary"], "右耳 中度 感音神經型（55）／左耳 正常（11.7）")


class RetestTympSpeech(unittest.TestCase):
    def test_retest(self):
        res = H.assess(mk(R={"ac": {1000: 40}}, retest={"R": 45}))
        self.assertTrue(res["retest"]["ok"])
        res = H.assess(mk(R={"ac": {1000: 40}}, retest={"R": 55}))
        self.assertFalse(res["retest"]["ok"])

    def test_tymp(self):
        cases = [({"pressure": 20, "compliance": 0.6}, "A"), ({"pressure": -50, "compliance": 0.2}, "As"),
                 ({"pressure": -40, "compliance": 2.2}, "Ad"), ({"np": True}, "B"),
                 ({"pressure": -140, "compliance": 0.5}, "C"), ({"pressure": 150, "compliance": 0.6}, None)]
        for t, want in cases:
            d = mk(tymp={"R": t})
            self.assertEqual(H.assess(d)["tymp"]["R"]["type"], want, t)

    def test_srt_pta_diff(self):
        d = mk(R={"ac": {500: 40, 1000: 45, 2000: 50}}, speech={"R": {"srt": 40}})
        self.assertEqual(H.assess(d)["speech"]["R"]["srt_pta_diff"], -5)


class FromVlm(unittest.TestCase):
    def test_rows_mapping_and_flags(self):
        raw = {"rows": [
            {"freq": 1000, "R_AC": 45, "R_ACM": None, "R_BC": "null", "L_BCM": 32},   # 32 非 5 倍數
            {"freq": 1000, "R_AC": 50},                                               # 重複不一致
            {"freq": 8000, "L_AC": "NR", "L_BC": 30},                                  # 骨導 8k 不收
            {"freq": 1200, "R_AC": 20},                                                # 非標準頻率
            {"freq": 2000, "R_AC": 130},                                               # 超出範圍
        ], "uncertain": ["L_AC_8000"],
           "current": {"ear": "R", "type": "AC", "freq": 2000, "level": 45, "mask_level": None}}
        out = H.from_vlm(raw)
        self.assertEqual(out["thr"]["R"]["ac"], {"1000": 45})
        self.assertEqual(out["thr"]["L"]["bcm"], {"1000": 32})
        self.assertEqual(out["thr"]["L"]["ac"], {"8000": "NR"})
        self.assertEqual(out["thr"]["R"]["bc"], {})
        self.assertEqual(set(out["uncertain"]), {"R-ac-1000", "L-bcm-1000", "L-ac-8000"})
        self.assertEqual((out["count"], out["dropped"]), (3, 3))
        self.assertEqual(out["current"], "右耳 氣導 2 kHz 45 dB HL")

    def test_garbage_is_safe(self):
        out = H.from_vlm({"rows": "nope", "current": [1]})
        self.assertEqual((out["count"], out["current"]), (0, None))
        self.assertEqual(H.from_vlm(None)["count"], 0)


if __name__ == "__main__":
    unittest.main()
