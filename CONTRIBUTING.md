# Contributing

Setup and local checks are in [AGENTS.md](AGENTS.md) ("Before finishing"); releases are in [RELEASE.md](RELEASE.md).

## Commits and PR titles

Commit messages and PR titles follow [Conventional Commits](https://www.conventionalcommits.org):

```
<type>(<optional scope>): <summary, no full stop>
```

Types: `feat`, `fix`, `docs`, `test`, `ci`, `build`, `refactor`, `perf`, `style`, `chore`, `revert`. The scope is optional; a plugin or area name works well (`feat(slack-home): show payments in a table`). Mark a breaking change with `!` (`feat!: ...`) and explain it in a `BREAKING CHANGE:` footer. The first line is at most 100 characters.

Merge commits, `git revert` commits and the messages GitHub's web UI writes ("Apply suggestions from code review", "Update README.md") are not checked.

## PR descriptions

GitHub fills a new PR with [the template](.github/pull_request_template.md). Keep its three headings:

- **Summary**: a few short bullets (at least one; more than six gets a warning).
- **Risks**: what could go wrong and how you checked. "None: docs only" is a fine answer.
- **How to revert**: usually "Revert this PR", plus anything a revert doesn't undo.

Add other sections (Testing, Screenshots) after them if they help.

## What CI runs

| Workflow | Job | What it checks | Typical time |
|---|---|---|---|
| PR conventions | `conventions` | Every commit message, the PR title and the PR description | about 10 seconds |
| CI | `tests` | Version, shell syntax, whitespace, `ruff`, the test suite | about 20 seconds |
| CI | `container` | Image build, `scripts/smoke_image.py`, the Helm chart on kind | 2 to 5 minutes |

Editing the PR title or description re-runs only **PR conventions**. `container` is skipped when a PR changes only repo docs and PR tooling (`AGENTS.md`, `CONTRIBUTING.md`, `RELEASE.md`, `.claude/`, `tools/pr-checks/`).

To run the convention checks locally you need [Bun](https://bun.sh):

```sh
cd tools/pr-checks
bun install --frozen-lockfile
bunx --bun commitlint --from origin/main --to HEAD --verbose   # your branch's commits
bun test                                                  # the description checker's own tests
```
