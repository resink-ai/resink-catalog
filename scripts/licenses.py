"""The licence inventory of everything the Local Catalog ships or needs at runtime, the same on every OS.

    uv run python scripts/licenses.py --markdown   # THIRD_PARTY_LICENSES.md (tests/test_licenses.py keeps it current)
    uv run python scripts/licenses.py --check      # exit 1 and list problems against the policy

Python packages come from `uv.lock` (every platform and Python version, markers ignored), web packages from
`web/package-lock.json`, never from the machine that runs this. Python licence texts are not in `uv.lock`, so each
package's licence is a *reviewed* entry in `scripts/python_licenses.json`; the test compares those entries with the
metadata of whatever is installed, so a licence that changes upstream is noticed. Policy: an explicit allowlist, plus
named exceptions with a reason.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parent.parent
PROJECT = "resink-catalog"
ALLOWED = {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "ISC", "0BSD", "Zlib", "CC0-1.0", "MIT-0"}
# reviewed exceptions: package -> (licence, why it is acceptable)
EXCEPTIONS = {
    "certifi": (
        "MPL-2.0",
        "file-level copyleft: applies only to modified copies of certifi's own files; we ship it unmodified",
    ),
}
DOCKER_NOTE = (
    "The Docker image contains exactly the Python packages above (installed from `uv.lock` with hashes) plus the "
    "`python:3.12-slim` base image, whose Debian packages carry their own licences under `/usr/share/doc/*/copyright`."
)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# The platforms we support (CI matrix): an edge counts if its marker holds in any of these environments.
ENVIRONMENTS = [
    {
        "sys_platform": plat,
        "platform_system": system,
        "os_name": "nt" if plat == "win32" else "posix",
        "platform_machine": machine,
        "platform_python_implementation": "CPython",
        "implementation_name": "cpython",
        "python_version": f"3.{minor}",
        "python_full_version": f"3.{minor}.0",
        "extra": "",
    }
    for plat, system, machine in (
        ("linux", "Linux", "x86_64"),
        ("linux", "Linux", "aarch64"),
        ("darwin", "Darwin", "arm64"),
        ("win32", "Windows", "AMD64"),
    )
    for minor in (10, 11, 12, 13)
]


def _applies(dep: dict) -> bool:
    marker = dep.get("marker")
    if not marker:
        return True
    from packaging.markers import Marker

    m = Marker(marker)
    return any(m.evaluate(env) for env in ENVIRONMENTS)


def lock_runtime_closure(lock_path: Path = ROOT / "uv.lock") -> dict[str, list[str]]:
    """{package: [versions]} reachable from the project's runtime dependencies in uv.lock (all markers)."""
    lock = tomllib.loads(lock_path.read_text())
    pkgs = {(_norm(p["name"]), p.get("version")): p for p in lock["package"]}
    by_name: dict[str, list[dict]] = {}
    for p in lock["package"]:
        by_name.setdefault(_norm(p["name"]), []).append(p)
    root = by_name[PROJECT][0]
    seen: set[tuple[str, str | None]] = set()
    todo = [d for d in root.get("dependencies", []) if _applies(d)]
    while todo:
        dep = todo.pop()
        name = _norm(dep["name"])
        candidates = [pkgs[(name, dep["version"])]] if dep.get("version") else by_name[name]
        for pkg in candidates:
            key = (name, pkg.get("version"))
            if key not in seen:
                seen.add(key)
                todo += [d for d in pkg.get("dependencies", []) if _applies(d)]
            for extra in dep.get("extra", []):
                todo += [d for d in pkg.get("optional-dependencies", {}).get(extra, []) if _applies(d)]
    out: dict[str, list[str]] = {}
    for name, version in seen:
        out.setdefault(name, []).append(version or "")
    return {n: sorted(v) for n, v in sorted(out.items())}


def reviewed() -> dict[str, str]:
    return json.loads((ROOT / "scripts" / "python_licenses.json").read_text())


def web_runtime(lock_path: Path = ROOT / "web" / "package-lock.json") -> dict[str, tuple[str, str]]:
    """{package: (version, licence)} for the web app's production dependencies, from package-lock.json."""
    lock = json.loads(lock_path.read_text())
    return {
        key.removeprefix("node_modules/"): (p["version"], p.get("license", "UNKNOWN"))
        for key, p in sorted(lock["packages"].items())
        if key and not p.get("dev") and not p.get("devOptional")
    }


def _parts(expression: str) -> list[str]:
    return [x.strip("() ") for x in re.split(r"\s+(?:AND|OR)\s+", expression)]


def allowed(expression: str, package: str) -> bool:
    """Every licence in the expression is on the allowlist, or the package has a reviewed exception for it."""
    exc = EXCEPTIONS.get(package, ("", ""))[0]
    return all(p in ALLOWED or p == exc for p in _parts(expression))


def problems() -> list[str]:
    out = []
    closure, table = lock_runtime_closure(), reviewed()
    for name in closure:
        if name not in table:
            out.append(f"python: {name} is in uv.lock's runtime closure but has no reviewed licence")
        elif not allowed(table[name], name):
            out.append(f"python: {name} has licence {table[name]!r}, not on the allowlist")
    out += [f"python: {n} is in python_licenses.json but not in the runtime closure" for n in table if n not in closure]
    for name, (_, lic) in web_runtime().items():
        if not allowed(lic, name):
            out.append(f"web: {name} has licence {lic!r}, not on the allowlist")
    return out


def markdown() -> str:
    closure, table = lock_runtime_closure(), reviewed()
    out = [
        "# Third-party licences",
        "",
        "resink-catalog is Apache-2.0. These are the runtime dependencies of the Python package and of the web UI it",
        "ships, with their licences. Generated by `uv run python scripts/licenses.py --markdown` from `uv.lock` and",
        "`web/package-lock.json`, so it is identical on every OS; `tests/test_licenses.py` fails when the file is",
        "stale or when a dependency's licence is not on the policy below.",
        "",
        "Policy: an explicit allowlist: " + ", ".join(sorted(ALLOWED)) + ". Reviewed exceptions:",
        *[f"`{n}` is {lic}, {why}." for n, (lic, why) in EXCEPTIONS.items()],
        "Anything else (GPL, AGPL, SSPL, source-available, unknown) fails the test.",
        "",
        "## Python (every platform and Python version in `uv.lock`)",
        "",
        "| Package | Versions | Licence |",
        "| --- | --- | --- |",
        *[f"| {n} | {', '.join(v)} | {table.get(n, 'UNREVIEWED')} |" for n, v in closure.items()],
        "",
        "## Web UI bundle (`web/package-lock.json`, production dependencies)",
        "",
        "| Package | Version | Licence |",
        "| --- | --- | --- |",
        *[f"| {n} | {v} | {lic} |" for n, (v, lic) in web_runtime().items()],
        "",
        "Development tools (Vite, Vitest, Playwright, TypeScript, pytest, ruff) are not shipped.",
        "",
        "## Docker image",
        "",
        DOCKER_NOTE,
        "",
    ]
    return "\n".join(out)


ALIASES = {
    "MIT License": "MIT",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "Apache Software License": "Apache-2.0",
}


def installed_licence(name: str) -> str | None:
    """The licence the installed distribution declares, or None if it is not installed here."""
    from importlib import metadata

    try:
        md = metadata.distribution(name).metadata
    except metadata.PackageNotFoundError:
        return None
    if md.get("License-Expression"):
        return ALIASES.get(md["License-Expression"], md["License-Expression"])
    classifiers = [c.split("::")[-1].strip() for c in md.get_all("Classifier") or [] if c.startswith("License ::")]
    found = "; ".join(classifiers) or (md.get("License") or "UNKNOWN").strip().splitlines()[0][:60]
    return ALIASES.get(found, found)


# Releases of one package declare their licence in different ways: newer ones with an SPDX `License-Expression`, older
# ones (the versions Python 3.10 resolves to) with trove classifiers only, which are coarse ("BSD License" is any of
# three licences). The reviewed table is the authority for the policy; installed metadata must be CONSISTENT with it.
CLASSIFIER_IDS = {
    "BSD License": {"BSD-2-Clause", "BSD-3-Clause", "0BSD"},
    "MIT License": {"MIT", "MIT-0"},
    "Apache Software License": {"Apache-2.0"},
    "Python Software Foundation License": {"PSF-2.0"},
    "ISC License (ISCL)": {"ISC"},
    "Mozilla Public License 2.0 (MPL 2.0)": {"MPL-2.0"},
}


def licence_ids(expression: str) -> set[str]:
    return {t for t in re.split(r"\s+(?:AND|OR|WITH)\s+|[()]|\s{2,}", expression) if t.strip()}


def consistent(reviewed_entry: str, declared: str) -> bool:
    """Is what an installed distribution declares (`installed_licence`) compatible with the reviewed entry?
    The entry is the union over the locked versions of a package (one release may declare less than another), so
    every licence an installed release declares must be in it: an SPDX expression by its identifiers, classifier-only
    metadata (`A; B`) by classifier family, where an unknown classifier is a mismatch. The entry may be broader than
    one release (it was reviewed from the licence files). A licence the table lacks is never accepted."""
    if declared == reviewed_entry or (declared and licence_ids(declared) <= licence_ids(reviewed_entry)):
        return True
    parts = [c.strip() for c in declared.split(";")]
    if not all(c in CLASSIFIER_IDS for c in parts):
        return False
    have = licence_ids(reviewed_entry)
    return all(CLASSIFIER_IDS[c] & have for c in parts)


if __name__ == "__main__":
    if "--markdown" in sys.argv:
        print(markdown(), end="")
    elif "--draft" in sys.argv:  # a starting point for python_licenses.json: review every entry by hand
        print(json.dumps({n: installed_licence(n) or "TODO" for n in lock_runtime_closure()}, indent=2))
    else:
        found = problems()
        print("\n".join(found) or "ok")
        sys.exit(1 if found else 0)
