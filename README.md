# Platform Images

[![CI](https://github.com/davehewy/platform-images/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/davehewy/platform-images/actions/workflows/ci.yml)
[![Release](https://github.com/davehewy/platform-images/actions/workflows/release.yml/badge.svg?branch=main)](https://github.com/davehewy/platform-images/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)

`platform-images` is a lightweight way to keep multiple container images in one repository. The
images may be completely independent, share a common parent, or form deeper dependency chains. When
something changes in an image directory or in shared build configuration, the tool selects the
smallest affected subgraph and orchestrates its rebuild.

For a repository owner, it answers one operational question:

> Given this Git change, which container images must be rebuilt, in what order, and with which
> exact versions of their local parent images?

A repository containing many Dockerfiles usually starts with one CI job per image. Over time that
becomes either wasteful (build every image on every commit) or unsafe (directory path rules rebuild
the directly edited image but miss its consumers). Independent images should remain independent;
related images should be rebuilt together only when their part of the graph changes. The dependency
information already exists in instructions such as `FROM base`, but normal CI path filters do not
understand it. They also do not solve how independently scheduled jobs pass the exact newly built
parent image to their consumers.

This tool discovers `images/<name>/Dockerfile` targets, infers their **build-time** dependency graph,
maps a Git diff onto that graph, follows changes downstream, and generates the required GitLab jobs
dynamically. Each job receives exact image references, builds in deterministic topological order,
and pushes before dependent jobs run. A default-branch pipeline promotes the affected graph to
stable tags only after the complete graph build succeeds.

There is no second image catalogue or hand-maintained CI dependency list. The Dockerfiles and Git
history remain the source of truth.

## Contents

- [What this solves and what it does not](#what-this-solves-and-what-it-does-not)
- [Installation](#installation)
- [Quick start](#quick-start)
- [The checked-in example](#the-checked-in-example)
- [Demonstration](#demonstration)
- [Worked examples](#worked-examples)
- [Configuration capabilities](#configuration-capabilities)
- [Recommended adoption flow](#recommended-adoption-flow)
- [Command guide](#command-guide)
- [CI operating model](#ci-operating-model)
- [Quality and integration checks](#quality-and-integration-checks)
- [License](#license)

## What this solves and what it does not

For a platform or DevOps team, it solves:

- discovering image targets by repository convention;
- inferring local dependencies from `FROM`, `COPY`/`ADD --from`, and `RUN --mount=from`;
- rebuilding a changed image and every transitive consumer, while leaving unrelated images alone;
- building an affected consumer against either its newly built parent or an immutable digest of the
  existing stable parent;
- turning the calculated graph into a GitLab child pipeline with correct `needs` edges;
- using unique merge-request and commit tags, with graph-wide stable promotion on `main`;
- making retries safe by verifying image identity before reusing an immutable output tag; and
- failing early on cycles, missing targets, ambiguous short image names, unsafe paths, and malformed
  Dockerfiles.

It intentionally models **container build dependencies**, not runtime service relationships. It
will understand `FROM base` but not that an API container talks to PostgreSQL at runtime. It also
does not rebuild merely because a remote tag such as `alpine:3.22` changed without a Git change;
pin external bases by digest or use a dependency-update process when that distinction matters.

## Installation

### Standalone executable (recommended)

The quickest installation on Linux or macOS downloads the correct archive for the current machine,
verifies it against the release checksum, and installs `platform` into `~/.local/bin`:

```bash
curl -fsSL https://raw.githubusercontent.com/davehewy/platform-images/main/scripts/install.sh | sh
```

Install a specific version or choose another destination with environment variables:

```bash
curl -fsSL https://raw.githubusercontent.com/davehewy/platform-images/main/scripts/install.sh |
  PLATFORM_IMAGES_VERSION=0.2.0 PLATFORM_IMAGES_INSTALL_DIR=/usr/local/bin sh
```

Each [GitHub release](https://github.com/davehewy/platform-images/releases) provides:

| System | Architecture | Release asset |
| --- | --- | --- |
| GNU/Linux | AMD64 / x86_64 | `platform-images-linux-amd64.tar.gz` |
| GNU/Linux | ARM64 / AArch64 | `platform-images-linux-arm64.tar.gz` |
| macOS / Darwin | Intel AMD64 | `platform-images-darwin-amd64.tar.gz` |
| macOS / Darwin | Apple Silicon ARM64 | `platform-images-darwin-arm64.tar.gz` |

Every archive contains the `platform` executable, README, and MIT license. `SHA256SUMS` covers all
four archives. The Linux executables are built on Ubuntu 22.04 and target modern glibc-based 64-bit
distributions. The macOS executables are built natively on Intel and Apple Silicon runners.

The executable bundles Python and the Python package dependencies. Commands still expect the tools
they orchestrate to exist: Git for change inspection, Podman for image builds, and AWS CLI plus
credentials for ECR login and stable-reference resolution.

### Install as a Python tool

If Python 3.12 is already part of the team's toolchain, `uv` can install an isolated, pinned release:

```bash
uv tool install --python 3.12 \
  "git+https://github.com/davehewy/platform-images.git@v0.2.0"
```

The universal `platform_images-<version>-py3-none-any.whl` and conventional Python source
`platform_images-<version>.tar.gz` are also attached to each GitHub release. The wheel is portable
across CPU architectures and operating systems with Python 3.12.

### Install for development

Clone the repository and install its locked development environment:

```bash
uv sync --locked --extra dev
```

This keeps Ruff, pytest, pre-commit, and Commitizen available. An ordinary editable pip environment
also works:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick start

From the repository root:

```bash
platform images list
platform images validate
platform images graph
platform images build curl --dry-run
```

If installed through the development environment without activating it, prefix commands with
`uv run`:

```bash
uv run platform images list
uv run platform images validate
uv run platform images graph
uv run platform images build curl --dry-run
```

Remove `--dry-run` when Podman is installed and you are ready to execute the build.

## The checked-in example

The repository contains two deliberately small images:

```text
images/
├── base/
│   └── Dockerfile
└── curl/
    └── Dockerfile
```

`images/base/Dockerfile` starts from an allowed external image:

```dockerfile
FROM alpine:3.22

RUN printf '%s\n' 'platform-local-base-v1' > /etc/platform-base-marker
```

`images/curl/Dockerfile` names the local target `base` directly:

```dockerfile
FROM base

RUN apk add --no-cache curl

ENTRYPOINT ["curl"]
CMD ["--version"]
```

The tool resolves `base` as the checked-in target—not as `docker.io/library/base`—and discovers:

```text
base
└── curl
```

## Demonstration

![Terminal demonstration of discovery, affected-image selection, build ordering, and generated CI jobs](docs/demo.svg)

The graphic uses the checked-in `base` and `curl` images. The commands below are copyable:

```bash
platform images list
platform images graph
platform images build curl --dry-run

# With GitLab registry and CI variables present:
platform images plan --ci --all --format gitlab
```

## Worked examples

### Worked example 1: inspect what the repository owns

Use this during adoption, review, or incident diagnosis to confirm that discovery agrees with the
team's mental model:

```console
$ platform images list
base
curl

$ platform images validate
Validation passed (2 image targets).

$ platform images show curl
curl
  dockerfile: images/curl/Dockerfile
  dependencies: base
  dependents: none

$ platform images graph
base
└── curl
```

Use `--format json` with `show`, `validate`, or `graph` when another tool needs to consume the
result. JSON output includes a schema version so it can be treated as an automation contract.

### Worked example 2: build a dependent image locally

A developer asks to build `curl`. Its parent is included automatically:

```console
$ platform images build curl --dry-run
podman build --file images/base/Dockerfile --tag localhost/platform-images/base:dev images/base
podman build --build-context base=container-image://localhost/platform-images/base:dev --file images/curl/Dockerfile --tag localhost/platform-images/curl:dev images/curl
```

Without `--dry-run`, those commands execute in that order. The second build does not rewrite
`FROM base`; Podman binds the logical name through a named `container-image://` build context.

When the stable local parent is already present and only the leaf needs rebuilding, opt out of the
upstream closure explicitly:

```bash
platform images build curl --no-deps
```

Use `--no-deps` deliberately: it trusts the existing
`localhost/platform-images/base:dev` rather than recreating it.

### Worked example 3: a leaf image changes

Suppose a merge request edits only `images/curl/Dockerfile`:

```bash
BASE_SHA=$(git merge-base HEAD origin/main)

platform images changed --base "$BASE_SHA" --head HEAD
# curl

platform images affected --base "$BASE_SHA" --head HEAD
# curl
```

`base` is not rebuilt because no consumer needs a new base. The `curl` plan still needs an exact
parent, so CI resolves `base:main` in ECR to its immutable digest and injects a reference like:

```text
123456789012.dkr.ecr.eu-west-2.amazonaws.com/platform-images/base@sha256:<digest>
```

This is the important partial-rebuild case: the leaf is rebuilt, its unchanged parent is reused,
and the parent cannot move underneath the running build.

For offline plan inspection, provide the stable lookup instead of calling ECR:

```bash
export PLATFORM_IMAGES_REGISTRY=123456789012.dkr.ecr.eu-west-2.amazonaws.com
export PLATFORM_IMAGES_STABLE_REFS='{
  "base": "123456789012.dkr.ecr.eu-west-2.amazonaws.com/platform-images/base@sha256:<digest>"
}'
export CI_PIPELINE_ID=812
export CI_MERGE_REQUEST_IID=42

platform images plan --base "$BASE_SHA" --head HEAD --format json
```

### Worked example 4: a base image changes

Now suppose the change is under `images/base/`:

```console
$ platform images changed --base "$BASE_SHA" --head HEAD
base

$ platform images affected --base "$BASE_SHA" --head HEAD
base
curl
```

`curl` is included because it is a transitive dependent of `base`. In a larger graph, traversal
continues through every downstream consumer. Unrelated roots and their consumers remain absent.

A complete merge-request plan for the checked-in graph looks like this:

```console
$ platform images plan --ci --all
Build plan (merge_request):
  base
    reasons: all-images
    output: 123456789012.dkr.ecr.eu-west-2.amazonaws.com/platform-images/base:ci-812-0123456789abcdef0123456789abcdef01234567
  curl
    reasons: all-images, dependent-of:base
    output: 123456789012.dkr.ecr.eu-west-2.amazonaws.com/platform-images/curl:ci-812-0123456789abcdef0123456789abcdef01234567
    needs: base
    input base: 123456789012.dkr.ecr.eu-west-2.amazonaws.com/platform-images/base:ci-812-0123456789abcdef0123456789abcdef01234567
```

Both targets use the same pipeline-unique identity. `curl` consumes the output produced by the
`base` job in this plan, rather than the older stable base.

For the exact illustrative references above, `CI_COMMIT_SHA` was set explicitly:

```bash
export CI_COMMIT_SHA=0123456789abcdef0123456789abcdef01234567
platform images plan --ci --all
```

### Worked example 5: generate the GitLab jobs

The parent pipeline calculates once, persists the JSON plan, and renders a child pipeline from that
same artifact:

```yaml
generate-image-pipeline:
  variables:
    GIT_DEPTH: "0"
  script:
    - platform images validate
    - platform images plan --ci --format json > image-plan.json
    - platform images render-plan image-plan.json --format gitlab > generated-images.yml
  artifacts:
    paths: [image-plan.json, generated-images.yml]
```

For the `base -> curl` merge-request plan, the generated portion is equivalent to:

```yaml
stages: [build]

image_base:
  extends: .image-build
  script:
    - platform images ci-build base --output-ref <registry>/platform-images/base:ci-812-<sha>

image_curl:
  extends: .image-build
  needs: [image_base]
  script:
    - platform images ci-build curl --output-ref <registry>/platform-images/curl:ci-812-<sha> --input-ref base=<registry>/platform-images/base:ci-812-<sha>
```

The `needs` edge is derived from the Dockerfile. Because every job pushes its unique output before
completion, `image_curl` can run on a different GitLab runner with no shared Podman image store.

### Worked example 6: merge request versus default branch

Merge requests create isolated images and never update the stable alias:

```text
<registry>/platform-images/base:ci-812-<full-commit-sha>
<registry>/platform-images/curl:ci-812-<full-commit-sha>
```

The default branch creates commit-addressed outputs:

```text
<registry>/platform-images/base:sha-<full-commit-sha>
<registry>/platform-images/curl:sha-<full-commit-sha>
```

It also adds one final job:

```yaml
promote_main:
  stage: promote
  needs: [image_base, image_curl]
  script:
    - platform images promote --source <registry>/platform-images/base:sha-<sha> --destination <registry>/platform-images/base:main
    - platform images promote --source <registry>/platform-images/curl:sha-<sha> --destination <registry>/platform-images/curl:main
```

If either build fails, promotion never starts. Consumers therefore do not observe a half-promoted
set where `base:main` is new but `curl:main` is old.

### Worked example 7: no image-related change

A documentation change outside configured global inputs affects no target:

```bash
platform images affected --base "$BASE_SHA" --head HEAD
# no output
```

GitLab will not accept a child pipeline with no runnable jobs, so the renderer emits a successful
no-op job:

```yaml
stages: [build]
no_image_changes:
  stage: build
  script:
    - echo "No container images are affected."
```

The parent pipeline remains structurally identical whether zero, one, or hundreds of images are
selected.

### Worked example 8: a shared build input changes

Some files affect every image even though they do not live beneath `images/`. Add those paths to
`changes.global_inputs`:

```toml
[changes]
global_inputs = [
  "shared/install-ca-certificates.sh",
  "shared/company-root-ca.pem",
  "build/container-policy/**",
]
```

A change to any matching path selects every discovered target. Use this for real shared build
inputs only; adding broad application or documentation paths defeats selective rebuilding.

Controller-critical paths are always global even if omitted from configuration:

```text
.gitlab-ci.yml
.gitlab/**
platform-images.toml
pyproject.toml
src/platform_images/**
uv.lock
```

These mandatory patterns cannot be configured away, because a controller or pipeline change must
re-evaluate the entire image set safely.

### Worked example 9: add another local image

Add a direct directory; do not add a corresponding GitLab job:

```text
images/debug/Dockerfile
```

```dockerfile
FROM curl

RUN apk add --no-cache bind-tools jq
```

Validation and graph inspection now produce:

```console
$ platform images validate
Validation passed (3 image targets).

$ platform images graph
base
└── curl
    └── debug
```

A change to `base` rebuilds `base`, `curl`, and `debug`; a change to `curl` rebuilds `curl` and
`debug`; a change to `debug` rebuilds only `debug`. The generated child pipeline grows by one job
and adds the correct `needs` edge automatically.

Dependencies can appear outside `FROM` too:

```dockerfile
FROM alpine:3.22
COPY --from=base /etc/platform-base-marker /marker
RUN --mount=from=curl,target=/tools,ro cp /tools/usr/bin/curl /usr/local/bin/curl
```

Local references in `COPY`/`ADD --from` and `RUN --mount=from` participate in the same graph.
Numeric and named stages inside one Dockerfile remain Dockerfile stages rather than image targets.

### Worked example 10: keep unrelated images independent

Not every image needs to belong to the same chain. Add an independent utility image:

```text
images/healthcheck/Dockerfile
```

```dockerfile
FROM busybox:1.37

CMD ["wget", "--help"]
```

Because `busybox` is an allowed external image rather than a local target, the repository becomes a
forest with two roots:

```text
base
└── curl
healthcheck
```

The selection behavior is now:

| Changed directory | Rebuilt images | Why |
| --- | --- | --- |
| `images/base/**` | `base`, `curl` | Rebuild the changed target and its local consumer. |
| `images/curl/**` | `curl` | The leaf changed; its parent remains an immutable stable input. |
| `images/healthcheck/**` | `healthcheck` | It is an independent root with no local consumers. |
| configured shared input | `base`, `curl`, `healthcheck` | The shared input is declared to affect every target. |

This is the central reason to calculate a graph instead of maintaining a single “images changed”
switch: independent images stay cheap, while dependent images stay correct.

### Worked example 11: bootstrap and recovery

On the first default-branch pipeline GitLab supplies an all-zero `CI_COMMIT_BEFORE_SHA`. There is no
valid previous commit to compare, so the controller deliberately builds the complete graph.

For a manual bootstrap or recovery rebuild, request the same behavior explicitly:

```bash
platform images plan --ci --all --format json > image-plan.json
platform images render-plan image-plan.json --format gitlab > generated-images.yml
```

Use `--ci --all` after introducing the tool, creating a new registry namespace, or intentionally
invalidating every stable image. Normal pipelines should use `--ci` so Git determines the minimal
affected set.

### Worked example 12: catch dependency mistakes before CI spends money

If `curl` accidentally says `FROM bsae`, validation does not silently pull an unrelated public
image:

```text
images/curl/Dockerfile:1
  unqualified image reference is neither a local target nor an allowed external image: bsae
  possible local target: base
```

If a local image directory is deleted while a surviving Dockerfile still references it, planning
also fails. If images reference each other cyclically, validation reports the deterministic cycle.
These fail-closed checks turn repository mistakes into reviewable errors rather than surprising
registry pulls or deadlocked pipelines.

## Configuration capabilities

Configuration lives in `platform-images.toml`. This repository's complete configuration is:

```toml
[registry]
namespace = "platform-images"
registry_environment_variable = "PLATFORM_IMAGES_REGISTRY"
stable_tag = "main"

[tags]
ci_prefix = "ci"
commit_prefix = "sha"

[dockerfile]
allowed_short_external_images = ["alpine", "busybox"]

[changes]
global_inputs = [
  "src/platform_images/**",
  "pyproject.toml",
  "uv.lock",
  "platform-images.toml",
  ".gitlab-ci.yml",
  ".gitlab/**",
]
```

### Registry settings

| Setting | What it controls | When and why to change it |
| --- | --- | --- |
| `registry.namespace` | The repository prefix between the registry host and target name. Local images use the same namespace beneath `localhost/`. | Change it to match the team's ECR repository hierarchy, such as `platform/base-images`. It also identifies registry-qualified references intended to be internal, allowing missing internal targets to fail validation. |
| `registry.registry_environment_variable` | The environment-variable name from which the registry hostname is read. | Keep the default when one standard variable is acceptable. Change it to align with an established CI variable such as `COMPANY_ECR_REGISTRY` without hard-coding an account or region in the repository. |
| `registry.stable_tag` | The mutable alias resolved for unchanged parents and updated after a successful default-branch graph. | `main` is a clear default. Change it to `stable`, `production`, or another existing convention. This is a build baseline, not an environment deployment mechanism. |

For ECR, the registry value is the host only:

```bash
export PLATFORM_IMAGES_REGISTRY=123456789012.dkr.ecr.eu-west-2.amazonaws.com
```

The account and region are parsed from that hostname and passed explicitly to AWS CLI operations.

### Tag settings

| Setting | What it controls | When and why to change it |
| --- | --- | --- |
| `tags.ci_prefix` | Prefix for merge-request outputs: `<prefix>-<pipeline-id>-<commit-sha>`. | Change it when registry lifecycle rules or naming policy expect a prefix such as `mr` or `review`. Pipeline and commit identity remain part of the tag. |
| `tags.commit_prefix` | Prefix for default-branch immutable outputs: `<prefix>-<full-commit-sha>`. | Change it to match retention or audit conventions, for example `git`. Do not make this a mutable channel name; retry and collision safety rely on commit-specific outputs. |

The stable tag is deliberately separate from immutable output tags. Builds publish unique outputs;
promotion moves only the stable alias after success.

### Dockerfile reference settings

`dockerfile.allowed_short_external_images` is the explicit allowlist for unqualified external
repository names:

```toml
[dockerfile]
allowed_short_external_images = ["alpine", "busybox", "debian"]
```

Use it for intentional short external references such as `FROM alpine:3.22`. Do not add local image
names: discovered targets are local automatically. Keeping the list small catches typos and deleted
targets. `scratch` is always allowed. A registry-qualified external reference such as
`ghcr.io/organisation/tooling@sha256:...` does not need an allowlist entry.

### Change-selection settings

`changes.global_inputs` adds repository paths whose modification must rebuild every target. Patterns
support exact paths, shell-style matching, and directory forms ending in `/**`:

```toml
[changes]
global_inputs = [
  "shared/**",
  "scripts/build-image.sh",
  "security/container-policy/*.json",
]
```

Use this for files actually read by multiple image builds or for policy that changes the meaning of
all builds. Do not use it for ordinary per-image inputs inside `images/<name>/`; those are mapped to
their owning target automatically. Configured patterns are unioned with the mandatory controller
inputs listed earlier.

### Environment and CI inputs

| Variable | Purpose | When to provide it |
| --- | --- | --- |
| `PLATFORM_IMAGES_ROOT` | Run against a repository root other than the current directory. | Useful for wrappers, monorepo tooling, and local diagnostics invoked from another directory. |
| The variable named by `registry_environment_variable` | Registry hostname used for CI outputs, stable lookups, login, and promotion. | Required for CI and change-based plans; normally define it as a protected GitLab project/group variable. |
| `PLATFORM_IMAGES_STABLE_REFS` | JSON mapping of target names to immutable image references. | Use in tests, offline plan previews, or a non-ECR registry adapter. In normal ECR CI, omit it so the tool resolves the configured stable tag through AWS. |
| `CI_PIPELINE_ID` | Makes merge-request tags unique to a pipeline. | Supplied automatically by GitLab and required for merge-request planning. |
| `CI_COMMIT_SHA`, `CI_PROJECT_URL` | Identify and label a CI build. | Supplied automatically by GitLab; CI builds fail rather than emit untraceable images when either is absent. |
| `CI_MERGE_REQUEST_DIFF_BASE_SHA` | Preferred merge-request comparison base. | Supplied by GitLab merge-request pipelines. |
| `CI_COMMIT_BEFORE_SHA`, `CI_DEFAULT_BRANCH`, `CI_COMMIT_BRANCH` | Select the comparison and distinguish default-branch promotion from review builds. | Supplied by GitLab. Fetch full history with `GIT_DEPTH: "0"` so fallback merge-base calculation works. |
| `SOURCE_DATE_EPOCH` or `CI_COMMIT_TIMESTAMP` | Sets the OCI creation timestamp label. | Use `SOURCE_DATE_EPOCH` for reproducible build metadata; otherwise GitLab's commit timestamp is used when present. |

Never put registry credentials in `platform-images.toml`. `platform images registry-login` obtains a
regional ECR token from the AWS CLI and passes it to Podman over stdin. Use the organisation's
short-lived workload identity for AWS authentication.

## Recommended adoption flow

1. Put every owned target at `images/<name>/Dockerfile`; keep its ordinary context files in the same
   directory.
2. Replace internal registry references between these images with logical names such as
   `FROM base`.
3. Configure the registry namespace, stable tag, approved short external images, and genuine shared
   inputs in `platform-images.toml`.
4. Run `platform images validate`, `graph`, and `build <leaf> --dry-run` locally. Review the inferred
   graph with image owners.
5. Create the ECR repositories and runner permissions described in [ECR setup](docs/ecr-setup.md).
6. Add the checked-in parent pipeline and `.image-build` template described in
   [GitLab CI](docs/gitlab-ci.md). Keep full Git history available to the planning job.
7. Run one `--ci --all` default-branch bootstrap to populate stable tags.
8. Let ordinary merge-request and default-branch pipelines use `--ci` to calculate the minimal
   rebuild set.
9. Use the JSON graph and plan artifacts during review or incident diagnosis to explain exactly why
   each image was selected and which dependency digest it consumed.

## Command guide

| Command | Typical use |
| --- | --- |
| `platform images list` | Inventory discovered image targets. |
| `platform images show <name>` | Inspect one target's direct dependencies and dependents. |
| `platform images validate` | Gate commits before planning or building. |
| `platform images graph [--format json]` | Review or export the complete dependency graph. |
| `platform images build <name> [--dry-run] [--no-deps]` | Build locally with deterministic dependency binding. |
| `platform images changed --base <sha> --head <sha>` | Show directly changed targets and removed target directories. |
| `platform images affected --base <sha> --head <sha>` | Show the topologically ordered downstream rebuild set. |
| `platform images plan --image <name>` | Preview a local plan for one or more explicitly selected targets. Repeat `--image` to select several. |
| `platform images plan --all` | Preview a complete local build plan. |
| `platform images plan --ci` | Calculate the GitLab-aware change plan and exact references. |
| `platform images plan --ci --all` | Force a complete CI bootstrap or recovery plan. |
| `platform images render-plan image-plan.json --format gitlab` | Validate and render the persisted plan into child-pipeline YAML. |
| `platform images ci-build ...` | Execute one generated CI target with exact input/output references; normally called only by generated jobs. |
| `platform images registry-login` | Authenticate Podman to the configured ECR registry without exposing the token. |
| `platform images promote ...` | Move a successfully built immutable output to a stable alias; normally called only by the promotion job. |

## CI operating model

The checked-in parent pipeline runs quality checks, generates `image-plan.json`, renders
`generated-images.yml`, and mirrors the child pipeline. It does not contain one checked-in job per
image.

Each generated job uses only its direct local dependencies in `needs`. It pushes before completing,
so jobs do not rely on a shared runner cache. Retries pull the exact output tag and inspect identity
labels for target, commit, source, and dependency references. A matching image is reused by digest;
a mismatched immutable tag fails as a collision.

Merge-request plans never promote stable tags. Default-branch plans add a single promotion job that
needs every selected build. Removed targets are reported but are not deleted from ECR; lifecycle and
deletion policy remain explicit infrastructure responsibilities.

See [architecture](docs/architecture.md), [adding an image](docs/adding-an-image.md),
[GitLab CI](docs/gitlab-ci.md), [ECR setup](docs/ecr-setup.md), and
[contributing and releases](CONTRIBUTING.md).

## Quality and integration checks

```bash
uv run --locked --extra dev pre-commit run --all-files
uv run --locked --extra dev pytest
uv run --locked --extra dev pytest -m integration
uv build
```

The Podman integration test builds and runs the `base -> curl` topology. It is skipped locally when
Podman is absent or unavailable. GitHub CI requires Podman, repeats the locked quality suite, and
builds the wheel and source distribution. Conventional Commit messages drive Python Semantic
Release after `main` passes CI.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 David Heward.
