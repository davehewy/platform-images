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
before completion, so no local container-engine store is assumed to cross runner jobs. Each job
also uploads `image-results/<target>.json`, containing its commit SHA, pushed tag, registry digest,
immutable reference, and exact dependency inputs.

The later `publish_image_manifest` job waits for the whole build stage, downloads all result
artifacts, verifies the expected target set, and publishes `image-build-manifest.json`. It has no
large `needs` fan-in, so an affected graph wider than GitLab's per-job `needs` limit remains valid.
An empty plan still publishes a manifest with an empty `images` object because GitLab requires an
executable child pipeline and downstream automation needs an explicit answer.

Before emitting YAML, the renderer counts one job per affected image, the manifest job, and the
default-branch promotion job when present. `--gitlab-max-jobs` defaults to 500, matching the current
[GitLab.com Free per-pipeline limit](https://docs.gitlab.com/user/gitlab_com/#gitlab-cicd); raise it
only to the documented allowance for the project's tier or self-managed instance:

```bash
platform images render-plan image-plan.json --format gitlab \
  --gitlab-max-jobs 1500 > generated-images.yml
```

The locally included template may add scan, test, or deployment jobs that the dynamic renderer
cannot see, and those jobs consume the same limit. Leave explicit headroom for them. Exceeding the
budget fails during generation with the exact required count; the controller does not hide the
problem by splitting a connected DAG into child pipelines whose cross-pipeline artifact and
dependency semantics would be weaker.

Merge-request tags are unique `ci-<pipeline>-<commit>` values and are never promoted. Default-branch
tags are immutable `sha-<full-commit>` values. A single `promote_main` job runs in the final stage;
only after builds, manifest verification, and any user-defined `consume` jobs succeed does it retag
and push each mutable `main` alias. A failed earlier job therefore prevents promotion from starting.
Registry operations across multiple image repositories are not transactional, so digest-pinned
manifest consumption remains the correct approach for an atomic deployment set.

Job retries first pull the immutable output tag. If its target, commit, source, and dependency-input
identity labels match, the job returns the existing digest without rebuilding or pushing. A tag
whose identity differs is an explicit collision failure; immutable registry tags never need to be made
mutable for retry support.

## Test or deploy the newly built images

The generated stages are `build`, `manifest`, `consume`, and—on the default branch—`promote`.
`consume` is the supported extension point for project-specific scanning, integration testing, or
review deployment. Add a job to the local file already included with the generated YAML:

```yaml
integration-test-images:
  stage: consume
  needs:
    - job: publish_image_manifest
      artifacts: true
  script:
    - IMAGE_REF="$(jq -er '.images.api.immutable_reference' image-build-manifest.json)"
    - docker pull "$IMAGE_REF"
    - ./scripts/test-api-image "$IMAGE_REF"
```

For a dynamic target name use
`jq -er --arg name "$IMAGE_NAME" '.images[$name].immutable_reference'`. The job fails clearly when
the target was not rebuilt; if that is an allowed no-change case, test for membership before using
it. Because `promote_main` does not use `needs` to skip stages, it waits for every `consume` job.

For a semantic release, retain or download the manifest belonging to the exact release commit and
run:

```bash
platform images promote-manifest image-build-manifest.json \
  --tag "$CI_COMMIT_TAG" \
  --expected-commit "$CI_COMMIT_SHA" \
  --registry-transport docker
```

The command accepts only a default-branch manifest and promotes the recorded `@sha256` bytes; it
does not rebuild. See [the complete commit-to-release process](../README.md#from-commit-build-to-semantic-release).

The selected runner must provide:

- Python 3.12 and this installed package
- Git with full repository history
- a supported builder/transport pair: Docker Buildx, Podman, Buildah, or nerdctl/BuildKit, with the
  named image contexts and versions in [container backends](container-backends.md)
- network access to pull base images and pull/push images in the configured registry
- for generic OCI providers, username/password variables or an already authenticated trusted runner
- for ECR, AWS CLI plus short-lived workload authentication

Required variables include `PLATFORM_IMAGES_REGISTRY`, or the custom name in
`registry.registry_environment_variable`. GitLab supplies commit, pipeline, project, branch, and
merge-request variables. For a generic OCI provider with `authentication = "credentials"`, expose
the username and password variables named in `platform-images.toml` as masked, protected CI/CD
variables. `registry-login` passes the password to the selected transport over stdin and never
prints it. For `authentication = "ambient"`, runner provisioning must already own both transport
login and OCI API access.

ECR users should rely on the organisation's short-lived workload identity rather than static
repository secrets. `registry-login` parses the ECR region from the registry hostname, retrieves
the regional token, and passes it to the transport over stdin; it does not depend on an ambient
default region.

The example template sets `PLATFORM_IMAGES_RUNNER_TAG=podman` and expands it in `.image-build.tags`.
Override that CI/CD variable to route generated jobs to the fleet that provides the configured
builder and transport—for example `docker`, `buildah`, or `containerd`. Keep the actual backend
selection in `platform-images.toml`; the runner tag is scheduling metadata, not an execution switch.
