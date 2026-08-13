# GitLab CI

The root pipeline has three responsibilities: run quality checks, generate the plan artifacts, and
mirror the generated child pipeline. It does not contain one checked-in job per image.

The planning job uses `GIT_DEPTH: "0"`. Merge-request comparisons use
`CI_MERGE_REQUEST_DIFF_BASE_SHA`; ordinary pushes use a non-zero `CI_COMMIT_BEFORE_SHA`. For an
initial default-branch pipeline (an all-zero before SHA), the controller automatically bootstraps
the complete graph. A first non-default branch instead uses its merge base with
`origin/$CI_DEFAULT_BRANCH` and fails with recovery instructions if that base is unavailable.
`--ci --all` remains available as an explicit complete rebuild.

The planning job runs selection once, saves `image-plan.json`, and invokes `render-plan` to produce
the child YAML from that exact artifact. Rendering rejects a stale or altered target order,
dependency set, `needs` edge, input binding, or discovered path.

Generated image jobs extend `.image-build`, use only their direct local dependencies in `needs`,
and call `platform images ci-build` with an exact output and exact input references. Each job pushes
before completion, so no local container-engine store is assumed to cross runner jobs. An empty plan gets a
`no_image_changes` job because GitLab rejects a downstream pipeline with no executable jobs.

Merge-request tags are unique `ci-<pipeline>-<commit>` values and are never promoted. Default-branch
tags are immutable `sha-<full-commit>` values. A single `promote_main` job needs every image build;
only after all succeed does it retag and push each mutable `main` alias. A failed image therefore
cannot expose a partially promoted graph.

Job retries first pull the immutable output tag. If its target, commit, source, and dependency-input
identity labels match, the job returns the existing digest without rebuilding or pushing. A tag
whose identity differs is an explicit collision failure; immutable ECR tags never need to be made
mutable for retry support.

The selected runner must provide:

- Python 3.12 and this installed package
- Git with full repository history
- a supported builder/transport pair: Docker Buildx, Podman, Buildah, or nerdctl/BuildKit, with the
  named image contexts and versions in [container backends](container-backends.md)
- AWS CLI and workload authentication for ECR
- network access to pull base images and pull/push ECR images

Required variables include `PLATFORM_IMAGES_REGISTRY`. GitLab supplies commit, pipeline, project,
branch, and merge-request variables. AWS credentials should use the organisation's existing
short-lived workload identity rather than static repository secrets.
`platform images registry-login` parses the ECR region from that registry value, retrieves the
regional token, and passes it to the selected registry transport's login command over stdin; it does
not depend on an ambient default region or print the credential.

The example template sets `PLATFORM_IMAGES_RUNNER_TAG=podman` and expands it in `.image-build.tags`.
Override that CI/CD variable to route generated jobs to the fleet that provides the configured
builder and transport—for example `docker`, `buildah`, or `containerd`. Keep the actual backend
selection in `platform-images.toml`; the runner tag is scheduling metadata, not an execution switch.
