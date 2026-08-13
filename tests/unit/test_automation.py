from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[2]
PINNED_ACTION = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def load_workflow(name: str) -> dict[str, object]:
    value = yaml.load(
        (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    assert isinstance(value, dict)
    return value


def action_references(workflow: dict[str, object]) -> list[str]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    references: list[str] = []
    for job in jobs.values():
        assert isinstance(job, dict)
        steps = job["steps"]
        assert isinstance(steps, list)
        for step in steps:
            assert isinstance(step, dict)
            if "uses" in step:
                references.append(str(step["uses"]))
    return references


def test_github_actions_are_immutable_and_ci_gates_release() -> None:
    ci = load_workflow("ci.yml")
    release = load_workflow("release.yml")

    assert all(PINNED_ACTION.fullmatch(value) for value in action_references(ci))
    assert all(PINNED_ACTION.fullmatch(value) for value in action_references(release))
    release_events = release["on"]
    assert isinstance(release_events, dict)
    workflow_run = release_events["workflow_run"]
    assert isinstance(workflow_run, dict)
    assert workflow_run["workflows"] == ["CI"]

    release_job = release["jobs"]["release"]  # type: ignore[index]
    assert release_job["permissions"] == {"contents": "write"}  # type: ignore[index]
    assert any(
        "workflow_run.head_sha" in str(step.get("env", {}))
        for step in release_job["steps"]  # type: ignore[index]
    )


def test_release_workflow_builds_all_supported_standalone_platforms() -> None:
    release = load_workflow("release.yml")
    jobs = release["jobs"]
    assert isinstance(jobs, dict)
    standalone = jobs["standalone"]
    publish = jobs["publish-standalone"]
    verify = jobs["verify-installer"]
    assert isinstance(standalone, dict)
    assert isinstance(publish, dict)
    assert isinstance(verify, dict)
    entries = standalone["strategy"]["matrix"]["include"]  # type: ignore[index]
    assert {(entry["os"], entry["arch"]) for entry in entries} == {
        ("linux", "amd64"),
        ("linux", "arm64"),
        ("darwin", "amd64"),
        ("darwin", "arm64"),
        ("windows", "amd64"),
        ("windows", "arm64"),
    }
    assert standalone["needs"] == "release"
    dependency_step = next(
        step
        for step in standalone["steps"]
        if step["name"] == "Install standalone build dependencies"
    )
    assert "steps.python.outputs.python-path" in dependency_step["run"]
    build_step = next(
        step
        for step in standalone["steps"]
        if step["name"] == "Build and smoke-test standalone archive"
    )
    assert "steps.python.outputs.python-path" in build_step["run"]
    assert publish["needs"] == ["release", "standalone"]
    assert publish["permissions"] == {"contents": "write"}
    upload_step = next(
        step
        for step in publish["steps"]
        if step["name"] == "Attach standalone archives to the GitHub release"
    )
    assert upload_step["env"]["GH_REPO"] == "${{ github.repository }}"
    assert verify["needs"] == ["release", "publish-standalone"]
    assert {entry["platform"] for entry in verify["strategy"]["matrix"]["include"]} == {
        entry["platform"] for entry in entries
    }


def test_standalone_archive_names_are_stable_for_latest_downloads() -> None:
    release = load_workflow("release.yml")
    entries = release["jobs"]["standalone"]["strategy"]["matrix"]["include"]  # type: ignore[index]
    assert {entry["platform"] for entry in entries} == {
        "linux-amd64",
        "linux-arm64",
        "darwin-amd64",
        "darwin-arm64",
        "windows-amd64",
        "windows-arm64",
    }


def test_installer_is_executable_and_references_every_release_asset() -> None:
    installer = ROOT / "scripts" / "install.sh"
    assert installer.stat().st_mode & 0o111
    contents = installer.read_text(encoding="utf-8")
    assert "platform-images-${operating_system}-${architecture}.tar.gz" in contents
    assert "SHA256SUMS" in contents
    assert "linux" in contents
    assert "darwin" in contents
    windows_installer = ROOT / "scripts" / "install.ps1"
    windows_contents = windows_installer.read_text(encoding="utf-8")
    assert "platform-images-windows-$architecture.zip" in windows_contents
    assert "SHA256SUMS" in windows_contents
    assert "Get-FileHash" in windows_contents


def test_issue_forms_and_support_links_are_configured() -> None:
    templates = ROOT / ".github" / "ISSUE_TEMPLATE"
    bug_report = yaml.safe_load((templates / "bug_report.yml").read_text(encoding="utf-8"))
    feature_request = yaml.safe_load(
        (templates / "feature_request.yml").read_text(encoding="utf-8")
    )
    issue_config = yaml.safe_load((templates / "config.yml").read_text(encoding="utf-8"))
    funding = yaml.safe_load((ROOT / ".github" / "FUNDING.yml").read_text(encoding="utf-8"))

    assert bug_report["name"] == "Bug report"
    assert feature_request["name"] == "Feature request"
    assert issue_config["blank_issues_enabled"] is False
    assert funding["custom"] == ["https://buymeacoffee.com/davehewy"]


def test_release_and_commit_conventions_share_project_version() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)

    commitizen = project["tool"]["commitizen"]
    semantic_release = project["tool"]["semantic_release"]
    assert commitizen["name"] == "cz_conventional_commits"
    assert commitizen["version_provider"] == "pep621"
    assert semantic_release["version_toml"] == ["pyproject.toml:project.version"]
    assert semantic_release["version_variables"] == ["src/platform_images/__init__.py:__version__"]
    assert semantic_release["assets"] == ["uv.lock"]
    assert semantic_release["build_command"] == "python scripts/build-release.py"
    assert semantic_release["publish"]["upload_to_vcs_release"] is True


def test_pre_commit_installs_commit_message_and_pre_push_gates() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    assert config["default_install_hook_types"] == [
        "pre-commit",
        "commit-msg",
        "pre-push",
    ]
    hooks = {hook["id"]: hook for repository in config["repos"] for hook in repository["hooks"]}
    assert hooks["commitizen"]["stages"] == ["commit-msg"]
    assert hooks["unit-tests"]["stages"] == ["pre-push"]
