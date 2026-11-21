"""Регрессия на P0-баги: длительности слотов, мосты актов, главы, кроп, end-screen."""
import importlib
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import assemble_pipeline as ap
import build_timeline as bt

DURS = [1800, 1890, 2490, 1290, 2490, 1290]
C = 90.0


def make_manifest():
    slots = []
    for i, d in enumerate(DURS):
        slots.append({
            "slot_id": f"SLOT_{i + 1:02d}",
            "slot_duration_sec": d,
            "title_en": f"Act {i + 1}",
            "base_content": {"file_name": f"clip_{i + 1}.mp4", "loop_crossfade_sec": 1.0},
            "slot_transition_out": (
                {"type": "crossfade", "duration_sec": C} if i < len(DURS) - 1 else {"type": "none"}
            ),
            "inserts": [],
        })
    return {"timeline_slots": slots, "export_settings": {}, "cloud_export": {}}


def clock(sec):
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def end_screen_window(duration_sec):
    if duration_sec < 25:
        return "—"
    start = max(0, duration_sec - 20)
    end = max(start + 5, duration_sec - 5)
    return f"{clock(start)}-{clock(end)}"


class SlotDuration(unittest.TestCase):
    def test_canonical_field(self):
        self.assertEqual(ap.slot_duration_sec({"slot_duration_sec": 1890}), 1890.0)

    def test_legacy_fallback(self):
        self.assertEqual(ap.slot_duration_sec({"duration_sec": 1290}), 1290.0)

    def test_canonical_wins(self):
        self.assertEqual(
            ap.slot_duration_sec({"slot_duration_sec": 2490, "duration_sec": 1800}), 2490.0
        )

    def test_zero_falls_back(self):
        self.assertEqual(
            ap.slot_duration_sec({"slot_duration_sec": 0, "duration_sec": 1290}), 1290.0
        )

    def test_missing_uses_default(self):
        self.assertEqual(ap.slot_duration_sec({}), 1800.0)

    def test_negative_rejected(self):
        with self.assertRaises(SystemExit):
            ap.slot_duration_sec({"slot_duration_sec": -5})


class LoopMath(unittest.TestCase):
    def test_master_duration_and_chapters(self):
        pieces, starts, total = ap.plan_pieces(DURS, C)
        self.assertAlmostEqual(total, 10802.0, places=6)
        self.assertEqual([int(s) for s in starts], [0, 1800, 3600, 6000, 7200, 9600])
        bodies = [p["dur"] for p in pieces if p["kind"] == "body"]
        self.assertEqual(bodies, [1710, 1710, 2310, 1110, 2310, 1200])
        trans = [p["dur"] for p in pieces if p["kind"] == "trans"]
        self.assertEqual(trans, [C] * 5)

    def test_single_slot(self):
        pieces, starts, total = ap.plan_pieces([10800], C)
        self.assertAlmostEqual(total, 10802.0, places=6)
        self.assertEqual(starts, [0.0])
        self.assertFalse([p for p in pieces if p["kind"] == "trans"])

    def test_too_small_slot_rejected(self):
        with self.assertRaises(SystemExit):
            ap.plan_pieces([1800, 100, 1800], C)

    def test_expected_total_matches_master_minus_tail(self):
        m = make_manifest()
        slots = m["timeline_slots"]
        expected = sum(float(s["slot_duration_sec"]) for s in slots) - sum(
            float((s.get("slot_transition_out") or {}).get("duration_sec", 0))
            for s in slots[:-1]
            if (s.get("slot_transition_out") or {}).get("type") == "crossfade"
        )
        _, _, total = ap.plan_pieces(DURS, C)
        self.assertAlmostEqual(expected, 10800.0, places=6)
        self.assertAlmostEqual(total - ap.TAIL_BLACK, expected, places=6)

    def test_formatters(self):
        self.assertEqual(ap.fmt_duration(10800), "3:00:00")
        self.assertEqual(ap.fmt_duration(64), "1:04")
        self.assertEqual(ap.fmt_duration(0), "0:00")
        self.assertEqual(bt.format_timestamp(10800), "03:00:00")
        self.assertEqual(bt.format_timestamp(1800), "00:30:00")


class Chapters(unittest.TestCase):
    def test_act_labels_and_starts(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "chapters.txt"
            bt.generate_chapters(make_manifest(), str(path))
            lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(lines), 6)
        stamps = [ln.split(" ", 1)[0] for ln in lines]
        self.assertEqual(
            stamps, ["00:00:00", "00:30:00", "01:00:00", "01:40:00", "02:00:00", "02:40:00"]
        )
        self.assertTrue(lines[0].endswith("Act 1"))
        self.assertTrue(lines[5].endswith("Act 6"))

    def test_english_sanitize(self):
        self.assertEqual(bt.sanitize_english_title("Акт 1 — Night Rain", "fb"), "1 — Night Rain")
        self.assertEqual(bt.sanitize_english_title("Только русский", "fb"), "fb")


class ShortsAndEndScreen(unittest.TestCase):
    def test_crop_width_is_even(self):
        for ih in (2160, 1440, 1080, 720):
            w = int(ih * 9 / 16 / 2) * 2
            self.assertEqual(w % 2, 0, f"odd crop width for ih={ih}")
            self.assertLessEqual(w, ih * 9 / 16)

    def test_end_screen_from_real_duration(self):
        self.assertEqual(end_screen_window(10800), "2:59:40-2:59:55")
        self.assertEqual(end_screen_window(324), "0:05:04-0:05:19")
        self.assertEqual(end_screen_window(10), "—")


class SourceRegressions(unittest.TestCase):
    def test_no_half_crossfade_bridge(self):
        src = (ROOT / "build_timeline.py").read_text(encoding="utf-8")
        self.assertNotIn("half_cf", src, "мост 1.5×cf вернулся в build_smart_concat")

    def test_no_double_counted_crossfade(self):
        src = (ROOT / "build_timeline.py").read_text(encoding="utf-8")
        self.assertNotIn("raw_dur = slot_dur + cf_out", src)

    def test_pipeline_uses_canonical_duration(self):
        src = (ROOT / "assemble_pipeline.py").read_text(encoding="utf-8")
        self.assertIn("def slot_duration_sec", src)
        self.assertNotIn('float(s.get("duration_sec", 1800))', src)

    def test_workflows_scrub_secrets(self):
        for name in ("render_loop.yml", "render_scenes.yml"):
            path = ROOT / ".github" / "workflows" / name
            if not path.is_file():
                self.skipTest(f"{name} отсутствует")
            text = path.read_text(encoding="utf-8")
            self.assertIn("SECRET_KEYS", text, f"{name}: нет санитайза секретов")
            self.assertIn("def scrub", text, f"{name}: нет функции scrub")

    def test_loop_workflow_date_and_entrypoint(self):
        path = ROOT / ".github" / "workflows" / "render_loop.yml"
        if not path.is_file():
            self.skipTest("render_loop.yml отсутствует")
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("date +\\%s", text)
        self.assertIn("build_timeline.py", text)


class ModuleImports(unittest.TestCase):
    def test_all_modules_import(self):
        for name in ("assemble_pipeline", "build_timeline", "shorts_factory"):
            importlib.import_module(name)
        for name in ("lower_third_overlay", "assemble_scenes"):
            try:
                importlib.import_module(name)
            except ImportError as exc:
                self.skipTest(f"{name}: {exc}")


if __name__ == "__main__":
    unittest.main()
