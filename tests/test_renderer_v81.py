import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import subprocess
import wave
import assemble_scenes as r

class RendererRegression(unittest.TestCase):
    def test_native_hybrid_and_cta_segment(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "source.mp4"
            voice = root / "voice.wav"
            subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i",
                "testsrc2=size=320x180:rate=24","-f","lavfi","-i",
                "sine=frequency=220:sample_rate=48000","-t","2",
                "-c:v","libx264","-pix_fmt","yuv420p","-c:a","aac",str(video)],check=True)
            subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i",
                "sine=frequency=440:sample_rate=48000","-t","2.2",str(voice)],check=True)
            with self.assertRaises(ValueError):
                r.scene_timing({"video_duration_mode":"native"},video,voice)
            dur, sync = r.scene_timing({"video_duration_mode":"hybrid"},video,voice)
            self.assertGreater(dur,2)
            self.assertLessEqual(dur,2.3)
            self.assertEqual(sync["voice_start_sec"],0)
            entry = {"scene_id":"test","file_name":video.name,"voiceover_file":voice.name,
                     "video_duration_mode":"hybrid","cta_config":{"enabled":True,
                     "style":"subscribe_bell","show_sec":[0.1,1.9],"click_sec":0.6,
                     "bell_sec":1.1,"sfx_enabled":True}}
            with patch.object(r,"cache_get",return_value=None), patch.object(r,"cache_put"):
                out, length, start, qc = r.build_segment(entry,1,root,"",320,180,24,
                    "ultrafast",24,0,0,{"enabled":False},root)
            self.assertEqual(start,0)
            self.assertTrue(r.segment_is_valid(out,(length,320,180,24)))
            self.assertIsNotNone(qc["tracks"]["voice"]["input_lufs"])
            self.assertIsNotNone(qc["tracks"]["source_environment"]["input_lufs"])
            self.assertIsNotNone(qc["mixed_segment_lufs"])
            with wave.open(str(root/"cta_001"/"cta.wav")) as f:
                self.assertEqual(f.getnframes(),round(length*48000))

if __name__ == "__main__":
    unittest.main()
