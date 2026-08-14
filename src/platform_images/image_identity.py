from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass


def repository_name(reference: str) -> str:
    """Return an image repository without a tag or digest."""
    without_digest = reference.split("@", 1)[0]
    slash = without_digest.rfind("/")
    colon = without_digest.rfind(":")
    return without_digest[:colon] if colon > slash else without_digest


def registry_relative_repository(reference: str) -> str:
    """Remove a Docker-style registry hostname while retaining the repository path."""
    repository = repository_name(reference)
    first, separator, remainder = repository.partition("/")
    registry_qualified = separator and (first == "localhost" or "." in first or ":" in first)
    return remainder if registry_qualified else repository


@dataclass(frozen=True)
class ImageIdentityResolver:
    identities: Mapping[str, frozenset[str]]
    managed_prefixes: frozenset[str]

    def candidates(self, reference: str) -> frozenset[str]:
        repository = repository_name(reference)
        relative = registry_relative_repository(repository)
        return self.identities.get(repository, frozenset()) | self.identities.get(
            relative, frozenset()
        )

    def is_managed(self, reference: str) -> bool:
        repository = repository_name(reference)
        relative = registry_relative_repository(repository)
        return any(
            candidate.startswith(f"{prefix}/")
            for candidate in {repository, relative}
            for prefix in self.managed_prefixes
        )

    @property
    def collisions(self) -> Mapping[str, frozenset[str]]:
        return {
            identity: targets for identity, targets in self.identities.items() if len(targets) > 1
        }


def build_image_identity_resolver(
    target_names: Iterable[str],
    namespace: str,
    *,
    repositories: Mapping[str, str] | None = None,
    aliases: Mapping[str, tuple[str, ...]] | None = None,
) -> ImageIdentityResolver:
    configured_repositories = repositories or {}
    configured_aliases = aliases or {}
    identities: dict[str, set[str]] = {}
    managed_prefixes: set[str] = set()

    def register(identity: str, target: str, *, managed: bool) -> None:
        repository = repository_name(identity)
        relative = registry_relative_repository(repository)
        for candidate in {repository, relative}:
            identities.setdefault(candidate, set()).add(target)
            if managed and "/" in candidate:
                managed_prefixes.add(candidate.rsplit("/", 1)[0])

    for target in sorted(target_names):
        register(target, target, managed=False)
        repository = configured_repositories.get(target, f"{namespace.strip('/')}/{target}")
        register(repository, target, managed=True)
        for alias in configured_aliases.get(target, ()):
            register(alias, target, managed=True)

    return ImageIdentityResolver(
        {identity: frozenset(targets) for identity, targets in sorted(identities.items())},
        frozenset(managed_prefixes),
    )
