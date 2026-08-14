from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from platform_images.backends import TRANSPORTS
from platform_images.config import RepositoryConfig
from platform_images.errors import MissingStableImageError, ProcessError, RegistryError
from platform_images.models import (
    BuildBackend,
    RegistryAuthentication,
    RegistryProvider,
    RegistryTransport,
)
from platform_images.process import ProcessRunner
from platform_images.references import ReferencePolicy, immutable_reference

ECR_REGISTRY_RE = re.compile(
    r"^(?P<account>[0-9]{12})\.dkr\.ecr(?:-fips)?\."
    r"(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$"
)
DIGEST_RE = re.compile(r"^sha256:[0-9A-Fa-f]{64}$")
BEARER_PARAMETER_RE = re.compile(r'([A-Za-z][A-Za-z0-9_-]*)="([^"\\]*(?:\\.[^"\\]*)*)"')
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class _CredentialSafeRedirectHandler(HTTPRedirectHandler):
    """Never forward registry credentials to a different redirect authority."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        redirected = super().redirect_request(request, fp, code, message, headers, new_url)
        if redirected is not None and (
            urlparse(request.full_url).netloc.casefold() != urlparse(new_url).netloc.casefold()
        ):
            redirected.remove_header("Authorization")
        return redirected


def oci_registry_hostname(registry: str) -> str:
    normalized = registry.rstrip("/")
    if (
        not normalized
        or "://" in normalized
        or "/" in normalized
        or any(character.isspace() for character in normalized)
    ):
        raise RegistryError("OCI registry must be a hostname with an optional port, not a URL")
    parsed = urlparse(f"//{normalized}")
    try:
        _port = parsed.port
    except ValueError as exc:
        raise RegistryError("OCI registry has an invalid port") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RegistryError("OCI registry must be a hostname with an optional port, not a URL")
    return normalized


def ecr_registry_coordinates(registry: str) -> tuple[str, str, str]:
    normalized = registry.rstrip("/")
    match = ECR_REGISTRY_RE.fullmatch(normalized)
    if not match:
        raise RegistryError(
            "AWS ECR operation requires an ECR registry hostname of the form "
            "<12-digit-account>.dkr.ecr.<region>.amazonaws.com"
        )
    return normalized, match.group("account"), match.group("region")


class RegistryClient:
    def resolve_stable(self, target: str) -> str:
        raise NotImplementedError

    def promote(self, source: str, destination: str) -> None:
        raise NotImplementedError


class StaticRegistryClient(RegistryClient):
    def __init__(self, references: Mapping[str, str]) -> None:
        self.references = dict(references)

    def resolve_stable(self, target: str) -> str:
        try:
            reference = self.references[target]
        except KeyError as exc:
            raise MissingStableImageError(target) from exc
        if "@sha256:" not in reference:
            raise RegistryError(
                f'static stable reference for "{target}" is not pinned by sha256 digest'
            )
        return reference

    def promote(self, source: str, destination: str) -> None:
        del source, destination
        raise NotImplementedError("static reference clients cannot promote images")


class ECRRegistryClient(RegistryClient):
    def __init__(
        self,
        config: RepositoryConfig,
        registry: str,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.config = config
        self.registry = registry.rstrip("/")
        self.runner = runner or ProcessRunner()
        match = ECR_REGISTRY_RE.fullmatch(self.registry)
        self.registry_id = match.group("account") if match else None
        self.region = match.group("region") if match else None

    def resolve_stable(self, target: str) -> str:
        if self.registry_id is None or self.region is None:
            raise RegistryError(
                "automatic stable-image resolution requires an AWS ECR registry hostname; "
                "set PLATFORM_IMAGES_STABLE_REFS for another registry"
            )
        repository = self.config.image_repository(target)
        try:
            result = self.runner.run(
                [
                    "aws",
                    "ecr",
                    "describe-images",
                    "--registry-id",
                    self.registry_id,
                    "--region",
                    self.region,
                    "--repository-name",
                    repository,
                    "--image-ids",
                    f"imageTag={self.config.registry.stable_tag}",
                    "--query",
                    "imageDetails[0].imageDigest",
                    "--output",
                    "text",
                ],
                cwd=self.config.root,
                capture_output=True,
            )
        except ProcessError as exc:
            if "ImageNotFoundException" in str(exc):
                raise MissingStableImageError(target) from exc
            raise RegistryError(
                f'unable to resolve stable image for "{target}" in '
                f"AWS account {self.registry_id}, region {self.region}: {exc}"
            ) from exc
        digest = result.stdout.strip()
        if not digest.startswith("sha256:"):
            raise MissingStableImageError(target)
        stable = ReferencePolicy(self.config, self.registry).stable(target)
        return immutable_reference(stable, digest)

    def promote(self, source: str, destination: str) -> None:
        _container_promote(
            source,
            destination,
            transport=self.config.registry.transport,
            runner=self.runner,
            cwd=self.config.root,
        )


class OCIRegistryClient(RegistryClient):
    """Resolve tags through the OCI Distribution API without pulling image layers."""

    def __init__(
        self,
        config: RepositoryConfig,
        registry: str,
        environment: Mapping[str, str] | None = None,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.config = config
        self.registry = oci_registry_hostname(registry)
        self.environment = dict(os.environ if environment is None else environment)
        self.opener = opener or build_opener(_CredentialSafeRedirectHandler())
        self.username = self.environment.get(config.registry.username_environment_variable)
        self.password = self.environment.get(config.registry.password_environment_variable)
        if bool(self.username) != bool(self.password):
            raise RegistryError(
                "OCI registry credentials are incomplete; set both "
                f"{config.registry.username_environment_variable} and "
                f"{config.registry.password_environment_variable}"
            )
        if (
            config.registry.authentication is RegistryAuthentication.CREDENTIALS
            and not self.username
        ):
            raise RegistryError(
                "OCI registry access requires: "
                f"{config.registry.username_environment_variable}, "
                f"{config.registry.password_environment_variable}"
            )

    def resolve_stable(self, target: str) -> str:
        repository = self.config.image_repository(target)
        tag = self.config.registry.stable_tag
        digest = self._manifest_digest(repository, tag, target)
        stable = ReferencePolicy(self.config, self.registry).stable(target)
        return immutable_reference(stable, digest)

    def promote(self, source: str, destination: str) -> None:
        _container_promote(
            source,
            destination,
            transport=self.config.registry.transport,
            cwd=self.config.root,
        )

    def _manifest_digest(self, repository: str, tag: str, target: str) -> str:
        path = f"/v2/{quote(repository, safe='/')}/manifests/{quote(tag, safe='')}"
        url = f"{self.config.registry.scheme}://{self.registry}{path}"
        headers = {"Accept": MANIFEST_ACCEPT}
        if self.username and self.password:
            headers["Authorization"] = self._basic_authorization()
        try:
            response_headers, body = self._request(url, headers)
        except HTTPError as exc:
            if exc.code == 404:
                raise MissingStableImageError(target) from exc
            if exc.code != 401:
                raise RegistryError(
                    f"OCI registry manifest lookup failed for {repository}:{tag}: HTTP {exc.code}"
                ) from exc
            challenge = exc.headers.get("WWW-Authenticate", "")
            token = self._bearer_token(challenge, repository)
            try:
                response_headers, body = self._request(
                    url,
                    {"Accept": MANIFEST_ACCEPT, "Authorization": f"Bearer {token}"},
                )
            except HTTPError as retry:
                if retry.code == 404:
                    raise MissingStableImageError(target) from retry
                raise RegistryError(
                    f"OCI registry manifest lookup failed for {repository}:{tag}: HTTP {retry.code}"
                ) from retry
        except URLError as exc:
            raise RegistryError(
                f"unable to reach OCI registry {self.registry}: {exc.reason}"
            ) from exc
        digest = next(
            (
                value
                for name, value in response_headers.items()
                if name.casefold() == "docker-content-digest"
            ),
            None,
        )
        if digest is None:
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if DIGEST_RE.fullmatch(digest) is None:
            raise RegistryError(
                f"OCI registry returned an invalid manifest digest for {repository}:{tag}"
            )
        return digest.casefold()

    def _request(self, url: str, headers: Mapping[str, str]) -> tuple[Mapping[str, str], bytes]:
        request = Request(url, headers=dict(headers), method="GET")
        with self.opener.open(request, timeout=30) as response:
            return dict(response.headers.items()), response.read()

    def _basic_authorization(self) -> str:
        credentials = f"{self.username}:{self.password}".encode()
        return "Basic " + base64.b64encode(credentials).decode("ascii")

    def _bearer_token(self, challenge: str, repository: str) -> str:
        if not challenge.casefold().startswith("bearer "):
            raise RegistryError(
                "OCI registry rejected the manifest lookup without a supported Bearer challenge"
            )
        parameters = {
            match.group(1).casefold(): match.group(2).replace(r"\"", '"')
            for match in BEARER_PARAMETER_RE.finditer(challenge[7:])
        }
        realm = parameters.get("realm")
        if not realm:
            raise RegistryError("OCI registry Bearer challenge does not include a token realm")
        parsed_realm = urlparse(realm)
        if parsed_realm.scheme not in {"https", "http"} or not parsed_realm.netloc:
            raise RegistryError("OCI registry returned an invalid Bearer token realm")
        if parsed_realm.scheme != "https" and self.config.registry.scheme != "http":
            raise RegistryError("OCI registry refused to use HTTPS for its Bearer token realm")
        query = {
            "service": parameters.get("service", self.registry),
            "scope": parameters.get("scope", f"repository:{repository}:pull"),
        }
        separator = "&" if parsed_realm.query else "?"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.username and self.password:
            headers["Authorization"] = self._basic_authorization()
        try:
            _response_headers, body = self._request(
                realm + separator + urlencode(query),
                headers,
            )
            payload = json.loads(body)
        except HTTPError as exc:
            raise RegistryError(f"OCI registry token request failed: HTTP {exc.code}") from exc
        except (URLError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"OCI registry token response is invalid: {exc}") from exc
        token = (
            payload.get("token") or payload.get("access_token")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(token, str) or not token:
            raise RegistryError("OCI registry token response does not contain a token")
        return token


class ContainerRegistryClient(RegistryClient):
    """Registry transport used by the promotion command."""

    def __init__(
        self,
        cwd: Path,
        runner: ProcessRunner | None = None,
        transport: RegistryTransport = RegistryTransport.PODMAN,
    ) -> None:
        self.cwd = cwd
        self.runner = runner or ProcessRunner()
        self.transport = RegistryTransport(transport)

    def resolve_stable(self, target: str) -> str:
        del target
        raise NotImplementedError("container transports do not resolve stable registry tags")

    def promote(self, source: str, destination: str) -> None:
        _container_promote(
            source,
            destination,
            transport=self.transport,
            runner=self.runner,
            cwd=self.cwd,
        )


class PodmanRegistryClient(ContainerRegistryClient):
    """Backward-compatible Podman registry transport."""


def registry_from_environment_json(value: str | None) -> RegistryClient | None:
    if value is None:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        raise ValueError("PLATFORM_IMAGES_STABLE_REFS must be a JSON object of strings")
    return StaticRegistryClient(parsed)


def login_to_ecr(
    registry: str,
    *,
    cwd: Path,
    runner: ProcessRunner | None = None,
    transport: RegistryTransport | None = None,
    engine: BuildBackend | None = None,
) -> None:
    if transport is not None and engine is not None and transport.value != engine.value:
        raise ValueError("transport and legacy engine select different executables")
    transport = transport or RegistryTransport(engine.value if engine else "podman")
    normalized, _account, region = ecr_registry_coordinates(registry)
    process = runner or ProcessRunner()
    password = process.run(
        ["aws", "ecr", "get-login-password", "--region", region],
        cwd=cwd,
        capture_output=True,
    ).stdout
    if not password.strip():
        raise RegistryError("AWS returned an empty ECR login password")
    process.run(
        [
            TRANSPORTS[transport].executable,
            "login",
            "--username",
            "AWS",
            "--password-stdin",
            normalized,
        ],
        cwd=cwd,
        capture_output=True,
        input_text=password,
    )


def login_to_registry(
    config: RepositoryConfig,
    registry: str,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: Path,
    runner: ProcessRunner | None = None,
    transport: RegistryTransport | None = None,
) -> bool:
    """Authenticate the configured transport; return False for deliberate ambient auth."""
    selected_transport = transport or config.registry.transport
    if config.registry.provider is RegistryProvider.ECR:
        login_to_ecr(registry, cwd=cwd, runner=runner, transport=selected_transport)
        return True
    if config.registry.authentication is RegistryAuthentication.AMBIENT:
        oci_registry_hostname(registry)
        return False
    values = dict(os.environ if environment is None else environment)
    username = values.get(config.registry.username_environment_variable)
    password = values.get(config.registry.password_environment_variable)
    missing = [
        name
        for name, value in (
            (config.registry.username_environment_variable, username),
            (config.registry.password_environment_variable, password),
        )
        if not value
    ]
    if missing:
        raise RegistryError("OCI registry login requires: " + ", ".join(missing))
    process = runner or ProcessRunner()
    normalized = oci_registry_hostname(registry)
    process.run(
        [
            TRANSPORTS[selected_transport].executable,
            "login",
            "--username",
            username,
            "--password-stdin",
            normalized,
        ],
        cwd=cwd,
        capture_output=True,
        input_text=password,
    )
    return True


def _container_promote(
    source: str,
    destination: str,
    *,
    runner: ProcessRunner | None = None,
    cwd: Path | None = None,
    transport: RegistryTransport = RegistryTransport.PODMAN,
) -> None:
    process = runner or ProcessRunner()
    executable = TRANSPORTS[transport].executable
    process.run([executable, "pull", source], cwd=cwd)
    process.run([executable, "tag", source, destination], cwd=cwd)
    process.run([executable, "push", destination], cwd=cwd)
