"""The release machinery says what the plan requires (L4): extensions pre-installed and required, three operating
systems, a Docker image that is private by default. The workflows themselves run on GitHub; this checks their shape."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent


def workflow(name):
    return yaml.safe_load((ROOT / ".github" / "workflows" / f"{name}.yml").read_text())


@pytest.mark.parametrize(("name", "job"), [("ci", "test"), ("release", "verify")])
def test_duckdb_extensions_are_preinstalled_and_their_tests_may_not_skip(name, job):
    j = workflow(name)["jobs"][job]
    assert j["env"]["RESINK_CATALOG_REQUIRE_DUCKDB_EXTENSIONS"] == "1"
    runs = [s.get("run", "") for s in j["steps"]]
    install = next(i for i, r in enumerate(runs) if "install_duckdb_extensions.py" in r)
    tests = next(i for i, r in enumerate(runs) if "pytest" in r)
    assert install < tests
    assert "-ra" in runs[tests]  # skips are listed in the log


@pytest.mark.parametrize(("name", "job"), [("ci", "test"), ("release", "verify")])
def test_linux_macos_and_windows_are_tested(name, job):
    assert set(workflow(name)["jobs"][job]["strategy"]["matrix"]["os"]) == {
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
    }


def test_release_publishes_only_after_verification_and_without_a_stored_token():
    jobs = workflow("release")["jobs"]
    assert (
        jobs["build"]["needs"] == "verify" and jobs["pypi"]["needs"] == "build" and jobs["docker"]["needs"] == "verify"
    )
    assert jobs["pypi"]["permissions"] == {"id-token": "write"}  # trusted publishing
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "PYPI_API_TOKEN" not in text and "password: ${{ secrets.PYPI" not in text


def test_the_dockerfile_runs_unprivileged_and_is_private_by_default():
    d = (ROOT / "Dockerfile").read_text()
    assert "USER catalog" in d and "RESINK_CATALOG_IN_CONTAINER=1" in d and "VOLUME /data" in d
    assert "127.0.0.1:8765:8765" in d  # the documented run command publishes on the host's loopback only


def test_extension_script_installs_what_the_duckdb_tests_load():
    script = (ROOT / "scripts" / "install_duckdb_extensions.py").read_text()
    tests = (ROOT / "tests" / "test_duckdb_rest.py").read_text()
    for ext in ("httpfs", "iceberg"):
        assert f'"{ext}"' in script and f'"{ext}"' in tests
