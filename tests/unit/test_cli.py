from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import git

from platform_images.cli import _ci_base_head, main
from platform_images.models import BuildMode


def test_list_validate_graph_and_dry_run(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    assert main(["images", "list"], cwd=root, environment={}) == 0
    assert capsys.readouterr().out == "base\ncurl\n"

    assert main(["images", "validate", "--format", "json"], cwd=root, environment={}) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

    assert main(["images", "graph"], cwd=root, environment={}) == 0
    assert capsys.readouterr().out == "base\n└── curl\n"

    assert main(["images", "build", "curl", "--dry-run"], cwd=root, environment={}) == 0
    commands = capsys.readouterr().out
    assert commands.index("--tag localhost/platform-images/base:dev") < commands.index(
        "--tag localhost/platform-images/curl:dev"
    )
    assert "base=container-image://localhost/platform-images/base:dev" in commands


def test_graph_uses_ascii_tree_on_windows(
    repository_factory: Callable[[dict[str, str]], Path], capsys, monkeypatch
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    monkeypatch.setattr("platform_images.cli.sys.platform", "win32")

    assert main(["images", "graph"], cwd=root, environment={}) == 0

    assert capsys.readouterr().out == "base\n\\-- curl\n"


def test_validation_exit_code_and_json_error_code(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"api": "ARG BASE\nFROM ${BASE}\n"})
    assert main(["images", "validate", "--format", "json"], cwd=root, environment={}) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["errors"][0]["code"] == "unresolved-reference"
    assert main(["images", "graph"], cwd=root, environment={}) == 1
    assert "Validation failed" in capsys.readouterr().err


def test_cli_changed_affected_and_change_plan(git_repository: Path, capsys) -> None:
    root = git_repository
    base = git(root, "rev-parse", "HEAD")
    (root / "images" / "base" / "Dockerfile").write_text(
        "FROM alpine:3.22\nRUN echo changed\n", encoding="utf-8"
    )
    git(root, "add", ".")
    git(root, "commit", "-qm", "base change")
    head = git(root, "rev-parse", "HEAD")

    args = ["images", "changed", "--base", base, "--head", head]
    assert main(args, cwd=root, environment={}) == 0
    assert capsys.readouterr().out == "base\n"

    args[1] = "affected"
    assert main(args, cwd=root, environment={}) == 0
    assert capsys.readouterr().out == "base\ncurl\n"

    environment = {
        "PLATFORM_IMAGES_REGISTRY": "registry.example.com",
        "CI_PIPELINE_ID": "55",
        "CI_MERGE_REQUEST_IID": "2",
    }
    plan_args = [
        "images",
        "plan",
        "--base",
        base,
        "--head",
        head,
        "--format",
        "json",
    ]
    assert main(plan_args, cwd=root, environment=environment) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [target["name"] for target in plan["targets"]] == ["base", "curl"]
    assert plan["targets"][1]["needs"] == ["base"]


def test_ci_no_change_generates_noop_child(git_repository: Path, capsys) -> None:
    root = git_repository
    base = git(root, "rev-parse", "HEAD")
    (root / "notes.md").write_text("docs only", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "docs")
    head = git(root, "rev-parse", "HEAD")
    environment = {
        "PLATFORM_IMAGES_REGISTRY": "registry.example.com",
        "CI_PIPELINE_ID": "55",
        "CI_MERGE_REQUEST_IID": "2",
        "CI_MERGE_REQUEST_DIFF_BASE_SHA": base,
        "CI_COMMIT_SHA": head,
    }
    assert (
        main(
            ["images", "plan", "--ci", "--format", "gitlab"],
            cwd=root,
            environment=environment,
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "no_image_changes:" in output
    assert "No container images are affected." in output


class FakeGit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def merge_base(self, left: str, right: str) -> str:
        self.calls.append((left, right))
        return "merge-base"


def test_ci_base_and_head_resolution() -> None:
    git_client = FakeGit()
    assert _ci_base_head(
        {
            "CI_COMMIT_SHA": "head",
            "CI_MERGE_REQUEST_IID": "1",
            "CI_MERGE_REQUEST_DIFF_BASE_SHA": "mr-base",
        },
        git_client,  # type: ignore[arg-type]
    ) == ("mr-base", "head", BuildMode.MERGE_REQUEST)
    assert _ci_base_head(
        {
            "CI_COMMIT_SHA": "head",
            "CI_COMMIT_BEFORE_SHA": "push-base",
            "CI_COMMIT_BRANCH": "main",
            "CI_DEFAULT_BRANCH": "main",
        },
        git_client,  # type: ignore[arg-type]
    ) == ("push-base", "head", BuildMode.DEFAULT_BRANCH)
    assert _ci_base_head(
        {
            "CI_COMMIT_SHA": "head",
            "CI_COMMIT_BEFORE_SHA": "0" * 40,
            "CI_COMMIT_BRANCH": "main",
            "CI_DEFAULT_BRANCH": "main",
        },
        git_client,  # type: ignore[arg-type]
    ) == (None, "head", BuildMode.DEFAULT_BRANCH)
    assert git_client.calls == []


def test_first_default_branch_pipeline_bootstraps_every_target(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    result = main(
        ["images", "plan", "--ci", "--format", "json"],
        cwd=root,
        environment={
            "PLATFORM_IMAGES_REGISTRY": "registry.example.com",
            "CI_PIPELINE_ID": "1",
            "CI_COMMIT_SHA": "first-commit",
            "CI_COMMIT_BEFORE_SHA": "0" * 40,
            "CI_COMMIT_BRANCH": "main",
            "CI_DEFAULT_BRANCH": "main",
        },
    )
    assert result == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["base_sha"] is None
    assert [target["name"] for target in plan["targets"]] == ["base", "curl"]
    assert all("all-images" in target["reasons"] for target in plan["targets"])


def test_render_plan_uses_persisted_json_and_rejects_graph_drift(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    environment = {
        "PLATFORM_IMAGES_REGISTRY": "registry.example.com",
        "CI_PIPELINE_ID": "42",
        "CI_COMMIT_SHA": "abc",
        "CI_COMMIT_BRANCH": "main",
        "CI_DEFAULT_BRANCH": "main",
    }
    assert (
        main(
            ["images", "plan", "--ci", "--all", "--format", "json"],
            cwd=root,
            environment=environment,
        )
        == 0
    )
    plan_path = root / "image-plan.json"
    plan_path.write_text(capsys.readouterr().out, encoding="utf-8")

    assert (
        main(
            ["images", "render-plan", str(plan_path), "--format", "gitlab"],
            cwd=root,
            environment=environment,
        )
        == 0
    )
    assert "image_base:" in capsys.readouterr().out

    data = json.loads(plan_path.read_text(encoding="utf-8"))
    data["targets"][1]["dependencies"] = []
    plan_path.write_text(json.dumps(data), encoding="utf-8")
    assert (
        main(
            ["images", "render-plan", str(plan_path), "--format", "gitlab"],
            cwd=root,
            environment=environment,
        )
        == 1
    )
    assert "dependencies do not match graph" in capsys.readouterr().err


def test_argparse_invalid_invocation_exits_two() -> None:
    with pytest.raises(SystemExit) as error:
        main(["images", "changed", "--base", "one"])
    assert error.value.code == 2
