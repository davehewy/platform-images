# Amazon ECR setup

ECR infrastructure is owned outside this image-source repository. Configure it once through the
organisation's normal infrastructure repository; the controller deliberately does not call
`create-repository` for every target and never deletes repositories for removed image directories.

Create an ECR repository creation template matching the `platform-images/` namespace and apply it
to `CREATE_ON_PUSH`, so a first push to `platform-images/<new-target>` creates a consistently
governed repository. Apply:

- organisation-approved encryption and repository policy
- standard AWS resource tags
- immutable tags by default, with a mutability exclusion for the exact `main` alias
- a lifecycle rule expiring `ci-*` tags after 7 days
- a lifecycle rule expiring untagged manifests after 1 day
- the organisation's retention policy for older immutable `sha-*` images

Repository creation templates affect repositories created after the template is installed; they do
not retroactively align existing repositories. Inventory existing `platform-images/*` repositories
and perform a one-time policy, encryption, tag-mutability, lifecycle, and resource-tag migration.

The GitLab role needs ECR authentication plus pull and push operations: token retrieval, layer
availability checks, upload initiation/parts/completion, image put, batch image lookup, and image
download URLs. Limit repository resources to `platform-images/*` wherever the API supports it.
The planning role also needs `ecr:DescribeImages` so `main` can be resolved to an immutable digest.
The controller passes the account ID and region parsed from `PLATFORM_IMAGES_REGISTRY` explicitly
to the AWS CLI; ambient AWS defaults cannot redirect a stable dependency lookup.

Bootstrap the namespace from the default branch with:

```bash
platform images plan --ci --all --format gitlab
```

That builds every target under immutable `sha-*` tags and promotes all `main` aliases only after the
graph succeeds. Partial merge-request builds that need an upstream image intentionally fail until
this bootstrap has established a stable digest.

Immutable `ci-*` and `sha-*` tags are retry-safe. A retried job reuses an existing output only when
its image identity labels match the exact target, commit, project source, and dependency references;
otherwise the collision fails before a rebuild can overwrite anything.
