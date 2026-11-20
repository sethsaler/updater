import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lib_update_all_clis import collect_emit_lines, EMIT_SEP, _effective_origin
from scripts import update_uv


ROOT = Path(__file__).resolve().parents[1]
BULK = json.loads((ROOT / "tool_config.json").read_text())["bulk"]


class PackageSweeps(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bin = Path(directory.name) / "bin"
        self.bin.mkdir()
        self.log = Path(directory.name) / "calls"
        self.env = {
            **os.environ,
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "LIB_SCRIPT": str(ROOT / "lib_update_all_clis.py"),
            "CALL_LOG": str(self.log),
            "BUN_INSTALL": directory.name,
        }

    def sweep(self, name, behavior, **env):
        binary = self.bin / name
        binary.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n' + behavior)
        binary.chmod(0o755)
        result = subprocess.run(
            ["bash", "-c", BULK[name]], env={**self.env, **env},
            capture_output=True, text=True
        )
        return result, self.log.read_text().splitlines()

    def test_brew_upgrade_failure_reaches_executor(self):
        result, calls = self.sweep("brew", '[[ "$1" == update ]] && exit 0\necho Failure; exit 7')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(calls, ["update", "upgrade --greedy-auto-updates --no-quit"])
        self.assertEqual(BULK["manual"], "")

    def test_claude_science_defers_only_for_sign_in(self):
        cli = self.bin / "claude-science"
        cli.write_text('#!/bin/sh\nprintf "%s\\n" "$VENDOR_MESSAGE" >&2\nexit 9\n')
        cli.chmod(0o755)
        for message, status in (("update: not signed in to claude.ai", 0), ("checksum mismatch", 9)):
            run = subprocess.run(["bash", str(ROOT / "scripts/update_packages.sh"), "claude-science"],
                                 env={**self.env, "VENDOR_MESSAGE": message}, capture_output=True, text=True)
            self.assertEqual(run.returncode, status)
            self.assertIn("Deferred claude-science" if status == 0 else message, run.stdout + run.stderr)

    def test_standalone_and_shared_venv_ownership(self):
        home = self.bin.parent
        local = home / ".local/bin"
        local.mkdir(parents=True)
        venv = home / ".local/share/jevgram/.venv/bin"
        venv.mkdir(parents=True)
        with patch.dict(os.environ, {"HOME": str(home), "PATH": str(local)}), patch("sys.platform", "darwin"):
            for name in ("jevgram", "jevgram-web"):
                entry = venv / name
                entry.write_text("#!/bin/sh\n")
                entry.chmod(0o755)
                (local / name).write_text(f'#!/bin/sh\nexec "{entry}" "$@"\n')
                self.assertEqual(_effective_origin({"origin": "uv/pip", "dir": str(local), "name": name}), "jevgram")
            cli = local / "gemini-api"
            cli.write_text("#!/bin/sh\n")
            cli.chmod(0o755)
            row = {"origin": "uv/pip", "dir": str(local), "name": cli.name}
            self.assertEqual(_effective_origin(row), "gemini-api")
            cli.unlink()
            cli.symlink_to(venv / "jevgram")
            self.assertEqual(_effective_origin(row), "path")

    def test_browser_use_aliases_require_vendor_launchers(self):
        home = self.bin.parent
        local = home / ".local/bin"
        local.mkdir(parents=True)
        binary = home / ".browser-use-terminal/packages/standalone/current/bin/browser-use-terminal"
        binary.parent.mkdir(parents=True)
        binary.touch(mode=0o755)
        script = b'#!/bin/sh\nCURRENT="$BUT_HOME_DIR/packages/standalone/current"\n' \
                 b'export BUT_RELEASE_REPO="${BUT_RELEASE_REPO:-browser-use/terminal}"\n'
        with patch.dict(os.environ, {"HOME": str(home), "BUT_HOME": ""}):
            for name in ("browser", "browser-use", "but", "browser-use-terminal"):
                row = {"name": name, "origin": "uv/pip", "dir": str(local)}
                (local / name).write_bytes(script + b'exec "$CURRENT/bin/but" "$@"\n')
                self.assertEqual(_effective_origin(row), "browser-use-terminal")
                (local / name).write_text("#!/bin/sh\necho independent tool\n")
                self.assertEqual(_effective_origin(row), "path")

    def test_cargo_plugin_updates_registry_and_git_tools(self):
        result, calls = self.sweep("cargo", '[[ "$*" != "install --list" ]] || echo "crate v1:"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["install --list", "install-update --version", "install-update --all --git"])

    def test_dedicated_installs_coalesce_matching_launchers_only(self):
        home = self.bin.parent.resolve()
        local = home / ".local/bin"
        local.mkdir(parents=True)
        headlong = home / ".headlong/app"
        for directory, name in (("bin", "llm"), ("tools", "headlong-init")):
            target = headlong / directory / name
            target.parent.mkdir(parents=True)
            target.touch()
            target.chmod(0o755)
            (local / name).symlink_to(target)
        binary = home / ".openinterpreter/packages/standalone/current/interpreter"
        binary.parent.mkdir(parents=True)
        binary.touch()
        binary.chmod(0o755)
        for name in ("i", "interpreter"):
            (local / name).write_text(f'#!/bin/sh\nexec "{binary}" "$@"\n')
        names = ["llm", "headlong-init", "i", "interpreter"]
        cache = home / "cache.json"
        rows = [{"name": n, "dir": str(local), "origin": "uv/pip"} for n in names]
        cache.write_text(json.dumps(rows))
        with patch.dict(os.environ, {"HOME": str(home), "HEADLONG_HOME": str(headlong.parent)}):
            cfg = {"known": {}, "bulk": {"headlong": "headlong update", "openinterpreter": "interpreter update now"}}
            jobs = collect_emit_lines(str(cache), cfg, None, None)
            self.assertEqual([j.split(EMIT_SEP)[1] for j in jobs], ["headlong", "openinterpreter"])
            (local / "i").write_text("#!/bin/sh\necho unrelated\n")
            self.assertEqual(_effective_origin(rows[2]), "path")

    def test_openinterpreter_layout_migration_preserves_one_vendor_job(self):
        home = self.bin.parent.resolve()
        local = home / ".local/bin"
        local.mkdir(parents=True)
        current = home / ".openinterpreter/packages/standalone/current"
        current.mkdir(parents=True)
        legacy = current / "interpreter"
        legacy.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n')
        legacy.chmod(0o755)
        env = {**self.env, "HOME": str(home)}
        subprocess.run(["bash", "-c", BULK["openinterpreter"]], env=env, check=True)

        bindir = current / "bin"
        bindir.mkdir()
        names = ("i", "interpreter", "codex-code-mode-host")
        rows = []
        for name in names:
            binary = bindir / name
            binary.write_text(legacy.read_text())
            binary.chmod(0o755)
            target = "interpreter" if name == "i" else name
            (local / name).symlink_to(bindir / target)
            rows.append({"name": name, "dir": str(local), "origin": "uv/pip"})
        legacy.unlink()

        cfg = json.loads((ROOT / "tool_config.json").read_text())
        cache = home / "cache.json"
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertTrue(all(_effective_origin(row) == "openinterpreter" for row in rows))
            dirs = {Path(os.path.expandvars(entry["dir"])) for entry in cfg["scan_dirs"]
                    if entry["origin"] == "openinterpreter"}
            self.assertEqual(dirs, {current, bindir})
            cache.write_text(json.dumps(rows + [{"name": name, "dir": str(bindir), "origin": "openinterpreter"}
                                                for name in names]))
            jobs = collect_emit_lines(str(cache), cfg, "openinterpreter", None)
            self.assertEqual([j.split(EMIT_SEP)[1] for j in jobs], ["openinterpreter"])
            helper = local / "codex-code-mode-host"
            helper.unlink()
            helper.write_text("#!/bin/sh\n")
            self.assertEqual(_effective_origin(rows[-1]), "path")
        subprocess.run(["bash", "-c", BULK["openinterpreter"]], env=env, check=True)
        self.assertEqual(self.log.read_text().splitlines(), ["update now", "update"])

    def test_headlong_only_pulls_clean_vendor_upstream(self):
        checkout = self.bin.parent / ".headlong/app"
        (checkout / ".git").mkdir(parents=True)
        git = self.bin / "git"
        git.write_text('''#!/bin/bash
shift 2
case "$1" in
  symbolic-ref) echo custom-branch ;;
  config) [[ "$*" == 'config --get branch.custom-branch.remote' ]] && echo upstream ;;
  remote) [[ "$*" == 'remote get-url upstream' ]] && echo "$TEST_REMOTE" ;;
  status) printf '%s' "$TEST_CHANGES" ;;
  pull) echo "$*" >> "$CALL_LOG" ;;
  *) exit 1 ;;
esac
''')
        git.chmod(0o755)
        vendor = "https://github.com/laude-institute/headlong.git"
        for remote, changes, status in ((vendor, " M changed", 1), ("https://example.org/other", "", 1),
                                        (vendor, "", 0)):
            result = subprocess.run(["bash", "-c", BULK["headlong"]],
                                    env={**self.env, "HEADLONG_HOME": str(checkout.parent),
                                         "TEST_REMOTE": remote, "TEST_CHANGES": changes},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, status, result.stderr)
        self.assertEqual(self.log.read_text().splitlines(), ["pull --ff-only --no-autostash"])

    def test_nexus_recovers_partial_installs_and_skips_current_dependencies(self):
        home = self.bin.parent
        checkout = home / ".nexus/cli"
        (checkout / ".git").mkdir(parents=True)
        setup = checkout / "packages/setup-cli"
        setup.mkdir(parents=True)
        manifest = {"dependencies": {"example": "^1"}}
        (setup / "package.json").write_text(json.dumps(manifest))
        lock = {"packages": {"": manifest, "node_modules/example": {"version": "1.2.0"}}}
        (setup / "package-lock.json").write_text(json.dumps(lock))
        git = self.bin / "git"
        git.write_text('''#!/bin/bash
[[ "$1" == -C && "$2" == "$HOME/.nexus/cli" ]] || exit 23
shift 2
case "$1" in
  symbolic-ref) echo main ;;
  config) echo origin ;;
  remote) echo https://github.com/fw-ai/fireconnect.git ;;
  rev-parse) echo "$TEST_UPSTREAM" ;;
  status) exit 0 ;;
  pull) [[ "$*" == 'pull --ff-only --no-autostash' ]] ;;
  *) exit 1 ;;
esac
''')
        git.chmod(0o755)
        npm = self.bin / "npm"
        npm.write_text('''#!/bin/bash
set -eu
[[ "$PWD" == "$HOME/.nexus/cli/packages/setup-cli" && "$NEXUS_SKIP_POSTINSTALL_FINALIZE" == 1 ]]
echo "$*" >> "$CALL_LOG"
mkdir -p node_modules/example
[[ "$TEST_FAIL" != 1 ]] || exit 17
cp package-lock.json node_modules/.package-lock.json
echo '{"version":"1.2.0"}' > node_modules/example/package.json
''')
        npm.chmod(0o755)
        def update(fail="0", upstream="origin/main"):
            return subprocess.run(["bash", str(ROOT / "scripts/update_packages.sh"), "nexus"],
                                  env={**self.env, "HOME": str(home), "TEST_FAIL": fail, "TEST_UPSTREAM": upstream},
                                  capture_output=True, text=True)

        self.assertNotEqual(update(upstream="origin/other").returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(update(fail="1").returncode, 17)
        for _ in range(2):
            result = update()
            self.assertEqual(result.returncode, 0, result.stderr)
        (setup / "node_modules/example/package.json").write_text('{"version":"1.0.0"}')
        self.assertEqual(update().returncode, 0)
        self.assertEqual(self.log.read_text().splitlines(), ["ci --omit=dev --no-fund --no-audit --ignore-scripts"] * 3)

    def test_cargo_missing_plugin_preserves_project_lockfile(self):
        result, calls = self.sweep("cargo", '[[ "$*" == "install --list" ]] && { echo "crate v1:"; exit 0; }; exit 101')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cargo install cargo-update --locked", result.stderr)
        self.assertEqual(calls, ["install --list", "install-update --version"])

    def test_bun_empty_store(self):
        result, calls = self.sweep("bun", '[[ "$*" != "pm ls -g" ]] || echo "global node_modules (0 installed)"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["upgrade", "pm ls -g"])

    def test_bun_keeps_runtime_failure_and_updates_globals(self):
        result, calls = self.sweep("bun", '[[ "$1" == upgrade ]] && exit 8\n[[ "$1" != pm ]] || echo "(1 installed)"')
        self.assertEqual(result.returncode, 8)
        self.assertEqual(calls, ["upgrade", "pm ls -g", "update -g"])

    def test_bun_runtime_owned_by_another_manager(self):
        result, calls = self.sweep("bun", '[[ "$1" != pm ]] || echo "(2 installed)"', BUN_INSTALL="/other")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["pm ls -g", "update -g"])

    def test_uv_list_failure_visible_with_external_runtime(self):
        result, calls = self.sweep("uv", '''
case "$*" in
"python upgrade --help") exit 2;;
"tool dir") echo "$AXOUPDATER_CONFIG_PATH";;
"tool list") echo "list failed" >&2; exit 7;;
*) exit 31;;
esac
''', AXOUPDATER_CONFIG_PATH=str(self.bin))
        self.assertEqual(result.returncode, 7)
        self.assertIn("list failed", result.stderr)
        self.assertNotIn("self update", calls)

    def test_uv_tool_sweep_continues_after_self_failure_and_missing_source(self):
        (self.bin / "uv-receipt.json").write_text(json.dumps({"install_prefix": str(self.bin)}))
        source_dir = self.bin / "gone"
        source_dir.mkdir()
        (source_dir / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "gone", path = "/no-such-updater-fixture.whl" }]\n'
            'entrypoints = []\n')
        working = self.bin / "working"
        working.mkdir()
        (working / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "working" }]\n'
            f'entrypoints = [{{ name = "working", install-path = "{self.bin}/launchers/working" }}]\n')
        result, calls = self.sweep("uv", '''
case "$*" in
"self update") exit 8;;
"python upgrade --help"|"python upgrade") exit 0;;
"python list --managed-python --only-installed") echo "cpython-3.11";;
"tool dir") echo "$AXOUPDATER_CONFIG_PATH";;
"tool list") printf "gone v1\\n- gone\\nworking v2\\n- working\\n";;
"tool upgrade working") exit 0;;
*) exit 99;;
esac
''', AXOUPDATER_CONFIG_PATH=str(self.bin))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Deferred gone", result.stdout)
        self.assertIn("python upgrade", calls)
        self.assertIn("tool upgrade working", calls)
        self.assertNotIn("tool upgrade gone", calls)

    def test_uv_shadowed_launchers_keep_owner(self):
        environment = self.bin / "shadowed"
        environment.mkdir()
        launcher = self.bin / "command"
        launcher.write_text("native binary")
        receipt = environment / "uv-receipt.toml"
        receipt.write_text('[tool]\nrequirements = [{ name = "shadowed" }]\n'
                           f'entrypoints = [{{ name = "command", install-path = "{launcher}" }}]\n')
        command = update_uv.tool_update_command("uv", self.bin, "shadowed")
        if update_uv.tomllib:
            self.assertEqual(command, ["uv", "pip", "install", "--python", str(environment / "bin/python"),
                                       "--upgrade", "shadowed"])
        else:
            self.assertIsNone(command)
        receipt.write_text(receipt.read_text() + '\n[tool.options]\nindex = "custom"\n')
        self.assertIsNone(update_uv.tool_update_command("uv", self.bin, "shadowed"))

    def test_uv_alias_sweep_preserves_origin_filters_and_local_commands(self):
        cache = self.bin / "cache.json"
        cache.write_text(json.dumps([{"name": "tool-a", "origin": "uv/pip"},
                                     {"name": "tool-a", "origin": "uv/venv"},
                                     {"name": "tool-b", "origin": "uv/venv"}]))
        cfg = {"known": {}, "bulk": {"uv/pip": "sweep", "uv/venv": "sweep"}}

        def jobs(only=None):
            return [line.split(EMIT_SEP)[1:3] for line in collect_emit_lines(str(cache), cfg, only, None)]

        self.assertEqual(jobs(), [["uv/pip", "sweep"]])
        self.assertEqual(jobs("uv/venv"), [["uv/venv", "sweep"]])
        cfg["bulk"]["uv/venv"] = "custom upgrade"
        self.assertEqual(jobs(), [["uv/pip", "sweep"], ["uv/venv", "custom upgrade"]])
