"""Exercise the fork's real release shell steps without contacting GitHub."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml
from awesomeversion import AwesomeVersion

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.check_output(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        text=True,
    ).strip()


@pytest.fixture
def release_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    component = repo / "custom_components/tuya_local"
    component.mkdir(parents=True)
    (component / "manifest.json").write_text(
        json.dumps({"domain": "tuya_local", "version": "2026.9.2"})
    )
    (component / "climate.py").write_text("# upstream\n")
    git(repo, "add", "custom_components")
    git(repo, "commit", "-m", "Upstream")
    upstream = git(repo, "rev-parse", "HEAD")
    (component / "euroster_schedule.py").write_text("# schedule support\n")
    git(repo, "add", "custom_components/tuya_local/euroster_schedule.py")
    git(repo, "commit", "-m", "Euroster schedule")
    # Untracked runtime files must never enter the public archive.
    (component / "private-untracked.txt").write_text("not part of a release")
    return repo, upstream


def run_workflow(tmp_path, repo, upstream, *, tip=None):
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    gh = tools / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "path = Path(os.environ['RELEASE_STATE'])\n"
        "state = json.loads(path.read_text()) if path.exists() else {}\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['release', 'view']:\n"
        "    sys.exit(0 if args[2] in state else 1)\n"
        "if args[:2] == ['release', 'create']:\n"
        "    state[args[2]] = args[3:]\n"
        "    path.write_text(json.dumps(state))\n"
        "elif args[0] == 'api':\n"
        "    print(os.environ['TIP_SHA'])\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    gh.chmod(0o755)
    state = tmp_path / "releases.json"
    if not state.exists():
        state.write_text(json.dumps({"2026.9.2-rel": []}))
    sha = git(repo, "rev-parse", "HEAD")
    env = {
        **os.environ,
        "PATH": f"{tools}:{os.environ['PATH']}",
        "GITHUB_WORKSPACE": str(ROOT),
        "GITHUB_SHA": sha,
        "GITHUB_REPOSITORY": "example/tuya-local",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "UPSTREAM_TAG": "2026.9.2-rel",
        "UPSTREAM_SHA": upstream,
        "TAG": "2026.9.2-rel",
        "TIP_SHA": tip or sha,
        "RELEASE_STATE": str(state),
    }
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-fork.yml").read_text())
    steps = workflow["jobs"]["release"]["steps"]
    start = next(i for i, s in enumerate(steps) if s.get("id") == "upstream") + 1
    for step in steps[start:]:
        if "run" in step:
            # Execute the checked-in workflow with a fake GitHub CLI.
            subprocess.run(  # noqa: S603
                ["bash", "-e", "-c", step["run"]],  # noqa: S607
                cwd=repo,
                env=env,
                check=True,
            )
    return json.loads(state.read_text())


def test_new_fork_fixes_are_installable_even_when_upstream_tag_exists(
    tmp_path, release_repo
):
    repo, upstream = release_repo
    releases = run_workflow(tmp_path, repo, upstream)
    tags = set(releases) - {"2026.9.2-rel"}
    assert len(tags) == 1, "Merged fork fixes must not be skipped as already released"
    tag = tags.pop()
    assert AwesomeVersion(tag) > AwesomeVersion("2026.9.2")
    assert AwesomeVersion(tag) < AwesomeVersion("2026.9.3")
    assert not AwesomeVersion(tag).beta
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == ""
    with ZipFile(repo / "dist/tuya_local.zip") as archive:
        assert "euroster_schedule.py" in archive.namelist()
        assert "private-untracked.txt" not in archive.namelist()
        assert json.loads(archive.read("manifest.json"))["version"] == tag
        assert json.loads(archive.read("fork_release.json"))["source_commit"] == git(
            repo, "rev-parse", "HEAD"
        )
    # Rerunning the same source neither moves an existing tag nor creates another release.
    assert run_workflow(tmp_path, repo, upstream) == releases
    (repo / "custom_components/tuya_local/climate.py").write_text("# next fix\n")
    git(repo, "add", "custom_components/tuya_local/climate.py")
    git(repo, "commit", "-m", "Next fix")
    next_releases = run_workflow(tmp_path, repo, upstream)
    new_tag = (set(next_releases) - set(releases)).pop()
    assert AwesomeVersion(new_tag) > AwesomeVersion(tag)


def test_old_queued_run_does_not_publish_over_newer_main(tmp_path, release_repo):
    repo, upstream = release_repo
    assert run_workflow(tmp_path, repo, upstream, tip="f" * 40) == {"2026.9.2-rel": []}


def test_release_archive_contains_complete_current_euroster_backend(tmp_path):
    from util.fork_release import build_release

    sha = git(ROOT, "rev-parse", "HEAD")
    upstream = git(ROOT, "rev-parse", "HEAD^")
    base = json.loads(
        (ROOT / "custom_components/tuya_local/manifest.json").read_text()
    )["version"]
    output = tmp_path / "artifact"
    version = build_release(f"{base}-rel", upstream, sha, output)
    first = (output / "tuya_local.zip").read_bytes()
    build_release(f"{base}-rel", upstream, sha, output)
    assert (output / "tuya_local.zip").read_bytes() == first
    with ZipFile(output / "tuya_local.zip") as archive:
        provenance = json.loads(archive.read("fork_release.json"))
        assert provenance["source_commit"] == sha
        assert provenance["version"] == version
        for name, checksum in provenance["sha256"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == checksum
        for name in (
            "euroster.py",
            "euroster_schedule.py",
            "services.py",
            "services.yaml",
            "helpers/device_config.py",
            "devices/euroster4040_thermostat.yaml",
        ):
            assert archive.read(name) == subprocess.check_output(  # noqa: S603
                [  # noqa: S607
                    "git",
                    "-C",
                    str(ROOT),
                    "show",
                    f"{sha}:custom_components/tuya_local/{name}",
                ]
            )
        profile = yaml.safe_load(archive.read("devices/euroster4040_thermostat.yaml"))
        climate = next(e for e in profile["entities"] if e["entity"] == "climate")
        hvac = next(d for d in climate["dps"] if d["name"] == "hvac_mode")
        assert hvac["mapping"][1]["value"] == "heat"
        assert {e.get("translation_key") for e in profile["entities"]} >= {
            "day_temperature",
            "night_temperature",
        }
        hacs = json.loads((ROOT / "hacs.json").read_text())
        assert hacs["zip_release"] is True
        assert hacs["filename"] == (output / "tuya_local.zip").name
