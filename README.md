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
[`sfdx-plugin-source-read`](https://github.com/amtrack/sfdx-plugin-source-read):
that plugin's `sf crud-mdapi read` command retrieves a **"full"** profile, but
the retrieved profile may contain `tabVisibilities` for tabs that are not
available in the org (the plugin README itself warns about this). Profile
Pruner cleans exactly those leftover references.

## How it works

1. `sf project deploy start --dry-run --wait` on the profile (the CLI waits
   and returns the full result — no manual polling).
2. Parse every component error and remove the offending XML blocks in one pass.
3. Refresh a cumulative `package.xml` with the errored metadata.
4. Repeat until the dry-run succeeds.

## Requirements

- Python 3.8+
- `lxml`
- Salesforce CLI (`sf`) on `PATH`, or pass `--sf <path>`
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
| `--project-dir` | Root of the SFDX project. Default: directory of this script. |
| `--api-version` | Manifest API version. Default: `68.0`. |
| `--max-iter` | Iteration cap. Default: `5000`. |
| `--sf` | Path to the Salesforce CLI. Default: `sf`. |
| `--wait-minutes` | Minutes the CLI waits for each deploy to complete. Default: `30`. |
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
└── .work/manifest.xml                       # internal manifest (not a deliverable)
```

## Notes

- The org is **never** modified unless you pass `--deploy-on-success`.
- `--quiet` still writes `report.txt`, `errors_log.txt` and `run.log`.

## Author

made with <3 by Lorenzo D'Aleo — turbolino.dev@proton.me

## License

GNU Affero General Public License v3.0 (AGPL-3.0) — see [LICENSE](LICENSE).
