# Platform Images

This repository is a convention-driven controller for container images. Every direct
`images/<name>/Dockerfile` directory is an image target. Dockerfiles express local dependencies
with logical names such as `FROM base`; the controller binds those names to exact Podman build
contexts locally and exact ECR references in CI.

## Five-minute quick start

Python 3.12 and Podman are expected.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

platform images list
platform images validate
platform images graph
platform images build curl
```

The example topology is discovered without metadata:

```text
base
└── curl
```

`platform images build curl` first builds `localhost/platform-images/base:dev`, then binds that
precise reference as the named `base` context while building
`localhost/platform-images/curl:dev`. Preview both commands without executing them:

```bash
platform images build curl --dry-run
```

Use `--no-deps` only when the dependency images already exist under their local `:dev` tags.

## Repository and change inspection

```bash
platform images show curl
platform images graph --format json
platform images changed --base <sha> --head <sha>
platform images affected --base <sha> --head <sha>
platform images plan --base <sha> --head <sha> --format json
```

Change-based plans require `PLATFORM_IMAGES_REGISTRY`, `CI_PIPELINE_ID`, and stable dependency
lookups. Production uses the AWS CLI to resolve `main` in ECR to an immutable digest. Tests and
offline inspection can supply a deterministic JSON map through `PLATFORM_IMAGES_STABLE_REFS`:

```bash
export PLATFORM_IMAGES_REGISTRY=123456789012.dkr.ecr.eu-west-2.amazonaws.com
export CI_PIPELINE_ID=123
export PLATFORM_IMAGES_STABLE_REFS='{"base":"registry/platform-images/base@sha256:..."}'
```

## CI workflow

The checked-in parent pipeline validates the repository and writes an explicit JSON build plan
and renders the GitLab child pipeline from that same persisted plan. Each generated image job
pushes its unique output before
dependent jobs start. Default-branch plans add one final promotion job that updates all affected
`main` aliases only after every build succeeds. Merge-request plans never update `main`.

Unqualified external image names are fail-closed. `FROM base` is local when `base` is a discovered
target; names such as `alpine` must be declared in
`dockerfile.allowed_short_external_images`. Registry-qualified references are external without an
allowlist entry. This prevents a misspelled or deleted local dependency from silently becoming a
public-registry pull.

See [architecture](docs/architecture.md), [adding an image](docs/adding-an-image.md),
[GitLab CI](docs/gitlab-ci.md), and [ECR setup](docs/ecr-setup.md).

## Quality and integration checks

```bash
ruff check .
ruff format --check .
pytest
pytest -m integration
```

The Podman integration test builds and runs the dummy `base -> curl` topology. It is skipped when
Podman is absent or its service is unavailable.
