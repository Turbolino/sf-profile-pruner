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

You have three ways to feed it a profile:

* **via 1 — from a file** (`-pp/--profile-path` + `-do/--dest-org`): prune a
  `.profile-meta.xml` you already have.
* **via 2 — from an org** (`-so/--src-org` + `-sp/--src-profile` + `-do` +
  `-dp`): retrieve the profile from the source org, prune it against the
  destination org, then dry-run or deploy.
* **via 3 — retrieve only** (`-so/--src-org` + `-sp/--src-profile`): just
  download the profile from the source org — no prune, no dry-run, no deploy.
  You can then feed the retrieved file to via 1, or run via 2 with
  `-so == -do` and `-sp == -dp` to prune it.

**Alignment** (`--align-from-source`): upgrades via 1 or via 2 into a profile
alignment. It prunes the source profile against the destination, then
retrieves and prunes the *destination* profile too, and turns **OFF** every
permission that exists only in the destination (hidden tab/app, disabled
field/object/class/page/flow) so the destination matches the source. This lets
you align, for example, a UAT profile to a test profile. It **runs
automatically (no Y/N prompt)**: the pre-align destination profile is always
saved in the run folder (`2-destination-retrieved/`), so the operation is
reversible.

The alignment merge is **source-authoritative**: every source entry is kept
verbatim, and every destination-only entry is turned OFF with an explicit off
value (a Profile deploy is a per-element merge, so omitting an entry is a
silent no-op). Sections with no expressible off value (`layoutAssignments`,
`loginFlows`, `loginIpRanges`, `loginHours`) cannot be turned off by a deploy,
so their destination-only entries are listed in `MANUAL-OPERATIONS.txt` for a
manual Setup action. The merged profile is re-validated with a dry-run before
any deploy — the deploy is attempted only when that dry-run is clean.

> The third-party `sf-profile-full` plugin is **no longer required**. To use
> via 2 you need the Salesforce CLI installed **with Node.js and its global
> node_modules** (`@salesforce/core` + `@jsforce/jsforce-node`), which any recent
> `sf` installation already ships.

## When to align vs prune (environment chains)

`--align-from-source` and the plain prune solve two **different** problems. Pick
the one that matches your goal, not the one that happens to look similar.

### Plain prune — "make this profile deployable in the target org"

Without `--align-from-source`, the tool only removes references to metadata
that **does not exist** in the destination org and keeps every surviving entry
with the **source's values**. It never downloads the destination profile and
never turns anything off. It answers one question: *will this profile deploy
in that org, and what must I drop for it to deploy?*

### `--align-from-source` — "make org B match org A"

With alignment, the tool also retrieves the destination profile and turns
**OFF** every permission that exists only in the destination, so the
destination becomes a copy of the source (source-authoritative). Use it when
the goal is to make two orgs **converge to the same profile**, not just to make
one file deployable. Typical case: force UAT to mirror the test org.

### Environment chains (dev → uat → prod)

In a promotion chain you almost always want the **plain prune** — not
alignment — and you prune against the **smallest environment** (typically
prod):

```bash
sfpp -so dev -sp "My Profile" -do prod -dp "My Profile" -od ./out
```

Why prod and not uat? The pruned result still references every component the
target org has, and it only deploys into a *next* environment when that
environment contains at least as much metadata as the target you pruned
against. Pruning against the smallest org (prod) therefore yields a profile
that deploys everywhere along the chain: dev and uat are **supersets** of prod,
so anything prod has, they have too.

If you prune against uat instead, you keep everything uat has — including
components that have not yet reached prod — so the result is **not** guaranteed
to deploy into prod. Metadata in a chain flows in one direction (dev is the
biggest, prod the smallest); prune against the one that is contained in all the
others.

The trade-off: permissions on dev-only components that do not exist in prod are
dropped from the profile. That is exactly what "deployable everywhere" costs.

Use `--align-from-source` in the same chain only when you actually want to
*rewrite* one environment's profile to equal another's, not to promote a
profile through the chain.

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

# via 1 — same, but skip the Y/N confirmation
sfpp -pp "path/to/My Profile.profile-meta.xml" -do prod -dp "My Profile Clean" --deploy-on-success --sure

# via 2 — retrieve from a source org, prune, deploy to the destination org
sfpp -so devorg -sp "My Profile" -do mysandbox -dp "My Profile" --deploy-on-success

# via 3 — retrieve only (no prune): download the profile from a source org
sfpp -so devorg -sp "My Profile" -od ./profiles
```

Or, without the `sfpp` launcher, replace `sfpp` with `python -m prune_profile`.

## Behaviour

- **Dry-run by default**: the org is never modified.
- **Read-only field normalization (default ON)**: system read-only fields
  (formula, roll-up summary, auto number, compound address/location, and fields
  the org keeps read-only) get `editable=false` in both the align and the plain
  prune output, so the deliverable profile reflects the effective permission
  instead of a dead `true`. Pass `--avoid-normalize-ro` to keep the flags
  exactly as retrieved.
- **Non-blocking admin check**: at startup the tool checks the profile of the
  connecting user in the involved org(s); if it is not "System Administrator" it
  prints a non-blocking warning (results can depend on the current profile's
  permissions). It never stops the run.
- **`--deploy-on-success`**: after the profile is clean, deploy it to the org.
- **Live progress board**: during pruning, the console shows a live board with
  **two worker cells per line**, each with columns `[Tx] status | tag |
  operation | [deleted/initial]`, plus a summary line with the elapsed time
  (hh:mm:ss) and the tag counters (done / running / todo). The
  `[deleted/initial]` column auto-widens when the counters grow (e.g. with
  `-bs 1000`/`10000`) so the two columns stay aligned. The elapsed clock ticks
  every second even while the workers are just polling. The full timestamped
  log still goes to `run.log`, and a final summary is printed at the end.
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
- **Alignment report (`--align-from-source`)**: the merged profile lives in
  `4-merged/` together with `MANUAL-OPERATIONS.txt` (sections with no
  deploy-able "off" value) and `what-is-changed.txt` (the full diff the deploy
  will apply, split into `GRANTED` / `REVOKED` / `MANUAL` sections, each
  changed field annotated with `old value from <org>: <value>`).
- **Merged-profile validation**: before any deploy, the merged profile is
  dry-run against the destination; the deploy only runs when that dry-run is
  clean, otherwise the run ends with `VALIDATION_FAILED`.
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
* **via 3 (retrieve only):** `-so/--src-org` + `-sp/--src-profile`

| Option | Description |
| --- | --- |
| `-pp`, `--profile-path` | Path of the source `.profile-meta.xml` file (via 1). |
| `-so`, `--src-org` | Source org alias/username to retrieve from (via 2). |
| `-sp`, `--src-profile` | Name of the Profile to retrieve from `--src-org` (via 2). |
| `-fp`, `--full-profile` | Retrieve the full (non-stripped) profile; default is a clean retrieve (`loginIpRanges`/`loginHours`/`userLicense` removed). |
| `-do`, `--dest-org` | Destination org alias/username (dry-run and optional deploy target). Optional: omit it (with `-so` + `-sp`) for retrieve only. |
| `-dp`, `--dest-profile` | Name to deploy the cleaned profile under; created if it does not exist. Default: the source profile name. |
| `-od`, `--out-dir` | Output directory for report/packages/logs. Default: the current directory. |
| `-bs`, `--batch-size` | Split a section into chunks of `N` entries (min 100, max 10000, default 200); `0` disables splitting. |
| `-tn`, `--thread-number` | Max parallel workers, `1`–`64`. Default: `16`. |
| `--job-timeout` | Minutes to wait for a deploy job before aborting (`1`–`60`; `0` = no timeout). Default: `0`. |
| `-av`, `--api-version` | Manifest API version. Default: detected from the destination org. |
| `--deploy-throttle-ms` | Min interval between deploy starts, in ms. Default: `500`; `0` disables. |
| `--poll-interval` | Seconds between status polls of a deploy job. Default: `3`. |
| `--max-iter` | Iteration cap. Default: `5000`. |
| `--sf` | Path to the Salesforce CLI. Default: `sf`. |
| `--deploy-on-success` | Deploy the cleaned profile to `--dest-org` once pruning succeeds. Modifies the org: asks for Y/N confirmation (No = dry-run only). Use as `[--deploy-on-success [--sure]]`. |
| `--sure` | Skip the Y/N confirmation of `--deploy-on-success` and deploy without asking. No effect without `--deploy-on-success`. |
| `--align-from-source` | Align the destination profile to the source: turn OFF destination-only permissions. Works with via 1 or via 2. Runs automatically (no prompt); the pre-align destination profile is saved in the run folder. |
| `--diff` | Compare the source profile against the destination profile and write a pure profile-to-profile diff report (`diff/diff-report.txt`). Read-only: no prune, no dry-run, no deploy. Works with via 1 or via 2. |
| `--avoid-normalize-ro` | Skip the read-only field normalization. By default the tool forces `editable=false` on system read-only fields (formula, roll-up summary, auto number, compound address/location, and fields the org keeps read-only) in both the align and the plain prune output; with this flag the flags are left exactly as retrieved. |
| `--fresh` | Restart from the original, ignoring any previous state. |
| `--quiet` | Suppress the progressive stdout output. |
| `--tell-me-a-story` | Turbolino will tell you a fascinating story about code, algorithms and copyright. |
| `-V`, `--version` | Show version and author. |

## Output layout (`--out-dir`)

A plain prune (no `--align-from-source`) now uses the same numbered layout as
the alignment flow, so the two modes are easy to compare:

```
out-dir/
├── ORIGINAL_<name>.profile-meta.xml   # untouched copy of the original
├── 1-source-pruned/
│   ├── COMMON/
│   │   ├── <name>.profile-meta.xml    # clean, deployable profile
│   │   └── toDeploy.xml               # manifest of the deployable (COMMON) members
│   ├── MISSING/
│   │   └── package.xml                # everything removed / missing
│   ├── missing_log.txt                # resumable line-by-line log
│   └── report.txt                     # final summary (OUTCOME: SUCCESS/ABORTED)
├── 4-merged/
│   ├── <name>.profile-meta.xml        # the clean, deployable profile (copy)
│   ├── what-is-changed.txt            # removed dangling refs + manual operations
│   └── MANUAL-OPERATIONS.txt          # sections with no deploy-able "off" value
└── run.log                            # progressive log copy
```

> You don't need your own SFDX project: the tool always creates a minimal one
> (`sfdx-project.json` only) under `1-source-pruned/.work/project/`, uses it for
> the deploy commands, and deletes it when finished.

### Alignment output (`--align-from-source`)

When aligning, `--out-dir` is a run folder (one per invocation):

```
run/
├── 1-source-pruned/               # source pruned against the destination org
├── 2-destination-retrieved/       # destination profile retrieved from the org
├── 3-destination-pruned/          # destination pruned against itself
├── 4-merged/
│   ├── <name>.profile-meta.xml    # source-authoritative merged profile
│   ├── MANUAL-OPERATIONS.txt      # sections with no deploy-able "off" value
│   └── what-is-changed.txt        # the full diff the deploy will apply
└── run.log                        # full log
```

`layoutAssignments` manual operations are auto-classified in both reports:
**`REDUNDANT (ignorable)`** when the object has a single page layout (the
assignment is necessarily the default), or **`NEEDS CHECK`** when the object
has several layouts. This keeps you from chasing assignments that would have
no effect.

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
├── retrieve.py     native full-profile retrieve (readMetadata)
├── stage.py        prune-stage orchestration (workers + prefetch + board)
├── merge.py        alignment merge (source-authoritative) + reports
├── cli.py          command-line entry point (`main()`)
└── node/retrieve_profile.cjs  Node helper for readMetadata retrieval
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
