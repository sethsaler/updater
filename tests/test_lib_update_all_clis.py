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
    format_history, group_history_by_run, historical_mean_durations,
    history_append, load_history_by_name, load_history_records,
    quarantined_names,
    _stdout_signals_uptodate, precheck_candidate_origins, run_prechecks,
    incremental_scan_merge, parse_scan_rows, _scan_dir_entries,
    normalize_hold_entries, edit_local_hold, format_run_summary,
    is_major_upgrade, leading_major,
    doctor_broken_symlinks, doctor_shadowed_duplicates,
    doctor_chronic_failures, doctor_config_issues, doctor_not_installed, doctor_report,
    doctor_has_findings, format_doctor_report,
    tag_in_range, tag_to_version, truncate_changelog_body,
    format_changelog_section, changed_tools_with_repos, build_changelog_digest,
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

class TestInferOriginFromSymlink(unittest.TestCase):
    """npm globals installed under an npm prefix of ~/.local land in
    ~/.local/bin (scanned as origin uv/pip); their symlinks resolve into
    node_modules, so they must be rerouted to the npm bulk update."""

    def setUp(self):
        import lib_update_all_clis as m
        self.m = m
        self.tmp = tempfile.mkdtemp()
        self.bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin_dir)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bin_dir

    def tearDown(self):
        os.environ["PATH"] = self._old_path
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def _make_symlink_tool(self, name, target_rel):
        target = os.path.join(self.tmp, target_rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(target, 0o755)
        link = os.path.join(self.bin_dir, name)
        os.symlink(target, link)
        os.chmod(link, 0o755)
        return link

    def test_uv_pip_origin_node_modules_symlink_reroutes_to_npm(self):
        self._make_symlink_tool("pi", "lib/node_modules/@scope/pi/dist/cli.js")
        self.assertEqual(self.m._infer_origin_from_symlink("pi", "uv/pip"), "npm")

    def test_uv_origin_non_node_modules_symlink_stays(self):
        self._make_symlink_tool("realuv", "share/uv/tools/realuv/bin/realuv")
        self.assertIsNone(self.m._infer_origin_from_symlink("realuv", "uv/pip"))

    def test_uv_origin_regular_file_stays(self):
        p = os.path.join(self.bin_dir, "plainbin")
        with open(p, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(p, 0o755)
        self.assertIsNone(self.m._infer_origin_from_symlink("plainbin", "uv"))

    def test_path_origin_node_modules_symlink_reroutes_to_npm(self):
        self._make_symlink_tool("qwen", "lib/node_modules/@qwen/qwen/cli.js")
        self.assertEqual(self.m._infer_origin_from_symlink("qwen", "path"), "npm")

    def test_known_pm_origin_untouched(self):
        self.assertIsNone(self.m._infer_origin_from_symlink("anything", "brew"))


class TestEmitReroutesNpmSymlinkFromUvDir(unittest.TestCase):
    """End-to-end through collect_emit_lines: a cache entry with origin
    uv/pip whose on-disk binary is a node_modules symlink emits the npm
    bulk line (not just uv), so new npm CLIs like pi/qwen get updated."""

    def setUp(self):
        import lib_update_all_clis as m
        self.m = m
        self.tmp = tempfile.mkdtemp()
        self.bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin_dir)
        target = os.path.join(self.tmp, "lib/node_modules/@scope/pi/cli.js")
        os.makedirs(os.path.dirname(target))
        with open(target, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(target, 0o755)
        link = os.path.join(self.bin_dir, "pi")
        os.symlink(target, link)
        self._old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = self.bin_dir
        self.cache_path = os.path.join(self.tmp, "cache.json")
        with open(self.cache_path, "w") as f:
            json.dump([{"name": "pi", "origin": "uv/pip", "dir": self.bin_dir}], f)

    def tearDown(self):
        os.environ["PATH"] = self._old_path
        import shutil as _sh
        _sh.rmtree(self.tmp, ignore_errors=True)

    def test_bulk_npm_emitted_for_uv_pip_node_symlink(self):
        cfg = {
            "known": {},
            "bulk": {"npm": "npm update -g", "uv/pip": "uv tool upgrade --all"},
        }
        lines = collect_emit_lines(self.cache_path, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "npm"), kinds)


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
    def test_known_tool_does_not_suppress_origin_bulk(self):
        # Regression: "bar" is a known npm tool with its own command, but
        # "baz" (origin npm, not in "known") should still trigger the npm
        # bulk update — a known tool sharing an origin must not silently
        # suppress that origin's bulk line for the rest of its globals.
        lines = collect_emit_lines(self.cache_path, self.cfg, None, None)
        bulk_npm = [l for l in lines if self.parts(l)[0] == "bulk" and self.parts(l)[1] == "npm"]
        self.assertEqual(len(bulk_npm), 1)

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


class TestCachedVersions(unittest.TestCase):
    def test_load_cached_versions(self):
        from lib_update_all_clis import _load_cached_versions
        dirpath = tempfile.mkdtemp()
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "tool1", "origin": "npm", "version": "1.2.3"},
                {"name": "tool2", "origin": "cargo", "version": "9.9", "pm_version": "1.75"},
                {"name": "noversion", "origin": "npm"},
                {"scanned_at": "x", "count": 2},
            ], f)
        known, bulk = _load_cached_versions(cache_path)
        self.assertEqual(known.get("tool1"), "1.2.3")
        self.assertNotIn("noversion", known)
        self.assertEqual(bulk.get("cargo"), "1.75")
        # missing / none are no-ops
        self.assertEqual(_load_cached_versions("/nonexistent"), ({}, {}))
        self.assertEqual(_load_cached_versions(None), ({}, {}))
        import shutil
        shutil.rmtree(dirpath, ignore_errors=True)

    def test_snapshot_uses_cache_and_skips_probe(self):
        import lib_update_all_clis as m
        dirpath = tempfile.mkdtemp()
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "cachedtool", "origin": "npm", "version": "7.7.7"}], f)
        called = []
        orig = m.probe_known
        m.probe_known = lambda name: called.append(name) or "PROBED"
        try:
            lines = [f"known{EMIT_SEP}cachedtool{EMIT_SEP}echo{EMIT_SEP}npm"]
            snap = m.snapshot_versions(lines, cache_path)
        finally:
            m.probe_known = orig
        self.assertEqual(snap["known"]["cachedtool"], "7.7.7")
        self.assertEqual(called, [])
        import shutil
        shutil.rmtree(dirpath, ignore_errors=True)


class TestHoldLock(unittest.TestCase):
    def test_busy_then_acquire(self):
        import subprocess, time
        import shutil
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(dirpath, ignore_errors=True))
        lockfile = os.path.join(dirpath, "sub.lock")
        lib = os.path.join(REPO_ROOT, "lib_update_all_clis.py")
        py = sys.executable

        holder = subprocess.Popen([py, lib, "hold-lock", lockfile],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(holder.stdout.readline().decode().strip(), "LOCKED")
            # Non-blocking attempt while held must report BUSY (exit 2).
            r = subprocess.run([py, lib, "try-hold-lock", lockfile],
                               capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), "BUSY")
            self.assertEqual(r.returncode, 2)
        finally:
            holder.terminate()
            holder.wait()

        # After release, a fresh non-blocking attempt acquires (LOCKED).
        r = subprocess.Popen([py, lib, "try-hold-lock", lockfile],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.assertEqual(r.stdout.readline().decode().strip(), "LOCKED")
        finally:
            r.terminate()
            r.wait()


class TestHistoryAppend(unittest.TestCase):
    def setUp(self):
        self.dirpath = tempfile.mkdtemp()
        self.history_path = os.path.join(self.dirpath, "history.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dirpath, ignore_errors=True)

    def _line(self, kind, name, cmd, ec, start, end):
        return f"{kind}{EMIT_SEP}{name}{EMIT_SEP}{cmd}{EMIT_SEP}{ec}{EMIT_SEP}{start}{EMIT_SEP}{end}"

    def test_appends_one_record_per_job(self):
        lines = [
            self._line("known", "foo", "echo foo", 0, 100, 105),
            self._line("bulk", "npm", "npm update -g", 1, 200, 210),
        ]
        before = {"known": {"foo": "1.0"}, "bulk": {"npm": "9.0"}}
        after = {"known": {"foo": "1.1"}, "bulk": {"npm": "9.0"}}
        appended = history_append(self.history_path, "run1", lines, before, after)
        self.assertEqual(appended, 2)
        records = load_history_records(self.history_path)
        self.assertEqual(len(records), 2)
        foo = next(r for r in records if r["name"] == "foo")
        self.assertEqual(foo["status"], "ok")
        self.assertEqual(foo["duration_s"], 5.0)
        self.assertEqual(foo["version_before"], "1.0")
        self.assertEqual(foo["version_after"], "1.1")
        npm = next(r for r in records if r["name"] == "npm")
        self.assertEqual(npm["status"], "fail")
        self.assertEqual(npm["kind"], "bulk")

    def test_skip_and_quarantined_lines_ignored(self):
        lines = [
            "skip" + EMIT_SEP + "orphan" + EMIT_SEP + EMIT_SEP,
            "quarantined" + EMIT_SEP + "badtool" + EMIT_SEP + "3" + EMIT_SEP,
            self._line("known", "foo", "echo foo", 0, 1, 2),
        ]
        appended = history_append(self.history_path, "run1", lines, {}, {})
        self.assertEqual(appended, 1)
        records = load_history_records(self.history_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["name"], "foo")

    def test_missing_version_defaults_to_unknown(self):
        lines = [self._line("known", "foo", "echo foo", 0, 1, 2)]
        appended = history_append(self.history_path, "run1", lines, {}, {})
        self.assertEqual(appended, 1)
        rec = load_history_records(self.history_path)[0]
        self.assertEqual(rec["version_before"], "?")
        self.assertEqual(rec["version_after"], "?")

    def test_no_valid_lines_appends_nothing(self):
        appended = history_append(self.history_path, "run1", ["", "garbage"], {}, {})
        self.assertEqual(appended, 0)
        self.assertFalse(os.path.isfile(self.history_path))

    def test_prunes_to_max_lines(self):
        # Seed the file with more than max_lines entries directly.
        with open(self.history_path, "w") as f:
            for i in range(10):
                f.write(json.dumps({"run_id": f"old{i}", "name": f"t{i}"}) + "\n")
        lines = [self._line("known", "newtool", "echo", 0, 1, 2)]
        history_append(self.history_path, "run-new", lines, {}, {}, max_lines=5)
        records = load_history_records(self.history_path)
        self.assertEqual(len(records), 5)
        self.assertEqual(records[-1]["name"], "newtool")

    def test_appends_across_multiple_calls(self):
        history_append(self.history_path, "run1",
                        [self._line("known", "foo", "echo", 0, 1, 2)], {}, {})
        history_append(self.history_path, "run2",
                        [self._line("known", "foo", "echo", 1, 1, 3)], {}, {})
        records = load_history_records(self.history_path)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["run_id"], "run1")
        self.assertEqual(records[1]["run_id"], "run2")


class TestLoadHistoryByName(unittest.TestCase):
    def test_groups_by_name_preserving_order(self):
        dirpath = tempfile.mkdtemp()
        try:
            history_path = os.path.join(dirpath, "history.jsonl")
            with open(history_path, "w") as f:
                for rec in [
                    {"name": "foo", "run_id": "r1", "duration_s": 1.0, "status": "ok"},
                    {"name": "bar", "run_id": "r1", "duration_s": 2.0, "status": "fail"},
                    {"name": "foo", "run_id": "r2", "duration_s": 3.0, "status": "ok"},
                ]:
                    f.write(json.dumps(rec) + "\n")
            by_name = load_history_by_name(history_path)
            self.assertEqual([r["run_id"] for r in by_name["foo"]], ["r1", "r2"])
            self.assertEqual(len(by_name["bar"]), 1)
        finally:
            import shutil
            shutil.rmtree(dirpath, ignore_errors=True)

    def test_missing_file_returns_empty(self):
        self.assertEqual(load_history_by_name("/nonexistent/history.jsonl"), {})
        self.assertEqual(load_history_by_name(None), {})


class TestHistoricalMeanDurations(unittest.TestCase):
    def test_mean_of_last_n(self):
        by_name = {
            "brew": [{"duration_s": d} for d in [10, 20, 30]],
            "npm": [{"duration_s": 5}],
        }
        means = historical_mean_durations(by_name, per_job=10)
        self.assertEqual(means["brew"], 20.0)
        self.assertEqual(means["npm"], 5.0)

    def test_uses_only_last_per_job_records(self):
        by_name = {"brew": [{"duration_s": d} for d in [100, 100, 2, 2]]}
        means = historical_mean_durations(by_name, per_job=2)
        self.assertEqual(means["brew"], 2.0)

    def test_no_duration_data_omitted(self):
        by_name = {"noduration": [{"status": "ok"}]}
        means = historical_mean_durations(by_name)
        self.assertNotIn("noduration", means)


class TestQuarantinedNames(unittest.TestCase):
    def test_threshold_zero_disables(self):
        by_name = {"badtool": [{"status": "fail"}] * 5}
        self.assertEqual(quarantined_names(by_name, 0), set())

    def test_consecutive_failures_quarantines(self):
        by_name = {"badtool": [{"status": "fail"}] * 3}
        self.assertEqual(quarantined_names(by_name, 3), {"badtool"})

    def test_not_enough_history_not_quarantined(self):
        by_name = {"badtool": [{"status": "fail"}] * 2}
        self.assertEqual(quarantined_names(by_name, 3), set())

    def test_recent_success_clears_streak(self):
        by_name = {"badtool": [{"status": "fail"}, {"status": "fail"}, {"status": "ok"}]}
        self.assertEqual(quarantined_names(by_name, 3), set())

    def test_only_last_threshold_records_considered(self):
        # 5 fails then 3 successes: last 3 are ok, so not quarantined even
        # though there's a long-ago failure streak.
        by_name = {"badtool": [{"status": "fail"}] * 5 + [{"status": "ok"}] * 3}
        self.assertEqual(quarantined_names(by_name, 3), set())
        by_name2 = {"badtool": [{"status": "ok"}] + [{"status": "fail"}] * 3}
        self.assertEqual(quarantined_names(by_name2, 3), {"badtool"})


class TestCollectEmitLinesQuarantineAndOrdering(unittest.TestCase):
    def setUp(self):
        self.cfg = {"known": {"foo": "echo foo", "slow": "echo slow", "bad": "echo bad"},
                    "bulk": {}}
        self.cache_path = tempfile.mktemp(suffix=".json")
        with open(self.cache_path, "w") as f:
            json.dump([
                {"name": "foo", "origin": "manual"},
                {"name": "slow", "origin": "manual"},
                {"name": "bad", "origin": "manual"},
                {"scanned_at": "2026-01-01T00:00:00Z", "count": 3},
            ], f)
        self.history_path = tempfile.mktemp(suffix=".jsonl")

    def tearDown(self):
        for p in (self.cache_path, self.history_path):
            if os.path.isfile(p):
                os.unlink(p)

    def parts(self, line):
        return line.split(EMIT_SEP)

    def _write_history(self, records):
        with open(self.history_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

    def test_quarantined_job_becomes_quarantined_line(self):
        self._write_history([
            {"name": "bad", "run_id": f"r{i}", "status": "fail", "duration_s": 1}
            for i in range(3)
        ])
        lines = collect_emit_lines(self.cache_path, self.cfg, None, None,
                                    history_path=self.history_path, quarantine_after=3)
        bad = next(l for l in lines if self.parts(l)[1] == "bad")
        self.assertEqual(self.parts(bad)[0], "quarantined")
        self.assertEqual(self.parts(bad)[2], "3")

    def test_include_quarantined_bypasses_skip(self):
        self._write_history([
            {"name": "bad", "run_id": f"r{i}", "status": "fail", "duration_s": 1}
            for i in range(3)
        ])
        lines = collect_emit_lines(self.cache_path, self.cfg, None, None,
                                    history_path=self.history_path, quarantine_after=3,
                                    include_quarantined=True)
        bad = next(l for l in lines if self.parts(l)[1] == "bad")
        self.assertEqual(self.parts(bad)[0], "known")

    def test_slowest_first_ordering(self):
        self._write_history([
            {"name": "slow", "run_id": "r1", "status": "ok", "duration_s": 50},
            {"name": "foo", "run_id": "r1", "status": "ok", "duration_s": 1},
        ])
        lines = collect_emit_lines(self.cache_path, self.cfg, None, None,
                                    history_path=self.history_path)
        names = [self.parts(l)[1] for l in lines]
        # "slow" has the highest mean duration, so it should come first
        # among lines with history; "bad" has no history and sorts after.
        self.assertLess(names.index("slow"), names.index("foo"))
        self.assertLess(names.index("foo"), names.index("bad"))

    def test_no_history_file_is_stable_and_unquarantined(self):
        lines = collect_emit_lines(self.cache_path, self.cfg, None, None,
                                    history_path="/nonexistent/history.jsonl")
        kinds = {self.parts(l)[0] for l in lines}
        self.assertEqual(kinds, {"known"})


class TestFormatHistory(unittest.TestCase):
    def test_no_history_message(self):
        out = format_history("/nonexistent/history.jsonl")
        self.assertIn("No run history recorded", out)

    def test_formats_last_n_runs_most_recent_first(self):
        dirpath = tempfile.mkdtemp()
        try:
            history_path = os.path.join(dirpath, "history.jsonl")
            with open(history_path, "w") as f:
                records = [
                    {"ts": "2026-01-01T00:00:00Z", "run_id": "r1", "name": "foo",
                     "status": "ok", "version_before": "1.0", "version_after": "1.0"},
                    {"ts": "2026-01-01T00:00:00Z", "run_id": "r1", "name": "bar",
                     "status": "fail", "version_before": "2.0", "version_after": "2.0"},
                    {"ts": "2026-02-01T00:00:00Z", "run_id": "r2", "name": "foo",
                     "status": "ok", "version_before": "1.0", "version_after": "1.1"},
                ]
                for r in records:
                    f.write(json.dumps(r) + "\n")
            out = format_history(history_path, n=3)
            self.assertIn("Run r2", out)
            self.assertIn("Run r1", out)
            self.assertIn("foo: 1.0 → 1.1", out)
            self.assertIn("bar", out)
            # r2 (most recent) should appear before r1 in the output.
            self.assertLess(out.index("Run r2"), out.index("Run r1"))
        finally:
            import shutil
            shutil.rmtree(dirpath, ignore_errors=True)

    def test_limits_to_last_n_runs(self):
        dirpath = tempfile.mkdtemp()
        try:
            history_path = os.path.join(dirpath, "history.jsonl")
            with open(history_path, "w") as f:
                for i in range(5):
                    f.write(json.dumps({"ts": "x", "run_id": f"r{i}", "name": "foo",
                                        "status": "ok"}) + "\n")
            out = format_history(history_path, n=2)
            self.assertNotIn("Run r0", out)
            self.assertNotIn("Run r2", out)
            self.assertIn("Run r3", out)
            self.assertIn("Run r4", out)
        finally:
            import shutil
            shutil.rmtree(dirpath, ignore_errors=True)


class TestGroupHistoryByRun(unittest.TestCase):
    def test_contiguous_grouping(self):
        records = [
            {"run_id": "r1", "name": "a"},
            {"run_id": "r1", "name": "b"},
            {"run_id": "r2", "name": "c"},
        ]
        groups = group_history_by_run(records)
        self.assertEqual([g[0] for g in groups], ["r1", "r2"])
        self.assertEqual(len(groups[0][1]), 2)
        self.assertEqual(len(groups[1][1]), 1)


class TestStdoutSignalsUptodate(unittest.TestCase):
    def test_empty_means_uptodate(self):
        self.assertTrue(_stdout_signals_uptodate(""))
        self.assertTrue(_stdout_signals_uptodate("   \n  "))

    def test_empty_json_container_means_uptodate(self):
        self.assertTrue(_stdout_signals_uptodate("[]"))
        self.assertTrue(_stdout_signals_uptodate("{}"))
        self.assertTrue(_stdout_signals_uptodate("  [] \n"))

    def test_nonempty_json_means_outdated(self):
        self.assertFalse(_stdout_signals_uptodate('["pkg-a"]'))
        self.assertFalse(_stdout_signals_uptodate('{"pkg-a": "1.0"}'))

    def test_nonjson_nonempty_means_outdated(self):
        self.assertFalse(_stdout_signals_uptodate("some-pkg 1.0 -> 2.0"))


class TestRunPrechecks(unittest.TestCase):
    def test_check_reports_uptodate_on_empty_output(self):
        cfg = {"known": {}, "bulk": {}, "check": {"npm": "true"}}
        result = run_prechecks(cfg)
        self.assertIn("npm", result)
        self.assertIsInstance(result["npm"], float)

    def test_nonempty_output_is_not_uptodate(self):
        cfg = {"known": {}, "bulk": {}, "check": {"npm": "echo not-empty"}}
        result = run_prechecks(cfg)
        self.assertNotIn("npm", result)

    def test_failed_check_fails_open(self):
        cfg = {"known": {}, "bulk": {}, "check": {"npm": "exit 1"}}
        result = run_prechecks(cfg)
        self.assertNotIn("npm", result)

    def test_missing_check_section_returns_empty(self):
        cfg = {"known": {}, "bulk": {}}
        self.assertEqual(run_prechecks(cfg), {})

    def test_only_and_skip_filters(self):
        cfg = {"known": {}, "bulk": {}, "check": {"npm": "true", "brew": "true"}}
        self.assertEqual(precheck_candidate_origins(cfg, only_origins="npm"), ["npm"])
        self.assertEqual(precheck_candidate_origins(cfg, skip_origins="npm"), ["brew"])


class TestCollectEmitLinesPrecheck(unittest.TestCase):
    def test_uptodate_bulk_origin_becomes_uptodate_line(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "somepkg", "origin": "npm"}], f)
        cfg = {"known": {}, "bulk": {"npm": "npm update -g"}}
        lines = collect_emit_lines(
            cache_path, cfg, None, None,
            precheck_uptodate={"npm": 1.23},
        )
        self.assertEqual(len(lines), 1)
        parts = lines[0].split(EMIT_SEP)
        self.assertEqual(parts[0], "uptodate")
        self.assertEqual(parts[1], "npm")
        self.assertEqual(parts[2], "1.23")

    def test_no_precheck_leaves_bulk_line_untouched(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "somepkg", "origin": "npm"}], f)
        cfg = {"known": {}, "bulk": {"npm": "npm update -g"}}
        lines = collect_emit_lines(cache_path, cfg, None, None)
        self.assertTrue(lines[0].startswith(f"bulk{EMIT_SEP}npm"))


class TestSnapshotVersionsMtimeGate(unittest.TestCase):
    def test_unchanged_mtime_reuses_prior_version_without_probing(self):
        import lib_update_all_clis as m
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        fake_bin = os.path.join(dirpath, "cachedtool")
        with open(fake_bin, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake_bin, 0o755)

        orig_which = m.shutil.which
        m.shutil.which = lambda name: fake_bin if name == "cachedtool" else orig_which(name)
        called = []
        orig_probe = m.probe_known
        m.probe_known = lambda name: called.append(name) or "FRESH"
        try:
            lines = [f"known{EMIT_SEP}cachedtool{EMIT_SEP}echo{EMIT_SEP}npm"]
            prior_path = os.path.join(dirpath, "prior.json")
            mtime = os.stat(fake_bin).st_mtime
            with open(prior_path, "w") as f:
                json.dump({"known": {"cachedtool": "1.0.0"}, "bulk": {},
                           "mtimes": {"cachedtool": mtime}}, f)
            snap = m.snapshot_versions(lines, None, prior_path)
        finally:
            m.shutil.which = orig_which
            m.probe_known = orig_probe
        self.assertEqual(snap["known"]["cachedtool"], "1.0.0")
        self.assertEqual(called, [])

    def test_changed_mtime_triggers_reprobe(self):
        import lib_update_all_clis as m
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        fake_bin = os.path.join(dirpath, "cachedtool")
        with open(fake_bin, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake_bin, 0o755)

        orig_which = m.shutil.which
        m.shutil.which = lambda name: fake_bin if name == "cachedtool" else orig_which(name)
        called = []
        orig_probe = m.probe_known
        m.probe_known = lambda name: called.append(name) or "FRESH"
        try:
            lines = [f"known{EMIT_SEP}cachedtool{EMIT_SEP}echo{EMIT_SEP}npm"]
            prior_path = os.path.join(dirpath, "prior.json")
            with open(prior_path, "w") as f:
                json.dump({"known": {"cachedtool": "1.0.0"}, "bulk": {},
                           "mtimes": {"cachedtool": 1.0}}, f)  # stale mtime
            snap = m.snapshot_versions(lines, None, prior_path)
        finally:
            m.shutil.which = orig_which
            m.probe_known = orig_probe
        self.assertEqual(snap["known"]["cachedtool"], "FRESH")
        self.assertEqual(called, ["cachedtool"])


class TestScanDirEntries(unittest.TestCase):
    def test_excludes_hidden_and_shared_runtime_names(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        for name in ("mytool", ".hidden", "npm", "git-foo"):
            p = os.path.join(dirpath, name)
            with open(p, "w") as f:
                f.write("x")
            os.chmod(p, 0o755)
        names = _scan_dir_entries(dirpath)
        self.assertEqual(names, ["mytool"])

    def test_missing_dir_returns_empty(self):
        self.assertEqual(_scan_dir_entries("/definitely/not/a/real/dir"), [])


class TestIncrementalScanMerge(unittest.TestCase):
    def _mk_dir(self, dirpath, name, files=()):
        d = os.path.join(dirpath, name)
        os.makedirs(d, exist_ok=True)
        for fname in files:
            p = os.path.join(d, fname)
            with open(p, "w") as f:
                f.write("x")
            os.chmod(p, 0o755)
        return d

    def test_first_run_scans_everything(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        bindir = self._mk_dir(dirpath, "bin", files=["footool"])
        cache_path = os.path.join(dirpath, "cache.json")
        rows = [(bindir, "npm", "dir", True)]
        out = json.loads(incremental_scan_merge(rows, cache_path, "2026-01-01T00:00:00Z"))
        tools = [t for t in out if "name" in t]
        self.assertEqual([t["name"] for t in tools], ["footool"])
        self.assertEqual(tools[0]["dir"], bindir)
        mtimes_rec = next(t for t in out if "dir_mtimes" in t)
        self.assertIn(bindir, mtimes_rec["dir_mtimes"])

    def test_unchanged_dir_reuses_cache_without_rescanning(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        bindir = self._mk_dir(dirpath, "bin", files=["footool"])
        cache_path = os.path.join(dirpath, "cache.json")
        rows = [(bindir, "npm", "dir", True)]
        first = incremental_scan_merge(rows, cache_path, "2026-01-01T00:00:00Z")
        with open(cache_path, "w") as f:
            f.write(first)

        # Remove the file on disk WITHOUT touching the directory's mtime by
        # restoring it afterwards — simulate "nothing changed" by just not
        # touching the dir; a second run should still report footool from
        # cache even without re-listing (best proxy: monkeypatch the listdir
        # call to prove it isn't invoked).
        import lib_update_all_clis as m
        orig_listdir = m.os.listdir
        calls = []
        def spy_listdir(path):
            calls.append(path)
            return orig_listdir(path)
        m.os.listdir = spy_listdir
        try:
            second = incremental_scan_merge(rows, cache_path, "2026-01-01T00:01:00Z")
        finally:
            m.os.listdir = orig_listdir
        tools = [t for t in json.loads(second) if "name" in t]
        self.assertEqual([t["name"] for t in tools], ["footool"])
        self.assertEqual(calls, [])  # directory was NOT re-listed

    def test_changed_mtime_triggers_rescan(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        bindir = self._mk_dir(dirpath, "bin", files=["footool"])
        cache_path = os.path.join(dirpath, "cache.json")
        rows = [(bindir, "npm", "dir", True)]
        first = incremental_scan_merge(rows, cache_path, "2026-01-01T00:00:00Z")
        with open(cache_path, "w") as f:
            f.write(first)

        # Add a new binary — changes the directory's mtime.
        newbin = os.path.join(bindir, "bartool")
        with open(newbin, "w") as f:
            f.write("x")
        os.chmod(newbin, 0o755)

        second = json.loads(incremental_scan_merge(rows, cache_path, "2026-01-01T00:01:00Z"))
        names = sorted(t["name"] for t in second if "name" in t)
        self.assertEqual(names, ["bartool", "footool"])

    def test_force_rescan_ignores_stored_mtime(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        bindir = self._mk_dir(dirpath, "bin", files=["footool"])
        cache_path = os.path.join(dirpath, "cache.json")
        rows = [(bindir, "npm", "dir", True)]
        first = incremental_scan_merge(rows, cache_path, "2026-01-01T00:00:00Z")
        with open(cache_path, "w") as f:
            f.write(first)

        import lib_update_all_clis as m
        orig_listdir = m.os.listdir
        calls = []
        def spy_listdir(path):
            calls.append(path)
            return orig_listdir(path)
        m.os.listdir = spy_listdir
        try:
            incremental_scan_merge(rows, cache_path, "2026-01-01T00:01:00Z", force=True)
        finally:
            m.os.listdir = orig_listdir
        self.assertEqual(calls, [bindir])  # forced: re-listed despite unchanged mtime

    def test_disappeared_dir_prunes_cached_tools(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        bindir = self._mk_dir(dirpath, "bin", files=["footool"])
        cache_path = os.path.join(dirpath, "cache.json")
        rows = [(bindir, "npm", "dir", True)]
        first = incremental_scan_merge(rows, cache_path, "2026-01-01T00:00:00Z")
        with open(cache_path, "w") as f:
            f.write(first)

        # Directory no longer exists this run.
        gone_rows = [(bindir, "npm", "dir", False)]
        second = json.loads(incremental_scan_merge(gone_rows, cache_path, "2026-01-01T00:01:00Z"))
        tools = [t for t in second if "name" in t]
        self.assertEqual(tools, [])
        mtimes_rec = next(t for t in second if "dir_mtimes" in t)
        self.assertNotIn(bindir, mtimes_rec["dir_mtimes"])

    def test_carries_forward_untouched_dirs_and_extra_tools(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "untouched", "origin": "manual", "dir": "/some/other/dir"},
            ], f)
        out = json.loads(incremental_scan_merge(
            [], cache_path, "2026-01-01T00:00:00Z",
            extra_tools=[("fnm", "fnm")],
        ))
        names = sorted(t["name"] for t in out if "name" in t)
        self.assertEqual(names, ["fnm", "untouched"])

    def test_tree_mode_scans_one_level_of_bin_subdirs(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cellar = os.path.join(dirpath, "opt")
        self._mk_dir(dirpath, "opt/pkg-a/bin", files=["toola"])
        self._mk_dir(dirpath, "opt/pkg-b/bin", files=["toolb"])
        cache_path = os.path.join(dirpath, "cache.json")
        rows = [(cellar, "brew", "tree", True)]
        out = json.loads(incremental_scan_merge(rows, cache_path, "2026-01-01T00:00:00Z"))
        names = sorted(t["name"] for t in out if "name" in t)
        self.assertEqual(names, ["toola", "toolb"])


class TestParseScanRows(unittest.TestCase):
    def test_parses_tab_separated_rows(self):
        text = "/a/b\tnpm\tdir\t1\n/c/d\tbrew\ttree\t0\n"
        rows = parse_scan_rows(text)
        self.assertEqual(rows, [("/a/b", "npm", "dir", True), ("/c/d", "brew", "tree", False)])

    def test_ignores_blank_lines(self):
        self.assertEqual(parse_scan_rows("\n\n"), [])


class TestNormalizeHoldEntries(unittest.TestCase):
    def test_plain_entries(self):
        self.assertEqual(normalize_hold_entries(["claude", "brew"]), {"claude", "brew"})

    def test_strips_major_suffix(self):
        self.assertEqual(normalize_hold_entries(["claude:major", "brew"]), {"claude", "brew"})

    def test_empty_or_none(self):
        self.assertEqual(normalize_hold_entries(None), set())
        self.assertEqual(normalize_hold_entries([]), set())


class TestEditLocalHold(unittest.TestCase):
    def test_creates_file_and_adds(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        path = os.path.join(dirpath, "config.local.json")
        hold = edit_local_hold(path, add={"claude", "fzf"})
        self.assertEqual(sorted(hold), ["claude", "fzf"])
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(sorted(data["hold"]), ["claude", "fzf"])

    def test_add_is_idempotent(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        path = os.path.join(dirpath, "config.local.json")
        edit_local_hold(path, add={"claude"})
        hold = edit_local_hold(path, add={"claude"})
        self.assertEqual(hold, ["claude"])

    def test_remove_strips_major_suffix_match(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        path = os.path.join(dirpath, "config.local.json")
        edit_local_hold(path, add={"claude:major"})
        hold = edit_local_hold(path, remove={"claude"})
        self.assertEqual(hold, [])

    def test_preserves_other_keys(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        path = os.path.join(dirpath, "config.local.json")
        with open(path, "w") as f:
            json.dump({"known": {"foo": "foo update"}}, f)
        edit_local_hold(path, add={"claude"})
        with open(path) as f:
            data = json.load(f)
        self.assertEqual(data["known"], {"foo": "foo update"})
        self.assertEqual(data["hold"], ["claude"])


class TestCollectEmitLinesHeld(unittest.TestCase):
    def _cache(self, tools):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump(tools, f)
        return cache_path

    def test_config_held_known_tool_becomes_held_line(self):
        cache_path = self._cache([{"name": "claude", "origin": "manual"}])
        cfg = {"known": {"claude": "claude update"}, "bulk": {}}
        lines = collect_emit_lines(cache_path, cfg, None, None, held_config={"claude"})
        self.assertEqual(len(lines), 1)
        parts = lines[0].split(EMIT_SEP)
        self.assertEqual(parts[0], "held")
        self.assertEqual(parts[1], "claude")
        self.assertEqual(parts[2], "config")

    def test_adhoc_held_bulk_origin_becomes_held_line(self):
        cache_path = self._cache([{"name": "somepkg", "origin": "npm"}])
        cfg = {"known": {}, "bulk": {"npm": "npm update -g"}}
        lines = collect_emit_lines(cache_path, cfg, None, None, held_adhoc={"npm"})
        self.assertEqual(len(lines), 1)
        parts = lines[0].split(EMIT_SEP)
        self.assertEqual(parts[0], "held")
        self.assertEqual(parts[1], "npm")
        self.assertEqual(parts[2], "env")

    def test_config_hold_takes_precedence_over_adhoc(self):
        cache_path = self._cache([{"name": "claude", "origin": "manual"}])
        cfg = {"known": {"claude": "claude update"}, "bulk": {}}
        lines = collect_emit_lines(
            cache_path, cfg, None, None, held_config={"claude"}, held_adhoc={"claude"},
        )
        parts = lines[0].split(EMIT_SEP)
        self.assertEqual(parts[2], "config")

    def test_not_held_runs_normally(self):
        cache_path = self._cache([{"name": "claude", "origin": "manual"}])
        cfg = {"known": {"claude": "claude update"}, "bulk": {}}
        lines = collect_emit_lines(cache_path, cfg, None, None)
        self.assertTrue(lines[0].startswith(f"known{EMIT_SEP}claude"))

    def test_held_overrides_quarantine(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = self._cache([{"name": "claude", "origin": "manual"}])
        history_path = os.path.join(dirpath, "history.jsonl")
        with open(history_path, "w") as f:
            for _ in range(3):
                f.write(json.dumps({"name": "claude", "status": "fail", "duration_s": 1}) + "\n")
        cfg = {"known": {"claude": "claude update"}, "bulk": {}}
        lines = collect_emit_lines(
            cache_path, cfg, None, None,
            history_path=history_path, held_config={"claude"},
        )
        parts = lines[0].split(EMIT_SEP)
        self.assertEqual(parts[0], "held")


class TestVersionHelpers(unittest.TestCase):
    def test_leading_major_parses_leading_digits(self):
        self.assertEqual(leading_major("2.1.139"), 2)
        self.assertEqual(leading_major("v3.0.0"), 3)

    def test_leading_major_unparseable_is_none(self):
        self.assertIsNone(leading_major("?"))
        self.assertIsNone(leading_major(""))
        self.assertIsNone(leading_major(None))

    def test_is_major_upgrade_true(self):
        self.assertTrue(is_major_upgrade("1.9.0", "2.0.0"))

    def test_is_major_upgrade_false_same_major(self):
        self.assertFalse(is_major_upgrade("1.9.0", "1.10.0"))

    def test_is_major_upgrade_false_when_unparseable(self):
        self.assertFalse(is_major_upgrade("?", "2.0.0"))
        self.assertFalse(is_major_upgrade("1.0.0", "?"))


class TestFormatRunSummaryHeldAndMajor(unittest.TestCase):
    def test_held_section_lists_names(self):
        out = format_run_summary({}, {}, 1, 0, held=["claude", "fzf"])
        self.assertIn("Held (pinned in config), skipped this run (2):", out)
        self.assertIn("claude, fzf", out)

    def test_held_none_shows_none_placeholder(self):
        out = format_run_summary({}, {}, 1, 0)
        self.assertIn("Held (pinned in config), skipped this run (0):", out)

    def test_major_upgrade_marker(self):
        before = {"known": {"claude": "1.9.0"}}
        after = {"known": {"claude": "2.0.0"}}
        out = format_run_summary(before, after, 1, 0)
        self.assertIn("MAJOR UPGRADE", out)

    def test_no_marker_for_minor_upgrade(self):
        before = {"known": {"claude": "1.9.0"}}
        after = {"known": {"claude": "1.10.0"}}
        out = format_run_summary(before, after, 1, 0)
        self.assertNotIn("MAJOR UPGRADE", out)


class TestHistoryAppendHeld(unittest.TestCase):
    def test_held_line_recorded_with_held_status(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        history_path = os.path.join(dirpath, "history.jsonl")
        line = f"held{EMIT_SEP}claude{EMIT_SEP}config{EMIT_SEP}3{EMIT_SEP}100{EMIT_SEP}100"
        appended = history_append(history_path, "run1", [line], {}, {})
        self.assertEqual(appended, 1)
        with open(history_path) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["status"], "held")
        self.assertTrue(rec["held"])
        self.assertEqual(rec["name"], "claude")

    def test_held_records_dont_trigger_quarantine(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        history_path = os.path.join(dirpath, "history.jsonl")
        line = f"held{EMIT_SEP}claude{EMIT_SEP}config{EMIT_SEP}3{EMIT_SEP}100{EMIT_SEP}100"
        for _ in range(3):
            history_append(history_path, "run1", [line], {}, {})
        by_name = load_history_by_name(history_path)
        self.assertEqual(quarantined_names(by_name, 3), set())


class TestDoctorBrokenSymlinks(unittest.TestCase):
    def test_detects_broken_symlink(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        broken = os.path.join(dirpath, "broken")
        os.symlink("/definitely/not/a/real/target", broken)
        good_target = os.path.join(dirpath, "target")
        with open(good_target, "w") as f:
            f.write("x")
        good_link = os.path.join(dirpath, "good")
        os.symlink(good_target, good_link)
        found = doctor_broken_symlinks([dirpath])
        self.assertEqual(found, [broken])

    def test_missing_dir_is_skipped(self):
        self.assertEqual(doctor_broken_symlinks(["/definitely/not/a/real/dir"]), [])


class TestDoctorShadowedDuplicates(unittest.TestCase):
    def _write_cache(self, dirpath, entries):
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump(entries, f)
        return cache_path

    def test_distinct_real_files_reported(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        dir_a = os.path.join(dirpath, "a")
        dir_b = os.path.join(dirpath, "b")
        os.makedirs(dir_a)
        os.makedirs(dir_b)
        for d in (dir_a, dir_b):
            with open(os.path.join(d, "foo"), "w") as f:
                f.write(d)  # distinct contents, distinct real files
        cache_path = self._write_cache(dirpath, [
            {"name": "foo", "origin": "npm", "dir": dir_a},
            {"name": "foo", "origin": "path", "dir": dir_b},
            {"name": "bar", "origin": "npm", "dir": dir_a},
        ])
        dupes = doctor_shadowed_duplicates(cache_path)
        self.assertEqual(len(dupes), 1)
        self.assertEqual(dupes[0]["name"], "foo")
        self.assertEqual(dupes[0]["origins"], ["npm", "path"])
        self.assertEqual(
            dupes[0]["paths"],
            sorted([os.path.realpath(os.path.join(dir_a, "foo")),
                    os.path.realpath(os.path.join(dir_b, "foo"))]),
        )

    def test_same_real_file_via_two_origins_not_reported(self):
        # An npm global seen by both the npm query and the $PATH scan is
        # one file, not shadowing — the pre-fix check false-positived here.
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        dir_a = os.path.join(dirpath, "a")
        dir_b = os.path.join(dirpath, "b")
        os.makedirs(dir_a)
        os.makedirs(dir_b)
        real = os.path.join(dir_a, "foo")
        with open(real, "w") as f:
            f.write("x")
        os.symlink(real, os.path.join(dir_b, "foo"))
        cache_path = self._write_cache(dirpath, [
            {"name": "foo", "origin": "npm", "dir": dir_a},
            {"name": "foo", "origin": "path", "dir": dir_b},
        ])
        self.assertEqual(doctor_shadowed_duplicates(cache_path), [])

    def test_entries_without_dirs_not_reported(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = self._write_cache(dirpath, [
            {"name": "foo", "origin": "npm"},
            {"name": "foo", "origin": "path"},
        ])
        self.assertEqual(doctor_shadowed_duplicates(cache_path), [])

    def test_missing_cache_returns_empty(self):
        self.assertEqual(doctor_shadowed_duplicates("/definitely/not/a/real/cache.json"), [])


class TestDoctorChronicFailures(unittest.TestCase):
    def test_three_or_more_failures_in_window(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        history_path = os.path.join(dirpath, "history.jsonl")
        with open(history_path, "w") as f:
            statuses = ["fail", "ok", "fail", "ok", "fail"]
            for s in statuses:
                f.write(json.dumps({"name": "flaky", "status": s}) + "\n")
        result = doctor_chronic_failures(history_path)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "flaky")
        self.assertEqual(result[0]["failures"], 3)

    def test_below_threshold_not_reported(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        history_path = os.path.join(dirpath, "history.jsonl")
        with open(history_path, "w") as f:
            for s in ["fail", "ok", "ok"]:
                f.write(json.dumps({"name": "flaky", "status": s}) + "\n")
        self.assertEqual(doctor_chronic_failures(history_path), [])


class TestDoctorConfigIssues(unittest.TestCase):
    def test_hold_entry_matching_nothing(self):
        cfg = {"known": {}, "bulk": {}, "hold": ["ghost"]}
        issues = doctor_config_issues(cfg)
        self.assertTrue(any("ghost" in i for i in issues))

    def test_hold_entry_matching_known_is_fine(self):
        cfg = {"known": {"claude": "claude update"}, "bulk": {}, "hold": ["claude"]}
        issues = doctor_config_issues(cfg)
        self.assertFalse(any("claude" in i for i in issues))

    def test_check_without_bulk_command_reported(self):
        cfg = {"known": {}, "bulk": {}, "check": {"npm": "npm outdated"}}
        issues = doctor_config_issues(cfg)
        self.assertTrue(any("npm" in i for i in issues))

    def test_check_with_bulk_command_is_fine(self):
        cfg = {"known": {}, "bulk": {"npm": "npm update -g"}, "check": {"npm": "npm outdated"}}
        issues = doctor_config_issues(cfg)
        self.assertFalse(any("npm" in i for i in issues))

    def test_known_entry_missing_binary_is_informational_not_issue(self):
        # Known entries are a catalog of tools you *might* install; the
        # updater skips absent ones, so they're not config issues.
        cfg = {"known": {"definitely-not-a-real-binary-xyz": "echo hi"}, "bulk": {}}
        self.assertEqual(doctor_config_issues(cfg), [])
        self.assertEqual(
            doctor_not_installed(cfg), ["definitely-not-a-real-binary-xyz"])

    def test_not_installed_excludes_present_binaries(self):
        cfg = {"known": {"sh": "true"}, "bulk": {}}  # /bin/sh always exists
        self.assertEqual(doctor_not_installed(cfg), [])


class TestDoctorIgnore(unittest.TestCase):
    def _cache_with_real_shadow(self, dirpath, name):
        dir_a = os.path.join(dirpath, "a")
        dir_b = os.path.join(dirpath, "b")
        os.makedirs(dir_a)
        os.makedirs(dir_b)
        for d in (dir_a, dir_b):
            with open(os.path.join(d, name), "w") as f:
                f.write(d)
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": name, "origin": "npm", "dir": dir_a},
                {"name": name, "origin": "path", "dir": dir_b},
            ], f)
        return cache_path

    def test_ignored_shadow_moved_to_informational(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = self._cache_with_real_shadow(dirpath, "wrapped")
        cfg = {"known": {}, "bulk": {}, "doctor_ignore": ["wrapped"]}
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            report = doctor_report(cache_path, cfg, history_path=os.path.join(dirpath, "h.jsonl"))
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(report["shadowed_duplicates"], [])
        self.assertEqual(report["ignored_shadows"], ["wrapped"])
        self.assertFalse(doctor_has_findings(report))

    def test_doctor_ignore_merged_additively(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        base = os.path.join(dirpath, "base.json")
        local = os.path.join(dirpath, "local.json")
        with open(base, "w") as f:
            json.dump({"known": {}, "bulk": {}, "doctor_ignore": ["a"]}, f)
        with open(local, "w") as f:
            json.dump({"doctor_ignore": ["b", "a"]}, f)
        cfg = load_merge(base, local)
        self.assertEqual(cfg["doctor_ignore"], ["a", "b"])
        validate(cfg)

    def test_doctor_ignore_validation(self):
        with self.assertRaises(ValueError):
            validate({"known": {}, "bulk": {}, "doctor_ignore": "notalist"})
        with self.assertRaises(ValueError):
            validate({"known": {}, "bulk": {}, "doctor_ignore": [""]})


class TestDoctorReport(unittest.TestCase):
    def test_isolates_check_failures(self):
        import lib_update_all_clis as m
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "foo", "origin": "npm"}], f)

        orig = m.doctor_broken_symlinks
        m.doctor_broken_symlinks = lambda dirs: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            report = doctor_report(cache_path, {"known": {}, "bulk": {}}, history_path=None)
        finally:
            m.doctor_broken_symlinks = orig
        self.assertTrue(any("broken symlink" in e for e in report["errors"]))
        # Other checks still ran despite the crash.
        self.assertIn("cache_validation", report)
        self.assertIn("config_issues", report)

    def test_no_findings_means_clean(self):
        # PATH is cleared for this check so broken symlinks / shadowed dups
        # elsewhere on the real dev machine's PATH don't leak into the
        # "clean" case and make this test environment-dependent.
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "foo", "origin": "npm"}, {"scanned_at": "x", "count": 1}], f)
        history_path = os.path.join(dirpath, "history.jsonl")
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            report = doctor_report(cache_path, {"known": {}, "bulk": {"npm": "npm update -g"}}, history_path=history_path)
        finally:
            os.environ["PATH"] = old_path
        self.assertFalse(doctor_has_findings(report))

    def test_findings_when_config_issue_present(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        cache_path = os.path.join(dirpath, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "foo", "origin": "npm"}, {"scanned_at": "x", "count": 1}], f)
        history_path = os.path.join(dirpath, "history.jsonl")
        report = doctor_report(
            cache_path,
            {"known": {}, "bulk": {}, "check": {"npm": "npm outdated"}},
            history_path=history_path,
        )
        self.assertTrue(doctor_has_findings(report))

    def test_format_doctor_report_runs_without_error(self):
        report = doctor_report(
            "/definitely/not/a/real/cache.json", {"known": {}, "bulk": {}}, history_path=None,
        )
        text = format_doctor_report(report)
        self.assertIn("update-all-clis doctor report", text)


class TestChangelogTagMatching(unittest.TestCase):
    def test_tag_to_version_strips_v(self):
        self.assertEqual(tag_to_version("v1.2.3"), "1.2.3")
        self.assertEqual(tag_to_version("1.2.3"), "1.2.3")

    def test_tag_in_range_matches(self):
        self.assertTrue(tag_in_range("v1.2.0", "1.0.0", "1.2.0"))
        self.assertTrue(tag_in_range("v1.1.5", "1.0.0", "1.2.0"))

    def test_tag_in_range_excludes_before(self):
        self.assertFalse(tag_in_range("v1.0.0", "1.0.0", "1.2.0"))

    def test_tag_in_range_excludes_after_upper_bound(self):
        self.assertFalse(tag_in_range("v1.3.0", "1.0.0", "1.2.0"))

    def test_tag_in_range_unparseable_tag_excluded(self):
        self.assertFalse(tag_in_range("release-notes", "1.0.0", "1.2.0"))

    def test_tag_in_range_no_before_only_checks_upper(self):
        self.assertTrue(tag_in_range("v0.5.0", None, "1.2.0"))
        self.assertFalse(tag_in_range("v2.0.0", None, "1.2.0"))


class TestChangelogFormatting(unittest.TestCase):
    def test_truncate_short_body_unchanged(self):
        self.assertEqual(truncate_changelog_body("short"), "short")

    def test_truncate_long_body(self):
        body = "x" * 500
        out = truncate_changelog_body(body, limit=400)
        self.assertEqual(len(out), 401)  # 400 chars + ellipsis
        self.assertTrue(out.endswith("…"))

    def test_truncate_none_is_empty(self):
        self.assertEqual(truncate_changelog_body(None), "")

    def test_format_changelog_section_empty(self):
        self.assertEqual(format_changelog_section([]), "")

    def test_format_changelog_section_basic(self):
        entries = [{
            "name": "fzf", "version_before": "0.72.0", "version_after": "0.74.0",
            "releases": [{"tag": "v0.74.0", "body": "Fixed bugs"}],
        }]
        out = format_changelog_section(entries)
        self.assertIn("Changelog highlights:", out)
        self.assertIn("fzf (0.72.0 → 0.74.0):", out)
        self.assertIn("[v0.74.0] Fixed bugs", out)

    def test_format_changelog_section_capped_note(self):
        entries = [{
            "name": "fzf", "version_before": "0.72.0", "version_after": "0.74.0",
            "releases": [{"tag": "v0.74.0", "body": "x"}],
        }]
        out = format_changelog_section(entries, capped=True, cap=5)
        self.assertIn("capped at 5 tools", out)


class TestChangedToolsWithRepos(unittest.TestCase):
    def test_only_changed_tools_with_repo_mapping_included(self):
        before = {"known": {"fzf": "0.72.0", "claude": "1.0.0"}}
        after = {"known": {"fzf": "0.74.0", "claude": "1.0.0"}}
        repos = {"fzf": "junegunn/fzf", "claude": "anthropics/claude-code"}
        changed = changed_tools_with_repos(before, after, repos)
        self.assertEqual(changed, [("fzf", "0.72.0", "0.74.0")])

    def test_unknown_version_excluded(self):
        before = {"known": {"fzf": "?"}}
        after = {"known": {"fzf": "0.74.0"}}
        repos = {"fzf": "junegunn/fzf"}
        self.assertEqual(changed_tools_with_repos(before, after, repos), [])

    def test_no_repo_mapping_excluded(self):
        before = {"known": {"unmapped": "1.0.0"}}
        after = {"known": {"unmapped": "2.0.0"}}
        self.assertEqual(changed_tools_with_repos(before, after, {}), [])


class TestBuildChangelogDigest(unittest.TestCase):
    def test_no_network_uses_injected_fetch(self):
        before = {"known": {"fzf": "0.72.0"}}
        after = {"known": {"fzf": "0.74.0"}}
        cfg = {"repos": {"fzf": "junegunn/fzf"}}

        def fake_fetch(slug, timeout=8.0):
            self.assertEqual(slug, "junegunn/fzf")
            return [{"tag_name": "v0.74.0", "body": "release notes here"}]

        out = build_changelog_digest(before, after, cfg, fetch=fake_fetch)
        self.assertIn("fzf (0.72.0 → 0.74.0)", out)
        self.assertIn("release notes here", out)

    def test_no_changed_tools_returns_empty(self):
        cfg = {"repos": {"fzf": "junegunn/fzf"}}
        self.assertEqual(build_changelog_digest({}, {}, cfg, fetch=lambda *a, **k: []), "")

    def test_fetch_failure_degrades_silently(self):
        before = {"known": {"fzf": "0.72.0"}}
        after = {"known": {"fzf": "0.74.0"}}
        cfg = {"repos": {"fzf": "junegunn/fzf"}}

        def failing_fetch(slug, timeout=8.0):
            raise RuntimeError("offline")

        out = build_changelog_digest(before, after, cfg, fetch=failing_fetch)
        self.assertEqual(out, "")

    def test_caps_at_max_tools(self):
        before = {"known": {f"tool{i}": "1.0.0" for i in range(7)}}
        after = {"known": {f"tool{i}": "2.0.0" for i in range(7)}}
        cfg = {"repos": {f"tool{i}": f"owner/tool{i}" for i in range(7)}}
        calls = []

        def fake_fetch(slug, timeout=8.0):
            calls.append(slug)
            return [{"tag_name": "v2.0.0", "body": "notes"}]

        out = build_changelog_digest(before, after, cfg, max_tools=5, fetch=fake_fetch)
        self.assertEqual(len(calls), 5)
        self.assertIn("capped at 5 tools", out)


class TestValidateHoldAndRepos(unittest.TestCase):
    def test_valid_hold_and_repos(self):
        cfg = {"known": {}, "bulk": {}, "hold": ["claude"], "repos": {"fzf": "junegunn/fzf"}}
        validate(cfg)  # should not raise

    def test_invalid_hold_type_raises(self):
        cfg = {"known": {}, "bulk": {}, "hold": "claude"}
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_invalid_hold_entry_raises(self):
        cfg = {"known": {}, "bulk": {}, "hold": [123]}
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_invalid_repos_type_raises(self):
        cfg = {"known": {}, "bulk": {}, "repos": "nope"}
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_invalid_repos_value_missing_slash_raises(self):
        cfg = {"known": {}, "bulk": {}, "repos": {"fzf": "junegunn-fzf"}}
        with self.assertRaises(ValueError):
            validate(cfg)


class TestLoadMergeHold(unittest.TestCase):
    def test_local_hold_adds_to_base(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        base_path = os.path.join(dirpath, "base.json")
        local_path = os.path.join(dirpath, "local.json")
        with open(base_path, "w") as f:
            json.dump({"known": {}, "bulk": {}, "hold": ["claude"]}, f)
        with open(local_path, "w") as f:
            json.dump({"hold": ["fzf"]}, f)
        cfg = load_merge(base_path, local_path)
        self.assertEqual(sorted(cfg["hold"]), ["claude", "fzf"])

    def test_local_hold_dedups(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        base_path = os.path.join(dirpath, "base.json")
        local_path = os.path.join(dirpath, "local.json")
        with open(base_path, "w") as f:
            json.dump({"known": {}, "bulk": {}, "hold": ["claude"]}, f)
        with open(local_path, "w") as f:
            json.dump({"hold": ["claude"]}, f)
        cfg = load_merge(base_path, local_path)
        self.assertEqual(cfg["hold"], ["claude"])

    def test_local_repos_merge(self):
        dirpath = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(dirpath, ignore_errors=True))
        base_path = os.path.join(dirpath, "base.json")
        local_path = os.path.join(dirpath, "local.json")
        with open(base_path, "w") as f:
            json.dump({"known": {}, "bulk": {}, "repos": {"fzf": "junegunn/fzf"}}, f)
        with open(local_path, "w") as f:
            json.dump({"repos": {"gh": "cli/cli"}}, f)
        cfg = load_merge(base_path, local_path)
        self.assertEqual(cfg["repos"], {"fzf": "junegunn/fzf", "gh": "cli/cli"})


class TestNewOrigins(unittest.TestCase):
    """Tests for newly added origins (pip, asdf, proto, volta, rye, foundry, aqua, mason)."""

    def _cache_with_tool(self, name, origin):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cache_path = os.path.join(tmp, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": name, "origin": origin, "dir": "/fake"}], f)
        return cache_path

    def test_asdf_origin_emits_bulk(self):
        cache = self._cache_with_tool("node", "asdf")
        cfg = {"known": {}, "bulk": {"asdf": "asdf update 2>/dev/null || true"}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "asdf"), kinds)

    def test_proto_origin_emits_bulk(self):
        cache = self._cache_with_tool("node", "proto")
        cfg = {"known": {}, "bulk": {"proto": "proto update 2>/dev/null || true"}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "proto"), kinds)

    def test_volta_origin_emits_bulk(self):
        cache = self._cache_with_tool("node", "volta")
        cfg = {"known": {}, "bulk": {"volta": "volta update 2>/dev/null || true"}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "volta"), kinds)

    def test_pip_origin_emits_bulk(self):
        cache = self._cache_with_tool("some-tool", "pip")
        cfg = {"known": {}, "bulk": {"pip": "pip3 install --upgrade pip 2>/dev/null || true"}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "pip"), kinds)

    def test_rye_origin_emits_bulk(self):
        cache = self._cache_with_tool("python", "rye")
        cfg = {"known": {}, "bulk": {"rye": "rye self update 2>/dev/null || true"}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "rye"), kinds)

    def test_foundry_origin_emits_bulk(self):
        cache = self._cache_with_tool("forge", "foundry")
        cfg = {"known": {}, "bulk": {"foundry": "foundryup 2>/dev/null || true"}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "foundry"), kinds)

    def test_aqua_origin_emits_bulk(self):
        cache = self._cache_with_tool("aqua-tool", "aqua")
        cfg = {"known": {}, "bulk": {"aqua": "aqua update 2>/dev/null || true"}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("bulk", "aqua"), kinds)

    def test_mason_origin_empty_cmd_skips(self):
        cache = self._cache_with_tool("lua-language-server", "mason")
        cfg = {"known": {}, "bulk": {"mason": ""}}
        lines = collect_emit_lines(cache, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("skip", "lua-language-server"), kinds)

    def test_new_origins_lock_groups(self):
        self.assertEqual(lock_group_for("asdf", "", "x"), "asdf")
        self.assertEqual(lock_group_for("proto", "", "x"), "proto")
        self.assertEqual(lock_group_for("volta", "", "x"), "volta")
        self.assertEqual(lock_group_for("pip", "", "x"), "pip")
        self.assertEqual(lock_group_for("rye", "", "x"), "rye")
        self.assertEqual(lock_group_for("foundry", "", "x"), "foundry")
        self.assertEqual(lock_group_for("aqua", "", "x"), "aqua")
        self.assertEqual(lock_group_for("mason", "", "x"), "mason")


class TestQwenKnown(unittest.TestCase):
    """qwen is in the known list with a self-update command."""

    def test_qwen_known_emits_known_line(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cache_path = os.path.join(tmp, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([{"name": "qwen", "origin": "npm", "dir": "/fake"}], f)
        cfg = {
            "known": {"qwen": "qwen update 2>/dev/null || true"},
            "bulk": {"npm": "npm update -g"},
        }
        lines = collect_emit_lines(cache_path, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("known", "qwen"), kinds)

    def test_qwen_known_does_not_suppress_npm_bulk(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        cache_path = os.path.join(tmp, "cache.json")
        with open(cache_path, "w") as f:
            json.dump([
                {"name": "qwen", "origin": "npm", "dir": "/fake"},
                {"name": "other-npm-tool", "origin": "npm", "dir": "/fake"},
            ], f)
        cfg = {
            "known": {"qwen": "qwen update 2>/dev/null || true"},
            "bulk": {"npm": "npm update -g"},
        }
        lines = collect_emit_lines(cache_path, cfg, None, None)
        kinds = [(l.split(EMIT_SEP)[0], l.split(EMIT_SEP)[1]) for l in lines]
        self.assertIn(("known", "qwen"), kinds)
        self.assertIn(("bulk", "npm"), kinds)


if __name__ == "__main__":
    unittest.main()