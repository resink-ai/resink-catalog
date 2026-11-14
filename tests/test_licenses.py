"""The licence list is current and holds only licences we can ship under Apache-2.0."""

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


def test_no_runtime_dependency_has_a_licence_we_cannot_ship():
    mod = load()
    bad = {
        n: mod.licence(d)
        for n, d in mod.closure(mod.ROOT).items()
        if any(f.lower() in mod.licence(d).lower() for f in mod.FORBIDDEN)
    }
    assert bad == {}
    assert not [n for n, (lic, _) in mod.PLATFORM_ONLY.items() if any(f in lic for f in mod.FORBIDDEN)]
