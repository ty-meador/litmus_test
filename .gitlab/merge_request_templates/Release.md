# Release evidence

Version: <!-- MAJOR.MINOR.PATCH -->
Candidate commit: <!-- full SHA; refresh the evidence if the candidate changes -->
Previous release: <!-- tag, or "initial Python release" -->
Release issue: <!-- link -->

State what changed for the suite author and the Floor Technician. Link the dated
changelog section and name any upgrade action before requesting review.

## Before the release cut

- [ ] Read `docs/overview.md`, `docs/contracts.md`, and `docs/idioms.md` in full.
  Verify the candidate preserves runtime hardware and verdict integrity.
- [ ] Confirm the release scope and go/no-go decision with Ty and Zavala. For the
  initial release, record outstanding phase exit criteria and their disposition;
  merged hardening tooling alone does not establish release readiness.
- [ ] Resolve the distribution name and availability under
  [#9](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/9). Do not infer a publish
  target from the current `litmus` development metadata. The legacy repositories
  remain untouched; the decision leaves the TypeScript repository alone.
- [ ] Verify [#103](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/103) supplies
  the approved name, README, classifiers, URLs, and other package metadata, and
  [#102](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/102) supplies the wheel
  build and clean-install smoke test (`make smoke`, run by the gate). There is no
  publish pipeline: `make build` and the upload are bench steps below (#138).
  These are prerequisites, not capabilities supplied by this checklist.
- [ ] Review the version against `docs/api-stability.md`. If a promise changes, link
  Ty's `needs-sign-off` decision and update the public surface, reference table,
  documenting section, and changelog together. Apply the deprecation policy from 1.0.0.

## Candidate validation

- [ ] Run `uv sync --locked` and `make check` on the candidate at the bench (there is
  no CI). State "green locally" on the MR and record the commit, test count, coverage
  floors, strict docs build, and locked-dependency audit results. No checks or floors
  are weakened.
- [ ] Run `make soak` on the candidate and link all eight successful full-suite runs.
  For the initial release, one recorded bench soak on the candidate head is the
  Phase 6 exit (Ty, 2026-09-11 on #138): `make nightly-record` green, rack repeats
  included, posted to [#68](https://gitlab.com/Tyler-Meador/litmus_test/-/issues/68).
  Link that note.
- [ ] Run `make build` at the bench (`uv build` into `dist/`). Inspect both wheel and
  source distribution; verify the version, metadata, license, and packaged modules.
- [ ] Link the clean-environment wheel smoke test from #102: `litmus-check --list-rules`
  and `litmus run examples/hello_world.py` succeed using the installed wheel and a
  copied example outside the source checkout. Verify the expected PASS exit and
  generated evidence, and that imports resolve to the installed distribution.
- [ ] Confirm `make docs` builds strictly. The site is not hosted: the release branch
  cut below carries the build under `docs/site/`. Record the branch path readers
  will be given.

## Changelog cut and review

- [ ] Reconcile every notable merged change since the previous release with
  `CHANGELOG.md`. For the initial Python release, retain the six-phase migration
  history and the subsequent hardening/refinement entries; do not assign invented
  versions or release dates to those phases.
- [ ] Move the selected Unreleased entries to `## [VERSION] - YYYY-MM-DD`, using the
  actual release version and date. Leave a fresh Unreleased section for later MRs;
  refresh its historical cutoff text and any release comparison links.
- [ ] Set the matching distribution version in `pyproject.toml`, refresh `uv.lock`
  with uv, and rerun validation on the final candidate. `litmus.__version__` must
  report the installed version.
- [ ] Request Commander Zavala's review with the evidence above. Leave the MR open
  for him: only Zavala merges to `main`; never push the release commit there directly.

## After Zavala's merge and release authorization

- [ ] Record the merged commit and run `make check` on it again. If merge resolution
  changed the validated candidate, refresh the affected validation before the cut.
- [ ] The authorized releaser cuts `release/MAJOR.MINOR.PATCH` from that commit, runs
  `make docs-release`, and commits `docs/site/` on that branch only; `main` never
  carries the build. A new release branch is a new docs build. The branch is pushed
  and never rebased.
- [ ] The authorized releaser creates the approved version tag at the release branch
  head, then from the bench runs `make build` and uploads `dist/` to the approved
  package index (#9) with `uv publish`. Record the tag, the branch, and the upload
  output; a failed upload is not a released package, and an existing release tag is
  never moved.
- [ ] Install the published version in a fresh environment, rerun the CLI smoke test,
  and verify `litmus.__version__`. Confirm the public package page and the release
  branch's `docs/site/index.html` open; record their URLs and the installation result.
- [ ] Attach the dated changelog and upgrade instructions to the release record so
  station notes can cite the version and the behavior it establishes.

Evidence and remaining blockers:

<!-- Link each result. An unchecked prerequisite remains a blocker, not an assumption. -->
