# Container backends and registry transports

`platform-images` supports named container tools explicitly. “OCI-compatible” alone is not a
sufficient claim: a usable backend must bind named dependency contexts, preserve identity labels,
publish the unique planned output, expose its registry digest, support safe retry inspection, and
promote a fully successful graph.

The build backend and registry transport are separate capabilities:

- the **builder** executes a Dockerfile or Containerfile and binds exact dependency images;
- the **transport** logs in, pulls and inspects retry candidates, publishes local output when the
  builder does not push directly, and promotes stable aliases.

## Supported builders

The minimums below are conservative project support floors, not claims about the first upstream
release containing each individual flag.

| Builder | Minimum supported | CI coverage | Named image context | CI digest contract |
| --- | --- | --- | --- | --- |
| Docker Engine + Buildx | Engine 24.0, Buildx 0.12 | GitHub-hosted Linux, current preinstalled versions | `docker-image://` | `buildx --push --metadata-file`; reads `containerimage.digest` |
| Podman | 4.4 | Ubuntu Linux package | `container-image://` | Build locally, then transport `push --digestfile` |
| Buildah | 1.33 | Ubuntu Linux package | `container-image://` | Build locally, then transport `push --digestfile` |
| nerdctl + BuildKit | nerdctl 2.0, BuildKit 0.13, containerd 1.7 | Linux with pinned nerdctl full distribution 2.3.5 | CI: `docker-image://`; local chains: exact `oci-layout://` handoff | BuildKit image output pushes directly; transport pulls, verifies identity labels, and reads the registry digest |

The repository's CI prints installed versions before exercising the real `base -> curl` dependency
build. Local integration tests skip unavailable tooling; hosted CI does not treat an advertised
backend as optional.

[Docker Buildx](https://docs.docker.com/reference/cli/docker/buildx/build/) is the simplest default
for GitHub-hosted runners. [Podman](https://docs.podman.io/en/stable/markdown/podman-build.1.html)
and [Buildah](https://github.com/containers/buildah/blob/main/docs/buildah-build.1.md) are useful for
daemonless Linux fleets and share the containers-storage implementation. [nerdctl](https://github.com/containerd/nerdctl/blob/main/docs/command-reference.md)
adds materially different coverage for teams whose build and runtime substrate is containerd;
`nerdctl build` requires a running BuildKit daemon as well as containerd.

Docker users can also run `platform images generate-bake` to export a local, affected, or persisted
plan as native Buildx HCL. In-plan dependencies use Bake's `target:<parent>` contexts; unchanged
parents outside a partial CI plan use digest-pinned `docker-image://` contexts. The integration gate
parses and builds a qualified ARG-bound parent chain through the generated file. Bake is not listed
as a separate backend because it is an execution format for the existing Docker Buildx backend,
not a portable interface shared with Podman, Buildah, or nerdctl. See [the Bake guide](docker-bake.md).

BuildKit defines `docker-image://` as a registry source. Consequently, a local nerdctl graph does
not rely on a containerd tag being accidentally visible to BuildKit. After each parent build, the
controller exports it with `nerdctl save`, validates that the OCI index contains one image manifest
with a valid SHA-256 descriptor, and binds the consumer through
`oci-layout://<verified-temporary-layout>`. nerdctl's parser independently selects that sole
descriptor, so the handoff remains unambiguous even though nerdctl accepts the layout path rather
than a digest-qualified URI. The temporary layout lasts for the complete local plan and is removed
afterwards. CI parents are already pushed before consumers start, so CI uses immutable
registry-backed `docker-image://` contexts directly.

Raw [`buildctl`](https://github.com/moby/buildkit/blob/master/docs/reference/buildctl.md) is
intentionally not exposed as a complete backend. It is a strong builder and can
emit exact metadata, but it does not itself provide the whole login, local-image retry inspection,
tag, and promotion lifecycle. nerdctl supplies that missing containerd-facing lifecycle while still
using BuildKit for builds.

## Supported registry transports

| Transport | Minimum supported | Login | Retry inspection | Push digest | Promotion |
| --- | --- | --- | --- | --- | --- |
| Docker | Engine 24.0 | Password stdin | `docker image inspect` | Builder metadata | Pull, tag, push |
| Podman | 4.4 | Password stdin | `podman image inspect` | `--digestfile` | Pull, tag, push |
| Buildah | 1.33 | Password stdin | `buildah inspect --type image` | `--digestfile` | Pull, tag, push |
| nerdctl | 2.0 | Password stdin | `nerdctl image inspect` | Verified after direct BuildKit push | Pull, tag, push |

All retry paths compare four OCI identity labels before reusing an existing output: Git revision,
source repository, logical target name, and the exact dependency-reference map. A digest without a
matching identity is not accepted.

## Registry providers

Registry providers are separate from transports. The provider resolves an unchanged stable tag to
an immutable manifest digest during planning; the transport logs in and moves image data.

| Provider | Stable lookup | Authentication | Typical services |
| --- | --- | --- | --- |
| `oci` | OCI Distribution `GET /v2/<repository>/manifests/<tag>`; validates `Docker-Content-Digest` or hashes the returned manifest bytes | Username/password with Basic or Bearer challenge, or deliberately ambient credentials | Nexus, GitLab Container Registry, GHCR, Harbor, Docker Registry |
| `ecr` | AWS `ecr describe-images` with explicit registry account and region | Regional token from `aws ecr get-login-password` | Amazon ECR |

OCI lookup fetches only the manifest, never image layers. `authentication = "credentials"` reads
the configured username/password environment variables for both lookup and transport login.
`authentication = "ambient"` skips the login command and is appropriate only when a trusted runner
or earlier workflow step already configured the selected transport. Direct stable-manifest lookup
must then work anonymously or the runner must expose both configured OCI credential variables;
ambient mode never assumes that a transport credential store is readable as an HTTP credential
store. ECR requires `authentication = "ecr"`.

## Valid combinations

| Builder | Docker transport | Podman transport | Buildah transport | nerdctl transport |
| --- | --- | --- | --- | --- |
| Docker Buildx | Supported | — | — | — |
| Podman | — | Supported | Supported | — |
| Buildah | — | Supported | Supported | — |
| nerdctl/BuildKit | — | — | — | Supported |

Podman and Buildah can be mixed because both use containers-storage and compatible authentication
files when configured normally. Docker and nerdctl use distinct daemon/content stores and must use
their corresponding registry transport. Invalid pairs fail while loading configuration or parsing
execution options, before any build starts.

## Configuration

Choose the axes independently:

```toml
[build]
backend = "buildah"

[registry]
namespace = "platform-images"
registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"
stable_tag = "main"
transport = "podman"
provider = "oci"
authentication = "credentials"
```

Override them for one execution:

```bash
platform images ci-build api \
  --builder buildah \
  --registry-transport podman \
  --output-ref "$PLATFORM_IMAGES_REGISTRY/platform-images/api:sha-$CI_COMMIT_SHA"
```

For matching pairs, the legacy spelling remains a supported shorthand:

```bash
platform images build api --engine nerdctl
```

An existing `[build] engine = "podman"` configuration continues to select both Podman capabilities.
New repositories should use `build.backend` and `registry.transport` so ownership is unambiguous.

## Runner requirements

- **Docker:** Docker daemon plus Buildx. The generated GitHub workflow verifies Buildx.
- **Podman:** Podman with named build-context support. The generated Ubuntu workflow installs it.
- **Buildah:** Buildah with named build-context support. The generated Ubuntu workflow installs it.
- **nerdctl:** containerd, nerdctl, and a running BuildKit daemon visible to the job. The generated
  workflow verifies these components but does not replace a self-hosted runner's containerd
  topology. The project CI installs the pinned full distribution and starts isolated, transient
  containerd and BuildKit system services as its integration fixture.

Git, registry network access, and short-lived credentials remain common requirements; AWS CLI is
needed only for the ECR provider. The standalone `platform` executable is available on Linux,
macOS, and Windows, but
the advertised Buildah and nerdctl integration environments are Linux. Docker Desktop is the most
direct supported execution path on macOS and Windows; Podman-machine environments may work but are
not currently part of the hosted integration gate.
