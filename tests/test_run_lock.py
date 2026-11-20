import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest


class RunLockTests(unittest.TestCase):
    def test_concurrent_and_readonly_invocations_preserve_owners_lock(self):
        script = Path(__file__).resolve().parents[1] / "update_all_clis.sh"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state = root / ".config/update-all-clis"
            state.mkdir(parents=True)
            config = root / "config.json"
            config.write_text(json.dumps({"known": {"test-lock-holder":
                'touch "$HOME/started"; while [ ! -e "$HOME/finish" ]; do sleep 0.1; done'},
                "bulk": {}}))
            (state / "cache.json").write_text(json.dumps([{"name": "test-lock-holder", "origin": "manual"}]))
            env = dict(os.environ, HOME=td, XDG_CONFIG_HOME=str(root / ".config"), CONFIG_FILE=str(config),
                       UPDATE_ALL_CLIS_SELF_UPDATE="", UPDATE_ALL_CLIS_NOTIFY="0")
            def run(*args):
                return subprocess.run([str(script), *args], env=env, text=True,
                                      capture_output=True, timeout=10, check=True)

            owner = subprocess.Popen([str(script), "--no-scan", "--no-precheck", "--no-tui", "--quiet"],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while not (root / "started").exists() and owner.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((root / "started").exists(), "fixture update never acquired the lock")
                run("--version")
                for _ in range(2):
                    busy = run("--no-scan", "--json-plan")
                    self.assertIn("run is in progress", busy.stderr)
                    self.assertEqual("", busy.stdout)
            finally:
                (root / "finish").touch()
                stdout, stderr = owner.communicate(timeout=10)
            self.assertEqual(owner.returncode, 0, stdout + stderr)
            self.assertIn("plan", json.loads(run("--no-scan", "--json-plan").stdout))
