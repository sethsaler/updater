#!/usr/bin/env python3
"""Unit tests for lib_update_all_clis.py."""
import json, os, sys, tempfile, unittest
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from lib_update_all_clis import (
    EMIT_SEP, _parse_csv, ack_unknown, collect_emit_lines, emit_plan_json,
    load_merge, lock_group_for, log_unknowns, parse_npm_globals_json,
    convert_tools_array_to_json, validate,
)

class TestParseCSV(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(_parse_csv(None), set())
        self.assertEqual(_parse_csv(""), set())
        self.assertEqual(_parse_csv("   "), set())
    
    def test_simple(self):
        self.assertEqual(_parse_csv("a,b,c"), {"a", "b", "c"})
        self.assertEqual(_parse_csv("a, b , c"), {"a", "b", "c"})
        self.assertEqual(_parse_csv("a,,b"), {"a", "b"})

class TestLockGroup(unittest.TestCase):
    def test_uv_variants(self):
        self.assertEqual(lock_group_for("uv", "", "x"), "uv")
        self.assertEqual(lock_group_for("uv/pip", "", "x"), "uv")
        self.assertEqual(lock_group_for("uv/venv", "", "x"), "uv")
    
    def test_package_managers(self):
        self.assertEqual(lock_group_for("npm", "", "x"), "npm")
        self.assertEqual(lock_group_for("brew", "", "x"), "brew")
        self.assertEqual(lock_group_for("cargo", "", "x"), "cargo")
    
    def test_inference_from_command(self):
        self.assertEqual(lock_group_for("manual", "npm install", "tool"), "npm")
        self.assertEqual(lock_group_for("manual", "brew install", "tool"), "brew")
        self.assertEqual(lock_group_for("manual", "cargo install", "tool"), "cargo")
    
    def test_fallback_to_name(self):
        self.assertEqual(lock_group_for("manual", "unknown command", "toolname"), "toolname")

class TestLoadMerge(unittest.TestCase):
    def setUp(self):
        self.base_path = tempfile.mktemp(suffix=".json")
        self.local_path = tempfile.mktemp(suffix=".json")
    
    def tearDown(self):
        if os.path.isfile(self.base_path): os.unlink(self.base_path)
        if os.path.isfile(self.local_path): os.unlink(self.local_path)
    
    def test_base_only(self):
        with open(self.base_path, "w") as f:
            json.dump({"known": {"tool1": "cmd1"}, "bulk": {"npm": "npm update"}}, f)
        result = load_merge(self.base_path, None)
        self.assertEqual(result["known"]["tool1"], "cmd1")
        self.assertEqual(result["bulk"]["npm"], "npm update")
    
    def test_merge_override(self):
        with open(self.base_path, "w") as f:
            json.dump({"known": {"tool1": "cmd1"}, "bulk": {"npm": "npm update"}}, f)
        with open(self.local_path, "w") as f:
            json.dump({"known": {"tool1": "cmd2", "tool2": "cmd2"}}, f)
        result = load_merge(self.base_path, self.local_path)
        self.assertEqual(result["known"]["tool1"], "cmd2")  # Overridden
        self.assertEqual(result["known"]["tool2"], "cmd2")  # Added
    
    def test_file_not_found(self):
        with self.assertRaises(ValueError) as ctx:
            load_merge("/nonexistent.json", None)
        self.assertIn("not found", str(ctx.exception))
    
    def test_invalid_json(self):
        with open(self.base_path, "w") as f:
            f.write("{invalid json")
        with self.assertRaises(ValueError) as ctx:
            load_merge(self.base_path, None)
        self.assertIn("Invalid JSON", str(ctx.exception))

class TestValidate(unittest.TestCase):
    def test_valid(self):
        cfg = {"known": {"tool1": "cmd1"}, "bulk": {"npm": "npm update"}}
        validate(cfg)  # Should not raise
    
    def test_missing_sections(self):
        cfg = {"known": {"tool1": "cmd1"}}
        with self.assertRaises(ValueError) as ctx:
            validate(cfg)
        self.assertIn("must contain 'known' and 'bulk'", str(ctx.exception))
    
    def test_invalid_command_type(self):
        cfg = {"known": {"tool1": 123}, "bulk": {"npm": "npm update"}}
        with self.assertRaises(ValueError) as ctx:
            validate(cfg)
        self.assertIn("must be a string command", str(ctx.exception))

class TestParseNpmGlobalsJson(unittest.TestCase):
    def test_valid_json(self):
        json_str = '{"dependencies": {"pkg1": {"resolved": "/path1"}, "pkg2": {"path": "/path2"}}}'
        result = parse_npm_globals_json(json_str)
        self.assertIn("/path1", result)
        self.assertIn("/path2", result)
    
    def test_invalid_json(self):
        result = parse_npm_globals_json("{invalid")
        self.assertEqual(result, "")

class TestConvertToolsArrayToJson(unittest.TestCase):
    def test_basic_conversion(self):
        tools_input = "tool1|npm\ntool2|cargo\n"
        result = convert_tools_array_to_json(tools_input, "2026-06-02T12:00:00Z")
        data = json.loads(result)
        tools = [t for t in data if "name" in t]
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0]["name"], "tool1")
        self.assertEqual(tools[0]["origin"], "npm")

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

if __name__ == "__main__":
    unittest.main()
