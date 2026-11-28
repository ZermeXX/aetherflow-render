import os
import subprocess
import unittest
from pathlib import Path
import yaml

class RunnerIdentityTests(unittest.TestCase):
    def test_actual_workflow_guard(self):
        workflow=yaml.safe_load(Path(".github/workflows/render_loop.yml").read_text())
        steps=workflow["jobs"]["render_and_upload"]["steps"]
        guard=next(s for s in steps if s["name"]=="Strict Project Dispatch Identity Guard")
        self.assertLess(steps.index(guard),next(i for i,s in enumerate(steps) if s["name"]=="Setup Personal Rclone Config"))
        sha=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip()
        env={**os.environ,"EXPECTED_SHA":sha,"GITHUB_SHA":sha,"REQUEST_ID":"12345678-1234-1234-1234-123456789abc","PREFLIGHT_ONLY":"true"}
        def run(patch):
            return subprocess.run(["bash","-c",guard["run"]],env={**env,**patch},capture_output=True,text=True)
        valid=run({})
        self.assertEqual(valid.returncode,0,valid.stderr)
        self.assertIn("STRICT_DISPATCH_IDENTITY_PASS",valid.stdout)
        for patch in ({"EXPECTED_SHA":"0"*40},{"GITHUB_SHA":"0"*40},{"EXPECTED_SHA":""},{"REQUEST_ID":"invalid"},{"PREFLIGHT_ONLY":"false"}):
            with self.subTest(patch=patch):
                self.assertNotEqual(run(patch).returncode,0)
