# Contributing

Install the locked development environment and all Git hooks:

```bash
uv sync --locked --extra dev
uv run --locked --extra dev pre-commit install
```

The configured hook types run Ruff and repository validation before commits, Commitizen against
every commit message, and unit tests before pushes. Run the complete local gate explicitly with:

```bash
uv run --locked --extra dev pre-commit run --all-files
uv run --locked --extra dev pytest
uv run --locked python scripts/benchmark-graph.py --images 100 --iterations 25
uv build
```

The graph benchmark synthesizes a multi-root, 100-image DAG. CI applies a generous deep-leaf
planning ceiling to catch complexity regressions; use `--json` for comparable local measurements.

Use Conventional Commits because release versions and changelogs are derived from them. Commitizen
can prompt for a valid message:

```bash
uv run --locked --extra dev cz commit
```

Examples include `feat: add a parser capability`, `fix: preserve an affected dependency`, and
`docs: explain registry setup`. Mark breaking changes with `!` or a `BREAKING CHANGE:` footer.

Pull request titles are checked with Commitizen so squash merges remain release-compatible. After
`main` passes the GitHub CI workflow, Python Semantic Release calculates the version from Git
history, stamps the release workspace, creates a tag on that exact tested commit, and attaches the
universal Python wheel and source distribution. It does not push a generated version-bump commit
back to `main`; Hatch derives package and CLI versions from the immutable Git tag instead. Native
release jobs then build and smoke-test
standalone Linux, macOS, and Windows archives for AMD64 and ARM64, publish their checksums, and
attach them to the same release. The release also publishes an SPDX JSON SBOM, signs GitHub
artifact attestations for provenance and SBOM association, then verifies every checksum and
attestation from the public release before installer jobs execute the downloaded binary. It does
not publish to PyPI.

Release tags are created by `github-actions[bot]` only after CI succeeds. Contributor changes reach
`main` through its protected pull-request path and remain subject to the repository's signing
policy; the release automation needs no personal access token or branch-protection bypass.
