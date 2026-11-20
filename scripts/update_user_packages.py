"""Update packages installed in user-level Python and Ruby package stores."""
from __future__ import annotations

import argparse
import json
import os
from importlib import metadata
from pathlib import Path
import re
import shutil
import site
import subprocess
import sys
import tempfile


def python_user_sites():
    sites = {Path(site.getusersitepackages())}
    sites.update(Path.home().glob("Library/Python/3.*/lib/python/site-packages"))
    sites.update(Path.home().glob(".local/lib/python3.*/site-packages"))
    return sorted(path for path in sites if path.is_dir())


def python_for_site(path):
    match = re.search(r"(?:/Python/|/python)(3\.\d+)(?:/|$)", str(path))
    candidates = [shutil.which("python" + match[1])] if match else []
    candidates.extend((sys.executable, shutil.which("python3"), "/usr/bin/python3"))
    # The unversioned Apple interpreter can still own an older user site.
    # Verify the actual destination rather than guessing from a shebang.
    probe = ("import json, os, site, sys, sysconfig; "
             "print(json.dumps([site.getusersitepackages(), site.ENABLE_USER_SITE, "
             "sys.prefix == sys.base_prefix and os.path.isfile(os.path.join("
             "sysconfig.get_path('stdlib'), 'EXTERNALLY-MANAGED'))]))")
    for exe in dict.fromkeys(candidates):
        if not exe or not os.access(exe, os.X_OK):
            continue
        result = subprocess.run([exe, "-c", probe], capture_output=True, text=True)
        if result.returncode == 0:
            destination, enabled, managed = json.loads(result.stdout)
            if enabled and Path(destination).resolve() == path.resolve():
                return exe, managed
    return None, False


def pip_requirements(path):
    packages = []
    constraints = []
    for dist in metadata.distributions(path=[str(path)]):
        name = dist.metadata["Name"]
        if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
            continue
        if (dist.read_text("INSTALLER") or "").strip() == "pip" and not dist.read_text("direct_url.json"):
            packages.append(name)
        else:
            # Preserve local/editable sources and packages installed by other
            # tools even when a registry package depends on one of them.
            constraints.append(f"{name}==={dist.version}")
    return sorted(set(packages)), sorted(set(constraints))


def update_pip():
    failures = []
    deferred = []
    for path in python_user_sites():
        packages, constraints = pip_requirements(path)
        if not packages:
            continue
        exe, managed = python_for_site(path)
        if not exe or managed:
            reason = "externally managed" if managed else "matching interpreter unavailable"
            deferred.append(f"{path} ({len(packages)} packages, {reason})")
            continue
        print(f"Updating {len(packages)} user packages with {exe}", flush=True)
        with tempfile.TemporaryDirectory() as directory:
            args = [exe, "-m", "pip", "--disable-pip-version-check", "install",
                    "--user", "--upgrade", "--prefer-binary"]
            if constraints:
                constraint_file = Path(directory, "constraints.txt")
                constraint_file.write_text("\n".join(constraints) + "\n")
                args.extend(["--constraint", str(constraint_file)])
            result = subprocess.run([*args, *packages], cwd=directory)
        if result.returncode:
            failures.append(str(path))
    for item in deferred:
        print(f"Deferred {item}", file=sys.stderr)
    for item in failures:
        print(f"pip upgrade failed for {item}", file=sys.stderr)
    return int(bool(failures))


def update_gems():
    # Apple's RubyGems 3.0 picks the highest version matching the platform,
    # even if its gemspec requires a newer Ruby. Read newer candidates from
    # RubyGems' configured sources, reusing the index across all user gems.
    query = r"""
specs = Dir[File.join(Gem.user_dir, "specifications", "*.gemspec")].map do |file|
  Gem::Specification.load(file) || raise("Invalid gem specification: #{file}")
end
fetcher = Gem::SpecFetcher.fetcher
updates = []
specs.group_by(&:name).each do |name, versions|
  current = versions.max_by(&:version)
  dependency = Gem::Dependency.new(name, "> #{current.version}")
  tuples, problems = fetcher.search_for_dependency(dependency)
  failure = problems.find { |problem| problem.respond_to?(:exception) }
  raise failure.exception if failure
  next if tuples.empty?
  newest = nil
  compatible = nil
  tuples.sort_by { |tuple, source| tuple.version }.reverse_each do |tuple, source|
    spec = source.fetch_spec(tuple)
    newest ||= spec
    if spec.required_ruby_version.satisfied_by?(Gem.ruby_version) &&
       spec.required_rubygems_version.satisfied_by?(Gem.rubygems_version)
      compatible = spec
      break
    end
  end
  unless compatible && compatible.version == newest.version
    warn "Deferred #{name} #{newest.version}: requires Ruby #{newest.required_ruby_version} " \
         "and RubyGems #{newest.required_rubygems_version} " \
         "(installed #{Gem.ruby_version} and #{Gem.rubygems_version})."
  end
  updates << [name, compatible.version.to_s] if compatible
end
puts JSON.generate(updates)
"""
    result = subprocess.run(["ruby", "-rrubygems", "-rrubygems/spec_fetcher", "-rjson", "-e", query],
                            stdout=subprocess.PIPE, text=True)
    if result.returncode:
        return result.returncode
    failed = False
    for name, version in json.loads(result.stdout):
        result = subprocess.run(["ruby", "-S", "gem", "install", "--user-install", "--no-document",
                                 "--version", version, name])
        if result.returncode:
            failed = True
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manager", choices=["pip", "gem"])
    options = parser.parse_args()
    sys.exit(update_pip() if options.manager == "pip" else update_gems())
