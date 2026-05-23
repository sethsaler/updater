#!/usr/bin/env python3
"""Unit tests for lib_update_all_clis.py."""
import json, os, sys, tempfile, unittest
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from lib_update_all_clis import (
    EMIT_SEP, ack_unknown, collect_emit_lines, emit_plan_json,
    load_merge, lock_group_for, log_unknowns, report_unknown, validate,
)

class TestEmitLines(unittest.TestCase):
    def setUp(self):
        self.cfg = {"known": {"foo": "echo foo", "bar": "npm update -g bar || true"},
                    "bulk": {"npm": "npm update -g || true", "go": ""}}
        self.cache_path = tempfile.mktemp(suffix=".json")
        with open(self.cache_path, "w") as f:
            json.dump([
                {"name": "foo", "origin": "manual"},
                {"name": "bar", "origin": "npm"},
                {"name": "baz", "origin": "npm"},
                {"name": "orphan", "origin": "go"},
                {"scanned_at": "2026-01-01T00:00:00Z", "count": 4},
            ], f)
    def tearDown(self):
        if os.path.isfile(self.cache_path): os.unlink(self.cache_path)
    def parts(self, line): return line.split(EMIT_SEP)
    def test_dedup_bulk_npm(self):
        cfg = {"known": {"foo": "echo foo"}, "bulk": {"npm": "npm update -g || true", "go": ""}}
        lines = collect_emit_lines(self.cache_path, cfg, None, None)
        bulk = [l for l in lines if self.parts(l)[0] == "bulk" and self.parts(l)[1] == "npm"]
        self.assertEqual(len(bulk), 1)
    def test_skip_orphan_go(self):
        lines = collect_emit_lines(self.cache_path, self.cfg, None, None)
        self.assertTrue(any(self.parts(l)[0] == "skip" and self.parts(l)[1] == "orphan" for l in lines))
    def test_lock_bar(self):
        lines = collect_emit_lines(self.cache_path, self.cfg, None, None)
        bar = next(l for l in lines if self.parts(l)[1] == "bar")
        self.assertEqual(self.parts(bar)[3], "npm")

class TestLockGroup(unittest.TestCase):
    def test_uv(self):
        self.assertEqual(lock_group_for("uv/pip", "", "x"), "uv")

if __name__ == "__main__":
    unittest.main()
