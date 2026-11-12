# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.12.3] - 2026-11-12

### Fixed

- The Metadata API pre-pass no longer removes `standard-*` tab references
  (e.g. `standard-Account`, `standard-Case`): those are standard tabs, which
  `sf org list metadata --metadata-type CustomTab` cannot enumerate, so they
  were wrongly reported as dangling even when the profile came from the same
  org.
- The live board now keeps its frame at the end of a successful run: workers
  that finish because the queue is empty are marked `completed` (instead of
  `idle`) and the elapsed/clock summary line stays visible and frozen instead
  of being wiped.

## [1.12.2] - 2026-11-12

### Changed

- Replaced the ASCII banner with the new Unicode block-art banner.

## [1.12.1] - 2026-11-12

### Fixed

- **PowerShell mangling of trailing backslashes**: a quoted path ending in
  `\` (e.g. `--out-dir "...\\src Test\\dest Test\\"`) makes the Windows
  command-line parser turn the closing quote into a literal `"`, which then
  swallowed the remaining flags ("error: the following arguments are required:
  --org") or left a stray quote that crashed `os.makedirs` (WinError 123). The
  arguments are now detected and repaired automatically, with a clear NOTE
  printed. A malformed `--out-dir` also produces a clean error instead of a
  traceback.

## [1.12.0] - 2026-11-12

### Added

- `--api-version` no longer defaults to a hardcoded value. When omitted, the
  tool detects the **latest API version supported by the target org** via
  `sf org display` and uses it for the manifests. An explicit `--api-version`
  still overrides it. If the detection fails, the run stops with a clear error
  asking to pass `--api-version` explicitly.

### Changed

- Updated the CLI help (`sfpp -h`) and the README to document auto-detection
  and the Metadata API pre-pass consistently.

## [1.11.2] - 2026-11-11

### Changed

- Reworded the README intro: dropped the misleading "dry-run deploys only"
  framing (the tool can deploy via `--deploy-on-success`) and documented the
  self-healing final gate and the Metadata API pre-pass.

## [1.11.1] - 2026-11-11

### Fixed

- A **brand-new metadata type** (not yet in the section map) no longer forces
  `ABORTED` at the final gate: `apply_global_removals` now falls back to a
  generic removal by exact member name, so the reference is removed and the
  package is re-validated. This makes the final gate self-healing for future
  Profile sections whose deploy error uses the standard
  `In field: X - no Y named Z found` form.

## [1.11.0] - 2026-11-11

### Added

- **Metadata API pre-pass**: before the dry-run loop, the tool now enumerates the
  metadata that exists in the target org with `sf org list metadata` (one call
  per listable type) and removes every dangling reference in one shot — flows,
  login flows, layouts, record types, apps, tabs, permissions, custom metadata,
  external data sources, data category groups, presence statuses and more. It is
  version-aware: the newest Profile sections (`agentAccesses` API 63.0,
  `genComputingSummaryDefAccesses` API 66.0) are fetched only when the manifest
  API version includes them. Custom fields and custom/standard objects are left
  to the authoritative dry-run (listMetadata does not return standard fields/objects).
- New `--no-prefetch` flag to disable the pre-pass.
- `AgentDefinition` (agentAccesses) and `GenComputingSummaryDefinition`
  (genComputingSummaryDefAccesses) added to the section map.

## [1.10.12] - 2026-11-11

### Fixed

- `In field: Flow - no FlowDefinition named X found` from a **login flow** was
  not pruned: `Flow`/`FlowDefinition` were mapped only to `flowAccesses`, not
  to `loginFlows`. Both now also remove `loginFlows` (and `ApexPage` removes
  Visualforce-type `loginFlows`), so post-login flows whose flow is missing are
  stripped correctly.

### Added

- The final full-package dry-run is now a **self-healing loop**: when it
  surfaces a reference the per-section pass missed, it resolves and removes it
  and re-validates until clean (or until no progress is possible), instead of
  aborting on the first residual error.

## [1.10.11] - 2026-11-11

### Fixed

- Ctrl-C could hang on `Saving pending work...`: a worker blocked inside a
  slow `sf deploy report`/`deploy start` call never saw the stop flag. `sf`
  commands are now run via `Popen` with an interruptible poll loop, and the
  child process (and its tree) is terminated on stop, so the workers exit
  promptly and the shutdown completes.

## [1.10.10] - 2026-11-11

### Fixed

- The live board scrolled instead of redrawing in place on narrower consoles:
  the wide row format (~124 columns) wrapped onto two physical lines, which
  broke the ANSI cursor-up accounting. Rows are now clipped to the actual
  terminal width before rendering, and ANSI/VT enabling now uses the real
  stdout handle (with `STD_OUTPUT_HANDLE` as fallback).

## [1.10.9] - 2026-11-11

### Fixed

- The Windows-only `TestSfInvocation` tests are now skipped on non-Windows CI
  runners (the previous fix landed in the wrong working copy and was not
  committed), so the PyPI publish workflow finally passes on `ubuntu-latest`.

## [1.10.8] - 2026-11-11

### Fixed

- The PyPI publish workflow failed on the Linux CI runner because the
  `TestSfInvocation` tests exercise Windows-only batch-shim logic
  (`os.name == 'nt'`). Those tests now skip on non-Windows runners, so the
  release workflow passes while still running on Windows.

## [1.10.7] - 2026-11-11

### Added

- Trusted Publishing release workflow (`.github/workflows/publish.yml`):
  pushing a `v*` tag runs the tests, builds the sdist/wheel and publishes to
  PyPI via OpenID Connect (no PyPI token stored anywhere).

### Changed

- Merged `install_dependencies.py` into `first_config.py`: a single setup now
  installs the package + `lxml`, checks Node.js/`sf`, and puts `sfpp` on the
  PATH. The old script was removed.
- README now documents the PyPI install path (`pip install sf-profile-pruner`)
  as the recommended way to get the tool.

## [1.10.6] - 2026-11-11

### Fixed

- `first_config.py` could hang forever after `pip install` finished: it scanned
  the whole drive with a recursive `C:\**\Scripts` glob when the running Python
  lived at the drive root (e.g. `C:\Python310`). It now uses only the
  interpreter's own `sysconfig` scripts directory.

### Changed

- Refactored the single `prune_profile.py` module into a `prune_profile/`
  package split into thematic modules (`constants`, `xmlio`, `sfcli`, `board`,
  `state`, `engine`, `cli`). Public API re-exported from `__init__.py`; the
  console script is now `prune_profile.cli:main`, and `python -m prune_profile`
  also works. No behaviour change.
- Modernized `pyproject.toml`: SPDX license expression (`AGPL-3.0-only`),
  `license-files`, `setuptools>=77`; removed the deprecated license classifier
  and TOML-table license (clears the PyPI build warnings).
- `install_dependencies.py` now imports the package instead of loading the old
  single file.

## [1.10.5] - 2026-11-11

### Fixed

- `sfpp` could fail at startup with `ModuleNotFoundError: No module named
  'prune_profile'` because the editable install's finder was unreliable in
  multi-Python environments. The tool is now installed non-editable (the module
  is copied into site-packages), so `sfpp` resolves it deterministically.
- The `elapsed` clock on the live board only advanced when a worker posted an
  update; a background ticker now redraws it every second.
- Spawning more worker threads than there are sections to prune; the thread
  count is now capped to the number of sections.

### Changed

- `first_config.py` now performs a regular `pip install .` (refresh by
  re-running it after updates).

## [1.10.4] - 2026-11-11

### Fixed

- Ctrl-C was effectively ignored on Windows because the `sf` CLI is a
  `cmd.exe` batch shim: the console Ctrl-C also hit that cmd.exe, raised its
  "Terminate batch job (Y/N)?" prompt, and blocked the worker polling it. The
  `sf` command is now invoked directly as `node run.js` (bypassing the batch),
  so Ctrl-C reaches the program and stops it cleanly.
- On Ctrl-C the program now prints `Saving pending work...` before finalizing
  the state.

### Added

- `pyproject.toml` with a `sfpp` console-script entry point, so the tool can
  be installed with `pip install .` (and published to PyPI).

### Changed

- `first_config.py` now installs the package via `pip install -e .` and puts
  the real `sfpp.exe` (not a `.cmd` batch) on the PATH.

## [1.10.3] - 2026-11-11

### Fixed

- Salesforce deploy statuses are now shown lower-case with spaces on the live
  board (`InProgress` -> `in progress`, `Pending` -> `pending`).
- Ctrl-C no longer produces a traceback: the temporary work area is removed
  with an error-tolerant cleanup, and a second Ctrl-C during shutdown is
  ignored so the workers release their temp directories before cleanup.
- Resume now keeps the original section sizes, so the board shows the true
  cumulative `initial`/`deleted` instead of resetting them, and the error-log
  sequence continues instead of restarting at 1.

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
