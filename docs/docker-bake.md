# Docker Buildx Bake export

`platform images generate-bake` turns the same validated build plan used by the GitHub and GitLab
renderers into deterministic Docker Buildx Bake HCL. It is useful when a Docker-based team wants a
reviewable build definition, one parallel BuildKit invocation, or an integration point for custom
automation without maintaining a second dependency graph.

Bake is an optional Docker execution format. Discovery, identity resolution, affected-image
selection, exact references, and cycle safety remain backend-neutral. Podman, Buildah, and nerdctl
users continue to use `platform images build` and the generated CI workflows.

## Quick start

From the configured repository root:

```bash
platform images generate-bake --output docker-bake.hcl
docker buildx bake --file docker-bake.hcl --print
docker buildx bake --file docker-bake.hcl --load
```

With no selector, the definition contains every discovered image and provides both `default` and
`all` groups. `--print` asks Buildx to resolve and display the definition without building. `--load`
loads each result into the local Docker image store.

Always pass `--file` in automation. Buildx otherwise discovers and merges Compose, Bake, and Bake
override files in its default lookup order, which can add targets or settings that were not part of
the platform-images plan.

## Choose what the Bake file contains

| Command | Generated selection | Named group |
| --- | --- | --- |
| `platform images generate-bake` | Every image, using local `localhost/...:dev` tags | `all` |
| `platform images generate-bake --all` | Every image, explicitly | `all` |
| `platform images generate-bake --image api` | `api` and its required local parents | `selected` |
| `platform images generate-bake --ci` | Images affected by the current CI change | `affected` |
| `platform images generate-bake --ci --all` | Full commit-addressed CI bootstrap | `affected` |
| `platform images generate-bake --base <sha> --head <sha>` | Change plan using the normal registry and CI inputs | `affected` |
| `platform images generate-bake --plan image-plan.json` | Exact contents of a validated persisted plan | `selected` or `affected` |

Repeat `--image` to select several explicit leaves. Each generated definition also contains a
`default` group with the same targets, so naming the selection group is optional.

An empty affected plan is valid. The exporter writes empty `default` and `affected` groups, and
Buildx treats the result as a successful no-op.

## How graph edges become Bake dependencies

Given separate `base` and `application` Dockerfiles where `application` uses `FROM base`, the
important generated fragment is:

```hcl
target "image-base" {
  context    = "containers/base"
  dockerfile = "Dockerfile"
  tags       = ["localhost/team/base:dev"]
}

target "image-application" {
  context    = "containers/application"
  dockerfile = "Dockerfile"
  contexts = {
    "base" = "target:image-base"
  }
  tags = ["localhost/team/application:dev"]
}
```

The `target:image-base` context is Buildx's native dependency mechanism for targets backed by
separate Dockerfiles. BuildKit can schedule independent targets concurrently but cannot build the
consumer before the parent result exists.

Every spelling that resolved to a local target becomes a named context. Existing Dockerfiles can
therefore keep qualified references such as
`nexus.example.com:8088/risk-repo/ubuntu-24-04-base:latest`; the generated context binds that exact
spelling to the local Bake target. The Dockerfile does not have to be renamed or simplified.

For a partial CI plan, an unchanged local parent is intentionally absent from the target set. Its
resolved immutable reference is emitted instead:

```hcl
contexts = {
  "base" = "docker-image://registry.example.com/team/base@sha256:<digest>"
}
```

That distinction preserves the controller's central rule: consume a newly built parent when it is
in the plan; otherwise consume the already-published parent by digest, never by a moving stable tag.

## Configured Dockerfile ARG references

A repository may contain:

```dockerfile
ARG SOURCE_REGISTRY
FROM ${SOURCE_REGISTRY}/base:latest
```

When `dockerfile.arguments.SOURCE_REGISTRY` resolves this to a local target, the exporter embeds a
safe argument-bound `dockerfile-inline` for that target and rewrites only image-bearing `FROM`,
`COPY`/`ADD --from`, and `RUN --mount=from` operands. Other ARG usage and heredoc bodies are
preserved. HCL interpolation sequences are escaped so Buildx receives the intended Dockerfile.

Ordinary Dockerfiles and Containerfiles are never embedded; their repository-relative context and
build-file paths remain visible in the HCL.

## Local loading, registry pushes, and overrides

The generated file deliberately does not fix an output exporter. Choose the operation at execution
time:

```bash
# Local developer build
docker buildx bake --file docker-bake.hcl --load selected

# Push the exact tags already calculated by the CI plan
docker buildx bake --file docker-bake.hcl \
  --push \
  --metadata-file image-metadata.json \
  affected

# Apply a normal Bake override without regenerating the graph
docker buildx bake --file docker-bake.hcl --set '*.platform=linux/arm64' --push
```

Build arguments declared in `platform-images.toml` are copied into every applicable Bake target so
discovery and execution agree. Do not store credentials in those checked-in arguments. Use
BuildKit secrets, CI secret variables, or a separate reviewed Bake override file for sensitive
values.

## Use the authoritative CI plan

Custom Docker CI should calculate the plan once, persist it, and render Bake from the same bytes:

```bash
platform images plan --ci --format json > image-plan.json
platform images generate-bake \
  --plan image-plan.json \
  --output docker-bake.hcl
docker buildx bake \
  --file docker-bake.hcl \
  --push \
  --metadata-file image-metadata.json \
  affected
```

The persisted plan is validated against the current discovered graph before HCL is emitted. CI
targets retain their merge-request or commit-SHA output tags. Generated OCI labels identify the
logical target, source revision, project URL when available, and exact local dependency inputs.

Buildx metadata and `image-build-manifest.json` are different contracts. The Bake metadata file is
useful to custom automation, but it does not by itself perform the retry identity checks, digest
manifest validation, graph-wide promotion, or semantic-tag promotion implemented by the generated
GitHub and GitLab workflows. Keep those workflows when that complete delivery contract is wanted;
use Bake as the build execution export or provide equivalent metadata verification in custom CI.

## Paths and generated target names

Run Buildx from the repository root. Bake resolves local contexts from the current working
directory by default, and the exporter writes repository-relative paths. Keeping `docker-bake.hcl`
at the root also avoids surprises if a developer has opted into
`BUILDX_BAKE_FILE_RELATIVE_PATHS=1`.

Logical target names are preserved in tags and `io.platform-images.target` labels. Bake itself
accepts a narrower target-name alphabet, so generated IDs receive an `image-` prefix, dots become
`_dot_`, and literal underscores are doubled. For example, `ubuntu.base` becomes
`image-ubuntu_dot_base`. The encoding is deterministic and collision-safe; users should continue
to select logical names with `--image`, not generated Bake IDs.

The output path must remain inside the repository. Use stdout redirection when an external
temporary location is deliberately required.

## Current Docker boundary

Docker documents `contexts = { name = "target:other" }` as the way to use one Bake target as the
build context of another. That mechanism is specific to Docker Buildx Bake. There is no portable
Bake file consumed by Podman, Buildah, Docker, and nerdctl together, which is why
`platform-images` keeps its graph and JSON plan as the portable authority and treats HCL as one
execution renderer.

See Docker's official [Bake file reference](https://docs.docker.com/build/bake/reference/),
[additional-context documentation](https://docs.docker.com/build/bake/contexts/), and
[Bake CLI reference](https://docs.docker.com/reference/cli/docker/buildx/bake/) for the complete
set of Docker-side overrides.
