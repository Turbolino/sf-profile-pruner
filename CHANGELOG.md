# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.22.1] - 2026-11-24

### Fixed

- The pre-fetch no longer removes VALID metadata on false positives:
  - layout references: managed-package layouts are stored with the namespace
    (`ns__Obj__c-ns__Layout`) but listed without it, and `__hd`/analytics
    layouts may be missing from `list metadata Layout` entirely - names are now
    normalized before comparing, and an unmatched layout whose OBJECT exists
    (EntityDefinition) is left to the authoritative dry-run;
  - custom metadata types: existence is now checked via EntityDefinition
    (`X__mdt`), not via `list metadata CustomMetadata` (which does not
    enumerate them);
  - standard fields: FieldDefinition-based pruning is REMOVED entirely - it
    does not enumerate feature-gated fields (Milestones, Live Chat,
    Entitlements, Analytics, ...) that the Profile schema still accepts, and it
    silently stripped ~22 valid fieldPermissions from a real Profile; standard
    fields are now left to the authoritative dry-run, exactly like standard
    objects.
- The admin check no longer fails with a misleading "no user record found": it
  now queries the connecting user via STANDARD SOQL (the Tooling API does not
  support the `Profile` relation on `User`, which made the query error out), and
  matches the profile against a list of standard/localized System Administrator
  names (plus conventional custom admin names like "Amministratore LI") instead
  of the exact English name only.
- Every tool-produced file now carries a DOUBLE-quoted XML declaration even when
  the input (`-pp`) source file had a single-quoted one: verbatim copies of
  input files (`ORIGINAL_*`, work copies, the final 4-merged deliverable) are
  normalized in place.

### Changed

- Plain prune on a Profile with `--ignore` now STATES the destination's values
  for the ignored entries in the final deliverable (explicit and auditable)
  instead of silently omitting them: the destination Profile is retrieved and
  pruned against itself, the deliverable is patched (entry replaced in place
  when shared, appended when destination-only), the patched file is dry-run
  validated, and the WIC reports them under PRESERVED.
- Same-type plain prune (Profile output) now dry-run validates the FINAL
  deliverable AFTER the read-only field normalization: previously the
  authoritative gate ran before the `editable=false` rewrites, so the file that
  ships was never re-validated after being modified.
- The manual-operations table in the reports no longer renders the WHERE column
  and is ordered by verdict (REDUNDANT first, then NEEDS CHECK, then
  unclassified).

## [2.22.0] - 2026-11-24

### Added

- `--ignore path/to/package.xml`: exclude metadata from the operation. The
  package lists metadata type + members (e.g. `<name>ApexClass</name>` +
  `<members>Foo</members>`; `*` ignores a whole type). Supported on:
  - `--diff` — ignored entries are excluded from both sides and never reported
    as a difference;
  - `--align-from-source` — ignored entries are removed from the source AND
    kept verbatim in the destination (the source's value is never propagated
    and the destination value is never turned off);
  - plain prune — ignored entries are removed from the source, so their source
    value is not propagated; on a PermissionSet output they become
    destination-only and are preserved verbatim (see 2.21.4).

## [2.21.4] - 2026-11-24

### Fixed

- Plain prune on a PermissionSet no longer revokes destination-only permissions.
  A PermissionSet deploy is authoritative (anything absent from the file is
  revoked), so the old behavior — shipping only the pruned source — silently
  revoked every permission the destination held but the source did not declare.
  The plain prune now retrieves the destination, prunes it against itself, and
  merges it UNDER the source with `preserve_dest_only=True`: the final
  PermissionSet is a union that keeps every destination-only entry verbatim.
  A Profile output is unchanged (a deploy merge already leaves omitted entries
  untouched).
- `what-is-changed.txt` now reports a `PRESERVED (destination-only entries kept
  verbatim, NOT revoked)` section for a PermissionSet plain prune.

## [2.21.3] - 2026-11-24

### Fixed

- Cross-type plain prune PS -> Profile with a NON-existent destination Profile no
  longer fails with "No default record type specified": when there is no
  destination Profile to inherit the defaults from, the conversion now
  synthesizes a deterministic default (first record type per object and the
  first application, in source order) so the deliverable is a valid, deployable
  Profile.

## [2.21.2] - 2026-11-23

### Fixed

- The plain-prune `what-is-changed.txt` now reports the read-only field
  normalization: a new `NORMALIZED (read-only fields rewritten to
  editable=false)` section lists every fieldPermission whose `editable` value was
  actually changed (`true -> false`). Previously only the removed dangling
  references were shown, so the report did not reflect the changes the prune
  applied to the deliverable.
- All generated metadata XML now uses a DOUBLE-quoted XML declaration
  (`<?xml version="1.0" encoding="UTF-8"?>`) instead of lxml's single-quoted
  default (`<?xml version='1.0' encoding='UTF-8'?>`), matching Salesforce's
  convention.

## [2.21.1] - 2026-11-23

### Fixed

- Plain prune now classifies `layoutAssignments` manual operations into
  `REDUNDANT (ignorable)` vs `NEEDS CHECK` (using the destination org's Layout
  count), the same way the alignment flow does. Previously a plain prune left
  them unclassified under a single flat "OTHER MANUAL OPERATIONS" group, so it
  was impossible to tell which ones could be ignored.

### Changed

- The SETUP phase now logs its silent steps: the PyPI update check and the
  per-org admin check each print an in-progress line and an `OK:`/`WARNING:`
  result, so the pause between "Salesforce CLI ready" and "detecting the latest
  API version" is no longer a mystery.

## [2.21.0] - 2026-11-23

### Added

- `--new-ps-name NAME` for a PermissionSet -> PermissionSet align whose licenses
  differ: instead of overwriting the existing destination (whose license is
  immutable), create a NEW PermissionSet under `NAME`. The name must be a valid
  Salesforce API name (no file extension, e.g. `MyPS` not
  `MyPS.permissionset-meta.xml`) and must NOT already exist in the destination
  org (both checked, blocking on failure).
- When `--force-license-mismatch` detects differing licenses, it now emits a
  NON-blocking warning explaining that the license cannot be changed and that a
  new PermissionSet is required (`--new-ps-name "NEW_NAME"`); without the new
  name the run proceeds to a dry-run that fails with "License can't be updated".

### Fixed

- Creating a NEW PermissionSet (destination missing, or `--new-ps-name`) no
  longer copies the source's `label`: the fresh set keeps its own unique label
  (derived from the new API name), fixing "The label you entered is in use" when
  the source's label already exists in the destination org.

## [2.20.0] - 2026-11-23

### Added

- License guard for PermissionSet -> PermissionSet alignment: a PermissionSet's
  `license` binds which `userPermissions` are valid or mandatory for it AND is
  immutable once the set exists (Salesforce rejects a deploy that changes it
  with "License can't be updated"). A source-authoritative align would therefore
  silently try to overwrite the destination's license and then fail the deploy.
  When the source and destination declare DIFFERENT non-empty licenses, the
  alignment is now BLOCKED by default with a clear error (exit code 2); re-run
  with `--force-license-mismatch` to generate the merged output anyway (for
  inspection — the deploy itself will still be refused by the platform). An
  absent source license (a PermissionSet file without the element) never
  triggers the guard (the destination keeps its own license), and a missing
  destination is also exempt (the set is created fresh).

## [2.19.0] - 2026-11-20

### Added

- PermissionSet support, equivalent to Profile, plus cross-type comparison:
  - `-sps` / `--src-permissionset` and `-dps` / `--dest-permissionset` name a
    PermissionSet for the source / destination (mutually exclusive with
    `-sp` / `-dp`).
  - The retrieve helper (`readMetadata`), the prune engine, the deploy/validate
    steps and the merged output all honor the metadata type (folder
    `permissionsets/`, filename `.permissionset-meta.xml`, manifest type
    `PermissionSet`, XML root `<PermissionSet>`).
  - Cross-type merge/diff: a Profile source against a PermissionSet destination
    (or vice versa) is supported. The tab section — `tabVisibilities` in a
    Profile, `tabSettings` in a PermissionSet — is mapped across the two
    vocabularies (DefaultOn/DefaultOff/Hidden ↔ Visible/Available/None), and the
    merged file always uses the destination's schema. All other permission
    sections are structurally identical and merge/diff directly.
  - Tab visibility mapping is level-based and reversible (three semantic
    states, never collapsed to a binary on/off): `DefaultOn↔Visible`,
    `DefaultOff↔Available` ("off but available"), `Hidden↔None`. A source-only
    `DefaultOff` is carried into the PermissionSet as `Available`, and the
    cross-type diff keeps `Available` distinct from `None`.
  - Mixed-type runs print an explicit warning: the comparison/merge maps tab
    visibility between the two vocabularies, and Profile-only sections
    (`layoutAssignments`, `loginFlows`, `loginIpRanges`, `loginHours`,
    `userLicense`) are not carried across the type boundary (a Profile source
    cannot move them into a PermissionSet; a PermissionSet source leaves the
    destination Profile's own values there).
  - PermissionSet deploy semantics are surfaced explicitly: unlike a Profile
    (per-element MERGE, omission = no-op), a PermissionSet deploy is
    AUTHORITATIVE — every permission not present in the deployed file is
    REVOKED in the target org. The warning appears for every mode that produces
    a PermissionSet (plain prune and align; `--diff` is read-only and excluded).
    The cross-type merge filters Profile-only sections out of a PermissionSet
    output (they have no PS schema), so they are never injected into an invalid
    PermissionSet file.
  - Cross-type plain prune (`puro`) now CONVERTS the source content into the
    destination type (no revoke): a Profile source becomes a PermissionSet
    (tabs remapped, Profile-only sections dropped, a `label` synthesised); a
    PermissionSet source becomes a Profile (tabs remapped, and the destination
    Profile's own Profile-only sections — `layoutAssignments`, `loginFlows`,
    `loginIpRanges`, `loginHours`, `userLicense`, `custom` — are retrieved and
    kept verbatim so they remain present and unchanged).
  - Cross-type align PS -> Profile keeps the destination Profile's exclusive
    sections present and unchanged (they are out of scope for a PermissionSet
    source): they are no longer turned off or reported as manual removals,
    while destination-only entries in shared sections are still turned OFF.
  - Cross-type structural mapping: Profile `applicationVisibilities` drops its
    `default` child when the output is a PermissionSet (not valid in the PS
    schema); PermissionSet-only scalars (`label`, `hasActivationRequired`,
    `license`) are never copied into a Profile.
  - `depends on permission(s):` failures are now recognised as a chunking
    artifact (the prerequisite lives in another chunk), not a dangling
    reference: they are skipped, and a section whose failures are all of this
    kind is treated as clean.
  - Cross-type align PS -> Profile preserves the destination's `default` on
    `applicationVisibilities` / `recordTypeVisibilities` (a PS has no default
    concept); a destination entry marked default is kept instead of turned off.
  - userPermissions are preserved verbatim (license-bound) in cross-type
    PS -> Profile align instead of being turned off. License-mandated grants
    and their dependent objectPermissions are kept: the self-heal now resolves
    "Permission X depends on permission(s): Y" by keeping Y ON (removing its
    OFF entry), while still skipping genuine cross-chunk dependency artifacts
    in the source prune.
  - Fixed the dependency parser for multi-permission messages ("Edit Lead,
    Read Lead" now resolves the object "Lead" instead of "Lead,"); fixed the
    self-heal re-merge to keep the dependency-driven objectPermissions ON
    (``keep_object_permissions``) instead of re-turning them off; source-only
    userPermissions are now granted (not silently dropped) in PS -> Profile.
  - Standard-object pruning via EntityDefinition removed: EntityDefinition does
    not enumerate external-data / Data Cloud / AI standard objects
    (ExternalDataConnector, ExternalDataTranObject, ActivationTargetDef, …),
    so "absent from EntityDefinition" wrongly removed ~91 VALID standard
    objects from a PermissionSet and broke the final dry-run. Standard-object
    dangling detection now relies on the authoritative per-section dry-run +
    final gate; standard tabs (TabDefinition) and fields (FieldDefinition) are
    unaffected.
  - Cross-type plain prune now validates the CONVERTED deliverable: after a
    Profile<->PermissionSet conversion, the final file is dry-run AS the
    destination type and self-healed (resolve + retry) on error, instead of
    being written to 4-merged unvalidated (which could exit 0 with a
    non-deployable file).
  - PS -> Profile conversion preserves the destination's `default` record type
    and default app (a PermissionSet source has no `default` concept; a Profile
    without a default record type fails the deploy), keeping the destination's
    default-marked entries even when the source does not mention them.
  - File source (`-pp`) now copies the source file to the top-level
    `ORIGINAL_*` path before pruning, fixing a crash in manual-ops detection
    ("failed to load external entity") that occurred for file sources.
  - Plain prune rename guard: same-type `-dp`/`-dps` renaming onto a destination
    that already exists in the target org is now a blocking error (exit 2). A
    Profile rename would MERGE (leaving dest-only permissions) and a
    PermissionSet rename is authoritative (revoking dest-only permissions), so
    neither is a clean rename. Renaming is allowed only onto a name that does
    not yet exist; cross-type conversion and `--align-from-source` are exempt.
  - Missing-component policy: a missing SOURCE is a blocking error (clear
    "source ... not found" message); a missing DESTINATION is a non-blocking
    warning and the run proceeds (plain prune / diff / align treat it as empty
    and create it on deploy; PS->Profile conversion leaves the Profile-only
    sections empty instead of failing).

## [2.18.0] - 2026-11-19

### Added

- `-itn` / `--io-threads-number`: parallel threads for the read-only IO calls
  (list metadata, Tooling queries, sobject describe). Default `8`, `0`/`1` =
  sequential, clamped to a ceiling of `16`. The per-type `sf org list metadata`
  pre-pass, the standard-metadata Tooling batches (EntityDefinition /
  FieldDefinition), the field DataType batches and the sobject-describe
  normalization pass now run in parallel and merge their results in input order
  (deterministic output, no shared mutable state between workers).
- Rate-limit errors are now retried: `_NETWORK_HINTS` recognises `rate limit`,
  `ratelimit`, `REQUEST_LIMIT_EXCEEDED`, `TotalRequests limit`, and `too many
  requests`, without catching the Tooling `EXCEEDED_ID_LIMIT` (still handled by
  the caller's batch-splitting).

## [2.17.0] - 2026-11-19

### Added

- **Talkative resume**: resuming a previously completed alignment now prints a
  per-phase summary read back from the run folder (`PHASE 1/4 PRUNE SOURCE`,
  `PHASE 2/4 PRUNE DESTINATION` with removed counts from their `report.txt`,
  `PHASE 3/4 MERGE` with granted/revoked/layout-changed/manual, and
  `PHASE 4/4 VALIDATE`). The plain prune resume now lists the already-removed
  metadata broken down by type, and the "already clean" short-circuit reports
  the previous run's outcome and removed count.

## [2.16.10] - 2026-11-19

### Fixed

- The read-only describe pass now scopes to STANDARD destination objects
  (`EntityDefinition.IsCustomizable=false`), not merely "existing" objects.
  System read-only fields live on standard objects; custom objects (even ones
  present in both orgs, like `BenefitAssignment`) are never describable for that
  pass and were still producing `NOT_FOUND` warnings. Residual `NOT_FOUND` on a
  standard object is now a silent, counted skip instead of a per-object warning.

## [2.16.9] - 2026-11-19

### Fixed

- Read-only normalization now describes only objects that exist in the
  destination org (verified via EntityDefinition). Previously the plain-prune
  describe pass described every candidate object, so objects present only in
  the source (one-org-only) answered `NOT_FOUND` and produced a wall of
  warnings. Dangling fields on absent objects are pruned anyway, so they are
  skipped up front.

### Added

- A startup note when read-only normalization is ON, explaining that system
  read-only fields (formula, roll-up summary, auto number, compound
  Address/Location, and system read-only fields such as ChatKey/AccountId)
  will be normalized to `editable=false`, and that `--avoid-normalize-ro`
  keeps the raw flags. Shown for prune and align (not `--diff`, which never
  normalizes).

## [2.16.8] - 2026-11-19

### Fixed

- Layout assignments are now keyed by `(object, recordType)` instead of the
  full layout name, so an alignment that changes the layout of an existing
  slot (same object+recordType, different layout) is reported as a
  **layout change applied by the deploy** — the merged profile carries the
  source layout and `what-is-changed.txt` shows it under `LAYOUT CHANGED`,
  annotated with the destination's old layout. Previously a changed layout was
  misclassified as a destination-only *removal* (a manual op the deploy
  actually resolves on its own). A destination-only slot with no source
  counterpart remains a manual operation (a Profile deploy cannot turn an
  assignment off).

## [2.16.7] - 2026-11-19

### Fixed

- `--batch-size` help now reports the true maximum (`max 10000`) instead of the
  stale `max 4000` (the clamp and README already said 10000).
- Manifest classification (`toDeploy.xml` / `MISSING/package.xml`) now uses the
  same `is_standard_api_name` criterion as the standard-metadata pre-pass: any
  API name containing `__` is custom (so big objects `__b`, external objects
  `__x`, platform events `__e` are `CustomObject`), and a standard field on a
  custom object (`MyObj__c.CreatedDate`) stays `StandardField` instead of being
  misclassified as `CustomField` by the old `'__c' in` substring.
- Read-only normalization is now symmetric between plain prune and align: the
  plain prune (source org via `-so`) also runs the `sobject describe` pass for
  system read-only fields whose `DataType` looks normal (e.g.
  `WorkOrder.IsClosed`, `ServiceContract.Status`), not just the DataType pass.
  Candidates are the standard fields the source marks `editable=true`; a field
  is normalized only when read-only on both orgs (FLS-locked-on-one-side fields
  are kept). The describe runs source-first, so only objects that actually show
  a disagreement are re-described on the destination.

## [2.16.6] - 2026-11-19

### Fixed

- Serialized `sf deploy start` submissions across workers with a process-local
  mutex. With high thread counts (`-tn 64`) every worker previously submitted a
  deploy start at once, so N-1 of them tripped the per-org CLI lock and burned
  retries (and the short 5-attempt window was not enough to clear a busy lock).
  Submissions are now one-at-a-time, so the CLI lock is only ever contended by
  an external `sf` process.
- The deploy-lock retry is now more patient: `LOCK_RETRIES` (12) with a slower
  backoff than a network blip, reflecting that the lock is held for seconds.

## [2.16.5] - 2026-11-19

### Fixed

- The plain prune now exits with a non-zero code when the outcome is not
  `SUCCESS` (e.g. `ABORTED` after a deploy-lock hard failure). Previously it
  fell through `_main()` without `sys.exit()`, so a failed run returned exit
  code 0 and was indistinguishable from a success to scripts/CI.

## [2.16.4] - 2026-11-18

### Fixed

- Salesforce CLI per-org deploy lock (`Lock file is already being held`) is now
  detected and retried with backoff, exactly like transient network errors.
  Previously it was not recognized, so a parallel `deploy start` tripped the
  lock and hard-failed the section with `deploy start returned no job id`.
- Jittered the retry backoff: without jitter, every parallel worker that hit
  the same network/lock blip retried in lockstep and kept hammering the org
  together.

## [2.16.3] - 2026-11-18

### Changed

- Restructured the README into two parts: a short, plain-language "What it is"
  (problem, fix, one command, and one-line explanations of `--diff` and
  `--align-from-source`) followed by the full deep-dive documentation.
- Fresher project description (README tagline + PyPI `description`):
  "Ship Salesforce Profiles across orgs — without the deploy failures."

## [2.16.2] - 2026-11-18

### Fixed

- `UnicodeEncodeError` on legacy Windows consoles (cp1252/cp850) when the
  banner / live board printed box-drawing and arrow glyphs. `main()` now
  reconfigures stdout/stderr to UTF-8 with a `replace` fallback, so the CLI
  never crashes on `print`. Regression tests cover the cp1252 case.

## [2.16.1] - 2026-11-18

### Changed

- Clearer, explanatory wording for `--avoid-normalize-ro` in `-h` (two new
  examples: default normalization and raw mode) and a dedicated README section
  (`Read-only field normalization`) with the why, the affected field categories
  and the four scenarios.
- Extracted the alignment read-only decision into a testable helper
  (`_align_read_only_fields`); no behaviour change.

### Added

- Unit tests covering the four normalization scenarios
  (align/prune x normalization on/off).

## [2.16.0] - 2026-11-18

### Added

- `--avoid-normalize-ro`: read-only field normalization is now ON by default in
  BOTH the alignment and the plain prune. With the flag, `editable` flags are
  left exactly as retrieved.
- Plain prune (no `--align-from-source`) now also normalizes system read-only
  fields (`editable=false` for formula / roll-up summary / auto number /
  compound address & location) in the clean profile, matching the alignment.
- Non-blocking admin check at startup: for every invocation, the tool verifies
  the connecting user's profile in the involved org(s) and prints a warning when
  it is not "System Administrator" (results may depend on the current profile's
  permissions). Never stops the run.

## [2.15.0] - 2026-11-18

### Added

- A plain prune (no `--align-from-source`) now also writes a
  `what-is-changed.txt` report (removed dangling references grouped by metadata
  type, plus the non-alignable manual operations), so the removals are
  reviewable without touching the org.
- The plain-prune output now uses the same numbered folder layout as the
  alignment flow: the prune output lives in `1-source-pruned/` (COMMON, MISSING,
  missing_log.txt, report.txt) and the final deliverable + reports in
  `4-merged/` (the clean profile, `what-is-changed.txt`,
  `MANUAL-OPERATIONS.txt`).

## [2.14.4] - 2026-11-18

### Changed

- `report.txt` (written by the prune stage) now lists its output paths
  (`COMMON`, `MISSING`, `toDeploy`, `report`) relative to the output directory
  instead of absolute, matching the other user-facing paths.

## [2.14.3] - 2026-11-18

### Fixed

- Character encoding robustness: every place that decoded subprocess output
  (`sf` CLI and the Node readMetadata helper) or read text files used
  `errors='replace'`, which silently turns a non-UTF-8 byte into the Unicode
  replacement character ``�`` (U+FFFD). On Windows, if a subprocess emits the
  console ANSI code page instead of UTF-8, an accented ``à`` (lone ``0xE0``
  byte) would be permanently written as ``�`` into profile XML / package.xml /
  reports (e.g. layout names such as "specialità" or "attività"). Added
  `decode_bytes()` (strict UTF-8 first, Windows-1252 fallback) and applied it
  to the CLI output, the retrieve helper and the report/log readers.

## [2.14.2] - 2026-11-18

### Changed

- README gains an extended section on when to use `--align-from-source` vs the
  plain prune, and how to promote a profile through an environment chain
  (prune against the smallest org). `-h` examples now carry the same guidance
  in a short form.
- `MANUAL-OPERATIONS.txt` no longer repeats the layout-assignment explanation
  on every row: the "Explicit assignment is the object's ONLY layout …" and
  "Align the page layout assignment with the source …" texts now appear once in
  their section headers, and the MANUAL OPERATION column is dropped for those
  two groups (the full table for OTHER operations keeps its column).

## [2.14.1] - 2026-11-18

### Changed

- User-facing console paths (profile, project, report, merged profile, …) are
  now shown relative to the output directory; only the output directory itself
  stays absolute. Makes the final output block short and readable.

## [2.14.0] - 2026-11-18

### Changed

- The manual-operations TABLE is no longer printed to the terminal. Only the
  count lines (`REDUNDANT (ignorable)`, `NEEDS CHECK`, `OTHER MANUAL
  OPERATIONS`) are shown; the full table stays in `MANUAL-OPERATIONS.txt`.
- The align `ALIGNMENT RESULT` `MANUAL OPERATIONS:` line now carries a short
  breakdown: `N (X ignorable - redundant layout assignment, no action needed /
  Y need check - manual review required)`.

### Added

- When `--align-from-source` is run with a file source (`-pp`, no source org),
  a note warns that read-only detection only checks the destination side, so a
  field editable in the source but read-only-looking in the destination could
  be misclassified; it suggests `-so/-sp` to verify both sides.

## [2.13.0] - 2026-11-18

### Fixed

- Alignment no longer reports phantom `editable` grants/revokes for system
  read-only fields whose `DataType` looks normal. `LiveChatTranscript.ChatKey`
  (`Text(200)`) and `ServiceAppointment.AccountId` (`Lookup(Account)`) were
  still surfaced as `editable=true → false` grants because the type-based
  detector could not recognise them.

### Added

- A second read-only detection layer: `--align-from-source` now describes, on
  BOTH orgs, only the few objects whose fields actually differ on `editable`
  (and are not already formula/roll-up/auto-number/address). A field is treated
  as read-only only when it is `updateable=false` on both orgs, so fields that
  are merely field-level-security-locked on one side (e.g. `Campaign.Status`,
  which reports `updateable=false` only on the dev org whose Campaign object is
  locked) are correctly kept as real grants/revokes. With a file source (no
  source org), the destination-only describe is guarded by the object-level
  `updateable` flag. Scoped describe keeps the extra cost to a couple of
  `sobject describe` calls.

## [2.12.3] - 2026-11-18

### Changed

- `--diff` now prints only the summary counts and the paths of the three output
  files to the console (the full per-entry detail is no longer dumped to the
  terminal; it lives in `diff-report.txt`, `diff-data.json`, `diff-data.xml`).
- Help examples rewritten to explain the PURPOSE of each command (clean, clean
  + deploy, rename + deploy, retrieve + clean + deploy, full retrieve, retrieve
  only, align, compare, tune), including a full `--diff` example.

## [2.12.2] - 2026-11-18

### Added

- `--diff` now also emits the entries that are IDENTICAL in both profiles (with
  their full values), not just the differences.
- `--diff` writes two machine-readable files alongside `diff-report.txt`:
  `diff/diff-data.json` and `diff/diff-data.xml`. Each contains, for every
  section, the complete `common` (identical), `only_source`, `only_dest` and
  `different` entries with per-field values, so the result is consultable and
  manipulable programmatically.

## [2.12.1] - 2026-11-18

### Changed

- `--diff` now prints the full report to the console (summary counts AND the
  actual entries: what is only in the source, only in the destination, and what
  differs field-by-field), not just the counts table. The report is still
  written to `diff/diff-report.txt`.

## [2.12.0] - 2026-11-17

### Added

- `--diff`: compare the source profile against the destination profile and
  write a pure profile-to-profile diff report (`diff/diff-report.txt`). For
  every profile section it reports what is only in the source, only in the
  destination, and what differs for entries present in both (with per-field
  source/destination values), plus an on-screen summary table. Read-only: no
  prune, no dry-run, no deploy. Works with a source file (via 1) or a source
  org (via 2).

## [2.11.2] - 2026-11-17

### Fixed

- Reverted the SObject `describe()` `updateable` read-only detection introduced
  in 2.11.1: that flag reflects the running user's field-level security, not the
  field's inherent type, so it marked hundreds of ordinary editable fields (e.g.
  `Campaign.Status`, `Campaign.Description`) as read-only and produced phantom
  "revoked" entries. Read-only detection is again driven only by
  `FieldDefinition.DataType`, now also covering compound `Address`/`Location`
  fields (whose parent `editable` flag is always ignored by Salesforce).
- The field-level diff no longer reports `editable` for read-only fields, so the
  normalization (forced `false`) never surfaces a phantom revoke against a
  destination that holds the dead `true` value. `readable` is still diffed
  normally for these fields.

## [2.11.1] - 2026-11-17

### Fixed

- Alignment no longer reports phantom "revoked" entries that never converge:
  a source entry that is already fully OFF (e.g. `readable=false`/`editable=false`)
  and ABSENT from the destination is already aligned (both deny) and is now
  skipped — the destination does not store explicit off values, so they used to
  reappear on every re-run after the deploy.
- Read-only field detection now uses the org's authoritative SObject describe
  `updateable` flag, not just the `Formula`/`Roll-Up Summary`/`Auto Number`
  DataType heuristic. This catches system fields (e.g. `LiveChatTranscript.ChatKey`)
  and controlled lookups (e.g. `ServiceAppointment.AccountId`) whose `editable=true`
  is dead metadata, so they are no longer reported as phantom grants that never
  converge after deploy.

## [2.11.0] - 2026-11-17

### Added

- `--sure`: skip the Y/N confirmation of `--deploy-on-success` and deploy
  without asking. `--deploy-on-success` still asks Y/N by default; the pair is
  documented as `[--deploy-on-success [--sure]]`. No effect without
  `--deploy-on-success`.

## [2.10.0] - 2026-11-17

### Changed

- Reorganized the console output into numbered phases (`── PHASE 1/4 · … ──`)
  with a purpose line and explicit `▸`/`✓`/`⚠` sub-steps, written as each step
  runs. The banner/header and the `[HH:MM:SS]` timestamps are untouched, as are
  the live progress board and worker threads. Verbose details (the full `sf`
  binary path, etc.) now go to `run.log` only.

## [2.9.0] - 2026-11-17

### Added

- Alignment resume: a completed align run now saves `align-state.json` in the
  output folder. Re-running `--align-from-source` on that folder with the same
  `-so`/`-do`/`-sp`/`-dp` deploys the cached `4-merged/` profile directly
  (with `--deploy-on-success`) instead of redoing the whole pipeline. Different
  parameters fail with a clear error; `--fresh` discards the state and restarts
  from scratch.

## [2.8.1] - 2026-11-17

### Fixed

- Read-only field normalization now actually matches custom-object fields.
  `FieldDefinition.EntityDefinitionId` resolves to a DurableId (`01I...`) for
  custom objects, not the object API name, so formula / roll-up / auto-number
  fields on custom objects were never recognized and still appeared as fake
  `editable=true` grants in `what-is-changed.txt`. The field full name is now
  reconstructed from `EntityDefinition.QualifiedApiName`.

## [2.8.0] - 2026-11-17

### Changed

- The alignment now validates the merged profile with a dry-run **before**
  writing `MANUAL-OPERATIONS.txt` and `what-is-changed.txt`. If the dry-run
  surfaces residual dangling references, the merged profile is self-healed
  (re-pruned against the destination) and the reports are regenerated from the
  final deployable profile — so the reports always describe what will actually
  be deployed.

## [2.7.4] - 2026-11-17

### Changed

- `--deploy-throttle-ms` default lowered from 1000 to **500**.
- `--poll-interval` default lowered from 5.0 to **3.0** seconds.

## [2.7.3] - 2026-11-17

### Changed

- `MANUAL-OPERATIONS.txt` (and the end-of-run printout) now split the manual
  operations into three groups — `REDUNDANT (ignorable)`, `NEEDS CHECK`, and
  `OTHER MANUAL OPERATIONS` — so you can tell at a glance which entries need no
  action and which need a real decision. The per-section tables drop the
  redundant VERDICT column (the section header already states it).

## [2.7.2] - 2026-11-17

### Added

- The read-only field-type lookup now reports live progress (`ALIGN: field
  types: N/M object(s) (P%)`) so the merge phase is visibly advancing instead
  of appearing to stall.

## [2.7.1] - 2026-11-17

### Fixed

- The read-only field-type lookup no longer stalls the alignment: it now queries
  `FieldDefinition` in batches of 40 entities (`EntityDefinitionId IN (...)`)
  instead of one round-trip per object, so a profile spanning hundreds of
  objects resolves in a handful of queries instead of hundreds.

## [2.7.0] - 2026-11-17

### Added

- Fields whose type is inherently read-only (`Formula`, `Roll-Up Summary`,
  `Auto Number`) are now normalized by the alignment: their `editable` flag is
  forced to `false` in the merged profile and in `what-is-changed.txt`, instead
  of copying an `editable=true` from the source that Salesforce silently
  ignores. This removes fake "grant" entries and keeps the package consistent
  with the effective permission. The field types are looked up from the
  destination org via `FieldDefinition` (Tooling API).

## [2.6.0] - 2026-11-17

### Added

- Transient network failures from the Salesforce CLI (`fetch failed`,
  `connection reset`, DNS/TLS/timeout, …) are now retried automatically **up to
  5 attempts** with a short backoff, instead of aborting the whole run on the
  first blip. Each retry is logged on stderr; on final failure the error is
  reported with a clear message that it is a connection problem (not a
  profile/org-content error) and suggests checking connectivity/VPN.

### Fixed

- `--align-from-source` no longer forces a clean run: the two internal prune
  stages now honour `--fresh`. Without `--fresh` a re-run resumes from the
  previous partial state (via `missing_log.txt`) instead of clearing the
  `1-source-pruned/` and `3-destination-pruned/` folders. Only passing
  `--fresh` clears previous outputs.

## [2.5.2] - 2026-11-16

### Fixed

- The `what-is-changed.txt` header no longer mixes Italian labels into the
  English section legend (dropped the `(acceso)` / `(spento)` / `(a mano)`
  glosses).

## [2.5.1] - 2026-11-16

### Added

- `layoutAssignments` manual operations are now classified automatically:
  - **`REDUNDANT (ignorable)`** when the object has a single page layout (the
    explicit assignment is necessarily the default; the source inherits the
    same default, so there is nothing to do);
  - **`NEEDS CHECK`** when the object has several layouts (verify the explicit
    assignment matches the default and correct it if it does not).
  The verdict appears both in `MANUAL-OPERATIONS.txt` (new VERDICT column) and
  in the `MANUAL` section of `what-is-changed.txt`. The classification uses the
  destination org's `Layout` components (via `sf org list metadata`).

### Fixed

- The manual-operation wording for `layoutAssignments` no longer says "Remove
  the page layout assignment" (a page layout assignment cannot be deleted;
  when one exists it must remain). It now says the assignment must be aligned
  with the source, and the verdict tells you whether it is redundant or needs
  a real change.

## [2.5.0] - 2026-11-16

### Removed

- `--sure` flag and the `--align-from-source` Y/N prompt. The alignment now runs
  automatically: it only produces files (the pre-align destination profile is
  always saved in `2-destination-retrieved/`), so no confirmation is needed.
  `--deploy-on-success` still asks for Y/N confirmation because it modifies the
  org.

### Added

- The end-of-run `ALIGNMENT RESULT` now lists the path of **every** produced
  file, not just the merged profile and what-is-changed: original source
  (retrieved), source pruned, destination retrieved, destination pruned,
  merged profile, manual operations, and what-is-changed.

## [2.4.0] - 2026-11-16

### Added

- `what-is-changed.txt` now reports the **complete** set of differences the
  alignment deploy will apply, not just the turned-off rows. It is split into
  three clearly commented sections:
  - `GRANTED (acceso)` — the destination was OFF (or missing) and is turned ON;
  - `REVOKED (spento)` — the destination was ON and is turned OFF;
  - `MANUAL (a mano)` — sections with no deploy-able "off" value (do them in Setup).
  Each changed field keeps an inline `old value from <org>: <old value>` comment
  (`not present` when the entry/field did not exist in the destination).

### Changed

- `merge_profiles` now returns `(merged_xml, manual_ops, changes)` where
  `changes` classifies every deploy-time difference as `grant`, `revoke` or
  `removed`, instead of only tracking destination-only turn-offs.
- The alignment log and final summary report `GRANTED` and `REVOKED` counts
  instead of a single `TURNED OFF` count.

## [2.3.1] - 2026-11-16

### Fixed

- Destination-only entries that are **already off** in the destination are now
  skipped entirely by the alignment merge: they no longer appear in the merged
  profile, no longer count as "turned off", and no longer appear in
  `what-is-changed.txt`. Only real changes are reported.

## [2.3.0] - 2026-11-16

### Added

- `--batch-size` maximum raised from 4000 to **10000**, so `-bs 1000` and
  `-bs 10000` are usable as-is (larger chunks, fewer parallel deploy jobs).

### Fixed

- The two-column progress board's `[deleted/initial]` column now auto-widens
  when the counters grow to 4-5 digits (1000/10000), so the right-hand column
  stays aligned instead of drifting when the digit count changes.

### Changed

- README brought up to date: two-column board, alignment flow and its output
  (`4-merged/`, `MANUAL-OPERATIONS.txt`, `what-is-changed.txt`), merged-profile
  dry-run validation, and the full module layout (`retrieve.py`, `stage.py`,
  `merge.py`, `node/`).

## [2.2.1] - 2026-11-16

### Fixed

- The two-column progress board now pads the `[deleted/initial]` ratio to a
  fixed width, so the right-hand column no longer drifts to the left/right
  when the number of digits changes (e.g. `[0/0]` vs `[0/100]`).

## [2.2.0] - 2026-11-16

### Added

- **Merged profile validation**: `--align-from-source` now runs a dry-run against
  the destination on the merged profile before any deploy. A deploy is only
  attempted when the merged dry-run is clean; otherwise the run ends with
  `VALIDATION_FAILED` and reports the component failures.
- **`what-is-changed.txt` report**: a new file inside `4-merged/` listing only the
  changed (turned-off) and removed entries, each annotated with an XML comment —
  `old value from <org>: <value>` inline on the changed field for turned-off
  entries, or a grouped list with `... removed in destination org because not
  present in source org` for entries removed because they are not in the source org.
- **`MANUAL-OPERATIONS.txt` moved inside `4-merged/`** for the alignment flow.

### Fixed

- The alignment merge now carries the per-entry old values so the
  what-is-changed report can show exactly what changed.

## [2.1.1] - 2026-11-16

### Fixed

- **Merged profile formatting**: the "turned off" entries added by the merge
  were serialized concatenated on a single line (no newline/indentation) and
  the first section sat on the same line as `<Profile>`. The merged output is
  now normalized so every section/entry is on its own line.
- **4-space indentation**: retrieved, pruned (COMMON) and merged profile files
  now use a consistent 4-space indent (matching `package.xml`).

## [2.1.0] - 2026-11-04

### Added

- **Alignment engine (`--align-from-source`)**: the destination profile is now
  retrieved and pruned too, then merged with the (authoritative) source. Every
  destination-only permission is turned OFF with an explicit off value
  (`visible=false`, `visibility=Hidden`, `enabled=false`, …); sections with no
  expressible off value (`layoutAssignments`, `loginFlows`, `loginIpRanges`,
  `loginHours`) are collected into a `MANUAL-OPERATIONS.txt` file and printed in
  a table at the end of the run, with the exact Setup steps to finish the
  alignment by hand.
- **Manual-operations report for the normal prune too**: when the prune removes
  entries from non-alignable sections, the same file + table list which manual
  actions are still needed in the destination org (a Profile deploy cannot
  remove them).

### Fixed

- **Progress board two-column layout**: the board clears the screen and homes
  the cursor before the first draw, reserves two blank bottom rows, and renders
  two workers per line (dropping the `remaining`/`initial` columns and keeping
  only `deleted/initial`), so a 64-worker board fits without scrolling.
- **Upgrade hint** now suggests `pip install --upgrade --upgrade-strategy eager`
  so a future `pip` upgrade also bumps the dependencies (e.g. lxml).

## [2.0.1] - 2026-11-04

### Fixed

- **Progress board scrolling**: the board left the frame at exactly the terminal
  height (`height - 1` worker rows + 1 summary line), so the trailing newline
  scrolled the console by one line on every update and the board kept reprinting
  instead of redrawing in place. It now reserves one extra line (`height - 2`) and
  re-reads the window size on every render so a mid-run resize no longer desyncs
  the cursor accounting.

## [2.0.0] - 2026-11-13

### Added

- **`--align-from-source`**: align the destination profile to the source (turn
  off destination-only permissions). Destructive even on dry-run; asks for Y/N
  confirmation unless immediately followed by `--sure`.
- **`--sure`**: skip the Y/N confirmation of the immediately preceding
  destructive option (`--align-from-source` or `--deploy-on-success`). It must
  be placed right after it and applies to that one option only.
- **Y/N confirmation** on `--deploy-on-success` (it modifies the org).

### Removed

- Deprecated flags `--org`, `--profile-name`, `--retrieve-full` and
  `--max-elem-per-section` (use `--dest-org`, `--dest-profile`,
  `--full-profile` and `--batch-size`).

## [1.18.0] - 2026-11-13

### Added

- **Via 3 (retrieve only)**: `-so/--src-org` + `-sp/--src-profile` alone
  retrieves the profile from the source org and writes it to `--out-dir`,
  with no prune, no dry-run and no deploy. Feed the file to via 1, or run via 2
  with `-so == -do` and `-sp == -dp`, to prune it afterwards.

## [1.17.0] - 2026-11-13

### Added

- **Two input modes**: the CLI now accepts a profile from a file (via 1,
  `-pp/--profile-path` + `-do/--dest-org`) or retrieves it from a source org
  (via 2, `-so/--src-org` + `-sp/--src-profile` + `-do` + `-dp`). Source and
  destination org/profile may coincide.
- **`-dp/--dest-profile`**: deploy the cleaned profile under a different name;
  Salesforce creates the profile if it does not exist and merges into it
  otherwise.
- **`-fp/--full-profile`**: retrieve the full (non-stripped) profile; the
  default retrieve is now *clean* (strips `loginIpRanges`/`loginHours`/
  `userLicense`).
- Short flags: `-pp`, `-so`, `-sp`, `-fp`, `-do`, `-dp`, `-od`, `-bs`, `-tn`,
  `-av`.
- `-h` now groups options into sections and prints usage examples.

### Changed

- `--out-dir` now defaults to the current directory (`-od` to override).
- `--batch-size` (new) replaces `--max-elem-per-section`: min 100, max 4000,
  default 200; `0` disables splitting.
- `--thread-number` default raised to `16`.
- `--job-timeout` default is now `0` (no timeout); valid range 1–60 minutes.

### Deprecated (removed in v2.x.x)

- `--org` → `--dest-org`; `--profile-name` → `--dest-profile`;
  `--retrieve-full` → `--full-profile`; `--max-elem-per-section` →
  `--batch-size`. The old flags still work and print a warning.

### Removed

- `--project-dir`: the tool always uses a minimal project inside `--out-dir`.

## [1.16.0] - 2026-11-13

### Added

- **Standard metadata pre-pass**: before the dry-run loop the tool now queries
  the org's Tooling API (`TabDefinition`, `EntityDefinition`, `FieldDefinition`)
  and removes standard tabs, objects and fields the profile references but the
  org does not have — the class of error a dry-run never surfaces (e.g.
  `You can't edit tab settings for X, as it's not a valid tab`).
- **`--retrieve-full`**: retrieve the *complete* Profile directly from `--org`
  via the Metadata API `readMetadata()` (through a small, owned Node helper that
  reuses the CLI's authentication), no third-party plugin required. This is the
  only way to get the full profile — `project retrieve` returns a sparse one.
- `--deploy-throttle-ms` option (default `1000`) to space out deploy-start
  submissions; `0` disables the throttle.

### Changed

- **Prefetch is now mandatory**: the Metadata API pre-pass and the standard
  metadata pre-pass always run. The `--no-prefetch` and `--prefetch-standards`
  flags were removed — the cleaned profile is always validated against the full
  org metadata, not just the dry-run.

### Fixed

- `__mdt` objects (custom metadata types) were misclassified as standard
  objects; standard-object/field detection now uses `is_standard_api_name`
  (`__` in the API name), so only true standard objects are treated as such.
- `FieldDefinition.DurableId` is not reliably `Object.Field` for relationship
  and system fields; standard fields are now resolved per object from
  `EntityDefinition.QualifiedApiName + QualifiedApiName` (batched, split on
  `EXCEEDED_ID_LIMIT`).
- `--max-elem-per-section` is now clamped to a minimum of `100` (0 = no
  splitting is preserved). Tiny chunks restarted deploy jobs too often and
  tripped the CLI's per-org deploy lock (`Lock file is already being held`).

## [1.15.4] - 2026-11-12

### Fixed

- The Metadata API pre-pass now lists `customMetadataTypeAccesses` under its
  real API type `CustomMetadata` instead of `CustomMetadataType` (which the API
  rejects with `INVALID_TYPE: Unknown type:CustomMetadataType`).
- The live board no longer scrolls / reprints on every update when the number
  of worker rows exceeds the terminal height: the frame is capped to the
  terminal height so the ANSI cursor-up accounting never breaks (chunking can
  now spawn many more workers than before).

## [1.15.3] - 2026-11-12

### Added

- Restored `COMMON/toDeploy.xml`: a `package.xml` manifest of the deployable
  (COMMON) members referenced by the clean profile, generated via
  `extract_common_members` (standard objects/fields/tabs excluded).

## [1.15.2] - 2026-11-12

### Changed

- Removed the misleading `fetched` column from the live board. Each dry-run
  tests the whole remaining chunk at once, so there is no per-entry "processed"
  counter — the board now shows only the real, monotonic quantities:
  `deleted` (discarded so far), `remaining` (still to process) and `initial`
  (chunk total).

## [1.15.1] - 2026-11-12

### Removed

- Removed `COMMON/toDeploy.xml` (added in 1.15.0 by mistake). The clean profile
  keeps its real name (`COMMON/<name>.profile-meta.xml`, unchanged content); no
  extra manifest file is produced.

## [1.15.0] - 2026-11-12

### Changed

- **Output renamed**: `GOOD/` → `COMMON/` and `ERRORS/` → `MISSING/`, and
  `errors_log.txt` → `missing_log.txt` (in the code, the report and the docs).
- `COMMON/` now also contains `toDeploy.xml`: a `package.xml` manifest of the
  deployable (COMMON) members — every custom metadata the surviving profile
  references that exists in the target org. Standard objects/fields/tabs are
  excluded (always present).

## [1.14.2] - 2026-11-12

### Changed

- Restored the `deleted` column on the live board. It now shows four counters:
  `fetched` (processed so far), `deleted` (discarded so far), `remaining` (still
  to process) and `initial` (chunk total). While a chunk is in flight `fetched`
  and `deleted` are equal (an entry leaves the working set only when discarded).

## [1.14.1] - 2026-11-12

### Changed

- The live board now shows `fetched` (entries **processed so far**, a monotonic
  counter that grows as entries are discarded) and `remaining` (entries still
  to process = `initial - fetched`), replacing the previous `fetched`/`deleted`
  pair.

## [1.14.0] - 2026-11-12

### Added

- `--max-elem-per-section` (default `500`): sections holding more than `N`
  entries are now split into independent chunks, each pruned by its own worker
  in parallel, so a huge section (e.g. thousands of `fieldPermissions`) no
  longer serializes the whole run behind a single worker. `0` keeps the old
  behavior (one worker per section).
- The live board column `remaining` is now `fetched`: it shows how many entries
  the worker is currently evaluating, regardless of whether they end up kept
  or discarded.

## [1.13.3] - 2026-11-12

### Added

- Speaking startup and phase logs so the program never looks frozen: the
  logger is now created before any slow step, and every macro-operation
  (update check, locating the Salesforce CLI, detecting the API version,
  preparing the working copy, reading/splitting the profile, writing the
  initial and final outputs, launching workers, writing the report, cleaning
  up) prints a clear line to both the console and `run.log`.

## [1.13.2] - 2026-11-12

### Changed

- No functional change: version bump to verify the startup update check.

## [1.13.1] - 2026-11-12

### Fixed

- The metadata pre-fetch phase no longer looks frozen: it now prints per-type
  progress (`[i/N] listing X…`, `N existing, M dangling` / `skipped` / `zero
  members`) and shows a live in-place status line with the elapsed clock during
  the whole pre-fetch. In non-TTY (piped) output the messages flow through the
  regular logger instead.

## [1.13.0] - 2026-11-12

### Added

- Non-blocking startup version check: the tool queries PyPI (best-effort,
  ~2s timeout, silently skipped on any network error) and, only when a newer
  version exists, prints an informational warning on stderr with the upgrade
  command (`python -m pip install --upgrade sf-profile-pruner`).

## [1.12.5] - 2026-11-12

### Fixed

- A Ctrl-C while a blocking `sf` call is running with no stop control (e.g.
  `sf org display` when `--api-version` is omitted, or the final dry-run) no
  longer dumps a traceback: the child process tree is terminated and the run
  stops with a clean `stopped by user` message (exit code 130).

## [1.12.4] - 2026-11-12

### Changed

- Replaced the banner with a new two-part ASCII-art "PROFILE PRUNER" banner.

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
