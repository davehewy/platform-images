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


def test_build_supports_nerdctl_and_buildah_dry_runs(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})

    assert (
        main(
            ["images", "build", "curl", "--builder", "nerdctl", "--dry-run"],
            cwd=root,
            environment={},
        )
        == 0
    )
    nerdctl_plan = capsys.readouterr().out
    assert "nerdctl save" in nerdctl_plan
    assert "base=oci-layout://<verified-temporary-layout-for-base>" in nerdctl_plan

    assert (
        main(
            ["images", "build", "curl", "--builder", "buildah", "--dry-run"],
            cwd=root,
            environment={},
        )
        == 0
    )
    assert "base=container-image://localhost/platform-images/base:dev" in capsys.readouterr().out


def test_commands_discover_a_configured_nested_target_root(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({})
    target = root / "deploy" / "container-images" / "api"
    target.mkdir(parents=True)
    (target / "Containerfile").write_text("FROM alpine\n", encoding="utf-8")
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroot = "deploy/container-images"\n',
        encoding="utf-8",
    )

    assert main(["images", "list"], cwd=root, environment={}) == 0
    assert capsys.readouterr().out == "api\n"
    assert main(["images", "build", "api", "--dry-run"], cwd=root, environment={}) == 0
    assert "deploy/container-images/api/Containerfile" in capsys.readouterr().out


def test_commands_build_a_cross_root_dependency_chain(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({})
    base = root / "containers" / "shared" / "base"
    api = root / "services" / "payments" / "images" / "api"
    base.mkdir(parents=True)
    api.mkdir(parents=True)
    (base / "Containerfile").write_text("FROM alpine\n", encoding="utf-8")
    (api / "Dockerfile").write_text("FROM base\n", encoding="utf-8")
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + '\n[discovery]\nroots = ["containers/shared", "services/payments/images"]\n',
        encoding="utf-8",
    )

    assert main(["images", "graph"], cwd=root, environment={}) == 0
    assert capsys.readouterr().out == "base\n└── api\n"
    assert main(["images", "build", "api", "--dry-run"], cwd=root, environment={}) == 0
    output = capsys.readouterr().out
    assert "containers/shared/base/Containerfile" in output
    assert "services/payments/images/api/Dockerfile" in output


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
    assert "publish_image_manifest:" in output
    assert 'echo "No container images are affected."' in output
    assert "image-build-manifest.json" in output
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

    # GitHub exposes the base branch tip, not GitLab's merge-request diff-base semantic. The
    # generated workflow therefore leaves the GitLab-specific variable empty and calculates the
    # actual merge base from full history.
    assert _ci_base_head(
        {
            "CI_COMMIT_SHA": "github-head",
            "CI_MERGE_REQUEST_IID": "7",
            "CI_COMMIT_BEFORE_SHA": "",
            "CI_DEFAULT_BRANCH": "main",
        },
        git_client,  # type: ignore[arg-type]
    ) == ("merge-base", "github-head", BuildMode.MERGE_REQUEST)
    assert git_client.calls == [("github-head", "origin/main")]


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


def test_generate_github_workflow_writes_inside_repository(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n", "curl": "FROM base\n"})
    result = main(
        [
            "images",
            "generate-workflow",
            "github",
            "--output",
            ".github/workflows/images.yml",
        ],
        cwd=root,
        environment={},
    )

    assert result == 0
    assert capsys.readouterr().out == ".github/workflows/images.yml\n"
    workflow = (root / ".github" / "workflows" / "images.yml").read_text(encoding="utf-8")
    assert "image_layer_0:" in workflow
    assert "image_layer_1:" in workflow
    assert "--builder docker --registry-transport docker" in workflow


def test_generate_workflow_refuses_output_outside_repository(
    repository_factory: Callable[[dict[str, str]], Path], tmp_path: Path, capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    destination = tmp_path / "outside.yml"

    assert (
        main(
            [
                "images",
                "generate-workflow",
                "github",
                "--output",
                str(destination),
            ],
            cwd=root,
            environment={},
        )
        == 1
    )
    assert "must stay within" in capsys.readouterr().err


def test_build_manifest_command_writes_commit_handoff(
    repository_factory: Callable[[dict[str, str]], Path], capsys
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    digest = "sha256:" + "a" * 64
    results = root / "image-results"
    results.mkdir()
    (results / "base.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target": "base",
                "commit_sha": "commit",
                "source": "https://example.com/repository",
                "reference": "registry.example/platform-images/base:sha-commit",
                "digest": digest,
                "immutable_reference": f"registry.example/platform-images/base@{digest}",
                "input_references": {},
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "images",
                "build-manifest",
                str(results),
                "--mode",
                "default_branch",
                "--expected-target",
                "base",
                "--output",
                "image-build-manifest.json",
            ],
            cwd=root,
            environment={
                "CI_COMMIT_SHA": "commit",
                "CI_PROJECT_URL": "https://example.com/repository",
            },
        )
        == 0
    )
    assert capsys.readouterr().out == "image-build-manifest.json\n"
    manifest = json.loads((root / "image-build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["images"]["base"]["immutable_reference"].endswith(digest)


def test_promote_manifest_command_retags_only_digest_pinned_source(
    repository_factory: Callable[[dict[str, str]], Path], capsys, monkeypatch
) -> None:
    root = repository_factory({"base": "FROM alpine\n"})
    digest = "sha256:" + "b" * 64
    manifest = root / "image-build-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "default_branch",
                "base_sha": "before",
                "commit_sha": "commit",
                "source": "https://example.com/repository",
                "images": {
                    "base": {
                        "reference": "registry.example/platform-images/base:sha-commit",
                        "digest": digest,
                        "immutable_reference": f"registry.example/platform-images/base@{digest}",
                        "input_references": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[str, str]] = []

    class Registry:
        def promote(self, source: str, destination: str) -> None:
            calls.append((source, destination))

    monkeypatch.setattr(
        "platform_images.cli.ContainerRegistryClient",
        lambda *args, **kwargs: Registry(),
    )

    assert (
        main(
            [
                "images",
                "promote-manifest",
                str(manifest),
                "--tag",
                "v1.2.3",
                "--expected-commit",
                "commit",
                "--registry-transport",
                "docker",
            ],
            cwd=root,
            environment={},
        )
        == 0
    )
    assert calls == [
        (
            f"registry.example/platform-images/base@{digest}",
            "registry.example/platform-images/base:v1.2.3",
        )
    ]
    assert json.loads(capsys.readouterr().out)["promoted"][0]["target"] == "base"
