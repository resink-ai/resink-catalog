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


def _uses():
    for name in ("ci", "release"):
        for job in workflow(name)["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    yield name, step["uses"]


def test_every_action_is_pinned_to_a_full_commit_sha():
    import re

    refs = list(_uses())
    assert len(refs) >= 10
    assert [r for r in refs if not re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", r[1])] == []
    for name in ("ci", "release"):  # the tag stays visible for reviewers, as a comment
        text = (ROOT / ".github" / "workflows" / f"{name}.yml").read_text()
        assert all("# " in line for line in text.splitlines() if "uses: " in line)


def test_workflows_have_least_privilege_permissions_and_do_not_keep_checkout_credentials():
    for name in ("ci", "release"):
        wf = workflow(name)
        assert wf["permissions"] == {"contents": "read"}
        for job in wf["jobs"].values():
            for step in job["steps"]:
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] is False
    jobs = workflow("release")["jobs"]
    assert jobs["pypi"]["permissions"] == {"id-token": "write"}  # only the publishing job can mint the identity
    assert jobs["docker"]["permissions"] == {"contents": "read", "packages": "write"}


def test_the_docker_image_is_reproducible_and_runs_with_the_documented_user_flag():
    text = (ROOT / "Dockerfile").read_text()
    import re

    froms = re.findall(r"^FROM (\S+)", text, re.M)
    assert froms and all(re.search(r"@sha256:[0-9a-f]{64}$", f) for f in froms), froms
    assert "uv export --frozen --no-dev" in text and "--require-hashes" in text
    assert '--user "$(id -u):$(id -g)"' in text and '--user "$(id -u):$(id -g)"' in (ROOT / "README.md").read_text()
