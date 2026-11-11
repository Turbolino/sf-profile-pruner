# Profile Pruner

Remove every reference to metadata that does **not** exist in a Salesforce org
from a **Profile** `.profile-meta.xml` file.

Pruning is done with **dry-run deploys only** — the org is never modified
unless you explicitly pass `--deploy-on-success`. The result is a **GOOD**
package (a profile that deploys cleanly) and an **ERRORS** package (the list
of everything that was removed / is missing).

## Why it exists

A profile exported from a source org (or a managed package org) often
references objects, fields, tabs, classes, flows, layouts, record types, etc.
that do not exist in the target org. Deploying such a profile fails. Profile
Pruner finds those references one by one and strips them.

It works great together with
[`sf-profile-full`](https://github.com/Archallys-Inc/sf-profile-full):
install it with `sf plugins install @archallysinc/sf-profile-full` and run

```bash
sf profile retrieve full --name <Profile> --target-org <org> --clean
```

to retrieve the **complete** profile — all field-level security, layout
assignments, class access, page access and tab visibility. The `--clean` flag
already strips the org-specific, non-portable elements (`<loginIpRanges>`,
`<loginHours>`, `<userLicense>`) that differ between sandbox and production.

The retrieved profile may still reference metadata that does not exist in the
target org (for example `tabVisibilities` for tabs that are not available),
which causes deploy errors. Profile Pruner cleans exactly those leftover
references.

(A similar plugin is
[`sfdx-plugin-source-read`](https://github.com/amtrack/sfdx-plugin-source-read),
with the `sf crud-mdapi read` command.)

## How it works

1. Start an async deploy dry-run (`sf project deploy start --dry-run --async`)
   which returns a job ID immediately.
2. Poll the job status with `sf project deploy report --job-id <ID>` until it
   reaches a terminal status (Succeeded/Failed/…).
3. Parse every component error and remove the offending XML blocks in one pass.
4. Refresh a cumulative `package.xml` with the errored metadata.
5. Repeat until the dry-run succeeds.

## Requirements

- Python 3.8+
- `lxml`
- Salesforce CLI (`sf`) — auto-discovered from `PATH` and common install
  locations, or pass `--sf <path>` explicitly
- An authenticated org (alias or username) that the CLI can reach

## Installation

Install the dependencies:

```bash
python install_dependencies.py
```

One-time setup that installs the dependencies and makes the tool callable as
`sfpp` from any terminal:

```bash
python first_config.py
```

Then open a **new** terminal and run:

```bash
sfpp --help
```

> `first_config` creates an `sfpp` launcher (`sfpp.cmd` on Windows) and adds
> it to your user `PATH`. The launcher hardcodes the Python interpreter that
> ran `first_config`, so it keeps working even if `lxml` is not installed in
> your default `python`.

## Usage

```bash
sfpp \
  --profile-path "path/to/My Profile.profile-meta.xml" \
  --profile-name "My Profile" \
  --out-dir "path/to/output" \
  --org myorg
```

Or, without the `sfpp` launcher:

```bash
python prune_profile.py \
  --profile-path "path/to/My Profile.profile-meta.xml" \
  --profile-name "My Profile" \
  --out-dir "path/to/output" \
  --org myorg
```

## Behaviour

- **Dry-run by default**: the org is never modified.
- **`--deploy-on-success`**: after the profile is clean, deploy it to the org.
- **Batch removal**: every component failure reported by a single deploy is
  resolved and removed in one pass (not one error per run).
- **Resumable**: if it stops (network hiccup, unexpected error), run the exact
  same command again and it continues from the last error — nothing is redone.
- **Already clean**: if a previous run ended with `SUCCESS` and you launch it
  again on the same output directory, it exits immediately with "profile
  already clean".
- **`--fresh`**: restart from the original profile, ignoring any previous state.

## Options

| Option | Description |
| --- | --- |
| `--profile-path` | Path of the source `.profile-meta.xml` file. |
| `--out-dir` | Output directory (report, packages, logs). |
| `--org` | Alias or username of the org used for the dry-run. |
| `--profile-name` | Profile name (package member). Default: derived from the file name. |
| `--project-dir` | Root of an existing SFDX project. If omitted, a minimal temporary project is created inside `--out-dir` and removed at the end. |
| `--api-version` | Manifest API version. Default: `68.0`. |
| `--max-iter` | Iteration cap. Default: `5000`. |
| `--sf` | Path to the Salesforce CLI. Default: `sf`. |
| `--poll-interval` | Seconds between status polls of a deploy job. Default: `5`. |
| `--job-timeout` | Minutes to wait for a deploy job to finish before aborting. Default: `30`. |
| `--fresh` | Ignore previous state and restart from the original. |
| `--quiet` | Suppress progressive stdout output. |
| `--deploy-on-success` | Deploy the cleaned profile to the org once pruning succeeds. |
| `-V`, `--version` | Show version and author. |

## Output layout (`--out-dir`)

```
out-dir/
├── ORIGINAL_<name>.profile-meta.xml   # untouched copy of the original
├── GOOD/
│   ├── package.xml
│   └── profiles/<name>.profile-meta.xml   # clean, deployable profile
├── ERRORS/
│   └── package.xml                          # everything removed / missing
├── errors_log.txt                           # resumable line-by-line log
├── run.log                                  # progressive log copy
├── report.txt                               # final summary (OUTCOME: SUCCESS/ABORTED)
└── .work/
    ├── manifest.xml                         # internal manifest (not a deliverable)
    └── project/                             # minimal temporary SFDX project (removed at the end)
```

> You don't need your own SFDX project. If `--project-dir` is omitted, the tool
> creates a minimal one (`sfdx-project.json` only) under `.work/project/`, uses
> it for the deploy commands, and deletes it when finished.

## Notes

- The org is **never** modified unless you pass `--deploy-on-success`.
- `--quiet` still writes `report.txt`, `errors_log.txt` and `run.log`.

## Author

made with <3 by Lorenzo D'Aleo — turbolino.dev@proton.me

## License

GNU Affero General Public License v3.0 (AGPL-3.0) — see [LICENSE](LICENSE).
