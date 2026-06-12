#!/usr/bin/env python3
"""Unit tests for lib_update_all_clis.py."""
import json, os, sys, tempfile, unittest
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
from lib_update_all_clis import (
    EMIT_SEP, _parse_csv, ack_unknown, collect_emit_lines, create_backup,
    emit_plan_json, list_backups, load_merge, lock_group_for, log_unknowns,
    parse_npm_globals_json, report_unknown, restore_backup,
    convert_tools_array_to_json, update_cache_versions, validate,
    validate_cache,
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
    
    def test_version_preservation(self):
        """Test that existing versions are preserved from cache."""
        # Create an existing cache with versions
        existing_cache_path = tempfile.mktemp(suffix=".json")
        with open(existing_cache_path, "w") as f:
            json.dump([
                {"name": "tool1", "origin": "npm", "version": "1.0.0"},
                {"name": "tool2", "origin": "cargo", "version": "2.0.0"},
                {"scanned_at": "2026-06-01T00:00:00Z", "count": 2},
            ], f)
        
        tools_input = "tool1|npm\ntool2|cargo\ntool3|brew\n"
        result = convert_tools_array_to_json(tools_input, "2026-06-02T12:00:00Z", existing_cache_path)
        data = json.loads(result)
        tools = [t for t in data if "name" in t]
        
        # Check that versions are preserved for existing tools
        tool1 = next(t for t in tools if t["name"] == "tool1")
        tool2 = next(t for t in tools if t["name"] == "tool2")
        tool3 = next(t for t in tools if t["name"] == "tool3")
        
        self.assertEqual(tool1["version"], "1.0.0")
        self.assertEqual(tool2["version"], "2.0.0")
        self.assertNotIn("version", tool3)  # New tool shouldn't have version
        
        os.unlink(existing_cache_path)
    
    def test_no_existing_cache(self):
        """Test that conversion works without existing cache."""
        tools_input = "tool1|npm\n"
        result = convert_tools_array_to_json(tools_input, "2026-06-02T12:00:00Z", None)
        data = json.loads(result)
        tools = [t for t in data if "name" in t]
        self.assertEqual(len(tools), 1)
        self.assertNotIn("version", tools[0])  # No version without cache

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

class TestUpdateCacheVersions(unittest.TestCase):
    def test_update_known_versions(self):
        """Test updating versions for known tools."""
        cache_path = tempfile.mktemp(suffix=".json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "tool1", "origin": "npm"},
                {"name": "tool2", "origin": "cargo"},
                {"scanned_at": "2026-06-01T00:00:00Z", "count": 2},
            ], f)
        
        versions = {"known": {"tool1": "1.5.0", "tool2": "2.5.0"}, "bulk": {}}
        update_cache_versions(cache_path, versions)
        
        with open(cache_path, "r") as f:
            data = json.load(f)
        
        tool1 = next(t for t in data if t["name"] == "tool1")
        tool2 = next(t for t in data if t["name"] == "tool2")
        
        self.assertEqual(tool1["version"], "1.5.0")
        self.assertIn("version_updated_at", tool1)
        self.assertEqual(tool2["version"], "2.5.0")
        
        os.unlink(cache_path)
    
    def test_update_bulk_versions(self):
        """Test updating package manager versions for bulk origins."""
        cache_path = tempfile.mktemp(suffix=".json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "tool1", "origin": "npm"},
                {"name": "tool2", "origin": "cargo"},
                {"name": "tool3", "origin": "npm"},
                {"scanned_at": "2026-06-01T00:00:00Z", "count": 3},
            ], f)
        
        versions = {"known": {}, "bulk": {"npm": "10.0.0", "cargo": "1.75.0"}}
        update_cache_versions(cache_path, versions)
        
        with open(cache_path, "r") as f:
            data = json.load(f)
        
        # Check that npm tools got the npm version (filter out metadata)
        npm_tools = [t for t in data if "name" in t and t.get("origin") == "npm"]
        self.assertEqual(len(npm_tools), 2)
        for tool in npm_tools:
            self.assertIn("pm_version", tool)
            self.assertEqual(tool["pm_version"], "10.0.0")
        
        # Check that cargo tool got the cargo version
        cargo_tools = [t for t in data if "name" in t and t.get("origin") == "cargo"]
        self.assertEqual(len(cargo_tools), 1)
        for tool in cargo_tools:
            self.assertIn("pm_version", tool)
            self.assertEqual(tool["pm_version"], "1.75.0")
        
        os.unlink(cache_path)
    
    def test_nonexistent_cache(self):
        """Test that function handles nonexistent cache gracefully."""
        versions = {"known": {"tool1": "1.0.0"}, "bulk": {}}
        # Should not raise an exception
        update_cache_versions("/nonexistent/cache.json", versions)

class TestDiscoveryPaths(unittest.TestCase):
    """Integration tests for discovery path coverage."""
    
    def test_npm_global_paths(self):
        """Test that npm global paths are correctly identified."""
        # Test the format that the shell script uses
        tools_input = "lit|npm\nbb|npm\n"
        result = convert_tools_array_to_json(tools_input, "2026-06-02T12:00:00Z")
        data = json.loads(result)
        tools = [t for t in data if "name" in t]
        
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0]["name"], "lit")
        self.assertEqual(tools[0]["origin"], "npm")
    
    def test_bun_paths(self):
        """Test that bun paths are correctly identified."""
        tools_input = "bun|bun\nbunx|bun\n"
        result = convert_tools_array_to_json(tools_input, "2026-06-02T12:00:00Z")
        data = json.loads(result)
        tools = [t for t in data if "name" in t]
        
        self.assertEqual(len(tools), 2)
        self.assertEqual(tools[0]["origin"], "bun")
    
    def test_mixed_origins(self):
        """Test that tools from different origins are handled correctly."""
        tools_input = "tool1|npm\ntool2|cargo\ntool3|brew\ntool4|go\ntool5|manual\n"
        result = convert_tools_array_to_json(tools_input, "2026-06-02T12:00:00Z")
        data = json.loads(result)
        tools = [t for t in data if "name" in t]
        
        origins = {t["origin"] for t in tools}
        self.assertEqual(origins, {"npm", "cargo", "brew", "go", "manual"})

class TestBackups(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.dir, "cache.json")
        with open(self.cache_path, "w") as f:
            json.dump([{"name": "tool1", "origin": "npm"}], f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_create_and_list_backup(self):
        backup_path = create_backup(self.cache_path)
        self.assertTrue(backup_path)
        self.assertTrue(os.path.isfile(backup_path))
        backups = list_backups(self.cache_path)
        self.assertIn(backup_path, backups)

    def test_create_backup_missing_cache(self):
        self.assertEqual(create_backup(os.path.join(self.dir, "missing.json")), "")

    def test_restore_backup(self):
        backup_path = create_backup(self.cache_path)
        with open(self.cache_path, "w") as f:
            json.dump([], f)
        self.assertTrue(restore_backup(self.cache_path, backup_path))
        with open(self.cache_path) as f:
            data = json.load(f)
        self.assertEqual(data[0]["name"], "tool1")

    def test_restore_missing_backup(self):
        self.assertFalse(restore_backup(self.cache_path, "/nonexistent/backup"))

class TestValidateCache(unittest.TestCase):
    def test_valid_cache(self):
        cache_path = tempfile.mktemp(suffix=".json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "tool1", "origin": "npm", "version": "1.0"},
                {"scanned_at": "2026-06-01T00:00:00Z", "count": 1},
            ], f)
        result = validate_cache(cache_path)
        self.assertTrue(result["valid"])
        self.assertEqual(result["tools_with_versions"], 1)
        os.unlink(cache_path)

    def test_invalid_tool_name_marks_invalid(self):
        cache_path = tempfile.mktemp(suffix=".json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "", "origin": "npm"}], f)
        result = validate_cache(cache_path)
        self.assertFalse(result["valid"])
        self.assertTrue(result["errors"])
        os.unlink(cache_path)

    def test_missing_cache(self):
        result = validate_cache("/nonexistent/cache.json")
        self.assertFalse(result["valid"])

class TestLogUnknowns(unittest.TestCase):
    def test_missing_cache_raises_value_error(self):
        cfg = {"known": {}, "bulk": {}}
        with self.assertRaises(ValueError):
            log_unknowns("/nonexistent/cache.json", cfg, tempfile.mktemp(suffix=".json"))

    def test_logs_and_increments(self):
        dirpath = tempfile.mkdtemp()
        cache_path = os.path.join(dirpath, "cache.json")
        log_path = os.path.join(dirpath, "unknown.json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "mystery", "origin": "?"},
                {"scanned_at": "2026-06-01T00:00:00Z", "count": 1},
            ], f)
        cfg = {"known": {}, "bulk": {}}
        log_unknowns(cache_path, cfg, log_path)
        log_unknowns(cache_path, cfg, log_path)
        with open(log_path) as f:
            data = json.load(f)
        self.assertEqual(data["tools"]["mystery"]["times_seen"], 2)
        import shutil
        shutil.rmtree(dirpath, ignore_errors=True)

    def test_empty_bulk_command_origin_is_logged(self):
        dirpath = tempfile.mkdtemp()
        cache_path = os.path.join(dirpath, "cache.json")
        log_path = os.path.join(dirpath, "unknown.json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "mimo", "origin": "path"},
                {"name": "covered", "origin": "npm"},
                {"scanned_at": "2026-06-01T00:00:00Z", "count": 2},
            ], f)
        cfg = {"known": {}, "bulk": {"path": "", "npm": "npm update -g"}}
        log_unknowns(cache_path, cfg, log_path)
        with open(log_path) as f:
            data = json.load(f)
        self.assertIn("mimo", data["tools"])
        self.assertNotIn("covered", data["tools"])
        import shutil
        shutil.rmtree(dirpath, ignore_errors=True)

class TestRunSummary(unittest.TestCase):
    def test_sections(self):
        from lib_update_all_clis import format_run_summary
        before = {"known": {"a": "1.0", "b": "2.0"}, "bulk": {"npm": "10.0"}}
        after = {"known": {"a": "1.1", "b": "2.0"}, "bulk": {"npm": "10.0"}}
        out = format_run_summary(before, after, 3, 0, ["mimo"])
        self.assertIn("Upgraded (1):", out)
        self.assertIn("a: 1.0 → 1.1", out)
        self.assertIn("New installs added for future runs (1):", out)
        self.assertIn("  mimo", out)
        self.assertIn("Already up to date (2):", out)
        self.assertIn("b, npm", out)

    def test_empty_sections(self):
        from lib_update_all_clis import format_run_summary
        out = format_run_summary({}, {}, 0, 0)
        self.assertIn("Upgraded (0):", out)
        self.assertIn("New installs added for future runs (0):", out)
        self.assertIn("Already up to date (0):", out)


class TestDiffNewTools(unittest.TestCase):
    def test_detects_new_tools(self):
        from lib_update_all_clis import diff_new_tools
        dirpath = tempfile.mkdtemp()
        prev_path = os.path.join(dirpath, "prev.txt")
        cache_path = os.path.join(dirpath, "cache.json")
        with open(prev_path, "w") as f:
            f.write("old-tool\n")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "old-tool", "origin": "npm"},
                {"name": "mimo", "origin": "path"},
                {"scanned_at": "2026-06-12T00:00:00Z", "count": 2},
            ], f)
        self.assertEqual(diff_new_tools(prev_path, cache_path), ["mimo"])
        import shutil
        shutil.rmtree(dirpath, ignore_errors=True)

    def test_empty_prev_returns_nothing(self):
        from lib_update_all_clis import diff_new_tools
        dirpath = tempfile.mkdtemp()
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "x", "origin": "npm"}], f)
        self.assertEqual(diff_new_tools(os.path.join(dirpath, "missing.txt"), cache_path), [])
        import shutil
        shutil.rmtree(dirpath, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
