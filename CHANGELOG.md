# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.10.2] - 2026-11-11

### Fixed

- Ctrl-C was ignored on Windows: the main thread joined workers with an
  unbounded `join()`, which never checks the pending interrupt. Reverted to
  short bounded joins (0.5 s) so the interrupt is raised promptly while still
  stopping gracefully (save pending work, keep board rows, mark active threads
  "stopped by user").

## [1.10.1] - 2026-11-11

### Changed

- Ctrl-C no longer prompts for resume/stop: it now stops the run automatically
  (as if "S" were pressed), saves all pending work, keeps the board rows on
  screen, and marks the active threads "stopped by user".

## [1.10.0] - 2026-11-11

### Added

- Ctrl-C now pauses the run and prompts `Resume [N] / stop [S]`: N resumes the
  workers, S marks the active threads "stopped by user" and prints the summary.
  The board rows are no longer wiped on interrupt.

### Changed

- Live board columns widened and labelled `remaining` / `deleted` / `initial`.
- Child `sf` process runs in its own process group, so Ctrl-C no longer shows
  the cmd.exe "Terminate batch job" prompt.

## [1.9.0] - 2026-11-11

### Added

- Structured live progress board: one line per thread with columns
  `[Tx] status | tag | operation | rem/del/ini`, plus a summary line with the
  elapsed time (hh:mm:ss) and tag counters (done / running / todo).
- Graceful handling of Ctrl-C (interrupts the run and reports `ABORTED`).

### Changed

- Author note capitalised to "Made with <3 …".
- Docstring and README output layout aligned with the actual `.work/` behaviour.

## [1.8.0] - 2026-11-11

### Changed

- `GOOD/` now contains only the cleaned profile (`GOOD/<name>.profile-meta.xml`);
  the `profiles/` subfolder and the GOOD `package.xml` are no longer produced.
- Updated the "already clean" early exit and `--fresh` cleanup to match the new
  GOOD layout.

## [1.7.0] - 2026-11-11

### Added

- Live progress board: one self-updating line per worker thread (status + current
  activity) during pruning, with a summary printed at the end.

### Removed

- Per-metadata `removed '...'` console/run.log lines.

## [1.6.1] - 2026-11-11

### Added

- Mapping for `customSettingAccesses`, `servicePresenceStatusAccesses` and
  `categoryGroupVisibilities` blocks (previously left untouched).

## [1.6.0] - 2026-11-11

### Added

- Final dry-run of the whole assembled package as a safety gate before `SUCCESS`.

## [1.5.0] - 2026-11-11

### Added

- Parallel pruning: profile split by top-level sections (discovered dynamically),
  processed by up to `--thread-number` workers (default 8, range 1-64).
- Mutex-protected writes to the GOOD profile, ERRORS package and error log.

## [1.4.2] - 2026-11-11

### Added

- Auto-detection of a compatible Node.js (>= 18.11), prepended to `PATH` so the
  Salesforce CLI works even when an old Node is the system default.

## [1.4.1] - 2026-11-11

### Added

- Auto-discovery of the Salesforce CLI from `PATH` and common install locations
  (including toolchain prefixes).

## [1.4.0] - 2026-11-11

### Added

- Self-contained temporary micro-project (single `sfdx-project.json`), created
  inside the output dir and removed at the end, so no external SFDX project is
  required (`--project-dir` is now optional).
- Reconstruction of the working copy from `errors_log.txt` on resume.

## [1.3.0] - 2026-11-11

### Added

- Async deploy + job polling (`deploy start --async` + `deploy report`) replacing
  the blocking `--wait`; new `--poll-interval` and `--job-timeout` options.

## [1.2.0] - 2026-11-11

### Added

- Batch removal of every failure reported by a single deploy.
- Robust error capture for unparseable/empty CLI output.

## [1.1.0] - 2026-11-11

### Added

- `--deploy-on-success` (default remains dry-run only).
- Early exit when a previous run already ended with `SUCCESS`.
- `sfpp` launcher via `first_config.py`; dependency installer.

## [1.0.0] - 2026-11-11

### Added

- Initial release: iterative dry-run pruning of a Salesforce Profile, producing
  a deployable GOOD profile and an ERRORS package of removed references.
- AGPL-3.0 licensing, ASCII banner, author metadata.
