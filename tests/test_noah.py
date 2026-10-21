"""
Noah 匯出檔解析（api/noah.py）的單元測試。用合成檔，不放真實客戶資料。

    ./venv/bin/python -m unittest tests.test_noah -v
"""
import base64
import gzip
import sys
import unittest
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from api import noah as N  # noqa: E402


def b64(s):
    return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()


def himsa(curves):
    """curves：[(output, [(freq, level, status, masking_level)], masking_type, aided)]"""
    out = []
    for output, pts, mtype, aided in curves:
        tp = "".join(
            f"<TonePoints><StimulusFrequency>{f}</StimulusFrequency><StimulusLevel>{lv}</StimulusLevel>"
            f"<TonePointStatus>{st}</TonePointStatus>"
            + (f"<MaskingFrequency>{f}</MaskingFrequency><MaskingLevel>{ml}</MaskingLevel>" if ml is not None else "")
            + "</TonePoints>" for f, lv, st, ml in pts)
        out.append(f"<ToneThresholdAudiogram><AudMeasurementConditions><StimulusSignalType>PureTone</StimulusSignalType>"
                   f"<MaskingSignalType>{mtype}</MaskingSignalType><StimulusSignalOutput>{output}</StimulusSignalOutput>"
                   f"<HearingInstrument_1_Condition>{aided}</HearingInstrument_1_Condition>"
                   f"</AudMeasurementConditions>{tp}</ToneThresholdAudiogram>")
    return ('﻿<?xml version="1.0" encoding="utf-8"?><HIMSAAudiometricStandard Version="500" '
            'xmlns="http://www.himsa.com/Measurement/Audiogram">' + "".join(out) + "</HIMSAAudiometricStandard>")


MAICO_PRIV = ("<TestData><DeviceInformation><DisplayName>MA 42</DisplayName></DeviceInformation>"
              "<Measurements><ToneAudiometryList><Tone><Transducer>B71</Transducer></Tone>"
              "<Tone><Transducer>DD45</Transducer></Tone></ToneAudiometryList></Measurements></TestData>")


def feature(fid, value, init, ids):
    vals = "".join(f'<FeatureValue FeatureValueId="{i}"/>' for i in ids)
    return (f'<Feature FeatureID="{fid}" FeatureValue="{value}" InitialFitFeatureValue="{init}">'
            f"<FeatureValues>{vals}</FeatureValues></Feature>")


def resound(ear, model="EC498-DW", dfs="Moderate", g4k=30):
    feats = (feature("DFS", dfs, "AFX-DFS and AFGC mild", ["Off", "Mild", "Moderate", "Strong", "Music"])
             + feature("FreqLow.Level", "LevelOff", "Off", ["LevelOff", "Level1", "Level2", "Level4", "Level6"])
             + feature("Expansion", "Mild", "Medium", ["Mild", "Medium", "Moderate"])
             + feature("AGCI", "Syllabic", "Syllabic", ["Syllabic"]))           # 不在對照表 → 不顯示
    gains = "".join(f'<SoundInputLevel InputLevel="DB{lv}"> (250 {50 - i}) (4000 {g4k - i}) (125 9)</SoundInputLevel>'
                    for i, lv in enumerate((50, 65, 80)))
    prog = (lambda name, loc: f'<Program DisplayName="{name}" Location="{loc}" EnvironmentId="{loc}">'
            f"<Features>{feats}</Features><Gains>{gains}</Gains>"
            f'<MPO InputLevel="DBMPO"> (250 120) (4000 125)</MPO></Program>')
    xml = (f'<!-- Version: 4.0 --><ActiveProduct EarSide="{ear}" Name="{model}"><Programs>'
           f'{prog("Program", "1")}{prog("噪音中聆聽", "2")}</Programs></ActiveProduct>')
    return b64(gzip.compress(("﻿" + xml).encode()))


def action(code, module, when, desc="", pub=None, priv=None):
    return (f'<Action DataTypeCode="{code}" ModuleId="{module}" CreateDate="{when}" Description="{desc}">'
            + (f"<PublicData>{pub}</PublicData>" if pub is not None else "")
            + (f"<PrivateData>{priv}</PrivateData>" if priv is not None else "") + "</Action>")


def nhax(actions, first="王小明", last="王小明", extra=""):
    patient = ('﻿<?xml version="1.0" encoding="utf-8"?>'
               f'<Patient PatientNo="0000001" FirstName="{first}" LastName="{last}" BirthDate="1950-01-02T00:00:00">'
               f'{extra}<Sessions><Session CreateDate="2026-10-01">{"".join(actions)}</Session></Sessions></Patient>')
    index = b'<?xml version="1.0"?><Patients><Patient PatientNo="0000001"/></Patients>'
    head = b"\x28\x00\x00\x00\x01\x02\x00\x00\x88\x99\x00\xdd\xcc\xbb\xaa\x00" + bytes(24)
    return head + zlib.compress(patient.encode()) + bytes(12) + zlib.compress(index)


MA42_CURVES = [
    ("AirConductorRight", [(1000, 50, "Normal", None), (500, 70, "Normal", 60),
                           (125, 40, "Normal", None), (8000, 120, "NoResponse", None)], "NarrowBandNoise", "UnAided"),
    ("BoneConductorRight", [(1000, 30, "Normal", 0.0), (8000, 40, "Normal", None),
                            (2000, 95, "Normal", None)], "NoSignalApplied", "UnAided"),
    ("InsertPhoneLeft", [(1000, 20.0, "Normal", None)], "NoSignalApplied", "UnAided"),
    ("AirConductorLeft", [(1000, 10, "Normal", None)], "NoSignalApplied", "Aided"),
]


class Audiogram(unittest.TestCase):
    def setUp(self):
        self.r = N.parse_nhax(nhax([
            action("1", "2031716", "2026-09-01T12:33:33", "纯音测听", b64(himsa(MA42_CURVES)), b64(MAICO_PRIV)),
            action("1", "65596", "2026-10-15T20:14:46", "新聽力圖", b64(himsa([
                ("AirConductorRight", [(1000, 55, "Normal", 0.0)], "NoSignalApplied", "UnAided")]))),
        ]))

    def test_points_and_masking(self):
        a = self.r["audiograms"][0]
        self.assertEqual(a["thr"]["R"]["ac"], {"1000": 50, "8000": "NR"})
        self.assertEqual(a["thr"]["R"]["acm"], {"500": 70})          # MaskingLevel>0 → 遮蔽值
        self.assertEqual(a["thr"]["R"]["bc"], {"1000": 30})          # MaskingLevel 0 → 未遮蔽
        self.assertEqual(a["thr"]["L"]["ac"], {"1000": 20})          # 插入式耳機也算氣導
        self.assertEqual(a["count"], 5)
        self.assertTrue(a["masking_info"])
        skipped = " ".join(a["skipped"])
        for s in ("125 Hz", "骨導 8 kHz", "數值 95 超出範圍", "Aided"):
            self.assertIn(s, skipped)

    def test_device_and_order(self):
        first, second = self.r["audiograms"]
        self.assertEqual((first["source"], first["measured"], first["transducer"]), ("MA 42", True, "DD45"))
        self.assertEqual((second["source"], second["measured"], second["masking_info"]), ("新聽力圖", False, False))
        self.assertEqual((first["date"], second["datetime"]), ("2026-09-01", "2026-10-15 20:14"))

    def test_diff_uses_final_values(self):
        diff = self.r["audiograms"][1]["diff_prev"]
        self.assertIn("右耳 氣導 1 kHz：50 → 55", diff)
        self.assertIn("右耳 氣導 500 Hz：70 → —", diff)               # 遮蔽值當最終閾值比
        self.assertEqual(self.r["audiograms"][0]["diff_prev"], [])

    def test_patient(self):
        self.assertEqual(self.r["patient"], {"no": "0000001", "name": "王小明", "birth_date": "1950-01-02"})
        r = N.parse_nhax(nhax([], first="小明", last="王"))
        self.assertEqual(r["patient"]["name"], "王小明")
        self.assertEqual(N.parse_nhax(nhax([], first="Ann", last="Lee"))["patient"]["name"], "Ann Lee")


class Resound(unittest.TestCase):
    def setUp(self):
        self.r = N.parse_nhax(nhax([
            action("132", "5177347", "2026-10-07T20:29:00", priv=resound("RIGHT_EAR", "EI498-DWHC")),
            action("132", "5177347", "2026-10-07T20:40:00", priv=resound("RIGHT_EAR", "EI498-DWHC")),   # 沒改
            action("132", "5177347", "2026-10-08T10:00:00", priv=resound("RIGHT_EAR", dfs="Strong", g4k=25)),
            action("131", "5177347", "2026-10-08T10:00:00", priv=resound("LEFT_EAR")),
            action("131", "5177347", "2026-10-08T10:05:00", priv=b64(b"\x1f\x8bgarbage")),
            action("129", "327880", "2026-10-09T09:00:00", "Phonak Target Naida L30-UP"),
            action("129", "5177347", "2026-10-09T09:00:00", "選擇 ReSound Smart Fit EC498-DW 用於 左耳 耳"),
        ]))
        self.fit = self.r["fitting"]

    def test_latest_settings_with_screen_labels(self):
        self.assertEqual(self.fit["devices"]["R"], {"model": "EC498-DW", "datetime": "2026-10-08 10:00"})
        p1, p2 = self.fit["programs"]
        self.assertEqual((p1["name"], p2["name"]), ("全面", "噪音中聆聽"))
        feats = {f["id"]: f for f in p1["features"]}
        self.assertEqual(set(feats), {"DFS", "FreqLow.Level", "Expansion"})
        self.assertEqual(feats["DFS"]["R"], {"value": "強度", "init": "輕度", "changed": True})
        self.assertEqual(feats["DFS"]["L"]["value"], "中度")
        self.assertEqual(feats["FreqLow.Level"]["R"], {"value": "關", "init": "關", "changed": False})
        self.assertEqual(feats["Expansion"]["L"], {"value": "關", "init": "輕度", "changed": True})

    def test_gains(self):
        g = self.fit["programs"][0]["gains"]["R"]
        self.assertEqual(len(g["65"]), len(N.H.FREQS))
        self.assertEqual((g["50"][0], g["65"][0], g["65"][7], g["mpo"][7]), (50, 49, 24, 125))
        self.assertIsNone(g["65"][1])                                 # 沒給的頻率

    def test_history_skips_unchanged_saves(self):
        hist = [(h["datetime"], h["ear"]) for h in self.fit["history"]]
        self.assertEqual(hist, [("2026-10-07 20:29", "R"), ("2026-10-08 10:00", "R"), ("2026-10-08 10:00", "L")])
        changes = self.fit["history"][1]["changes"]
        self.assertIn("機型 EI498-DWHC → EC498-DW", changes)
        self.assertIn("全面 數位回饋音抑制 DFS 中度 → 強度", changes)

    def test_other_brands_and_bad_records(self):
        self.assertEqual([o["text"] for o in self.r["other_fittings"]], ["Phonak Target Naida L30-UP"])
        self.assertEqual(self.r["warnings"], ["有 1 筆紀錄無法解讀，已略過。"])


class Rejects(unittest.TestCase):
    def test_not_nhax(self):
        for raw in (b"", b"PK\x03\x04 not noah", zlib.compress(b"<?xml version='1.0'?><Other/>")):
            with self.assertRaises(N.NoahError):
                N.parse_nhax(raw)

    def test_doctype_rejected(self):
        bomb = '<!DOCTYPE x [<!ENTITY a "aaaa">]>'
        with self.assertRaises(N.NoahError):
            N._xml(f'<?xml version="1.0"?>{bomb}<Patient/>'.encode())
        aud = himsa(MA42_CURVES).replace("<HIMSAAudiometricStandard", bomb + "<HIMSAAudiometricStandard")
        r = N.parse_nhax(nhax([action("1", "65596", "2026-10-15T20:14:46", "x", b64(aud))]))
        self.assertEqual((r["audiograms"], r["warnings"]), ([], ["有 1 筆紀錄無法解讀，已略過。"]))

    def test_size_cap(self):
        old = N.MAX_SECTION
        N.MAX_SECTION = 1000
        try:
            with self.assertRaises(N.NoahError):
                N.parse_nhax(nhax([], extra="<Pad>" + "x" * 5000 + "</Pad>"))
        finally:
            N.MAX_SECTION = old

    def test_match_value(self):
        ids = ["Off", "Mild", "Moderate"]
        self.assertEqual(N._match_value("AFX-DFS and AFGC mild", ids), "Mild")
        self.assertEqual(N._match_value("Off", ["LevelOff", "Level1"]), "LevelOff")
        self.assertEqual(N._match_value("Weird", ids), "Weird")


if __name__ == "__main__":
    unittest.main()
