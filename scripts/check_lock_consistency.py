"""Consistency of the dependency lock, checked against a FRESH environment (lot 46).

Run it AFTER `pip install -r requirements.lock.txt` in a clean virtual environment — never in a long-lived
one (an environment that still holds packages removed from the lock would hide exactly what this looks for):

    python scripts/check_lock_consistency.py

Exit code 0 only if ALL of these hold:
  1. `pip check` reports no broken requirement;
  2. the installed set is EXACTLY the lock (same names, same versions);
  3. every distribution reachable from requirements.txt + requirements-test.txt (extras included, environment
     markers evaluated) is installed, and the lock holds NOTHING else (no orphan left by a hand edit);
  4. every installed version satisfies the specifier written in requirements*.txt;
  5. every third-party module imported by main.py, src/, scripts/, tests/ and migrations/ is provided by an
     installed distribution, and none of the packages removed with Streamlit (lot 43) is imported.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
REMOVED_AT_LOT_43 = {"streamlit", "pandas", "altair", "pyarrow", "pydeck", "watchdog", "jsonschema", "protobuf", "blinker", "toml"}
LOCAL_MODULES = {"src", "tests", "main", "scripts", "migrations", "conftest"}


def _requirements(path: Path) -> list[Requirement]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out


def _lock() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            name, _, version = line.partition("==")
            pins[canonicalize_name(name)] = version
    return pins


PLATFORMS = {
    "windows": {"sys_platform": "win32", "platform_system": "Windows", "os_name": "nt"},
    "linux": {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix"},
}


def _closure(roots: list[Requirement], installed: dict, platform: dict | None = None) -> tuple[set[str], list[str]]:
    seen, unresolved = set(), []
    stack = [(canonicalize_name(r.name), frozenset(r.extras)) for r in roots]
    environment = {**default_environment(), **(platform or {})}
    while stack:
        name, extras = stack.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        dist = installed.get(name)
        if dist is None:
            unresolved.append(name)
            continue
        for spec in dist.requires or []:
            req = Requirement(spec)
            if req.marker is not None and not any(req.marker.evaluate({**environment, "extra": e}) for e in (set(extras) | {""})):
                continue
            stack.append((canonicalize_name(req.name), frozenset(req.extras)))
    return {n for n, _ in seen}, unresolved


def _third_party_imports() -> dict[str, set[str]]:
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, set[str]] = {}
    for base in ("main.py", "src", "scripts", "tests", "migrations"):
        path = ROOT / base
        for file in ([path] if path.is_file() else sorted(path.rglob("*.py"))):
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for name in names:
                    if name not in stdlib and name not in LOCAL_MODULES:
                        found.setdefault(name, set()).add(file.relative_to(ROOT).as_posix())
    return found


def main() -> int:
    problems: list[str] = []

    check = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    print("pip check:", (check.stdout + check.stderr).strip())
    if check.returncode != 0:
        problems.append("pip check failed")

    lock = _lock()
    # what `pip freeze` never lists (the installer's own tooling) is not part of a lock
    installed = {canonicalize_name(d.metadata["Name"]): d for d in metadata.distributions()
                 if canonicalize_name(d.metadata["Name"]) not in {"pip", "setuptools", "wheel"}}
    frozen = {name: dist.version for name, dist in installed.items()}
    only_lock, only_installed = sorted(set(lock) - set(frozen)), sorted(set(frozen) - set(lock))
    mismatched = sorted(n for n in lock if n in frozen and lock[n] != frozen[n])
    print(f"lock: {len(lock)} pins | installed: {len(frozen)}")
    if only_lock or only_installed or mismatched:
        problems.append(f"installed set != lock (only in lock {only_lock}; only installed {only_installed}; version mismatch {mismatched})")

    roots = _requirements(ROOT / "requirements.txt") + _requirements(ROOT / "requirements-test.txt")
    closure, unresolved = _closure(roots, installed)
    # The lock was generated on ONE platform: a package that only exists behind another platform's environment marker
    # (colorama on Windows, uvloop elsewhere) is legitimately present or absent — reported, never a failure. Anything
    # needed on BOTH platforms and missing, or reachable on neither, still fails.
    reachable = {name: _closure(roots, installed, env) for name, env in PLATFORMS.items()}
    everywhere = set(closure).union(*(c for c, _ in reachable.values()))
    platform_only = sorted(n for n in unresolved if not all(n in c for c, _ in reachable.values()))
    hard_unresolved = sorted(set(unresolved) - set(platform_only))
    orphans = sorted(set(lock) - everywhere)
    print(f"dependency closure of requirements*.txt: {len(closure)} | unresolved: {hard_unresolved} | orphans in the lock: {orphans}")
    if platform_only:
        print(f"NOTE — needed on this platform only (lock generated elsewhere, not a failure): {platform_only}")
    other_platform_pins = sorted(set(lock) - closure)
    if other_platform_pins and not orphans:
        print(f"NOTE — pinned for another platform only: {other_platform_pins}")
    if hard_unresolved:
        problems.append(f"needed but not installed: {hard_unresolved}")
    if orphans:
        problems.append(f"lock entries reachable from no requirement on any platform: {orphans}")

    unsatisfied = [(r.name, str(r.specifier), frozen.get(canonicalize_name(r.name))) for r in roots
                   if frozen.get(canonicalize_name(r.name)) is None or (r.specifier and not r.specifier.contains(frozen[canonicalize_name(r.name)], prereleases=True))]
    print("requirement specifiers not satisfied:", unsatisfied)
    if unsatisfied:
        problems.append(f"specifiers not satisfied: {unsatisfied}")

    imports = _third_party_imports()
    provided = metadata.packages_distributions()
    missing = {n: sorted(files)[:3] for n, files in imports.items() if n not in provided}
    removed = {n: sorted(files)[:3] for n, files in imports.items() if n in REMOVED_AT_LOT_43}
    print(f"third-party imports: {len(imports)} | not provided: {missing} | removed packages imported: {removed}")
    if missing:
        problems.append(f"imports without an installed distribution: {missing}")
    if removed:
        problems.append(f"packages removed at lot 43 are imported again: {removed}")

    if problems:
        print("\nLOCK CONSISTENCY: FAILED")
        for p in problems:
            print(" -", p)
        return 1
    print("\nLOCK CONSISTENCY: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
