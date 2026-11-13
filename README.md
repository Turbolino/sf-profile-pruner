# Profile Pruner

**The tool that finally lets you deploy Salesforce Profiles across
environments without second thoughts.**

Profile Pruner removes every reference to metadata that does **not** exist in a
target org from a **Profile** `.profile-meta.xml` file, so it deploys cleanly on
the first try. It is **dry-run by default** — the org is never modified unless
you explicitly pass `--deploy-on-success`. The result is a **COMMON** package (a
profile that deploys cleanly, plus a `toDeploy.xml` manifest of the deployable
members) and a **MISSING** package (the list of everything that was removed /
is missing).

## Why it exists

A profile exported from a source org (or a managed package org) often
references objects, fields, tabs, classes, flows, login flows, layouts, record
types, agents, enhanced summaries, etc. that do not exist in the target org.
Deploying such a profile fails. Profile Pruner finds those references one by
one and strips them.

You have two ways to feed it a profile:

* **via 1 — from a file** (`-pp/--profile-path` + `-do/--dest-org`): prune a
  `.profile-meta.xml` you already have.
* **via 2 — from an org** (`-so/--src-org` + `-sp/--src-profile` + `-do` +
  `-dp`): sfpp retrieves the **complete** profile from the source org itself —
  all field-level security, layout assignments, class access, page access and
  tab visibility — using the Metadata API `readMetadata()` (through a small,
  owned Node helper that reuses the Salesforce CLI's authentication). By default
  the retrieve is **clean** (it strips the org-specific, non-portable
  `loginIpRanges`, `loginHours`, `userLicense`); pass `-fp/--full-profile` for
  the full, non-stripped profile.

> The third-party `sf-profile-full` plugin is **no longer required**. To use
> via 2 you need the Salesforce CLI installed **with Node.js and its global
> node_modules** (`@salesforce/core` + `@jsforce/jsforce-node`), which any recent
> `sf` installation already ships.

The retrieved (or provided) profile may still reference metadata that does not
exist in the destination org (for example `tabVisibilities` for tabs that are
not available), which causes deploy errors. Profile Pruner cleans exactly those
leftover references.

## How it works

1. The profile is split by its top-level sections (`classAccesses`,
   `fieldPermissions`, `tabVisibilities`, `layoutAssignments`, …) — the sections
   are discovered dynamically from the XML, not hardcoded.
2. **Metadata API pre-pass (new in 1.11):** before anything is deployed, the
   tool enumerates the metadata that actually exists in the target org with
   `sf org list metadata` (one call per type) and removes every reference that
   has no matching component — login flows, agent access, enhanced summaries,
   flows, layouts, record types, and every other listable type — in one shot.
   This is much faster than discovering each missing reference through dry-runs
   and covers the newest Profile sections automatically. The per-section dry-run
   below remains the authoritative fallback.
3. Up to `--thread-number` workers run in parallel (capped to the number of
   sections to prune); each worker takes one section and, in a loop:
   - builds a sub-package containing only that section,
   - starts an async dry-run (`sf project deploy start --dry-run --async`)
     and polls `sf project deploy report` until terminal,
   - removes every offending entry and, under a mutex, writes the cleaned
     entries and the removed ones back in their original order.
4. The worker repeats until its section dry-runs clean, then takes the next
   section from the queue.
5. COMMON/ and MISSING/ are assembled from the surviving entries, keeping the
   original document order.
6. As a final safety check, the tool runs one last **dry-run of the whole
   assembled package**; only if it succeeds the outcome is `SUCCESS`. This final
   gate is **self-healing**: if the full package surfaces a reference the
   per-section pass missed — a login flow, or a brand-new metadata type — it
   removes it and re-validates until clean, instead of aborting on the first
   error.

## Requirements

- Python 3.9+
- `lxml` (installed automatically by the scripts below)
- Salesforce CLI (`sf`) — auto-discovered from `PATH` and common install
  locations, or pass `--sf <path>` explicitly
- Node.js >= 18.11 — auto-detected and prepended to `PATH`, so the CLI works
  even when an old Node is the system default
- An authenticated org (alias or username) that the CLI can reach

## Installation

### From PyPI (recommended)

```bash
pip install sf-profile-pruner
```

This installs the package, its dependency (`lxml`) and the `sfpp` command.

```bash
sfpp --help
```

> The `sfpp` command is a real console-script executable (not a `.cmd` batch
> file), so pressing Ctrl-C goes straight to the program — cmd.exe never shows
> its "Terminate batch job (Y/N)?" prompt.

> **`sfpp` not recognized after install?** This usually means the Python
> `Scripts` folder is not on your `PATH`. See
> [INSTALLATION.md](INSTALLATION.md) for a full step-by-step Windows guide
> (including the GUI method that works in Constrained Language Mode), or use
> `python -m prune_profile` as a PATH-independent fallback.

### From the repository

One-time setup that installs the package + dependency, creates the `sfpp`
command, checks Node.js and the Salesforce CLI, and puts `sfpp` on your PATH:

```bash
python first_config.py
```

Then open a **new** terminal and run:

```bash
sfpp --help
```

> `first_config` runs `pip install .` under the hood, so it also resolves the
> `lxml` dependency. Re-run it after updating the tool to refresh the installed
> copy.

> **Requirements checked at setup time** (and auto-discovered at run time if
> missing): a compatible **Node.js (>= 18.11)** and the **Salesforce CLI
> (`sf`)**. `first_config` reports their exact location, or what is missing.

## Usage

```bash
# via 1 — prune a profile file and dry-run against the destination org
sfpp -pp "path/to/My Profile.profile-meta.xml" -do myorg

# via 1 — deploy the cleaned profile (renamed) to the destination org
sfpp -pp "path/to/My Profile.profile-meta.xml" -do prod -dp "My Profile Clean" --deploy-on-success

# via 2 — retrieve from a source org, prune, deploy to the destination org
sfpp -so devorg -sp "My Profile" -do mysandbox -dp "My Profile" --deploy-on-success
```

Or, without the `sfpp` launcher, replace `sfpp` with `python -m prune_profile`.

## Behaviour

- **Dry-run by default**: the org is never modified.
- **`--deploy-on-success`**: after the profile is clean, deploy it to the org.
- **Live progress board**: during pruning, the console shows one
  self-updating line per worker thread with columns `[Tx] status | tag |
  operation | remaining/deleted/initial`, plus a summary line with the elapsed
  time (hh:mm:ss) and the tag counters (done / running / todo). The elapsed
  clock ticks every second even while the workers are just polling. The full
  timestamped log still goes to `run.log`, and a final summary is printed at
  the end.
- **Ctrl-C stops cleanly**: the run prints `Saving pending work...`, saves all
  pending work (the removals applied so far are kept), keeps the board rows on
  screen, marks the active threads "stopped by user", and reports `ABORTED`.
  The `sf` child is invoked as `node run.js` (not through the `cmd.exe` batch
  shim), so Ctrl-C can never trigger cmd's "Terminate batch job" prompt.
- **Metadata API pre-pass**: before the dry-run loop, the tool enumerates the
  metadata that actually exists in the target org (`sf org list metadata`) and
  removes every dangling reference in one shot — login flows included.
- **Standard metadata pre-pass**: the tool also queries the org's Tooling API
  (`TabDefinition`, `EntityDefinition`, `FieldDefinition`) and removes standard
  tabs, objects and fields the profile references but the org does not have.
  This catches the class of error a dry-run never surfaces — e.g. `You can't
  edit tab settings for X, as it's not a valid tab` — where an object exists
  but has no editable tab.
- **Self-healing final gate**: the final full-package dry-run loops — any
  residual reference (a login flow, or a metadata type not yet in the internal
  map) is removed and the package re-validated until clean, rather than
  aborting on the first error.
- **Batch removal**: every component failure reported by a single deploy is
  resolved and removed in one pass (not one error per run).
- **Resumable**: if it stops (network hiccup, unexpected error), run the exact
  same command again and it continues from the last error — nothing is redone.
- **Already clean**: if a previous run ended with `SUCCESS` and you launch it
  again on the same output directory, it exits immediately with "profile
  already clean".
- **`--fresh`**: restart from the original profile, ignoring any previous state.

## Options

Options are grouped in the `-h` help. The two input modes are:

* **via 1 (file):** `-pp/--profile-path` + `-do/--dest-org`
* **via 2 (org):** `-so/--src-org` + `-sp/--src-profile` + `-do` + `-dp`

| Option | Description |
| --- | --- |
| `-pp`, `--profile-path` | Path of the source `.profile-meta.xml` file (via 1). |
| `-so`, `--src-org` | Source org alias/username to retrieve from (via 2). |
| `-sp`, `--src-profile` | Name of the Profile to retrieve from `--src-org` (via 2). |
| `-fp`, `--full-profile` | Retrieve the full (non-stripped) profile; default is a clean retrieve (`loginIpRanges`/`loginHours`/`userLicense` removed). |
| `-do`, `--dest-org` | Destination org alias/username (dry-run and optional deploy target). |
| `-dp`, `--dest-profile` | Name to deploy the cleaned profile under; created if it does not exist. Default: the source profile name. |
| `-od`, `--out-dir` | Output directory for report/packages/logs. Default: the current directory. |
| `-bs`, `--batch-size` | Split a section into chunks of `N` entries (min 100, max 4000, default 200); `0` disables splitting. |
| `-tn`, `--thread-number` | Max parallel workers, `1`–`64`. Default: `16`. |
| `--job-timeout` | Minutes to wait for a deploy job before aborting (`1`–`60`; `0` = no timeout). Default: `0`. |
| `-av`, `--api-version` | Manifest API version. Default: detected from the destination org. |
| `--deploy-throttle-ms` | Min interval between deploy starts, in ms. Default: `1000`; `0` disables. |
| `--poll-interval` | Seconds between status polls of a deploy job. Default: `5`. |
| `--max-iter` | Iteration cap. Default: `5000`. |
| `--sf` | Path to the Salesforce CLI. Default: `sf`. |
| `--deploy-on-success` | Deploy the cleaned profile to `--dest-org` once pruning succeeds. |
| `--fresh` | Restart from the original, ignoring any previous state. |
| `--quiet` | Suppress the progressive stdout output. |
| `--tell-me-a-story` | Turbolino will tell you a fascinating story about code, algorithms and copyright. |
| `-V`, `--version` | Show version and author. |

**Deprecated** (still accepted, removed in v2.x.x): `--org` → `--dest-org`,
`--profile-name` → `--dest-profile`, `--retrieve-full` → `--full-profile`,
`--max-elem-per-section` → `--batch-size`.

## Output layout (`--out-dir`)

```
out-dir/
├── ORIGINAL_<name>.profile-meta.xml   # untouched copy of the original
├── COMMON/
│   ├── <name>.profile-meta.xml          # clean, deployable profile
│   └── toDeploy.xml                     # manifest of the deployable (COMMON) members
├── MISSING/
│   └── package.xml                          # everything removed / missing
├── missing_log.txt                           # resumable line-by-line log
├── run.log                                  # progressive log copy
├── report.txt                               # final summary (OUTCOME: SUCCESS/ABORTED)
└── .work/                                   # internal temporary working area (removed at the end)
```

> You don't need your own SFDX project: the tool always creates a minimal one
> (`sfdx-project.json` only) under `.work/project/`, uses it for the deploy
> commands, and deletes it when finished.

## Repository structure

The tool is a small Python package split into thematic modules:

```
prune_profile/
├── __init__.py     public package surface (re-exports)
├── __main__.py     lets you run `python -m prune_profile`
├── constants.py    identity/version, namespace and the error-type mapping
├── xmlio.py        profile XML + package/manifest file I/O
├── sfcli.py        Salesforce CLI integration + sf/Node discovery
├── board.py        console logging and the live progress board
├── state.py        thread-safe shared pruning state
├── engine.py       parallel per-section pruning workers
└── cli.py          command-line entry point (`main()`)
```

## Notes

- The org is **never** modified unless you pass `--deploy-on-success`.
- `--quiet` still writes `report.txt`, `missing_log.txt` and `run.log`.
- The manifest API version is detected automatically from the target org
  (`sf org display`, latest supported) unless you pass `--api-version`
  explicitly.
- The Metadata API pre-pass needs the connecting user to be able to **see** the
  metadata it lists (System Administrator recommended). Custom fields and
  custom objects are intentionally *not* enumerated (listMetadata does not
  return them), so those are still discovered and pruned by the authoritative
  dry-run. Standard tabs, objects and fields are covered by the Tooling API
  pre-pass.

## Author

Made with <3 by Lorenzo D'Aleo — turbolino.dev@proton.me

This software is unofficial and is written under the AGPL-3.0 license; use at your own risk.

(ɔ) All Right Reversed!

## License

GNU Affero General Public License v3.0 (AGPL-3.0) — see [LICENSE](LICENSE).
