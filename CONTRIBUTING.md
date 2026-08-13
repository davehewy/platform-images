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
uv build
```

Use Conventional Commits because release versions and changelogs are derived from them. Commitizen
can prompt for a valid message:

```bash
uv run --locked --extra dev cz commit
```

Examples include `feat: add a parser capability`, `fix: preserve an affected dependency`, and
`docs: explain registry setup`. Mark breaking changes with `!` or a `BREAKING CHANGE:` footer.

Pull request titles are checked with Commitizen so squash merges remain release-compatible. After
`main` passes the GitHub CI workflow, Python Semantic Release updates both version declarations and
the lockfile, writes `CHANGELOG.md`, creates the version tag and GitHub release, and attaches the
universal Python wheel and source distribution. Native release jobs then build and smoke-test
standalone Linux and macOS archives for AMD64 and ARM64, publish their checksums, and attach them to
the same release. It does not publish to PyPI.

Release commits and tags are created by `github-actions[bot]`. The release action can optionally be
configured with repository SSH signing-key secrets if cryptographic signatures are required for
automated release commits; contributor commits remain subject to the local signing policy.
