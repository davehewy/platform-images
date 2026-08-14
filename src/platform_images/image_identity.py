from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from difflib import SequenceMatcher


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
    target_names: frozenset[str]
    external_repositories: frozenset[str]

    def candidates(self, reference: str) -> frozenset[str]:
        repository = repository_name(reference)
        relative = registry_relative_repository(repository)
        configured = self.identities.get(repository, frozenset()) | self.identities.get(
            relative, frozenset()
        )
        if configured:
            return configured
        if self.is_explicit_external(repository) or "/" not in repository:
            return frozenset()
        basename = repository.rsplit("/", 1)[-1]
        return frozenset({basename}) if basename in self.target_names else frozenset()

    def is_explicit_external(self, reference: str) -> bool:
        repository = repository_name(reference)
        relative = registry_relative_repository(repository)
        return repository in self.external_repositories or relative in self.external_repositories

    def probable_local_targets(self, reference: str) -> tuple[str, ...]:
        """Return strong, deterministic near-matches for a qualified external reference."""
        repository = repository_name(reference)
        if "/" not in repository or self.is_explicit_external(repository):
            return ()
        basename = repository.rsplit("/", 1)[-1]
        compact_basename = "".join(character for character in basename if character.isalnum())
        scored: list[tuple[float, str]] = []
        for target in self.target_names:
            compact_target = "".join(character for character in target if character.isalnum())
            score = SequenceMatcher(None, basename, target).ratio()
            if compact_basename == compact_target:
                score = 1.0
            if score >= 0.75:
                scored.append((score, target))
        ordered = sorted(scored, key=lambda item: (-item[0], item[1]))
        return tuple(target for _score, target in ordered)

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
    external_repositories: Iterable[str] = (),
) -> ImageIdentityResolver:
    targets = frozenset(target_names)
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

    for target in sorted(targets):
        register(target, target, managed=False)
        repository = configured_repositories.get(target, f"{namespace.strip('/')}/{target}")
        register(repository, target, managed=True)
        for alias in configured_aliases.get(target, ()):
            register(alias, target, managed=True)

    return ImageIdentityResolver(
        {identity: frozenset(targets) for identity, targets in sorted(identities.items())},
        frozenset(managed_prefixes),
        targets,
        frozenset(repository_name(reference) for reference in external_repositories),
    )
