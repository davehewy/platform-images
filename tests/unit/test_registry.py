from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from platform_images.config import RepositoryConfig
from platform_images.errors import MissingStableImageError, ProcessError, RegistryError
from platform_images.models import BuildMode, RegistryTransport
from platform_images.process import ProcessResult
from platform_images.references import ReferencePolicy, immutable_reference
from platform_images.registry import (
    ContainerRegistryClient,
    ECRRegistryClient,
    OCIRegistryClient,
    PodmanRegistryClient,
    StaticRegistryClient,
    _CredentialSafeRedirectHandler,
    login_to_ecr,
    login_to_registry,
)


class RegistryRunner:
    def __init__(self, output: str = "", fail: bool = False) -> None:
        self.output = output
        self.fail = fail
        self.commands: list[list[str]] = []
        self.inputs: list[str | None] = []

    def run(
        self,
        arguments: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        capture_output: bool = False,
        input_text: str | None = None,
    ) -> ProcessResult:
        del cwd, env, capture_output
        self.commands.append(list(arguments))
        self.inputs.append(input_text)
        if self.fail:
            raise ProcessError("failed")
        return ProcessResult(self.output, "", 0)


class RegistryResponse:
    def __init__(self, body: bytes, headers: Mapping[str, str] | None = None) -> None:
        self.body = body
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def __enter__(self):
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def read(self) -> bytes:
        return self.body


class RegistryOpener:
    def __init__(self, *responses: RegistryResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[Request] = []

    def open(self, request: Request, timeout: int = 30) -> RegistryResponse:
        del timeout
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _oci_config(root: Path, *, authentication: str = "credentials") -> RepositoryConfig:
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            'stable_tag = "main"',
            f'stable_tag = "main"\nprovider = "oci"\nauthentication = "{authentication}"',
        ),
        encoding="utf-8",
    )
    return RepositoryConfig.load(root)


def _http_error(code: int, authenticate: str = "") -> HTTPError:
    headers = Message()
    if authenticate:
        headers["WWW-Authenticate"] = authenticate
    return HTTPError(
        "https://registry.example/v2/image/manifests/main", code, "error", headers, None
    )


def test_oci_redirects_do_not_forward_credentials_to_another_authority() -> None:
    request = Request(
        "https://registry.example/v2/image/manifests/main",
        headers={"Authorization": "Basic secret"},
    )

    redirected = _CredentialSafeRedirectHandler().redirect_request(
        request,
        None,
        302,
        "Found",
        Message(),
        "https://storage.example/manifest",
    )

    assert redirected is not None
    assert redirected.get_header("Authorization") is None


def test_reference_policy(repository_factory: Callable[[dict[str, str]], Path]) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    policy = ReferencePolicy(config, "registry.example.com/", "12")
    assert policy.local("base") == "localhost/platform-images/base:dev"
    assert policy.stable("base") == "registry.example.com/platform-images/base:main"
    assert immutable_reference(policy.stable("base"), "sha256:x") == (
        "registry.example.com/platform-images/base@sha256:x"
    )


def test_reference_policy_uses_target_published_repository(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    root = repository_factory({"ubuntu-base-24-04": "FROM alpine\n"})
    config_path = root / "platform-images.toml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n[images.ubuntu-base-24-04]\n"
        + 'repository = "gitlab/ubuntu-base-24-04"\n',
        encoding="utf-8",
    )
    policy = ReferencePolicy(RepositoryConfig.load(root), "nexus.example.com", "12")

    assert policy.local("ubuntu-base-24-04") == ("localhost/gitlab/ubuntu-base-24-04:dev")
    assert policy.stable("ubuntu-base-24-04") == ("nexus.example.com/gitlab/ubuntu-base-24-04:main")
    assert policy.output("ubuntu-base-24-04", BuildMode.DEFAULT_BRANCH, "abc") == (
        "nexus.example.com/gitlab/ubuntu-base-24-04:sha-abc"
    )

    runner = RegistryRunner("sha256:abc\n")
    ecr_registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
    resolved = ECRRegistryClient(
        policy.config,
        ecr_registry,
        runner,  # type: ignore[arg-type]
    ).resolve_stable("ubuntu-base-24-04")
    assert resolved == f"{ecr_registry}/gitlab/ubuntu-base-24-04@sha256:abc"
    assert "gitlab/ubuntu-base-24-04" in runner.commands[0]


def test_ecr_resolver_returns_immutable_digest(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    runner = RegistryRunner("sha256:abc\n")
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
    client = ECRRegistryClient(config, registry, runner)  # type: ignore[arg-type]
    assert client.resolve_stable("base") == (f"{registry}/platform-images/base@sha256:abc")
    assert runner.commands[0][0:3] == ["aws", "ecr", "describe-images"]
    assert runner.commands[0][3:7] == [
        "--registry-id",
        "123456789012",
        "--region",
        "eu-west-2",
    ]
    assert "platform-images/base" in runner.commands[0]


def test_ecr_and_static_resolvers_fail_clearly(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"
    with pytest.raises(MissingStableImageError):
        ECRRegistryClient(config, registry, RegistryRunner("None\n")).resolve_stable("base")  # type: ignore[arg-type]
    with pytest.raises(MissingStableImageError):
        StaticRegistryClient({}).resolve_stable("base")
    with pytest.raises(RegistryError, match="not pinned"):
        StaticRegistryClient({"base": f"{registry}/platform-images/base:main"}).resolve_stable(
            "base"
        )


def test_ecr_resolver_rejects_ambiguous_registry_and_surfaces_operational_failure(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    with pytest.raises(RegistryError, match="requires an AWS ECR registry hostname"):
        ECRRegistryClient(config, "registry.example.com").resolve_stable("base")
    with pytest.raises(RegistryError, match="eu-west-2"):
        ECRRegistryClient(
            config,
            "123456789012.dkr.ecr.eu-west-2.amazonaws.com",
            RegistryRunner(fail=True),  # type: ignore[arg-type]
        ).resolve_stable("base")


def test_oci_resolver_returns_header_digest_without_pulling_layers(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = _oci_config(repository_factory({"base": "FROM alpine\n"}))
    digest = "sha256:" + "a" * 64
    opener = RegistryOpener(
        RegistryResponse(b'{"schemaVersion":2}', {"docker-content-digest": digest})
    )

    resolved = OCIRegistryClient(
        config,
        "nexus.example.com",
        {
            "PLATFORM_IMAGES_REGISTRY_USERNAME": "ci-user",
            "PLATFORM_IMAGES_REGISTRY_PASSWORD": "secret",
        },
        opener,  # type: ignore[arg-type]
    ).resolve_stable("base")

    assert resolved == f"nexus.example.com/platform-images/base@{digest}"
    assert opener.requests[0].full_url.endswith("/v2/platform-images/base/manifests/main")
    assert opener.requests[0].get_header("Authorization", "").startswith("Basic ")


def test_oci_resolver_completes_bearer_challenge_and_computes_missing_digest(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = _oci_config(repository_factory({"base": "FROM alpine\n"}))
    manifest = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
    challenge = (
        'Bearer realm="https://auth.example/token",service="registry.example",'
        'scope="repository:platform-images/base:pull"'
    )
    opener = RegistryOpener(
        _http_error(401, challenge),
        RegistryResponse(b'{"token":"bearer-token"}'),
        RegistryResponse(manifest),
    )

    resolved = OCIRegistryClient(
        config,
        "registry.example",
        {
            "PLATFORM_IMAGES_REGISTRY_USERNAME": "ci-user",
            "PLATFORM_IMAGES_REGISTRY_PASSWORD": "secret",
        },
        opener,  # type: ignore[arg-type]
    ).resolve_stable("base")

    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    assert resolved == f"registry.example/platform-images/base@{digest}"
    assert opener.requests[1].full_url == (
        "https://auth.example/token?service=registry.example&"
        "scope=repository%3Aplatform-images%2Fbase%3Apull"
    )
    assert opener.requests[2].get_header("Authorization") == "Bearer bearer-token"


def test_oci_resolver_reports_missing_stable_image(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = _oci_config(repository_factory({"base": "FROM alpine\n"}), authentication="ambient")
    with pytest.raises(MissingStableImageError):
        OCIRegistryClient(
            config,
            "registry.example",
            {},
            RegistryOpener(_http_error(404)),  # type: ignore[arg-type]
        ).resolve_stable("base")


@pytest.mark.parametrize(
    "registry",
    ("https://registry.example", "registry.example/team", "registry.example:wrong", "bad host"),
)
def test_oci_resolver_rejects_values_that_are_not_registry_hostnames(
    repository_factory: Callable[[dict[str, str]], Path], registry: str
) -> None:
    config = _oci_config(repository_factory({"base": "FROM alpine\n"}))

    with pytest.raises(RegistryError, match="hostname|port"):
        OCIRegistryClient(
            config,
            registry,
            {
                "PLATFORM_IMAGES_REGISTRY_USERNAME": "ci-user",
                "PLATFORM_IMAGES_REGISTRY_PASSWORD": "secret",
            },
        )


def test_oci_resolver_requires_complete_configured_credentials(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = _oci_config(repository_factory({"base": "FROM alpine\n"}))

    with pytest.raises(RegistryError, match="requires"):
        OCIRegistryClient(config, "registry.example", {})
    with pytest.raises(RegistryError, match="incomplete"):
        OCIRegistryClient(
            config,
            "registry.example",
            {"PLATFORM_IMAGES_REGISTRY_USERNAME": "ci-user"},
        )


def test_ecr_missing_image_error_is_not_misreported_as_aws_outage(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = RepositoryConfig.load(repository_factory({"base": "FROM alpine\n"}))
    runner = RegistryRunner()
    runner.fail = True

    def missing(*args, **kwargs):
        del args, kwargs
        raise ProcessError("ImageNotFoundException")

    runner.run = missing  # type: ignore[method-assign]
    with pytest.raises(MissingStableImageError):
        ECRRegistryClient(
            config,
            "123456789012.dkr.ecr.eu-west-2.amazonaws.com",
            runner,  # type: ignore[arg-type]
        ).resolve_stable("base")


def test_promotion_is_pull_tag_push() -> None:
    runner = RegistryRunner()
    PodmanRegistryClient(Path.cwd(), runner).promote(  # type: ignore[arg-type]
        "registry/base:sha-a", "registry/base:main"
    )
    assert runner.commands == [
        ["podman", "pull", "registry/base:sha-a"],
        ["podman", "tag", "registry/base:sha-a", "registry/base:main"],
        ["podman", "push", "registry/base:main"],
    ]


def test_docker_promotion_is_pull_tag_push() -> None:
    runner = RegistryRunner()
    ContainerRegistryClient(
        Path.cwd(),
        runner,
        RegistryTransport.DOCKER,  # type: ignore[arg-type]
    ).promote("registry/base:sha-a", "registry/base:main")
    assert runner.commands == [
        ["docker", "pull", "registry/base:sha-a"],
        ["docker", "tag", "registry/base:sha-a", "registry/base:main"],
        ["docker", "push", "registry/base:main"],
    ]


def test_ecr_login_uses_registry_region_and_password_stdin() -> None:
    runner = RegistryRunner("secret-token\n")
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"

    login_to_ecr(registry, cwd=Path.cwd(), runner=runner)  # type: ignore[arg-type]

    assert runner.commands == [
        ["aws", "ecr", "get-login-password", "--region", "eu-west-2"],
        [
            "podman",
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            registry,
        ],
    ]
    assert runner.inputs == [None, "secret-token\n"]


def test_oci_login_uses_configured_credentials_and_password_stdin(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = _oci_config(repository_factory({"base": "FROM alpine\n"}))
    runner = RegistryRunner()

    authenticated = login_to_registry(
        config,
        "nexus.example.com",
        environment={
            "PLATFORM_IMAGES_REGISTRY_USERNAME": "ci-user",
            "PLATFORM_IMAGES_REGISTRY_PASSWORD": "secret-token",
        },
        cwd=config.root,
        runner=runner,  # type: ignore[arg-type]
        transport=RegistryTransport.DOCKER,
    )

    assert authenticated
    assert runner.commands == [
        [
            "docker",
            "login",
            "--username",
            "ci-user",
            "--password-stdin",
            "nexus.example.com",
        ]
    ]
    assert runner.inputs == ["secret-token"]


def test_oci_ambient_login_is_an_explicit_noop(
    repository_factory: Callable[[dict[str, str]], Path],
) -> None:
    config = _oci_config(repository_factory({"base": "FROM alpine\n"}), authentication="ambient")
    runner = RegistryRunner()

    assert not login_to_registry(
        config,
        "registry.example",
        environment={},
        cwd=config.root,
        runner=runner,  # type: ignore[arg-type]
    )
    assert not runner.commands


def test_ecr_login_supports_docker_password_stdin() -> None:
    runner = RegistryRunner("secret-token\n")
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"

    login_to_ecr(
        registry,
        cwd=Path.cwd(),
        runner=runner,  # type: ignore[arg-type]
        transport=RegistryTransport.DOCKER,
    )

    assert runner.commands[-1] == [
        "docker",
        "login",
        "--username",
        "AWS",
        "--password-stdin",
        registry,
    ]


@pytest.mark.parametrize(
    "transport",
    (RegistryTransport.NERDCTL, RegistryTransport.BUILDAH),
)
def test_ecr_login_supports_additional_password_stdin_transports(
    transport: RegistryTransport,
) -> None:
    runner = RegistryRunner("secret-token\n")
    registry = "123456789012.dkr.ecr.eu-west-2.amazonaws.com"

    login_to_ecr(
        registry,
        cwd=Path.cwd(),
        runner=runner,  # type: ignore[arg-type]
        transport=transport,
    )

    assert runner.commands[-1] == [
        transport.value,
        "login",
        "--username",
        "AWS",
        "--password-stdin",
        registry,
    ]


@pytest.mark.parametrize("transport", (RegistryTransport.NERDCTL, RegistryTransport.BUILDAH))
def test_additional_promotion_transports_are_pull_tag_push(
    transport: RegistryTransport,
) -> None:
    runner = RegistryRunner()

    ContainerRegistryClient(
        Path.cwd(),
        runner,
        transport,  # type: ignore[arg-type]
    ).promote("registry/base:sha-a", "registry/base:main")

    assert [command[0] for command in runner.commands] == [transport.value] * 3
    assert [command[1] for command in runner.commands] == ["pull", "tag", "push"]
