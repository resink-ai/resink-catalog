"""The licence inventory is derived from the lock files (so identical on every OS) and enforced against an allowlist."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent


def load():
    spec = importlib.util.spec_from_file_location("licenses", ROOT / "scripts" / "licenses.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_third_party_licenses_file_is_current():
    assert (ROOT / "THIRD_PARTY_LICENSES.md").read_text() == load().markdown(), (
        "run: uv run python scripts/licenses.py --markdown > THIRD_PARTY_LICENSES.md"
    )


def test_every_locked_runtime_dependency_python_and_web_is_on_the_allowlist():
    assert load().problems() == []


def test_the_policy_rejects_what_it_should():
    mod = load()
    assert mod.allowed("MIT", "x") and mod.allowed("BSD-3-Clause AND 0BSD", "x")
    for bad in ("GPL-3.0-only", "AGPL-3.0", "SSPL-1.0", "BUSL-1.1", "MPL-2.0", "UNKNOWN", "MIT OR GPL-2.0", "LGPL-2.1"):
        assert not mod.allowed(bad, "x"), bad
    assert mod.allowed("MPL-2.0", "certifi") and not mod.allowed("GPL-3.0", "certifi")  # the exception is per package


def test_the_reviewed_table_matches_what_is_installed_here():
    """A licence that changes upstream, or a wrong entry, shows up on the platforms where the package is installed."""
    mod = load()
    table = mod.reviewed()
    wrong = {
        n: (table[n], got)
        for n in mod.lock_runtime_closure()
        if (got := mod.installed_licence(n)) is not None and got != table[n]
    }
    assert wrong == {}


def test_the_inventory_is_complete_for_every_platform_and_covers_the_docker_image():
    mod = load()
    names = set(mod.lock_runtime_closure())
    assert {"keyring", "pywin32-ctypes", "importlib-metadata", "greenlet"} <= names  # present on some platform only
    assert {"react", "react-dom"} <= set(mod.web_runtime()) and "vitest" not in mod.web_runtime()
    assert "--require-hashes" in (ROOT / "Dockerfile").read_text()  # the image installs exactly the locked set


def test_a_forbidden_web_licence_in_the_lock_file_fails(tmp_path):
    import json

    mod = load()
    lock = json.loads((ROOT / "web" / "package-lock.json").read_text())
    lock["packages"]["node_modules/evil"] = {"version": "1.0.0", "license": "GPL-3.0-only"}
    f = tmp_path / "package-lock.json"
    f.write_text(json.dumps(lock))
    web = mod.web_runtime(f)
    assert web["evil"][1] == "GPL-3.0-only" and not mod.allowed(web["evil"][1], "evil")
