"""Дымовой прогон реальных filter_complex-графов на testsrc (2 с, 320x180)."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

ENC = ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
       "-r", "24", "-video_track_timescale", "24000",
       "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]


def duration_of(path):
    out = subprocess.check_output([
        FFPROBE, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]).decode().strip()
    return float(out)


@unittest.skipUnless(FFMPEG and FFPROBE, "ffmpeg/ffprobe не установлены")
class GraphSmoke(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.src = self.dir / "src.mp4"
        subprocess.run([
            FFMPEG, "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-shortest"
        ] + ENC + [str(self.src)], check=True, capture_output=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_bake_loop_graph(self):
        """xfade(src,src)+trim и acrossfade с ДВУМЯ входами — граф из bake_loop."""
        S, D = 2.0, 0.5
        L = S - D
        pre = "fps=24,settb=AVTB,format=yuv420p"
        vf = (
            f"[0:v]split=2[va][vb];"
            f"[va]trim=0:{S},setpts=PTS-STARTPTS,{pre}[fa];"
            f"[vb]trim=0:{S},setpts=PTS-STARTPTS,{pre}[fb];"
            f"[fa][fb]xfade=transition=fade:duration={D}:offset={S - D}[x];"
            f"[x]trim=start={D}:duration={L},setpts=PTS-STARTPTS,"
            f"scale=320:180:force_original_aspect_ratio=decrease,"
            f"pad=320:180:(ow-iw)/2:(oh-ih)/2,setsar=1[v]"
        )
        af = (
            f"[0:a]asplit=2[aa][ab];"
            f"[aa]atrim=0:{S},asetpts=PTS-STARTPTS[ga];"
            f"[ab]atrim=0:{S},asetpts=PTS-STARTPTS[gb];"
            f"[ga][gb]acrossfade=d={D}:c1=tri:c2=tri[xa];"
            f"[xa]atrim=start={D}:duration={L},asetpts=PTS-STARTPTS[a]"
        )
        out = self.dir / "baked.mp4"
        subprocess.run([
            FFMPEG, "-y", "-i", str(self.src), "-filter_complex", vf + ";" + af,
            "-map", "[v]", "-map", "[a]", "-t", f"{L}"
        ] + ENC + [str(out)], check=True, capture_output=True)
        self.assertAlmostEqual(duration_of(out), L, delta=0.2)

    def test_act_bridge_graph(self):
        """Мост акта: xfade duration=cf offset=0 → длина ровно cf (не 1.5×cf)."""
        cf = 0.5
        fc = (
            f"[0:v]fps=24,settb=AVTB,format=yuv420p[va];"
            f"[1:v]fps=24,settb=AVTB,format=yuv420p[vb];"
            f"[va][vb]xfade=transition=fade:duration={cf}:offset=0[v];"
            f"[0:a]atrim=0:{cf},asetpts=PTS-STARTPTS[aa];"
            f"[1:a]atrim=0:{cf},asetpts=PTS-STARTPTS[ab];"
            f"[aa][ab]acrossfade=d={cf}:c1=tri:c2=tri[a]"
        )
        out = self.dir / "bridge.mp4"
        subprocess.run([
            FFMPEG, "-y",
            "-stream_loop", "-1", "-ss", "1.5", "-t", f"{cf}", "-i", str(self.src),
            "-stream_loop", "-1", "-t", f"{cf}", "-i", str(self.src),
            "-filter_complex", fc, "-map", "[v]", "-map", "[a]", "-t", f"{cf}"
        ] + ENC + [str(out)], check=True, capture_output=True)
        self.assertAlmostEqual(duration_of(out), cf, delta=0.15)

    def test_body_render_and_concat_copy(self):
        """Тела из зацикленного источника + concat -c copy с едиными параметрами."""
        bodies = []
        for idx, skip in enumerate((0.0, 0.5)):
            out = self.dir / f"body_{idx}.mp4"
            subprocess.run([
                FFMPEG, "-y", "-stream_loop", "-1", "-ss", f"{skip}", "-i", str(self.src),
                "-t", "1.0", "-vf", "fps=24,format=yuv420p", "-af", "anull"
            ] + ENC + [str(out)], check=True, capture_output=True)
            bodies.append(out)
        listing = self.dir / "concat.txt"
        listing.write_text(
            "".join(f"file '{p.resolve().as_posix()}'\n" for p in bodies), encoding="utf-8"
        )
        master = self.dir / "master.mp4"
        subprocess.run([
            FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(master)
        ], check=True, capture_output=True)
        self.assertAlmostEqual(duration_of(master), 2.0, delta=0.25)

    def test_loudnorm_measure_pass(self):
        """Проход измерения loudnorm отдаёт JSON с input_i (нужен для two-pass)."""
        res = subprocess.run([
            FFMPEG, "-i", str(self.src), "-map", "0:a",
            "-af", "loudnorm=I=-16:TP=-3:LRA=11:print_format=json", "-f", "null", "-"
        ], capture_output=True, text=True)
        self.assertIn('"input_i"', res.stderr)


if __name__ == "__main__":
    unittest.main()
