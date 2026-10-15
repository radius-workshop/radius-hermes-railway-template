# Releasing the template

`VERSION` is the single template version source. The agent card, `/livez`,
authenticated `/health`, and image `/app/build-info.json` report that value.
Plugin versions and A2A/ANS protocol versions have their own compatibility rules.

Use Semantic Versioning: patches fix existing behavior, minor versions add
compatible capabilities, and major versions introduce incompatible changes.
During `0.x`, document incompatible changes in the release notes.

## Prepare a release

1. Merge the intended changes and ensure CI passes on `main`.
2. Update `VERSION` and `RELEASE_NOTES.md` together in a release PR.
3. Validate locally with Python 3.11:

   ```sh
   python3.11 -m venv .venv
   .venv/bin/python -m pip install -r requirements-dev.txt
   .venv/bin/python scripts/run_tests.py
   .venv/bin/python scripts/validate_release.py --tag "v$(cat VERSION)"
   docker build --build-arg VCS_REF="$(git rev-parse HEAD)" -t radius-hermes:release .
   docker run --rm --entrypoint python -e HERMES_HOME=/tmp/hermes -e PUBLIC_URL=https://agent.example.com radius-hermes:release /app/scripts/smoke_image.py
   ```

4. After merging, tag the merged commit and push the tag:

   ```sh
   git switch main
   git pull --ff-only
   git tag -a "v$(cat VERSION)" -m "Release v$(cat VERSION)"
   git push origin "v$(cat VERSION)"
   ```

The Release workflow verifies tag/version agreement and that the tagged commit
belongs to `main`, runs the test suite, builds and checks the container, then
publishes a GitHub Release with the build manifest attached. A failed run can be
retried through the workflow's manual trigger using the existing tag. Never move
a published tag; fix the problem in a new version.

## Build provenance and deployment

Hermes defaults to `v2026.9.24`, compatible with the Python 3.11 image. Override
`HERMES_GIT_REF` with a branch, tag, or full commit SHA when testing an upgrade.
`radius-cli` defaults to the pinned version declared in the Dockerfile.
`/app/build-info.json` records the resolved Hermes, Linear skill, and Radius skill
commits and installed Python package versions. CI attaches this manifest to every
release; it contains no credentials.

Some external installers and transitive dependencies remain mutable. The manifest
records a build's inputs; it does not guarantee byte-for-byte rebuilds. CI builds
and tests an image but does not publish a container image or deploy Railway.
Deploy the desired tag/commit through the Railway workflow or service settings,
then verify `/livez` reports the expected version. `/livez` checks the HTTP process;
verify Hermes responses and any configured integrations separately.

The image installs Telegram, Discord, and Slack chat dependencies from `scripts/requirements-messaging.txt`. These match the pinned Hermes messaging requirements with the optional Discord voice extra excluded: its published PyNaCl constraint conflicts with the upstream security override used by uv. Discord voice-channel support is unavailable in this image; update the messaging pins alongside `HERMES_GIT_REF`.
