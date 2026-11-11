# Profile Pruner

Remove every reference to metadata that does **not** exist in a Salesforce org
from a **Profile** `.profile-meta.xml` file — using **only** dry-run deploys.
The target org is never modified.

A common cause: a profile exported from a source org (or a managed package org)
references objects, fields, tabs, classes, flows, layouts, record types, etc.
that do not exist in the target org. Deploying such a profile fails. This tool
finds those references one by one and strips them.

## How it works

1. `sf project deploy start --dry-run` on the profile.
2. Parse the first component error and remove the offending XML block.
3. Refresh a cumulative `package.xml` with the errored metadata.
4. Repeat until the dry-run succeeds.

The result is a **GOOD** package (a profile that deploys cleanly) and an
**ERRORS** package (the list of everything that was removed / is missing).

## Requirements

- Python 3.8+
- `lxml` (`pip install lxml`)
- Salesforce CLI (`sf`) on `PATH`, or pass `--sf <path>`
- An authenticated org (alias or username) that the CLI can reach

## Installation

```bash
git clone <repo-url>
cd profile-pruner
pip install lxml
```

## Usage

```bash
python prune_profile.py \
  --profile-path "path/to/My Profile.profile-meta.xml" \
  --profile-name "My Profile" \
  --out-dir "path/to/output" \
  --org myorg
```

The tool is resumable: if it stops (network hiccup, unexpected error), just run
the exact same command again and it continues from the last error — nothing is
redone. Use `--fresh` to restart from the original profile.

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
| `--fresh` | Ignore previous state and restart from the original. |
| `--quiet` | Suppress progressive stdout output. |
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
├── report.txt                               # final summary
└── .work/manifest.xml                       # internal manifest (not a deliverable)
```

## Notes

- The org is **never** modified: every check is a `--dry-run`.
- `--quiet` still writes `report.txt`, `errors_log.txt` and `run.log`.

## Author

made with <3 by Lorenzo D'Aleo — turbolino.dev@proton.me

## License

MIT — see [LICENSE](LICENSE).
