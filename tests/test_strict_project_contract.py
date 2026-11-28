import unittest
from strict_project_contract import validate_manifest, validate_listing

class StrictProjectContractTests(unittest.TestCase):
    def manifest(self,name="base.mp4"):
        return {"cloud_export":{"target_folder_id":"fixture-folder-123"},"timeline_slots":[{"base_content":{"file_name":name},"inserts":[{"file_name":"insert.mp4"}]}]}
    def test_valid(self):
        names=validate_manifest(self.manifest())
        self.assertEqual(names,{"base.mp4","insert.mp4"})
        validate_listing(names,["base.mp4","insert.mp4"])
    def test_missing_folder_or_timeline(self):
        for data in ({},{"timeline_slots":[]}, {"cloud_export":{"target_folder_id":"fixture-folder-123"},"timeline_slots":[]}):
            with self.assertRaises(ValueError): validate_manifest(data)
    def test_unsafe_name(self):
        for name in ("../base.mp4","*.mp4","base\\clip.mp4",""):
            with self.assertRaises(ValueError): validate_manifest(self.manifest(name))
    def test_listing_rejects_case_and_duplicates(self):
        for listing in (["BASE.mp4","insert.mp4"],["base.mp4","base.mp4","insert.mp4"],["base.mp4","BASE.mp4","insert.mp4"],["base.mp4"]):
            with self.assertRaises(ValueError): validate_listing({"base.mp4","insert.mp4"},listing)

class DownloadedAssetTests(unittest.TestCase):
    def test_every_asset_probed_and_invalid_media_rejected(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from strict_project_contract import verify_downloaded_assets
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("base.mp4","insert.mp4"):
                (root/name).write_bytes(b"fixture")
            seen=[]
            def probe(path):
                seen.append(path.name)
                return {"format":{"duration":"1.0"},"streams":[{"codec_type":"video"}]}
            self.assertEqual(verify_downloaded_assets({"base.mp4","insert.mp4"},root,probe),2)
            self.assertEqual(seen,["base.mp4","insert.mp4"])
            for info in ({"format":{"duration":"nan"},"streams":[{"codec_type":"video"}]},
                         {"format":{"duration":"0"},"streams":[{"codec_type":"video"}]},
                         {"format":{"duration":"1"},"streams":[{"codec_type":"audio"}]}):
                with self.assertRaises(ValueError):
                    verify_downloaded_assets({"insert.mp4"},root,lambda _:info)
            (root/"insert.mp4").unlink()
            with self.assertRaises(ValueError):
                verify_downloaded_assets({"insert.mp4"},root,probe)
            (root/"insert.mp4").write_bytes(b"")
            with self.assertRaises(ValueError):
                verify_downloaded_assets({"insert.mp4"},root,probe)
            (root/"insert.mp4").unlink()
            (root/"insert.mp4").symlink_to(root/"base.mp4")
            with self.assertRaises(ValueError):
                verify_downloaded_assets({"insert.mp4"},root,probe)

    def test_real_ffprobe_video_and_audio_only(self):
        import subprocess
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from strict_project_contract import verify_downloaded_assets
        with TemporaryDirectory() as directory:
            root=Path(directory)
            for name in ("base.mp4","insert.mp4"):
                subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","color=c=blue:s=64x64:r=24:d=1","-c:v","libx264","-pix_fmt","yuv420p",str(root/name)],check=True)
            self.assertEqual(verify_downloaded_assets({"base.mp4","insert.mp4"},root),2)
            subprocess.run(["ffmpeg","-v","error","-y","-f","lavfi","-i","sine=frequency=440:duration=1","-c:a","aac",str(root/"audio.mp4")],check=True)
            with self.assertRaises(ValueError):
                verify_downloaded_assets({"audio.mp4"},root)
            (root/"corrupt.mp4").write_bytes(b"not a video")
            with self.assertRaises(subprocess.CalledProcessError):
                verify_downloaded_assets({"corrupt.mp4"},root)
