# Profile Pruner

**Ship Salesforce Profiles across orgs — without the deploy failures.**

[![PyPI version](https://img.shields.io/pypi/v/sf-profile-pruner.svg)](https://pypi.org/project/sf-profile-pruner/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)

Profile Pruner removes every reference in a Salesforce **Profile** to metadata
that does **not** exist in the target org, so the profile deploys cleanly on
the first try. Dry-run by default: the org is never touched unless you ask for
it.

---

## 1. What it is — read this first

**The problem.** A Profile exported from one org references objects, fields,
tabs, classes, flows, layouts, record types, login flows, … that simply don't
exist in another org. Deploying that profile fails.

**The fix.** Profile Pruner finds each of those dangling references and removes
it, then hands you two things:

- a **clean, deploy-ready Profile** (the `COMMON` package), and
- a **manifest of everything it removed** (the `MISSING` package) — so you can
  see exactly what was dropped and why.

**One command:**

```bash
pip install sf-profile-pruner
sfpp -so dev -sp "Sales Rep" -do prod -dp "Sales Rep" -od ./out
```

That retrieves `Sales Rep` from `dev`, prunes it against `prod`, and writes the
result to `./out`. Nothing is deployed — it's a dry run.

**When you're ready to deploy**, add `--deploy-on-success` (and `--sure` to
skip the Y/N confirmation):

```bash
sfpp -so dev -sp "Sales Rep" -do prod -dp "Sales Rep" --deploy-on-success --sure
```

**Two more things it does, in one flag each:**

- **Compare two profiles** (`--diff`) — a pure, read-only comparison that
  tells you what is only in one profile, only in the other, identical, and what
  differs field-by-field. It changes nothing and never deploys:

  ```bash
  sfpp -so uat -sp "Sales Rep" -do test -dp "Sales Rep" --diff -od ./out
  ```

- **Align two orgs** (`--align-from-source`) — make the destination profile a
  copy of the source: it turns OFF every permission that exists only in the
  destination, so org B matches org A. The pre-align destination is always
  saved first, so the operation is reversible:

  ```bash
  sfpp -so uat -sp "Sales Rep" -do test -dp "Sales Rep" --align-from-source -od ./out
  ```

  Use `--diff` when you only want to **see** the differences; use
  `--align-from-source` when you want to **apply** them.

**Who it's for.** Anyone promoting Profiles across an environment chain
(dev → uat → prod), moving a Profile between orgs that don't carry the same
metadata, or cleaning a Profile exported from a managed-package org before
deploying it somewhere else.

---

## 2. The full picture — deep dive

### Why deploys fail (and why this exists)

A profile exported from a source org (or a managed-package org) often
references objects, fields, tabs, classes, flows, login flows, layouts, record
types, agents, enhanced summaries, etc. that do not exist in the target org.
Deploying such a profile fails. Profile Pruner finds those references one by
one and strips them.

### Three ways to feed it a profile

* **via 1 — from a file** (`-pp/--profile-path` + `-do/--dest-org`): prune a
  `.profile-meta.xml` you already have.
* **via 2 — from an org** (`-so/--src-org` + `-sp/--src-profile` + `-do` +
  `-dp`): retrieve the profile from the source org, prune it against the
  destination org, then dry-run or deploy.
* **via 3 — retrieve only** (`-so/--src-org` + `-sp/--src-profile`): just
  download the profile from the source org — no prune, no dry-run, no deploy.
  You can then feed the retrieved file to via 1, or run via 2 with
  `-so == -do` and `-sp == -dp` to prune it.

### Plain prune vs align vs environment chains

`--align-from-source` and the plain prune solve two **different** problems.
Pick the one that matches your goal, not the one that happens to look similar.

**Plain prune — "make this profile deployable in the target org".** Without
`--align-from-source`, the tool only removes references to metadata that
**does not exist** in the destination org and keeps every surviving entry with
the **source's values**. It never downloads the destination profile and never
turns anything off. It answers one question: *will this profile deploy in that
org, and what must I drop for it to deploy?*

**`--align-from-source` — "make org B match org A".** With alignment, the tool
also retrieves the destination profile and turns **OFF** every permission that
exists only in the destination, so the destination becomes a copy of the source
(source-authoritative). Use it when the goal is to make two orgs **converge to
the same profile**, not just to make one file deployable. Typical case: force
UAT to mirror the test org.

**Environment chains (dev → uat → prod).** In a promotion chain you almost
always want the **plain prune** — not alignment — and you prune against the
**smallest environment** (typically prod):

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
others. The trade-off: permissions on dev-only components that do not exist in
prod are dropped from the profile. That is exactly what "deployable everywhere"
costs.

### Read-only field normalization (`--avoid-normalize-ro`)

Salesforce has two separate layers for a field's editability, combined with a
logical AND:

1. **The field's nature** — some fields are read-only by the platform itself,
   no matter what the profile says.
2. **Field-level security (FLS)** — the `editable`/`readable` flags you set in
   the profile.

For a field whose *nature* is read-only, the profile flag is **dead metadata**:
you can write `editable=true`, but the org keeps the field non-editable anyway.
That `true` then surfaces as a phantom difference in diffs/alignments and never
converges, because a deploy cannot make the field editable.

**Which fields are affected**

- **Formula** (`Formula (Text/Number/…)`)
- **Roll-Up Summary** and **Auto Number**
- **Compound Address / Location** — the compound parent (`Account.BillingAddress`,
  `Contact.MailingAddress`, …) is read-only; its components (`BillingStreet`,
  `BillingCity`, …) are the real editable fields.
- **System read-only fields** whose `DataType` looks normal, e.g.
  `LiveChatTranscript.ChatKey` (`Text(200)`) and `ServiceAppointment.AccountId`
  (`Lookup(Account)`) — detected by `sobject describe` on both orgs (only during
  alignment, where both sides are available).

**Default behaviour (normalization ON).** By default the tool rewrites those
fields to `editable=false` in **both** modes:

- **Plain prune** (`-so … -do …`): the clean profile is rewritten in place, so
  the deliverable matches what the org actually enforces.
- **`--align-from-source`**: normalization is applied during the merge, so no
  phantom grant/revoke is reported for read-only fields.

| Mode | Normalization | Result |
| --- | --- | --- |
| `--align-from-source` | ON (default) | read-only `editable` → `false` in the merged profile |
| `--align-from-source --avoid-normalize-ro` | OFF | `editable` kept exactly as retrieved |
| plain prune | ON (default) | read-only `editable` → `false` in the clean profile |
| plain prune `--avoid-normalize-ro` | OFF | `editable` kept exactly as retrieved |

Pass `--avoid-normalize-ro` only when you want to inspect the **raw** profile
values as they come from the org.

### How it works

1. The profile is split by its top-level sections (`classAccesses`,
   `fieldPermissions`, `tabVisibilities`, `layoutAssignments`, …) — the sections
   are discovered dynamically from the XML, not hardcoded.
2. **Metadata API pre-pass:** before anything is deployed, the tool enumerates
   the metadata that actually exists in the target org with
   `sf org list metadata` (one call per type) and removes every reference that
   has no matching component — login flows, agent access, enhanced summaries,
   flows, layouts, record types, and every other listable type — in one shot.
   This is much faster than discovering each missing reference through dry-runs
   and covers the newest Profile sections automatically. The per-section dry-run
   below remains the authoritative fallback.
3. **Standard metadata pre-pass:** the tool also queries the org's Tooling API
   (`TabDefinition`, `EntityDefinition`, `FieldDefinition`) and removes standard
   tabs, objects and fields the profile references but the org does not have —
   the class of error a dry-run never surfaces (e.g. `You can't edit tab
   settings for X, as it's not a valid tab`).
4. Up to `--thread-number` workers run in parallel (capped to the number of
   sections); each worker takes one section and, in a loop: builds a sub-package
   with only that section, starts an async dry-run
   (`sf project deploy start --dry-run --async`) and polls
   `sf project deploy report` until terminal, then removes every offending entry
   and writes the cleaned/removed entries back in their original order.
5. The worker repeats until its section dry-runs clean, then takes the next
   section from the queue.
6. `COMMON/` and `MISSING/` are assembled from the surviving entries, keeping
   the original document order.
7. As a final safety check, the tool runs one last **dry-run of the whole
   assembled package**; only if it succeeds the outcome is `SUCCESS`. This final
   gate is **self-healing**: if the full package surfaces a reference the
   per-section pass missed, it removes it and re-validates until clean.

### Requirements

- Python 3.9+
- `lxml` (installed automatically)
- Salesforce CLI (`sf`) — auto-discovered, or pass `--sf <path>`
- Node.js >= 18.11 — auto-detected, so the CLI works even when an old Node is
  the system default
- An authenticated org (alias or username) the CLI can reach

### Installation

**From PyPI (recommended):**

```bash
pip install sf-profile-pruner
sfpp --help
```

> The `sfpp` command is a real console-script executable (not a `.cmd` batch
> file), so Ctrl-C goes straight to the program. If `sfpp` is not recognized
> after install, your Python `Scripts` folder is not on `PATH` — see
> [INSTALLATION.md](INSTALLATION.md), or use `python -m prune_profile` as a
> PATH-independent fallback.

**From the repository:**

```bash
python first_config.py   # installs the package + dependency, checks Node/sf
sfpp --help              # in a NEW terminal
```

### All options

Options are grouped in the `-h` help.

| Option | Description |
| --- | --- |
| `-pp`, `--profile-path` | Path of the source `.profile-meta.xml` file (via 1). |
| `-so`, `--src-org` | Source org alias/username to retrieve from (via 2). |
| `-sp`, `--src-profile` | Name of the Profile to retrieve from `--src-org` (via 2). |
| `-fp`, `--full-profile` | Retrieve the full (non-stripped) profile; default removes `loginIpRanges`/`loginHours`/`userLicense`. |
| `-do`, `--dest-org` | Destination org alias/username (dry-run and optional deploy target). Omit it (with `-so` + `-sp`) for retrieve only. |
| `-dp`, `--dest-profile` | Name to deploy the cleaned profile under; created if it does not exist. Default: source name. |
| `-od`, `--out-dir` | Output directory for report/packages/logs. Default: current directory. |
| `-bs`, `--batch-size` | Split a section into chunks of `N` entries (min 100, max 10000, default 200); `0` disables. |
| `-tn`, `--thread-number` | Max parallel workers, `1`–`64`. Default `16`. |
| `--job-timeout` | Minutes to wait for a deploy job before aborting (`1`–`60`; `0` = no timeout). Default `0`. |
| `-av`, `--api-version` | Manifest API version. Default: detected from the destination org. |
| `--deploy-throttle-ms` | Min interval between deploy starts (ms). Default `500`; `0` disables. |
| `--poll-interval` | Seconds between status polls of a deploy job. Default `3`. |
| `--max-iter` | Iteration cap. Default `5000`. |
| `--sf` | Path to the Salesforce CLI. Default `sf`. |
| `--deploy-on-success` | Deploy the cleaned profile once pruning succeeds. Modifies the org: asks Y/N (No = dry-run only). Use as `[--deploy-on-success [--sure]]`. |
| `--sure` | Skip the Y/N confirmation of `--deploy-on-success`. No effect without it. |
| `--align-from-source` | Align the destination profile to the source: turn OFF destination-only permissions. Runs automatically (no prompt); pre-align destination saved in the run folder. |
| `--diff` | Compare source vs destination profile and write `diff/diff-report.txt` (+ JSON/XML data). Read-only: no prune, no dry-run, no deploy. |
| `--avoid-normalize-ro` | Skip the [read-only normalization](#read-only-field-normalization-avoid-normalize-ro). |
| `--fresh` | Restart from the original, ignoring any previous state. |
| `--quiet` | Suppress the progressive stdout output. |
| `--tell-me-a-story` | Turbolino will tell you a fascinating story about code, algorithms and copyright. |
| `-V`, `--version` | Show version and author. |

### Output layout (`--out-dir`)

A plain prune (no `--align-from-source`) uses the same numbered layout as the
alignment flow:

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

> You don't need your own SFDX project: the tool creates a minimal one
> (`sfdx-project.json` only) under `1-source-pruned/.work/project/`, uses it for
> the deploy commands, and deletes it when finished.

**Alignment output (`--align-from-source`)** — `--out-dir` is a run folder:

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

`layoutAssignments` manual operations are auto-classified: **REDUNDANT
(ignorable)** when the object has a single page layout, or **NEEDS CHECK** when
it has several — so you don't chase assignments that would have no effect.

### Behaviour details

- **Dry-run by default**: the org is never modified.
- **Non-blocking admin check**: at startup the tool checks the connecting
  user's profile in the involved org(s); if it is not "System Administrator" it
  prints a non-blocking warning (results can depend on the current profile's
  permissions).
- **Live progress board**: during pruning, the console shows a live board with
  two worker cells per line (`[Tx] status | tag | operation | [deleted/initial]`)
  plus a summary line with elapsed time (hh:mm:ss) and tag counters. The full
  timestamped log goes to `run.log`.
- **Ctrl-C stops cleanly**: prints `Saving pending work...`, keeps removals
  applied so far, marks active threads "stopped by user", reports `ABORTED`.
- **Batch removal**: every component failure reported by a single deploy is
  resolved in one pass.
- **Alignment report**: the merged profile lives in `4-merged/` with
  `MANUAL-OPERATIONS.txt` and `what-is-changed.txt` (diff split into
  `GRANTED` / `REVOKED` / `MANUAL`, each changed field annotated with
  `old value from <org>: <value>`).
- **Merged-profile validation**: before any deploy, the merged profile is
  dry-run against the destination; deploy runs only when that dry-run is clean.
- **Resumable**: if it stops (network hiccup, error), re-run the same command
  and it continues from the last error.
- **Already clean**: a previous `SUCCESS` on the same output dir exits
  immediately with "profile already clean".
- **`--fresh`**: restart from the original, ignoring any previous state.

### Repository structure

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

### Notes

- The org is **never** modified unless you pass `--deploy-on-success`.
- `--quiet` still writes `report.txt`, `missing_log.txt` and `run.log`.
- The manifest API version is detected automatically from the target org unless
  you pass `--api-version`.
- The Metadata API pre-pass needs the connecting user to be able to **see** the
  metadata it lists (System Administrator recommended). Custom fields/objects
  are intentionally not enumerated (listMetadata does not return them), so
  those are still discovered by the authoritative dry-run. Standard
  tabs/objects/fields are covered by the Tooling API pre-pass.

### Author

Made with <3 by Lorenzo D'Aleo — turbolino.dev@proton.me

This software is unofficial and is written under the AGPL-3.0 license; use at
your own risk.

(ɔ) All Right Reversed!

### License

GNU Affero General Public License v3.0 (AGPL-3.0) — see [LICENSE](LICENSE).
