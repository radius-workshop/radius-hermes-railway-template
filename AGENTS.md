# Notes for coding agents

Setup, configuration and deployment are in [README.md](README.md); versioning and
releases are in [RELEASE.md](RELEASE.md).

## Before finishing

Run what CI's `tests` job runs, with Python 3.11:

```sh
python scripts/validate_release.py
find . -type f -name '*.sh' -not -path './.git/*' -print0 | xargs -0 -r -n1 bash -n
git diff --check "$(git hash-object -t tree /dev/null)" HEAD
ruff check .
python scripts/run_tests.py
```

In Claude Code cloud sessions the SessionStart hook creates `.venv` and puts it on
`PATH`, and the Stop hook (`.claude/hooks/before-finish.sh`) runs these checks for
you, including on uncommitted and untracked files.

CI's `container` job (`docker build` plus `scripts/smoke_image.py`) can't run in
a cloud session: build steps have no network there. If you change the
`Dockerfile`, `scripts/entrypoint.sh`, the requirements files or
`scripts/smoke_image.py`, say so in the PR and rely on CI for that job.

## Rules

- This file is for agents working on the repo. The deployed Hermes agent reads
  `HERMES.md` and `skills/*.md`; they ship in the image, and tests assert on the
  tool and plugin names they mention. Put runtime behaviour there, not here.
- Use Python 3.11, as CI and the image do. The system `python3` in cloud
  sessions is 3.13 with no dependencies, and pip can't install into it.
- Image inputs are pinned in the `Dockerfile` (`HERMES_GIT_REF`,
  `RADIUS_CLI_VERSION`, `FOUNDRY_VERSION`, `LINEAR_SKILL_REF`,
  `RADIUS_SKILLS_REF`). Bump them deliberately, one at a time.
