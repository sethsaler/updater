from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_user_packages as updater


class UserPackageTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ruby"), "RubyGems required for resolver test")
    def test_gems_select_newest_compatible_registry_release(self):
        run = subprocess.run
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "specifications").mkdir()
            (root / "specifications" / "example-1.gemspec").write_text(
                'Gem::Specification.new { |s| s.name = "example"; s.version = "1" }')
            fixture = """
Gem.define_singleton_method(:user_dir) { ENV.fetch("TEST_GEM_STORE") }
Gem::SpecFetcher.define_singleton_method(:fetcher) { self }
Gem::SpecFetcher.define_singleton_method(:search_for_dependency) do |dependency|
  raise "unexpected gem" unless dependency.name == "example"
  [[2, 3, 4].map { |n| [Gem::NameTuple.new("example", Gem::Version.new(n.to_s), "ruby"), self] }, []]
end
Gem::SpecFetcher.define_singleton_method(:fetch_spec) do |tuple|
  Gem::Specification.new do |spec|
    spec.name = "example"; spec.version = tuple.version
    spec.required_ruby_version = ">= 999" if tuple.version.to_s == "4"
    spec.required_rubygems_version = ">= 999" if tuple.version.to_s == "3"
  end
end
"""
            def stub(args, **kwargs):
                if "-e" in args:
                    result = run([*args[:-1], fixture + args[-1]], **kwargs,
                                 stderr=subprocess.PIPE, env={**os.environ, "TEST_GEM_STORE": tmp})
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), [["example", "2"]])
                    self.assertIn("Deferred example 4", result.stderr)
                    return result
                self.assertEqual(args, ["ruby", "-S", "gem", "install", "--user-install",
                                        "--no-document", "--version", "2", "example"])
                return subprocess.CompletedProcess(args, 0)
            with patch.object(updater.subprocess, "run", side_effect=stub) as mocked:
                self.assertEqual(updater.update_gems(), 0)
                self.assertEqual(mocked.call_count, 2)

    def test_only_pip_registry_packages_are_upgrade_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, installer in [("tool", "pip"), ("checkout", "pip"), ("other", "conda")]:
                dist = root / f"{name}-1.dist-info"
                dist.mkdir()
                (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1\n")
                (dist / "INSTALLER").write_text(installer)
                if name == "checkout":
                    (dist / "direct_url.json").write_text('{"url":"file:///local/project"}')
            self.assertEqual(updater.pip_requirements(root), (["tool"], ["checkout===1", "other===1"]))

    def test_managed_or_missing_interpreter_leaves_package_store_intact(self):
        stores = [Path("missing"), Path("managed")]
        with patch.object(updater, "python_user_sites", return_value=stores), \
             patch.object(updater, "pip_requirements", return_value=(["tool"], [])), \
             patch.object(updater, "python_for_site", side_effect=[(None, False), ("python3", True)]), \
             patch.object(updater.subprocess, "run") as run:
            self.assertEqual(updater.update_pip(), 0)
            run.assert_not_called()
