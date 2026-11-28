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
