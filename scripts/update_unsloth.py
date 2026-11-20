"""Update the vendor-owned Unsloth Studio installation on macOS when idle."""
import json
import os
from importlib.metadata import distributions
from pathlib import Path
import re
import shutil
import subprocess
import sys


def installed_version(prefix):
    receipt = json.loads((prefix / "unsloth_install_manifest.json").read_text())
    if receipt.get("package") != "unsloth" or Path(receipt["prefix"]).resolve() != prefix:
        raise ValueError("Unsloth Studio install manifest does not match this environment")
    sites = list((prefix / "lib").glob("python*/site-packages"))
    packages = [dist for dist in distributions(path=sites) if dist.metadata["Name"] == "unsloth"]
    if len(packages) != 1 or packages[0].version != receipt["package_version"]:
        raise ValueError("Unsloth Studio package metadata disagrees with its install manifest")
    if packages[0].read_text("direct_url.json"):
        return None
    return packages[0].version


def compatible_version(python):
    # pip queries the official index using the Studio runtime's own supported
    # tags and Python version. Do not use the updater's system Python here.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env["PIP_CONFIG_FILE"] = os.devnull
    args = [str(python), "-I", "-B", "-m", "pip", "--disable-pip-version-check", "--no-cache-dir",
            "index", "versions", "unsloth", "--json", "--index-url", "https://pypi.org/simple"]
    report = json.loads(subprocess.check_output(args, env=env, text=True, timeout=90))
    if report.get("name") != "unsloth" or not report.get("latest"):
        raise ValueError("Invalid Unsloth release information")
    return report["latest"]


def active_workloads(studio):
    # Vendor PID records cover detached children. Process paths also cover a
    # Studio server starting up and Python jobs using the runtime directly.
    rows = subprocess.check_output(["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=,command="],
                                   text=True)
    processes = {}
    for line in rows.splitlines():
        pid, command = line.strip().split(None, 1)
        processes[int(pid)] = command
    if processes.pop(os.getpid(), None) is None:
        raise ValueError("Could not verify Studio's process inventory")
    markers = (str(studio) + "/", str(studio.parent / "llama.cpp") + "/",
               str(studio.parent / "whisper.cpp") + "/", "/unsloth.app/contents/macos/",
               "/unsloth studio.app/contents/macos/")
    if any(any(marker.lower() in cmd.lower() for marker in markers)
           or re.search(r"(?:^|[/\s])(?:unsloth (?:studio|train|start)|torchrun|accelerate launch)(?:\s|$)", cmd)
           for cmd in processes.values()):
        return True
    for path in [*studio.glob("studio-*.pid"), studio / "studio.pid"]:
        if path.exists() and int(path.read_text().splitlines()[0]) in processes:
            return True
    for path in (studio / "run" / "children").glob("*.json"):
        record = json.loads(path.read_text())
        if any(pid in processes for pid in [record["owner_pid"], *[c["pid"] for c in record["children"]]]):
            return True
    return False


def run(launcher, studio):
    prefix = (studio / "unsloth_studio").resolve()
    if not (prefix / ".unsloth-studio-owned").is_file() or launcher.resolve() != prefix / "bin" / "unsloth":
        print("Deferred Unsloth: the active command belongs to a different installation.")
        return 0
    current = installed_version(prefix)
    if current is None or os.environ.get("UNSLOTH_DISABLE_UPDATE_CHECK") == "1":
        print("Deferred Unsloth: direct-source installation or vendor release checks disabled.")
        return 0
    python = prefix / "bin" / "python"
    latest = compatible_version(python)
    # The Studio environment ships packaging. Compare with PEP 440 including
    # local, prerelease and postrelease versions without a host dependency.
    compare = "from packaging.version import Version; import sys; print(Version(sys.argv[1]) > Version(sys.argv[2]))"
    newer = subprocess.check_output([str(python), "-I", "-B", "-c", compare, latest, current], text=True)
    if newer.strip() == "False":
        print(f"Unsloth Studio {current} is current for its Python.")
        return 0
    if newer.strip() != "True":
        raise ValueError("Unsloth Studio version comparison failed")
    if active_workloads(studio):
        print(f"Deferred Unsloth Studio {latest}: a Studio or training process is running.")
        return 0
    print(f"Updating Unsloth Studio {current} to {latest}.", flush=True)
    subprocess.run([str(launcher), "studio", "update"], cwd=prefix, check=True)
    if installed_version(prefix) != latest:
        raise ValueError(f"Unsloth Studio did not activate the checked release {latest}")
    return 0


if __name__ == "__main__":
    try:
        if sys.platform != "darwin":
            sys.exit("This scheduled Unsloth Studio updater supports macOS.")
        binary = shutil.which("unsloth")
        if not binary:
            sys.exit("Unsloth executable is missing.")
        location = os.environ.get("UNSLOTH_STUDIO_HOME") or os.environ.get("STUDIO_HOME")
        root = Path(location).expanduser().resolve() if location else Path.home() / ".unsloth" / "studio"
        sys.exit(run(Path(binary), root))
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError) as exc:
        print(f"Unsloth Studio update failed: {exc}", file=sys.stderr)
        sys.exit(1)
