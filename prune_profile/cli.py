# -*- coding: utf-8 -*-
"""Profile Pruner - command-line entry point.

Removes from a Salesforce Profile every reference to metadata that does NOT
exist in the destination org. Pruning combines a Metadata API + Tooling API
pre-pass with an authoritative `sf project deploy start --dry-run`, and never
modifies the org; with `--deploy-on-success` the cleaned profile is deployed at
the end (add `--sure` to skip the Y/N confirmation).

Three input modes:
  * via 1 (file):   `--profile-path`  + `--dest-org`
  * via 2 (org):    `--src-org` + `--src-profile` + `--dest-org` + `--dest-profile`
                    (the profile is retrieved from the source org first)
  * via 3 (retrieve only): `--src-org` + `--src-profile` (no prune, no dry-run/deploy)

`--align-from-source` upgrades via 1/via 2 into an *alignment*: after pruning,
the destination profile is also retrieved and pruned, and every permission that
exists only in the destination is turned off so the destination matches the
source. It runs automatically (no Y/N prompt) because the pre-align destination
profile is always saved in the run folder (`2-destination-retrieved/`).

This module is the orchestration layer: it parses/validates arguments, resolves
the input mode, retrieves the profile when needed, calls the reusable prune
stage (`stage.prune_stage`), and optionally deploys the result. All the actual
pruning lives in `stage.py`.
"""

import argparse
import json
import os
import re
import shutil
import sys
import time

from lxml import etree

from .board import Logger
from .constants import (
    APP_NAME,
    AUTHOR_NOTE,
    BANNER,
    COPYLEFT,
    DEFAULT_BATCH_SIZE,
    DEFAULT_IO_THREADS,
    DISCLAIMER,
    M,
    MAX_CHUNK_SIZE,
    MAX_IO_THREADS,
    METADATA_TYPES,
    MIN_CHUNK_SIZE,
    NS,
    STORY,
    __version__,
    is_admin_profile,
    is_standard_api_name,
    meta_filename,
    meta_folder,
    meta_manifest,
    meta_root,
    version_ge,
)
from .merge import (PROFILE_EXCLUSIVE_SECTIONS,
                    READ_ONLY_FIELD_TYPE_EXACT, READ_ONLY_FIELD_TYPE_PREFIXES,
                    classify_layout_ops, convert_metadata_type, dest_only_members,
                    diff_profiles,
                    field_permissions_editable, field_permissions_names,
                    license_mismatch, merge_profiles, metadata_license,
                    normalize_profile_read_only, normalized_field_changes,
                    removed_non_alignable,
                    render_diff_report, render_diff_summary, write_diff_json,
                    write_diff_xml, write_manual_ops,
                    write_prune_what_is_changed, write_what_is_changed)
from .parallel import run_parallel
from .repair import (build_universe, entry_removals_from_failures,
                     flip_profile_on, grant_missing, strip_flagged_entries,
                     write_onniscent_wic)
from .retrieve import retrieve_profiles
from .sfcli import (deploy, describe_object, dry_run, field_data_types,
                    find_sf, list_metadata, org_api_version, org_username,
                    set_deploy_start_interval, soql_query, standard_entities)
from .stage import prune_stage
from .xmlio import (detect_metadata_type, load_log_entries, normalize_xml_declaration,
                    parse_ignore_package, read_report_summary, tostring_xml,
                    write_package_xml, write_sfdx_project)


def _rel(base, path):
    """Render `path` relative to `base` for the user-facing console output.

    The output directory itself is always shown absolute; every other path
    (profile, project, report, ...) is shown relative to it so the console stays
    short and readable."""
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return path


# Options that consume exactly one value. Used to reconstruct an argv that
# Windows/PowerShell mangled (see _repair_mangled_argv).
_VALUE_FLAGS = {
    '--profile-path', '-pp',
    '--out-dir', '-od',
    '--src-org', '-so',
    '--src-profile', '-sp',
    '--src-permissionset', '-sps',
    '--dest-org', '-do',
    '--dest-profile', '-dp',
    '--dest-permissionset', '-dps',
    '--batch-size', '-bs',
    '--api-version', '-av',
    '--thread-number', '-tn',
    '--io-threads-number', '-itn',
    '--job-timeout', '--deploy-throttle-ms', '--poll-interval',
    '--max-iter', '--sf',
}

EXAMPLES = '''\
Examples:
  # 1) CLEAN a profile file: remove references to metadata that does not exist
  #    in the destination org. Dry-run only - the org is never modified.
  sfpp -pp "Admin.profile-meta.xml" -do mysandbox

  # 2) CLEAN AND DEPLOY: after the profile is clean, deploy it to the
  #    destination org (asks Y/N). Add --sure to skip the confirmation.
  sfpp -pp "Admin.profile-meta.xml" -do prod --deploy-on-success

  # 3) CLEAN, RENAME AND DEPLOY: deploy the cleaned profile under a new name
  #    (creates it if it does not exist in the org).
  sfpp -pp "Admin.profile-meta.xml" -do prod -dp "Admin_Clean" --deploy-on-success --sure

  # 4) RETRIEVE + CLEAN + DEPLOY: pull a profile from a source org, clean it
  #    against a destination org, then deploy it there.
  sfpp -so devorg -sp "Sales Rep" -do mysandbox -dp "Sales Rep" --deploy-on-success

  # 5) FULL RETRIEVE (no stripping): keep loginIpRanges/loginHours/userLicense
  #    in the retrieved profile; dry-run only.
  sfpp -so mysandbox -sp "Sales Rep" -do mysandbox -dp "Sales Rep" -fp

  # 6) RETRIEVE ONLY: download the profile from a source org without any
  #    cleaning or deploy.
  sfpp -so devorg -sp "Sales Rep" -od ./profiles

  # 7) ALIGN: make the destination profile match the source (turns OFF
  #    destination-only permissions). Use ALIGN to make two orgs converge to
  #    the same profile; use plain prune (1-6) to PROMOTE a profile through
  #    dev->uat->prod.
  sfpp -so uat -sp "Sales Rep" -do test -dp "Sales Rep" --align-from-source

  # 8) COMPARE: diff the source profile against the destination profile without
  #    changing anything. Writes diff/diff-report.txt + diff/diff-data.json +
  #    diff/diff-data.xml (what is only in one, only in the other, identical,
  #    and what differs field-by-field).
  sfpp -so uat -sp "Sales Rep" -do test -dp "Sales Rep" --diff -od ./out

  # 9) PROMOTE across an environment chain: prune against the SMALLEST org
  #    (prod) so the profile deploys everywhere (dev/uat are supersets of prod).
  sfpp -so dev -sp "Sales Rep" -do prod -dp "Sales Rep" -od ./out

  # 10) TUNE the pruning: custom output dir, batch size and threads.
  sfpp -pp "Admin.profile-meta.xml" -do prod -od ./out -bs 300 -tn 32

  # 11) READ-ONLY NORMALIZATION (ON by default): Salesforce ignores the
  #     editable flag on fields it keeps read-only (formula, roll-up summary,
  #     auto number, compound Address/Location, system fields like ChatKey or
  #     AccountId). Both the plain prune and --align-from-source rewrite those
  #     to editable=false so the profile matches what the org actually enforces.
  sfpp -so dev -sp "Sales Rep" -do prod -dp "Sales Rep" -od ./out

  # 12) KEEP THE RAW editable FLAGS: pass --avoid-normalize-ro to inspect the
  #     profile exactly as retrieved, without the read-only rewrite.
  sfpp -so dev -sp "Sales Rep" -do prod -dp "Sales Rep" --avoid-normalize-ro -od ./out
'''


def _clamp_batch_size(n):
    """Clamp a batch size into the allowed range (0 = no chunking is kept)."""
    if n <= 0:
        return 0
    if n < MIN_CHUNK_SIZE:
        return MIN_CHUNK_SIZE
    if n > MAX_CHUNK_SIZE:
        return MAX_CHUNK_SIZE
    return n


def _read_only_field_set(sf, src_org, dest_org, src_clean_path, dst_clean_path,
                         logger, io_threads=8):
    """Detect ``fieldPermissions`` fields whose ``editable`` flag is dead
    metadata (the org always keeps them read-only), so the tool normalizes them
    to ``false`` instead of reporting a fake grant/revoke (align) or leaving a
    dead ``true`` in the deliverable (plain prune).

    Two layers:

    1. ``FieldDefinition.DataType`` (user-independent, batched) catches the
       self-evident read-only types: Formula, Roll-Up Summary, Auto Number,
       Address, Location.
    2. ``sobject describe`` catches the remaining system read-only fields whose
       DataType looks normal (e.g. ``LiveChatTranscript.ChatKey`` is
       ``Text(200)`` and ``ServiceAppointment.AccountId`` is ``Lookup(Account)``).
       A field is read-only only when ``updateable=False`` on BOTH orgs: a field
       that is merely FLS-locked on one org (e.g. ``Campaign.Status`` shows
       ``updateable=False`` only on the dev org whose Campaign object is locked)
       reports ``True`` on the other side and is kept. Describe runs source-first
       (only objects that actually show a disagreement are re-described on the
       destination), so it stays cheap.
    """
    names = field_permissions_names(src_clean_path)
    entities = []
    for n in names:
        ent = n.split('.', 1)[0]
        if ent and ent not in entities:
            entities.append(ent)

    # --- layer 1: DataType (batched FieldDefinition query) ---
    logger.log('  - fetching field types for %d object(s) to normalize '
               'read-only fields...' % len(entities))
    progress = {'last': -1}

    def _on_progress(done, total):
        pct = (done * 100) // total if total else 100
        if pct != progress['last']:
            progress['last'] = pct
            logger.log('  - field types: %d/%d object(s) (%d%%)' % (done, total, pct))

    types, terr = field_data_types(sf, dest_org, entities, on_progress=_on_progress,
                                   io_threads=io_threads)
    if types is None:
        logger.log('  WARNING: could not fetch field types (%s); editable flags left as-is.' % terr)
        return set()
    read_only = set()
    for n in names:
        dt = types.get(n, '')
        if dt.startswith(READ_ONLY_FIELD_TYPE_PREFIXES) or dt in READ_ONLY_FIELD_TYPE_EXACT:
            read_only.add(n)
    if read_only:
        logger.log('  OK: %d field(s) read-only by type (formula/roll-up/auto-number/address); '
                   'editable normalized to false.' % len(read_only))

    # --- layer 2: describe-based detection of system read-only fields ---
    # Scope differs by mode:
    #   * align (dst_clean_path present): the two profiles exist, so candidates
    #     are the (few) fields whose editable flag differs between them.
    #   * plain prune (no destination profile): candidates are the standard
    #     fields the source marks editable=true. Custom fields are excluded - a
    #     custom field is read-only only when formula / roll-up summary / auto
    #     number (already caught by layer 1) or FLS-locked (which must NOT be
    #     normalized) - so only standard system fields (Status, IsClosed,
    #     ChatKey, AccountId, ...) are worth describing.
    src_edit = field_permissions_editable(src_clean_path)
    if dst_clean_path is not None:
        dst_edit = field_permissions_editable(dst_clean_path)
        candidates = [n for n in names
                      if n not in read_only
                      and n in dst_edit
                      and src_edit.get(n, '') != dst_edit.get(n)]
    else:
        candidates = [n for n in names
                      if n not in read_only
                      and src_edit.get(n) == 'true'
                      and is_standard_api_name(n.rsplit('.', 1)[-1])]
    if not candidates:
        return read_only
    cand_entities = []
    for n in candidates:
        ent = n.split('.', 1)[0]
        if ent and ent not in cand_entities:
            cand_entities.append(ent)

    # Describe only STANDARD objects present in the destination org. System
    # read-only fields (Status, IsClosed, ChatKey, AccountId, ...) live on
    # standard objects; custom objects are never interesting for this pass and
    # may not even be describable (NOT_FOUND), so scoping to standard entities
    # removes the noise and the wasted round-trips.
    dest_entities = set(cand_entities)
    existing, eerr = standard_entities(sf, dest_org, cand_entities,
                                       io_threads=io_threads)
    if existing is not None:
        dest_entities = {e for e in cand_entities if e in existing}
    elif eerr:
        logger.log('  WARNING: could not list standard destination entities (%s); describing all candidates.' % eerr)
    cand_entities = [e for e in cand_entities if e in dest_entities]
    if not cand_entities:
        return read_only
    # Keep only candidates whose object is a standard object in the destination:
    # a field on a custom object (or an absent object) is never a system
    # read-only field to normalize. This also covers the file-source path, where
    # every candidate would otherwise be suspected.
    candidates = [n for n in candidates if n.split('.', 1)[0] in dest_entities]
    if not candidates:
        return read_only
    logger.log('  - describing %d object(s) to detect system read-only field(s)...'
               % len(cand_entities))

    def _describe_org(org, entities):
        """Describe `entities` in `org` in parallel, returning
        ``(info, not_found_count)``. Logging and counting happen here (caller
        thread), never in the workers, so the output order is deterministic."""
        def _one(entity):
            return describe_object(sf, org, entity)

        results, errors = run_parallel(entities, _one, max_workers=io_threads)
        info = {}
        not_found = 0
        for i, entity in enumerate(entities):
            if errors[i]:
                logger.log('  WARNING: describe %s on %s failed (%s); those fields left as-is.'
                           % (entity, org, errors[i]))
                continue
            res, err = results[i]
            if res is None:
                # NOT_FOUND = object not describable in this org (missing /
                # restricted). Not actionable: leave those fields as-is and just
                # count it, staying quiet.
                if err and ('NOT_FOUND' in str(err) or 'does not exist' in str(err)):
                    not_found += 1
                else:
                    logger.log('  WARNING: describe %s on %s failed (%s); those fields left as-is.'
                               % (entity, org, err))
                continue
            fields = res.get('fields') or []
            info[entity] = {
                'object_updateable': bool(res.get('updateable')),
                'fields': {str(f.get('name')): bool(f.get('updateable')) for f in fields
                           if f.get('name')},
            }
        return info, not_found

    # Source-first: describe the source org, keep only the fields the profile
    # marks editable=true but the source org itself reports read-only. Only
    # those (few) objects need a destination describe to confirm the field is
    # system read-only (false on both) rather than FLS-locked on the source.
    not_found_total = 0
    src_describe = {}
    if src_org:
        src_describe, nf = _describe_org(src_org, cand_entities)
        not_found_total += nf

    suspected = []
    for n in candidates:
        ent, field = n.split('.', 1)
        if src_org:
            src = src_describe.get(ent)
            if src is None or field not in src['fields']:
                continue
            if src['fields'][field] is False:
                suspected.append(n)
        else:
            # File source: no source org to compare against. Suspect every
            # candidate; the destination-side object guard filters FLS-locked
            # objects.
            suspected.append(n)
    if not suspected:
        if not_found_total:
            logger.log('  - %d object(s) not describable in the org(s) - their fields left '
                       'as-is (not normalized).' % not_found_total)
        return read_only

    dst_entities = []
    for n in suspected:
        ent = n.split('.', 1)[0]
        if ent and ent not in dst_entities:
            dst_entities.append(ent)
    dst_describe, nf = _describe_org(dest_org, dst_entities)
    not_found_total += nf

    added = []
    for n in suspected:
        ent, field = n.split('.', 1)
        dst = dst_describe.get(ent)
        if dst is None or field not in dst['fields']:
            continue
        if src_org:
            src = src_describe.get(ent)
            if src is None or field not in src['fields']:
                continue
            read_only_on_both = (src['fields'][field] is False
                                 and dst['fields'][field] is False)
        else:
            # File source: trust dest-only read-only only when the object itself
            # is updateable there (otherwise the whole object being FLS-locked
            # makes every field look read-only).
            read_only_on_both = (dst['object_updateable'] is True
                                 and dst['fields'][field] is False)
        if read_only_on_both:
            read_only.add(n)
            added.append(n)
    if added:
        logger.log('  OK: %d system read-only field(s) (updateable=false on both orgs) '
                   'normalized: %s' % (len(added), ', '.join(sorted(added))))
    if not_found_total:
        logger.log('  - %d object(s) not describable in the org(s) - their fields left '
                   'as-is (not normalized).' % not_found_total)
    return read_only


def _warn_if_not_admin(sf, org, logger):
    """Non-blocking warning when the connecting user of `org` is not an
    administrator. Prune/diff/align results can depend on the current profile's
    permissions (the tool connects as the logged-in user), so we surface this
    instead of failing. Never raises and never stops the run.

    Uses STANDARD SOQL: the Tooling API does not support the ``Profile``
    relation on ``User`` (``INVALID_FIELD: No such relation 'Profile' on
    entity 'User'``), which made the old check fail with a misleading "no user
    record found". The profile name is then matched against
    :func:`is_admin_profile` (standard localized names + conventional custom
    admin names).
    """
    logger.log('  - checking the connecting user profile in %s (admin check)...' % org)
    username, uerr = org_username(sf, org)
    if not username:
        logger.log('  WARNING: could not resolve the org username for %r '
                   '(admin check skipped): %s' % (org, uerr or 'unknown error'))
        return
    try:
        records, qerr = soql_query(
            sf, org,
            "SELECT Id, Profile.Name FROM User WHERE Username = '%s' LIMIT 1"
            % username.replace("'", "\\'"))
    except Exception:
        logger.log('  WARNING: admin check failed for %s (non-blocking).' % org)
        return
    if records is None:
        logger.log('  WARNING: admin check unavailable for %s (%s).'
                   % (org, qerr or 'query error'))
        return
    if not records:
        logger.log('  WARNING: no user record found for %r in %s (admin check skipped).'
                   % (username, org))
        return
    profile = (records[0].get('Profile') or {}).get('Name') or ''
    if profile and is_admin_profile(profile):
        logger.log('  OK: connecting user in %s is %s' % (org, profile))
        return
    logger.log('  WARNING: ALERT (non-blocking): the connecting user in %s has profile %r, '
               'which is not an administrator profile; the results may depend on the '
               'current profile permissions.' % (org, profile or 'unknown'))


def _warn_permissionset_deploy(logger, dest_mdtype, quiet, deploy=False):
    """Surface the PermissionSet deploy-semantics difference.

    A Profile deploy is a per-element MERGE: omitting an entry is a silent
    no-op. A PermissionSet deploy is authoritative: every permission NOT
    present in the deployed file is REVOKED in the target org. This matters
    whenever the tool hands back a PermissionSet whose content is a subset of
    what the org currently grants (plain prune removes dangling references;
    align turns destination-only permissions off - deliberate in the align case,
    but still worth stating explicitly). Never raises and never stops the run.
    """
    if dest_mdtype != 'PermissionSet':
        return
    verb = 'deploying' if deploy else 'producing'
    logger.log('')
    logger.log('  WARNING: PERMISSIONSET SEMANTICS: %s a PermissionSet, unlike a Profile,' % verb)
    logger.log('    is AUTHORITATIVE: every permission NOT present in the file is')
    logger.log('    REVOKED in the target org (a Profile deploy only MERGES and')
    logger.log('    leaves omitted entries untouched).')
    logger.log('    - plain prune: the dangling references it removes are NOT re-granted.')
    logger.log('    - align: destination-only permissions are turned OFF on purpose'
               ' (that is the alignment goal).')
    if not quiet:
        print('')
        print('WARNING: PermissionSet is authoritative - permissions not present in')
        print('         the deployed file are REVOKED in the target org (unlike a')
        print('         Profile, where an omitted entry is simply ignored).')
        print('', flush=True)


def _metadata_exists(sf, org, mdtype, name):
    """Read-only check: True when `name` already exists in `org` as a Profile
    or PermissionSet (plain SOQL, since Profile is not reachable via Tooling).
    Returns False on a query error (fail-open) so a transient failure never
    blocks an otherwise legitimate run."""
    object_api = 'PermissionSet' if mdtype == 'PermissionSet' else 'Profile'
    records, err = soql_query(
        sf, org,
        "SELECT Id FROM %s WHERE Name = '%s' LIMIT 1"
        % (object_api, name.replace("'", "\\'")))
    if records is None:
        return False
    return bool(records)


def _empty_metadata_xml(mdtype, name=''):
    """Minimal, valid source XML for a missing Profile / PermissionSet (used
    when a destination does not exist but the run proceeds anyway)."""
    root = etree.Element(M + meta_root(mdtype), nsmap={None: NS})
    if mdtype == 'PermissionSet':
        lbl = etree.SubElement(root, M + 'label')
        lbl.text = name or 'Pruner'
    return tostring_xml(root)


def _validate_ps_api_name(name):
    """Validate a user-supplied PermissionSet API name (no file extension).

    Salesforce rules for a PermissionSet DeveloperName: it starts with a letter,
    contains only letters, digits and single underscores (``__`` is reserved for
    namespaces and a trailing underscore is forbidden), and is at most 80
    characters. Returns ``(ok, reason)`` where ``reason`` is empty when valid.
    """
    if not name:
        return False, 'empty name'
    if '.' in name:
        return False, ('give the API name without the file extension '
                       '(e.g. "MyPS", not "MyPS.permissionset-meta.xml")')
    if not re.match(r'^[A-Za-z][A-Za-z0-9_]{0,79}$', name):
        return False, ('must start with a letter and contain only letters, digits '
                       'and single underscores (max 80 characters)')
    if '__' in name:
        return False, 'double underscore ("__") is reserved for namespaces'
    if name.endswith('_'):
        return False, 'must not end with an underscore'
    return True, ''



def _warn_destination_missing(logger, quiet, mdtype, name, org, action):
    """Non-blocking warning: the destination does not exist in `org`."""
    msg = 'destination %s %r does not exist in %s (%s)' % (mdtype, name, org, action)
    logger.log('')
    logger.log('  WARNING: %s' % msg)
    if not quiet:
        print('')
        print('WARNING: %s' % msg)
        print('', flush=True)


def _warn_license_mismatch(logger, quiet, src_license, dst_license, dest_name):
    """Non-blocking warning for a PS -> PS align whose licenses differ.

    A PermissionSet license is immutable once the set exists (Salesforce rejects
    an update with "License can't be updated"), so aligning onto the existing
    destination will fail the dry-run/deploy. The correct path is to create a
    NEW PermissionSet under a fresh name via --new-ps-name.
    """
    msg = ('PermissionSet license mismatch: source declares %r but the '
           'destination %r declares %r.' % (src_license, dest_name, dst_license))
    logger.log('')
    logger.log('  WARNING: %s' % msg)
    logger.log('    A PermissionSet license cannot be updated once it exists: aligning')
    logger.log('    onto %r will fail the dry-run/deploy with "License can\'t be updated".'
               % dest_name)
    logger.log('    To change the license, create a NEW PermissionSet instead: re-run with')
    logger.log('    --new-ps-name "NEW_NAME" (a name that does not exist in the '
               'destination org).')
    if not quiet:
        print('')
        print('WARNING: %s' % msg)
        print('         A PermissionSet license cannot be updated once it exists, so')
        print('         aligning onto %r will fail with "License can\'t be updated".'
              % dest_name)
        print('         To change the license, create a NEW PermissionSet instead:')
        print('         re-run with --new-ps-name "NEW_NAME" (a name that does not')
        print('         exist in the destination org).')
        print('', flush=True)



def _repair_mangled_argv(argv):
    """Reconstruct argv after PowerShell/Windows mangled a quoted argument.

    When a quoted argument ends with a backslash immediately before its closing
    quote (e.g. ``--out-dir "...\\\\src Test\\\\dest Test\\\\"``), the Windows command
    line parser treats the trailing ``\\\"`` as an escaped quote: the quote becomes
    a literal ``"``, the argument stays open, and every following token is
    swallowed into it. Python then sees either

      ['--out-dir', '...\\\\dest Test" --org Test', 'Org']       (case A)
      ['--out-dir', '...\\\\dest Test"']                          (case B)

    A literal ``"`` can never appear in a valid Windows path, so its presence is
    a reliable marker of this mangling. We split the damaged token at the first
    ``"`` (restoring the swallowed flags) and re-join value parts that were split
    at a space so ``--org "Test Org"`` is recovered whole. Returns (argv, note)
    where note is a non-empty warning string when a repair was applied.
    """
    if not any('"' in a for a in argv):
        return argv, ''

    flat = []
    for tok in argv:
        if '"' not in tok:
            flat.append(tok)
            continue
        idx = tok.index('"')
        flat.append(tok[:idx])           # the real value before the stray quote
        rest = tok[idx + 1:].strip()     # swallowed flags/values after it
        if rest:
            flat.extend(rest.split())

    out = []
    i, n = 0, len(flat)
    while i < n:
        tok = flat[i]
        out.append(tok)
        if tok in _VALUE_FLAGS and i + 1 < n:
            parts = []
            j = i + 1
            while j < n and not flat[j].startswith('-'):
                parts.append(flat[j])
                j += 1
            if parts:
                out.append(' '.join(parts))
                i = j
                continue
        i += 1
    return out, ('NOTE: a stray double-quote in the arguments was repaired '
                 'automatically. PowerShell mangles quoted paths that end in a '
                 'backslash; use forward slashes (/) or drop the trailing '
                 'backslash to avoid this.')


def _check_for_update():
    """Best-effort, non-blocking version check against PyPI.

    Returns a list of warning lines (possibly empty) when a newer version of
    `sf-profile-pruner` exists. Any network/parse failure silently returns no
    warning, so startup is never blocked (bounded ~2s timeout)."""
    import urllib.request
    try:
        req = urllib.request.Request(
            'https://pypi.org/pypi/sf-profile-pruner/json',
            headers={'User-Agent': 'sf-profile-pruner/%s' % __version__})
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        latest = str((data.get('info') or {}).get('version') or '')
    except Exception:
        return []
    if not latest or latest == __version__ or not version_ge(latest, __version__):
        return []
    return [
        'A newer version is available: %s (installed: %s).' % (latest, __version__),
        'Update with:  python -m pip install --upgrade --upgrade-strategy eager '
        'sf-profile-pruner',
    ]


def _confirm(prompt):
    """Ask a Y/N confirmation on screen. Returns True only on an explicit yes.

    Any non-interactive input (EOF) or Ctrl-C counts as 'no', so a destructive
    operation never proceeds without a human answering yes."""
    try:
        sys.stdout.write(prompt)
        sys.stdout.flush()
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ''
    return answer in ('y', 'yes')


def _deploy_profile(sf, org, profile_path, profile_name, api_version,
                    poll_interval, job_timeout, logger, out_dir, mdtype='Profile'):
    """Deploy `profile_path` to `org` under `profile_name`.

    Salesforce creates the Profile/PermissionSet if the name does not exist and
    merges into it otherwise, so a renamed deploy is exactly "create if missing".
    A fresh micro-project is created under `.work/deploy` and removed afterwards.
    """
    proj = os.path.join(out_dir, '.work', 'deploy')
    try:
        write_sfdx_project(proj, api_version)
        manifest = os.path.join(proj, 'manifest.xml')
        write_package_xml(manifest, {meta_manifest(mdtype): [profile_name]}, api_version)
        dst = os.path.join(proj, 'force-app', 'main', 'default',
                           meta_folder(mdtype), meta_filename(mdtype, profile_name))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(profile_path, dst)
        return deploy(sf, manifest, org, poll_interval, job_timeout, logger, cwd=proj)
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def _write_and_report_manual_ops(manual_ops, out_dir, profile_name, logger, quiet,
                                 mdtype='Profile'):
    """Write MANUAL-OPERATIONS.txt and, when there is work to do by hand, print
    the table at the end of the run. Returns the file path, or None when there
    are no manual operations."""
    if not manual_ops:
        logger.log('No manual operations required (every removed/off section is deploy-safe).')
        return None
    path = write_manual_ops(os.path.join(out_dir, 'MANUAL-OPERATIONS.txt'),
                            manual_ops, profile_name, mdtype=mdtype)
    logger.log('')
    logger.log('========================================================')
    logger.log('  MANUAL OPERATIONS REQUIRED (no deploy-able "off" value)')
    logger.log('========================================================')
    if not quiet:
        print('')
        print('  MANUAL OPERATIONS REQUIRED FOR A COMPLETE ALIGNMENT')
        print('  ' + '-' * 68)

        redundant = [o for o in manual_ops
                     if (o.get('verdict') or '').startswith('REDUNDANT')]
        check = [o for o in manual_ops
                 if (o.get('verdict') or '') == 'NEEDS CHECK']
        other = [o for o in manual_ops
                 if (o.get('verdict') or '') not in ('REDUNDANT (ignorable)', 'NEEDS CHECK')]

        if redundant:
            print('  REDUNDANT (ignorable) (%d)' % len(redundant))
        if check:
            print('  NEEDS CHECK (%d)' % len(check))
        if other:
            print('  OTHER MANUAL OPERATIONS (%d)' % len(other))
        print('  Full list saved to: %s' % _rel(out_dir, path))
    logger.log('Manual operations file: %s' % _rel(out_dir, path))
    logger.log('========================================================')
    return path


def _finalize_prune_output(sf, result, merge_dir, src_out, original_copy,
                           dest_profile_name, dest_org, logger, quiet,
                           mdtype='Profile', clean_path=None, preserved=None):
    """Assemble the ``4-merged/`` folder for a plain prune run: copy the clean
    profile, write ``what-is-changed.txt`` (removed dangling references,
    preserved destination-only entries, normalized read-only fields, and manual
    operations) and ``MANUAL-OPERATIONS.txt``. Returns the what-is-changed path.
    ``clean_path`` overrides the clean source path (used when a cross-type
    conversion or a preserve-destination merge produced the final deliverable).

    ``preserved`` is the ``{section: [member, ...]}`` map from
    :func:`dest_only_members` (every destination-only entry kept with the
    destination value - both metadata types, non-mixed plain prune).

    ``layoutAssignments`` manual operations are classified REDUNDANT (ignorable)
    vs NEEDS CHECK here too (via ``classify_layout_ops``), so a plain prune
    reports the same ignorable/check split as the alignment flow."""
    clean_path = clean_path or result['common_profile_path']
    os.makedirs(merge_dir, exist_ok=True)
    final_profile = os.path.join(merge_dir, meta_filename(mdtype, dest_profile_name))
    shutil.copyfile(clean_path, final_profile)
    normalize_xml_declaration(final_profile)
    # Manual ops reflect what the PRUNE removed from the source (not the
    # cross-type conversion drops, which are inherent to the type boundary).
    manual_ops = removed_non_alignable(original_copy, result['common_profile_path'])
    # Read-only field editable flags rewritten by the normalization pass: list
    # them so the what-is-changed report shows every ACTUAL change (not just the
    # dangling references removed).
    normalized = normalized_field_changes(original_copy, clean_path)
    # Classify layoutAssignments removals (single-layout = redundant/ignorable,
    # multi-layout = needs check) so both reports and the console split them.
    if any(op.get('section') == 'layoutAssignments' for op in manual_ops):
        layout_names, lerr = list_metadata(sf, dest_org, 'Layout')
        layout_count = {}
        if layout_names:
            for name in layout_names:
                obj = name.split('-', 1)[0] if '-' in name else name
                layout_count[obj] = layout_count.get(obj, 0) + 1
        elif lerr:
            logger.log('  WARNING: could not list Layout metadata (%s); '
                       'layoutAssignments marked for manual check.' % lerr)
        classify_layout_ops(manual_ops, [], layout_count)
    removed_by_type = {}
    missing_log = os.path.join(src_out, 'missing_log.txt')
    for typ, member in load_log_entries(missing_log):
        removed_by_type.setdefault(typ, set()).add(member)
    wic_path = write_prune_what_is_changed(
        os.path.join(merge_dir, 'what-is-changed.txt'),
        removed_by_type, manual_ops, dest_org, normalized, preserved)
    _write_and_report_manual_ops(manual_ops, merge_dir, dest_profile_name,
                                 logger, quiet, mdtype=mdtype)
    return wic_path


def _convert_prune_deliverable(sf, args, src_mdtype, dest_mdtype,
                               clean_source_path, dest_profile_name,
                               dest_org, out_dir, logger):
    """Cross-type plain prune: convert the clean source into the destination
    metadata type and return the converted deliverable path (written to a
    temporary work area - `_finalize_prune_output` copies it into 4-merged).

    Profile -> PermissionSet: portable content is re-materialised as a PS
    (tabs mapped, Profile-only sections dropped, a `label` synthesised).
    PermissionSet -> Profile: the PS content becomes a Profile; the destination
    Profile's own Profile-only sections are retrieved and kept verbatim so they
    remain present and unchanged."""
    work_dir = os.path.join(out_dir, '.work')
    os.makedirs(work_dir, exist_ok=True)

    dst_exclusive_path = None
    if dest_mdtype == 'Profile':
        username, uerr = org_username(sf, dest_org)
        if not username:
            print('ERROR: could not resolve the destination org %r: %s' % (dest_org, uerr))
            sys.exit(2)
        dst_profiles, derr = retrieve_profiles(username, [dest_profile_name],
                                               clean=not args.full_profile,
                                               mdtype='Profile')
        if not dst_profiles:
            _warn_destination_missing(
                logger, args.quiet, 'Profile', dest_profile_name, dest_org,
                'its Profile-only sections cannot be preserved (left empty)')
        else:
            dst_exclusive_path = os.path.join(work_dir, '.dest-exclusive.profile-meta.xml')
            with open(dst_exclusive_path, 'wb') as _f:
                _f.write(dst_profiles[0][1])

    xml, notes = convert_metadata_type(clean_source_path, dst_exclusive_path,
                                       label=dest_profile_name)
    # Write to .work (NOT 4-merged): the finalizer will copy this into
    # 4-merged/<name>, so a same-path copyfile would raise SameFileError.
    out_path = os.path.join(work_dir, 'converted-%s' % meta_filename(dest_mdtype, dest_profile_name))
    with open(out_path, 'wb') as _f:
        _f.write(xml)
    logger.log('  - cross-type conversion: %s -> %s' % (src_mdtype, dest_mdtype))
    for note in notes:
        logger.log('    - %s' % note)
    if dst_exclusive_path and os.path.isfile(dst_exclusive_path):
        os.remove(dst_exclusive_path)
    return out_path


def _validate_and_heal_deliverable(sf, args, deliverable_path, profile_name,
                                   dest_mdtype, dest_org, out_dir, logger):
    """Final gate for a plain-prune deliverable.

    Dry-runs the deliverable AS the destination metadata type and, when it is
    not clean, self-heals it (per-section prune + final dry-run loop, which
    resolves and retries) exactly like the same-type prune's built-in final
    gate. Returns the path of the final, validated deliverable.

    This closes the cross-type gap: a Profile<->PermissionSet conversion was
    previously written to 4-merged WITHOUT ever being validated, so an invalid
    deliverable could exit 0."""
    validation, _cf = _validate_profile_dry_run(
        sf, dest_org, deliverable_path, profile_name, args.api_version,
        args.poll_interval, args.job_timeout, logger, out_dir, mdtype=dest_mdtype)
    if validation == 'clean':
        logger.log('  OK: final deliverable dry-run clean')
        return deliverable_path

    logger.log('  WARNING: final deliverable dry-run not clean (%s); self-healing...'
               % validation)
    heal_out = os.path.join(out_dir, '.work', 'deliverable-heal')
    heal_res = prune_stage(
        sf=sf, org=dest_org, source_profile_path=deliverable_path,
        profile_name=profile_name, out_dir=heal_out,
        api_version=args.api_version, poll_interval=args.poll_interval,
        job_timeout=args.job_timeout, thread_number=args.thread_number,
        batch_size=args.batch_size, max_iter=args.max_iter,
        quiet=args.quiet, fresh=True, logger=logger,
        io_threads=args.io_threads_number)
    if heal_res['outcome'] != 'SUCCESS':
        print('ERROR: final deliverable self-heal failed: %s'
              % (heal_res.get('reason') or 'unknown error'))
        sys.exit(2)
    final_path = os.path.join(out_dir, '.work',
                              'deliverable-final-%s'
                              % meta_filename(dest_mdtype, profile_name))
    os.makedirs(os.path.dirname(final_path), exist_ok=True)
    shutil.copyfile(heal_res['common_profile_path'], final_path)
    shutil.rmtree(heal_out, ignore_errors=True)
    logger.log('  OK: final deliverable self-healed (%d reference(s) removed)'
               % heal_res['total_removed'])
    return final_path


def _retrieve_pruned_dest(sf, args, mdtype, dest_profile_name, dest_org,
                          out_dir, logger):
    """Retrieve the destination Profile/PermissionSet and prune it against
    itself (drop its own dangling references). Returns ``(dst_clean_path,
    dest_missing)``. Shared by the PermissionSet preserve-merge and the
    Profile ignore-patch."""
    username, uerr = org_username(sf, dest_org)
    if not username:
        print('ERROR: could not resolve the destination org %r: %s' % (dest_org, uerr))
        sys.exit(2)
    dest_dir = os.path.join(out_dir, '2-destination-retrieved')
    os.makedirs(dest_dir, exist_ok=True)
    logger.log('  - retrieving destination %s %r from %s (preserve only)...'
               % (mdtype, dest_profile_name, dest_org))
    clean = not args.full_profile
    dest_profiles, derr = retrieve_profiles(username, [dest_profile_name],
                                            clean=clean, mdtype=mdtype)
    dest_missing = not dest_profiles
    if dest_missing:
        _warn_destination_missing(
            logger, args.quiet, mdtype, dest_profile_name, dest_org,
            'nothing to preserve (it will be created from the source)')
    dest_raw = os.path.join(dest_dir, meta_filename(mdtype, dest_profile_name))
    with open(dest_raw, 'wb') as _f:
        if dest_missing:
            _f.write(_empty_metadata_xml(mdtype, dest_profile_name))
        else:
            _f.write(dest_profiles[0][1])

    dst_out = os.path.join(out_dir, '3-destination-pruned')
    logger.log('  - pruning the destination against itself...')
    dst_res = prune_stage(
        sf=sf, org=dest_org, source_profile_path=dest_raw,
        profile_name=dest_profile_name, out_dir=dst_out,
        api_version=args.api_version, poll_interval=args.poll_interval,
        job_timeout=args.job_timeout, thread_number=args.thread_number,
        batch_size=args.batch_size, max_iter=args.max_iter,
        quiet=args.quiet, fresh=args.fresh, logger=logger,
        io_threads=args.io_threads_number)
    if dst_res['outcome'] != 'SUCCESS':
        print('ERROR: destination prune failed: %s'
              % (dst_res.get('reason') or 'unknown error'))
        sys.exit(2)
    return dst_res['common_profile_path'], dest_missing


def _preserve_dest_merge(sf, args, src_clean, dest_profile_name, dest_org,
                         out_dir, logger, mdtype='PermissionSet'):
    """Plain prune, destination-preserving merge for MIXED and NON-MIXED runs
    alike: ``src_clean`` is already in the destination metadata type (a
    cross-type source was converted upstream), and the destination is merged
    UNDER it with ``preserve_dest_only=True``.

    The plain deliverable must state EVERY entry the destination values: a
    PermissionSet deploy is authoritative (anything absent from the file is
    REVOKED), and a Profile deploy would otherwise leave destination-only
    entries implicit. The destination is retrieved, pruned against itself
    (drop its own dangling references) and merged: destination-only entries are
    kept VERBATIM with the destination value (never turned off, never revoked).
    Returns ``(final_deliverable_path, preserved_members)`` where
    ``preserved_members`` is the ``{section: [member, ...]}`` map from
    :func:`dest_only_members`.
    """
    dst_clean, dest_missing = _retrieve_pruned_dest(
        sf, args, mdtype, dest_profile_name, dest_org, out_dir, logger)

    merged_xml, _manual_ops, _changes = merge_profiles(
        src_clean, dst_clean, dest_env=dest_org, preserve_dest_only=True)
    merge_path = os.path.join(
        out_dir, '.work', 'preserved-%s'
        % meta_filename(mdtype, dest_profile_name))
    os.makedirs(os.path.dirname(merge_path), exist_ok=True)
    with open(merge_path, 'wb') as _f:
        _f.write(merged_xml)

    preserved = dest_only_members(src_clean, dst_clean)
    final_path = _validate_and_heal_deliverable(
        sf, args, merge_path, dest_profile_name, mdtype, dest_org,
        out_dir, logger)
    return final_path, preserved


def _normalize_prune_read_only(sf, args, src_org, dest_org, common_path, logger):
    """Normalize system read-only field ``editable`` flags in a pruned (plain,
    non-merged) profile unless ``--avoid-normalize-ro`` is set. Detects read-only
    fields by DataType (formula / roll-up summary / auto number / compound
    address & location) plus - when a source org is available - the
    ``sobject describe`` system-read-only pass, then forces ``editable=false``
    in place. Returns the number of fields normalized."""
    if args.avoid_normalize_ro:
        logger.log('  - read-only normalization disabled (--avoid-normalize-ro): editable flags kept as-is.')
        return 0
    read_only = _read_only_field_set(sf, src_org, dest_org, common_path, None, logger,
                                     io_threads=args.io_threads_number)
    if not read_only:
        return 0
    n = normalize_profile_read_only(common_path, read_only)
    if n:
        logger.log('  OK: normalized %d read-only field(s) editable=false in the clean profile.' % n)
    return n


def _align_read_only_fields(args, sf, src_org, dest_org, src_clean, dst_clean, logger):
    """Return the read-only field set for an alignment merge, honoring
    ``--avoid-normalize-ro`` (empty set when the flag is present, so the merge
    leaves every ``editable`` flag exactly as retrieved)."""
    if args.avoid_normalize_ro:
        logger.log('  - read-only normalization disabled (--avoid-normalize-ro): editable flags kept as-is.')
        return set()
    return _read_only_field_set(sf, src_org, dest_org, src_clean, dst_clean, logger,
                                io_threads=args.io_threads_number)


def _validate_profile_dry_run(sf, org, profile_path, profile_name, api_version,
                              poll_interval, job_timeout, logger, out_dir,
                              mdtype='Profile'):
    """Dry-run a single Profile/PermissionSet package. Returns (validation,
    component_failures) where ``validation`` is ``'clean'`` on success or a
    failure description, and ``component_failures`` is the list of raw
    component-failure dicts (empty on clean)."""
    proj = os.path.join(out_dir, '.work', 'align-final')
    write_sfdx_project(proj, api_version)
    manifest = os.path.join(proj, 'manifest.xml')
    write_package_xml(manifest, {meta_manifest(mdtype): [profile_name]}, api_version)
    dst = os.path.join(proj, 'force-app', 'main', 'default',
                       meta_folder(mdtype), meta_filename(mdtype, profile_name))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(profile_path, dst)
    try:
        vres, verr = dry_run(sf, manifest, org, poll_interval, job_timeout,
                             logger, cwd=proj)
    finally:
        shutil.rmtree(proj, ignore_errors=True)
    if vres is None:
        return 'FAILED: %s' % (verr or 'unparseable output'), []
    result = vres.get('result') or {}
    if result.get('success') is True:
        return 'clean', []
    cf = (result.get('details') or {}).get('componentFailures') or []
    probs = '; '.join(str(f.get('problem', '')) for f in cf[:5])
    return 'FAILED (%d failure(s)): %s' % (len(cf), probs), cf


ALIGN_STATE_FILE = 'align-state.json'


def _write_align_state(out_dir, args, src_profile_name, dest_profile_name,
                       dest_org, merged_path, validation, n_granted, n_revoked,
                       n_layout, n_manual, src_mdtype='Profile', dest_mdtype='Profile'):
    """Persist the run identity + validated merged profile of a completed
    alignment, so a later invocation on the same folder can deploy the cached
    merge instead of redoing the whole pipeline."""
    state = {
        'version': 1,
        'tool_version': __version__,
        'src_org': args.src_org,
        'dest_org': dest_org,
        'src_profile': src_profile_name,
        'dest_profile': dest_profile_name,
        'src_mdtype': src_mdtype,
        'dest_mdtype': dest_mdtype,
        'api_version': args.api_version,
        'merged_profile': os.path.relpath(merged_path, out_dir),
        'validation': validation,
        'granted': n_granted,
        'revoked': n_revoked,
        'layout_changed': n_layout,
        'manual': n_manual,
        'completed_at': time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime()),
    }
    path = os.path.join(out_dir, ALIGN_STATE_FILE)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    return path


def _load_align_state(out_dir):
    path = os.path.join(out_dir, ALIGN_STATE_FILE)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _align_resume_summary(out_dir, state):
    """Per-phase summary of a completed alignment, read back from the run
    folder's artifacts so a resume prints what each phase already did (instead
    of just "this folder is done"). Returns a list of log lines."""
    src_report = os.path.join(out_dir, '1-source-pruned', 'report.txt')
    dst_report = os.path.join(out_dir, '3-destination-pruned', 'report.txt')
    src = read_report_summary(src_report)
    dst = read_report_summary(dst_report)

    def _phase1():
        if src.get('outcome') is None and src.get('removed') is None:
            return '  PHASE 1/4 / PRUNE SOURCE: not recorded in %s' % _rel(out_dir, src_report)
        return '  PHASE 1/4 / PRUNE SOURCE: %s (%d reference(s) removed)' % (
            src.get('outcome') or '?', src.get('removed') or 0)

    def _phase2():
        if dst.get('outcome') is None and dst.get('removed') is None:
            return '  PHASE 2/4 / PRUNE DESTINATION: not recorded in %s' % _rel(out_dir, dst_report)
        return '  PHASE 2/4 / PRUNE DESTINATION: %s (%d reference(s) removed)' % (
            dst.get('outcome') or '?', dst.get('removed') or 0)

    return [
        'Completed phases (from the previous run):',
        _phase1(),
        _phase2(),
        '  PHASE 3/4 / MERGE: %d granted, %d revoked, %d layout changed, %d manual' % (
            state.get('granted', 0), state.get('revoked', 0),
            state.get('layout_changed', 0), state.get('manual', 0)),
        '  PHASE 4/4 / VALIDATE: %s' % (state.get('validation') or '?'),
    ]


def _resume_alignment(args, src_org, src_profile_name, dest_profile_name,
                      dest_org, do_deploy, out_dir, logger, sf,
                      src_mdtype='Profile', dest_mdtype='Profile'):
    """Resume an alignment whose folder already holds a completed, validated
    merge. Returns an exit code when the invocation is handled here (deploy the
    cached merge, report done, or fail on a parameter mismatch), or None to run
    the full pipeline. ``--fresh`` always returns None (restart from scratch)."""
    state = _load_align_state(out_dir)
    if state is None or state.get('version') != 1:
        return None
    if args.fresh:
        return None

    saved = {
        'src_org': state.get('src_org'),
        'dest_org': state.get('dest_org'),
        'src_profile': state.get('src_profile'),
        'dest_profile': state.get('dest_profile'),
        # Missing mdtype in a pre-PermissionSet align-state means a Profile run.
        'src_mdtype': state.get('src_mdtype') or 'Profile',
        'dest_mdtype': state.get('dest_mdtype') or 'Profile',
    }
    current = {
        'src_org': src_org,
        'dest_org': dest_org,
        'src_profile': src_profile_name,
        'dest_profile': dest_profile_name,
        'src_mdtype': src_mdtype,
        'dest_mdtype': dest_mdtype,
    }
    mismatches = sorted(k for k in saved if saved[k] != current.get(k))
    if mismatches:
        print('ERROR: this output folder already contains a completed alignment run')
        print('       whose parameters do not match the current invocation:')
        for k in mismatches:
            print('         %s: previous run = %r, now = %r' % (k, saved[k], current.get(k)))
        print('       Use --fresh to discard the previous run and start over, or')
        print('       point --out-dir (-od) at the folder of the run you meant.')
        return 2

    merged_rel = state.get('merged_profile') or os.path.join(
        '4-merged', meta_filename(dest_mdtype, dest_profile_name))
    merged_path = os.path.normpath(os.path.join(out_dir, merged_rel))
    if not os.path.isfile(merged_path):
        print('ERROR: a completed alignment run was found in this folder, but its')
        print('       merged profile is missing: %s' % merged_path)
        print('       Use --fresh to restart from scratch.')
        return 2

    api_version = state.get('api_version') or args.api_version
    logger.log('Resuming a completed alignment (folder %s).' % out_dir)
    logger.log('  src_org      : %s' % (src_org or '(profile file)'))
    logger.log('  dest_org     : %s' % dest_org)
    logger.log('  src_profile  : %s' % src_profile_name)
    logger.log('  dest_profile : %s' % dest_profile_name)
    logger.log('  merged       : %s' % _rel(out_dir, merged_path))
    logger.log('')
    for line in _align_resume_summary(out_dir, state):
        logger.log(line)

    if not do_deploy:
        print('This folder already contains a completed, validated alignment.')
        print('Merged profile: %s' % _rel(out_dir, merged_path))
        print('Re-run with --deploy-on-success [--sure] to deploy it (asks Y/N '
              'unless --sure).')
        return 0

    logger.log('Deploying the cached merged profile %r to %s...'
               % (dest_profile_name, dest_org))
    ok, detail = _deploy_profile(sf, dest_org, merged_path, dest_profile_name,
                                 api_version, args.poll_interval, args.job_timeout,
                                 logger, out_dir, mdtype=dest_mdtype)
    if ok:
        logger.log('DEPLOY: %s' % detail)
        logger.log('RESULT:SUCCESS')
        print('Deployed the cached merged profile to %s.' % dest_org)
        return 0
    print('ERROR: deploy of the cached merged profile failed: %s' % detail)
    logger.log('DEPLOY: %s' % detail)
    logger.log('RESULT:DEPLOY_FAILED')
    return 1


def _run_diff(sf, args, out_dir, source_profile_path, src_profile_name,
              dest_profile_name, dest_org, src_org, src_mdtype, dest_mdtype,
              logger, ignore=None):
    """Run --diff: retrieve the destination profile/permission set and compare
    the two section by section (pure comparison; no prune, dry-run or deploy).
    Writes ``diff/diff-report.txt`` and prints a summary table. Exits with 0."""
    logger.log('')
    logger.log('-- DIFF -----------------------------------------------')
    logger.log('  purpose: compare the source against the destination')
    if src_mdtype != dest_mdtype:
        logger.log('')
        logger.log('  WARNING: MIXED COMPARISON: %s vs %s' % (src_mdtype, dest_mdtype))
        logger.log('    tab visibility is mapped between the two vocabularies')
        logger.log('    (DefaultOn<->Visible, DefaultOff<->Available, Hidden<->None).')
        logger.log('    Profile-only sections (%s) have no PermissionSet' %
                   ', '.join(PROFILE_EXCLUSIVE_SECTIONS))
        logger.log('    counterpart, so they cannot be compared symmetrically.')

    username, uerr = org_username(sf, dest_org)
    if not username:
        print('ERROR: could not resolve the destination org %r: %s' % (dest_org, uerr))
        sys.exit(2)
    clean = not args.full_profile
    logger.log('  - retrieving destination %s %r from %s (%s)...'
               % (dest_mdtype, dest_profile_name, dest_org, 'full' if args.full_profile else 'clean'))
    dest_profiles, derr = retrieve_profiles(username, [dest_profile_name], clean=clean,
                                            mdtype=dest_mdtype)
    dest_missing = not dest_profiles
    if dest_missing:
        _warn_destination_missing(
            logger, args.quiet, dest_mdtype, dest_profile_name, dest_org,
            'the comparison will treat it as empty')

    diff_dir = os.path.join(out_dir, 'diff')
    os.makedirs(diff_dir, exist_ok=True)
    dest_path = os.path.join(diff_dir, meta_filename(dest_mdtype, dest_profile_name))
    with open(dest_path, 'wb') as _f:
        if dest_missing:
            _f.write(_empty_metadata_xml(dest_mdtype, dest_profile_name))
        else:
            _f.write(dest_profiles[0][1])

    src_label = '%s %s "%s"' % (src_mdtype, src_org or 'file', src_profile_name)
    dst_label = '%s %s "%s"' % (dest_mdtype, dest_org, dest_profile_name)
    sections = diff_profiles(source_profile_path, dest_path, ignore=ignore)
    report_text = render_diff_report(sections, src_label, dst_label)
    report_path = os.path.join(diff_dir, 'diff-report.txt')
    with open(report_path, 'w', encoding='utf-8') as _f:
        _f.write(report_text + '\n')
    json_path = write_diff_json(sections, src_label, dst_label, diff_dir)
    xml_path = write_diff_xml(sections, src_label, dst_label, diff_dir)
    if dest_missing:
        logger.log('  OK: destination treated as empty (%s missing)' % dest_mdtype)
    else:
        logger.log('  OK: destination retrieved (%d bytes)' % len(dest_profiles[0][1]))
    logger.log('  OK: diff report written: %s' % _rel(out_dir, report_path))
    logger.log('  OK: diff data (JSON):     %s' % _rel(out_dir, json_path))
    logger.log('  OK: diff data (XML):      %s' % _rel(out_dir, xml_path))

    # Screen output: only the summary counts + the paths of the three files.
    # The full per-entry detail lives in the files (diff-report.txt has the
    # human-readable detail; diff-data.json/.xml are machine-readable).
    print('')
    print(render_diff_summary(sections, src_label, dst_label))
    print('')
    print('Files (full detail + machine-readable data):')
    print('  report : %s' % _rel(out_dir, report_path))
    print('  JSON   : %s' % _rel(out_dir, json_path))
    print('  XML    : %s' % _rel(out_dir, xml_path))
    print('')
    logger.log('RESULT:SUCCESS')
    sys.exit(0)


def _run_alignment(sf, args, out_dir, source_profile_path, src_profile_name,
                   dest_profile_name, dest_org, src_org, src_mdtype, dest_mdtype,
                   do_deploy, logger, ignore=None):
    """Run --align-from-source: prune the source against the destination,
    retrieve + prune the destination, merge (source authoritative, destination-
    only entries turned off), and optionally deploy. Exits with the result code."""
    # 0. mixed-type warning: what the cross-type mapping does and does not carry.
    if src_mdtype != dest_mdtype:
        logger.log('')
        logger.log('  WARNING: MIXED-TYPE ALIGNMENT: %s -> %s' % (src_mdtype, dest_mdtype))
        logger.log('    tab visibility is mapped between the two vocabularies')
        logger.log('    (DefaultOn<->Visible, DefaultOff<->Available, Hidden<->None);')
        logger.log('    the merged file always uses the destination schema.')
        if dest_mdtype == 'PermissionSet':
            logger.log('    Profile-only sections are NOT carried into the PermissionSet:')
            logger.log('    %s.' % ', '.join(PROFILE_EXCLUSIVE_SECTIONS))
        else:
            logger.log('    the source PermissionSet has no Profile-only sections')
            logger.log('    (%s); the destination Profile keeps' %
                       ', '.join(PROFILE_EXCLUSIVE_SECTIONS))
            logger.log('    its own values there (destination-only entries are reported')
            logger.log('    as manual operations).')

    # 1. prune the source against the destination (drop source references that
    #    do not exist in the destination org).
    src_out = os.path.join(out_dir, '1-source-pruned')
    logger.log('')
    logger.log('-- PHASE 1/4 / PRUNE SOURCE ---------------------------')
    logger.log('  purpose: drop every reference in %r that does not exist in %s'
               % (src_profile_name, dest_org))
    logger.log('  - pruning the source profile against %s...' % dest_org)
    src_res = prune_stage(
        sf=sf, org=dest_org, source_profile_path=source_profile_path,
        profile_name=src_profile_name, out_dir=src_out,
        api_version=args.api_version, poll_interval=args.poll_interval,
        job_timeout=args.job_timeout, thread_number=args.thread_number,
        batch_size=args.batch_size, max_iter=args.max_iter,
        quiet=args.quiet, fresh=args.fresh, logger=logger,
        io_threads=args.io_threads_number, ignore=ignore)
    if src_res['outcome'] != 'SUCCESS':
        print('ERROR: source prune failed: %s' % (src_res.get('reason') or 'unknown error'))
        sys.exit(2)
    src_clean = src_res['common_profile_path']
    logger.log('  OK: source pruned (%d reference(s) removed)' % src_res['total_removed'])

    # 2. retrieve the destination profile (same clean/full choice as the source).
    logger.log('')
    logger.log('-- PHASE 2/4 / RETRIEVE + PRUNE DESTINATION -----------')
    logger.log('  purpose: get a copy of the destination profile and strip its own dangling')
    logger.log('           references, so the merge starts from two valid profiles')
    username, uerr = org_username(sf, dest_org)
    if not username:
        print('ERROR: could not resolve the destination org %r: %s' % (dest_org, uerr))
        sys.exit(2)
    dest_dir = os.path.join(out_dir, '2-destination-retrieved')
    os.makedirs(dest_dir, exist_ok=True)
    logger.log('  - retrieving the destination %s %r from %s...'
               % (dest_mdtype, dest_profile_name, dest_org))
    clean = not args.full_profile
    dest_profiles, derr = retrieve_profiles(username, [dest_profile_name], clean=clean,
                                            mdtype=dest_mdtype)
    dest_missing = not dest_profiles
    if dest_missing:
        _warn_destination_missing(
            logger, args.quiet, dest_mdtype, dest_profile_name, dest_org,
            'alignment will create it from the source')
    dest_raw = os.path.join(dest_dir, meta_filename(dest_mdtype, dest_profile_name))
    with open(dest_raw, 'wb') as _f:
        if dest_missing:
            _f.write(_empty_metadata_xml(dest_mdtype, dest_profile_name))
        else:
            _f.write(dest_profiles[0][1])
    if dest_missing:
        logger.log('  OK: destination treated as empty (%s missing)' % dest_mdtype)
    else:
        logger.log('  OK: destination retrieved (%d bytes)' % len(dest_profiles[0][1]))

    # 2b. PS -> PS license guard: a PermissionSet's license binds which
    # userPermissions are valid/mandated for it AND is immutable once the set
    # exists (Salesforce rejects a change with "License can't be updated").
    # Source-authoritative align would overwrite the destination license and
    # then fail the deploy. Block by default; --force-license-mismatch warns
    # (non-blocking) and either creates a NEW PermissionSet (--new-ps-name) or
    # proceeds to a dry-run that will fail with "License can't be updated".
    if src_mdtype == 'PermissionSet' and dest_mdtype == 'PermissionSet':
        src_license = metadata_license(source_profile_path)
        if dest_missing:
            if args.new_ps_name:
                print('ERROR: --new-ps-name was given but the destination '
                      'PermissionSet does not exist yet; -dps already creates it '
                      'under that name.')
                sys.exit(2)
        else:
            dst_license = metadata_license(dest_raw)
            if license_mismatch(src_license, dst_license):
                if not args.force_license_mismatch:
                    print('ERROR: PermissionSet license mismatch: source declares %r but '
                          'the destination %r declares %r.'
                          % (src_license, dest_profile_name, dst_license))
                    print('       A PermissionSet license cannot be updated once it exists, '
                          'and it binds which userPermissions are valid for the set: '
                          'aligning source-authoritatively would overwrite the destination '
                          'license and the deploy would fail with "License can\'t be '
                          'updated".')
                    print('       To change the license, create a NEW PermissionSet: re-run '
                          'with --force-license-mismatch --new-ps-name "NEW_NAME".')
                    sys.exit(2)
                _warn_license_mismatch(logger, args.quiet, src_license, dst_license,
                                       dest_profile_name)
                if args.new_ps_name:
                    new_name = args.new_ps_name.strip()
                    ok, why = _validate_ps_api_name(new_name)
                    if not ok:
                        print('ERROR: invalid --new-ps-name %r: %s.' % (new_name, why))
                        sys.exit(2)
                    if _metadata_exists(sf, dest_org, dest_mdtype, new_name):
                        print('ERROR: --new-ps-name %r already exists in org %s.'
                              % (new_name, dest_org))
                        print('       The new PermissionSet name must not already exist in the '
                              'destination org.')
                        sys.exit(2)
                    logger.log('  OK: creating a NEW PermissionSet %r (license %r) instead of '
                               'overwriting %r (license %r).'
                               % (new_name, src_license, dest_profile_name, dst_license))
                    dest_profile_name = new_name
                    dest_missing = True
                    dest_raw = os.path.join(dest_dir,
                                            meta_filename(dest_mdtype, dest_profile_name))
                    with open(dest_raw, 'wb') as _f:
                        _f.write(_empty_metadata_xml(dest_mdtype, dest_profile_name))
            elif args.new_ps_name:
                print('ERROR: --new-ps-name was given but the source and destination '
                      'PermissionSet licenses already match (%r); no new PermissionSet '
                      'is needed.' % src_license)
                sys.exit(2)
    elif args.new_ps_name:
        print('ERROR: --new-ps-name is only valid for a PermissionSet -> PermissionSet '
              'alignment.')
        sys.exit(2)

    # 3. prune the destination against itself (drop its own dangling references).
    dst_out = os.path.join(out_dir, '3-destination-pruned')
    logger.log('  - pruning the destination profile against itself...')
    dst_res = prune_stage(
        sf=sf, org=dest_org, source_profile_path=dest_raw,
        profile_name=dest_profile_name, out_dir=dst_out,
        api_version=args.api_version, poll_interval=args.poll_interval,
        job_timeout=args.job_timeout, thread_number=args.thread_number,
        batch_size=args.batch_size, max_iter=args.max_iter,
        quiet=args.quiet, fresh=args.fresh, logger=logger,
        io_threads=args.io_threads_number)
    if dst_res['outcome'] != 'SUCCESS':
        print('ERROR: destination prune failed: %s' % (dst_res.get('reason') or 'unknown error'))
        sys.exit(2)
    dst_clean = dst_res['common_profile_path']
    logger.log('  OK: destination pruned (%d reference(s) removed)' % dst_res['total_removed'])

    # 4. merge: source authoritative; destination-only entries turned off.
    logger.log('')
    logger.log('-- PHASE 3/4 / MERGE (source authoritative) -----------')
    logger.log('  purpose: keep every source entry verbatim and turn OFF every destination-only entry')
    logger.log('  - merging source -> destination...')
    if not src_org:
        logger.log('  WARNING: NOTE (-pp file source): the source has no org to describe, so read-only '
                   'detection checks only the destination. A field editable in the source but '
                   'read-only-looking in the destination could be misclassified as read-only; '
                   'use -so/-sp to verify both sides.')
    read_only_fields = _align_read_only_fields(
        args, sf, src_org, dest_org, src_clean, dst_clean, logger)
    merged_xml, manual_ops, changes = merge_profiles(
        src_clean, dst_clean, dest_env=dest_org, read_only_fields=read_only_fields,
        preserve_dest_label=dest_missing, ignore=ignore)
    merge_dir = os.path.join(out_dir, '4-merged')
    os.makedirs(merge_dir, exist_ok=True)
    merged_path = os.path.join(merge_dir, meta_filename(dest_mdtype, dest_profile_name))
    with open(merged_path, 'wb') as _f:
        _f.write(merged_xml)

    # 5. validate the merged profile BEFORE writing any report. If the dry-run
    #    surfaces residual dangling references, self-heal by re-pruning the
    #    merged profile and recomputing the diff, so MANUAL-OPERATIONS.txt and
    #    what-is-changed.txt always describe the FINAL (deployable) profile.
    logger.log('')
    logger.log('-- PHASE 4/4 / VALIDATE + DEPLOY ----------------------')
    logger.log('  purpose: confirm the merged profile is deployable before writing reports (and deploying)')
    logger.log('  - validating the merged profile with a dry-run...')
    validation, _cf = _validate_profile_dry_run(
        sf, dest_org, merged_path, dest_profile_name, args.api_version,
        args.poll_interval, args.job_timeout, logger, out_dir, mdtype=dest_mdtype)
    if validation != 'clean':
        logger.log('  WARNING: dry-run not clean (%s); self-healing the merged profile...' % validation)
        heal_out = os.path.join(out_dir, '.work', 'align-heal')
        heal_res = prune_stage(
            sf=sf, org=dest_org, source_profile_path=merged_path,
            profile_name=dest_profile_name, out_dir=heal_out,
            api_version=args.api_version, poll_interval=args.poll_interval,
            job_timeout=args.job_timeout, thread_number=args.thread_number,
            batch_size=args.batch_size, max_iter=args.max_iter,
            quiet=args.quiet, fresh=True, logger=logger,
            io_threads=args.io_threads_number)
        if heal_res['outcome'] != 'SUCCESS':
            print('ERROR: merged self-heal prune failed: %s'
                  % (heal_res.get('reason') or 'unknown error'))
            sys.exit(2)
        shutil.copyfile(heal_res['common_profile_path'], merged_path)
        # The heal removed license-mandated userPermissions ("You may not turn
        # off ...") and objectPermissions that preserved userPermissions depend
        # on ("Permission X depends on permission(s): ... Object"): remember them
        # so the re-merge keeps them verbatim instead of turning them off again
        # (which would loop).
        keep_user_permissions = set()
        keep_object_permissions = set()
        heal_log = os.path.join(heal_out, 'missing_log.txt')
        for typ, member in load_log_entries(heal_log):
            if typ == 'UserPermission':
                keep_user_permissions.add(member)
            elif typ in ('StandardObject', 'CustomObject'):
                keep_object_permissions.add(member)
        shutil.rmtree(heal_out, ignore_errors=True)
        # Recompute the diff against the healed (final) merged profile.
        merged_xml, manual_ops, changes = merge_profiles(
            merged_path, dst_clean, dest_env=dest_org,
            read_only_fields=read_only_fields,
            keep_user_permissions=keep_user_permissions,
            keep_object_permissions=keep_object_permissions,
            preserve_dest_label=dest_missing, ignore=ignore)
        with open(merged_path, 'wb') as _f:
            _f.write(merged_xml)
        validation, _cf = _validate_profile_dry_run(
            sf, dest_org, merged_path, dest_profile_name, args.api_version,
            args.poll_interval, args.job_timeout, logger, out_dir, mdtype=dest_mdtype)
    logger.log('  OK: merged dry-run: %s' % validation)

    n_granted = sum(1 for c in changes if c['kind'] == 'grant')
    n_revoked = sum(1 for c in changes if c['kind'] == 'revoke')
    n_layout = sum(1 for c in changes if c['kind'] == 'layout_change')
    logger.log('  OK: merge result: %d granted (ON), %d revoked (OFF), %d layout changed, '
               '%d manual' % (n_granted, n_revoked, n_layout, len(manual_ops)))

    # 6. classify layoutAssignments manual ops (redundant vs needs check) using
    #    the destination org's Layout components.
    if any(op['section'] == 'layoutAssignments' for op in manual_ops):
        layout_names, lerr = list_metadata(sf, dest_org, 'Layout')
        layout_count = {}
        if layout_names:
            for name in layout_names:
                obj = name.split('-', 1)[0] if '-' in name else name
                layout_count[obj] = layout_count.get(obj, 0) + 1
        elif lerr:
            logger.log('  WARNING: could not list Layout metadata (%s); '
                       'layoutAssignments marked for manual check.' % lerr)
        classify_layout_ops(manual_ops, changes, layout_count)

    # 7. reports (only now, on the final validated merged profile).
    logger.log('  - writing reports...')
    manual_ops_path = _write_and_report_manual_ops(
        manual_ops, merge_dir, dest_profile_name, logger, args.quiet,
        mdtype=dest_mdtype)
    change_path = write_what_is_changed(
        os.path.join(merge_dir, 'what-is-changed.txt'), changes, dest_org)
    logger.log('  OK: what-is-changed written: %s' % _rel(out_dir, change_path))

    # 7b. persist the run identity + validated merge, so a later invocation on
    #     this folder can deploy the cache instead of redoing the pipeline.
    state_path = None
    if validation == 'clean':
        state_path = _write_align_state(
            out_dir, args, src_profile_name, dest_profile_name, dest_org,
            merged_path, validation, n_granted, n_revoked, n_layout, len(manual_ops),
            src_mdtype=src_mdtype, dest_mdtype=dest_mdtype)
        logger.log('  OK: run state saved: %s' % _rel(out_dir, state_path))

    # 8. optional deploy of the merged profile (only after a clean validation).
    deploy_detail = ''
    outcome = 'SUCCESS'
    if validation != 'clean':
        outcome = 'VALIDATION_FAILED'
    elif do_deploy:
        logger.log('  - deploying the merged profile to %s...' % dest_org)
        ok, deploy_detail = _deploy_profile(sf, dest_org, merged_path, dest_profile_name,
                                            args.api_version, args.poll_interval,
                                            args.job_timeout, logger, out_dir,
                                            mdtype=dest_mdtype)
        outcome = 'SUCCESS' if ok else 'DEPLOY_FAILED'

    logger.log('')
    logger.log('========================================================')
    logger.log('  ALIGNMENT RESULT')
    logger.log('========================================================')
    logger.log('OUTCOME: %s' % outcome)
    logger.log('GRANTED (turned ON): %d' % n_granted)
    logger.log('REVOKED (turned OFF): %d' % n_revoked)
    logger.log('LAYOUT CHANGED (applied by the deploy): %d' % n_layout)
    redundant_count = sum(1 for o in manual_ops
                          if (o.get('verdict') or '').startswith('REDUNDANT'))
    check_count = len(manual_ops) - redundant_count
    logger.log('MANUAL OPERATIONS: %d (%d ignorable - redundant layout assignment, no '
               'action needed / %d need check - manual review required)'
               % (len(manual_ops), redundant_count, check_count))
    logger.log('VALIDATION: %s' % validation)
    logger.log('')
    logger.log('FILES:')
    logger.log('  ORIGINAL SOURCE (retrieved): %s' % _rel(out_dir, source_profile_path))
    logger.log('  SOURCE PRUNED:               %s' % _rel(out_dir, src_clean))
    logger.log('  DESTINATION RETRIEVED:       %s' % _rel(out_dir, dest_raw))
    logger.log('  DESTINATION PRUNED:          %s' % _rel(out_dir, dst_clean))
    logger.log('  MERGED PROFILE:              %s' % _rel(out_dir, merged_path))
    if manual_ops_path:
        logger.log('  MANUAL OPERATIONS:           %s' % _rel(out_dir, manual_ops_path))
    logger.log('  WHAT-IS-CHANGED:             %s' % _rel(out_dir, change_path))
    if state_path:
        logger.log('  RUN STATE:                   %s' % _rel(out_dir, state_path))
    if deploy_detail:
        logger.log('DEPLOY: %s' % deploy_detail)
    logger.log('========================================================')
    logger.log('RESULT:%s' % outcome)

    sys.exit(0 if outcome == 'SUCCESS' else 1)


def _render_onniscent_gap(sections):
    """Preambolo ONNISCENT GAP del report diff: quanti permessi mancano
    (da concedere) e quanti sono parzialmente concessi (da accendere)."""
    missing = sum(len(d['only_source']) for d in sections.values())
    partial = sum(len(d['different']) for d in sections.values())
    return '\n'.join([
        'ONNISCENT GAP - what the full-access profile has that the current one lacks',
        '=' * 78,
        'missing permissions (to grant): %d' % missing,
        'partially granted (to turn ON):  %d' % partial,
        '',
    ])


def _onniscent_heal(sf, args, path, profile_name, org, out_dir, logger, dropped):
    """Dry-run the repaired profile and drop the entries the deploy rejects
    (targeted per-entry removal, far cheaper than a full self-heal). Falls
    back to the prune-stage self-heal when a failure is not entry-level.
    ``dropped`` collects ``(section, member)`` for the report. Returns the
    final validated path."""
    current = path
    cap = args.max_iter if args.max_iter > 0 else 5000
    for _round in range(cap):
        validation, cf = _validate_profile_dry_run(
            sf, org, current, profile_name, args.api_version,
            args.poll_interval, args.job_timeout, logger, out_dir,
            mdtype='Profile')
        if validation == 'clean':
            logger.log('  OK: repaired profile dry-run clean')
            return current
        removals = entry_removals_from_failures(cf)
        if not removals:
            break
        n = strip_flagged_entries(current, removals)
        shown = ', '.join(sorted({m for _s, _c, _m, m in removals})[:8])
        logger.log('  WARNING: dry-run not clean; dropped %d entry(ies): %s'
                   % (n, shown))
        for section, _child, _mode, member in removals:
            dropped.append((section, member))
    logger.log('  WARNING: repaired profile needs a full self-heal...')
    return _validate_and_heal_deliverable(
        sf, args, current, profile_name, 'Profile', org, out_dir, logger)


def _onniscent_diff(sf, args, out_dir, full_path, profile_name, org, logger,
                    ignore=None):
    """``--onniscent --diff``: pure comparison between the current profile and
    the computed full-access profile. No deliverable, no deploy, exit 0."""
    diff_dir = os.path.join(out_dir, 'diff')
    os.makedirs(diff_dir, exist_ok=True)
    onn_path = os.path.join(diff_dir, 'onniscent-%s'
                            % meta_filename('Profile', profile_name))
    shutil.copyfile(full_path, onn_path)

    username, uerr = org_username(sf, org)
    if not username:
        print('ERROR: could not resolve the org %r: %s' % (org, uerr))
        sys.exit(2)
    current, rerr = retrieve_profiles(username, [profile_name], clean=False,
                                      mdtype='Profile')
    current_path = os.path.join(diff_dir, meta_filename('Profile', profile_name))
    with open(current_path, 'wb') as _f:
        if not current:
            _f.write(_empty_metadata_xml('Profile', profile_name))
        else:
            _f.write(current[0][1])

    src_label = 'onniscent (computed full access)'
    dst_label = 'Profile %s "%s" (current)' % (org, profile_name)
    sections = diff_profiles(onn_path, current_path, ignore=ignore)
    report_path = os.path.join(diff_dir, 'diff-report.txt')
    with open(report_path, 'w', encoding='utf-8') as _f:
        _f.write(_render_onniscent_gap(sections))
        _f.write('\n')
        _f.write(render_diff_report(sections, src_label, dst_label))
        _f.write('\n')
    write_diff_json(sections, src_label, dst_label, diff_dir)
    write_diff_xml(sections, src_label, dst_label, diff_dir)
    print(_render_onniscent_gap(sections))
    print(render_diff_summary(sections, src_label, dst_label))
    logger.log('  OK: diff report written: %s' % _rel(out_dir, report_path))
    sys.exit(0)


def _run_onniscent(sf, args, out_dir, logger, ignore=None):
    """``--onniscent``: bring the destination Profile to FULL access (flip ON
    every existing permission entry, grant every org element missing from it),
    drop what the deploy rejects via the targeted heal, and optionally deploy.
    With ``--diff`` it becomes a pure comparison. Never touches the org
    without ``--deploy-on-success``."""
    dest_org = args.dest_org
    dest_profile_name = args.dest_profile

    logger.log('')
    logger.log('-- ONNISCENT -------------------------------------------')
    logger.log('  purpose: bring Profile %r in %s to FULL access'
               % (dest_profile_name, dest_org))
    if args.diff:
        logger.log('  mode: --diff (pure comparison: no deliverable, no deploy)')

    # Update notice + admin warning, exactly like every other mode.
    logger.log('  - checking for a newer sf-profile-pruner version...')
    update_lines = _check_for_update()
    for line in update_lines:
        print(line, file=sys.stderr)
    _warn_if_not_admin(sf, dest_org, logger)

    # API version: explicit or detected from the destination org.
    api_source = 'explicit'
    if args.api_version is None:
        logger.log('  - detecting the latest API version from org %s...' % dest_org)
        args.api_version, averr = org_api_version(sf, dest_org)
        if args.api_version is None:
            print('ERROR: could not detect the latest API version from org %r:'
                  % dest_org)
            print('       %s' % (averr or 'unknown error'))
            print('       Pass --api-version <X.Y> explicitly to continue.')
            sys.exit(2)
        api_source = 'auto (latest for org)'
    logger.log('  OK: API v%s (%s)' % (args.api_version, api_source))

    # Retrieve the profile (full: the untouched sections must stay present).
    username, uerr = org_username(sf, dest_org)
    if not username:
        print('ERROR: could not resolve the destination org %r: %s' % (dest_org, uerr))
        sys.exit(2)
    logger.log('  - retrieving Profile %r from %s...' % (dest_profile_name, dest_org))
    profiles, rerr = retrieve_profiles(username, [dest_profile_name],
                                       clean=False, mdtype='Profile')
    if not profiles:
        print('ERROR: Profile %r not found in org %s: %s'
              % (dest_profile_name, dest_org, rerr or 'no profile returned'))
        sys.exit(2)
    original_copy = os.path.join(out_dir, 'ORIGINAL_%s'
                                 % meta_filename('Profile', dest_profile_name))
    with open(original_copy, 'wb') as _f:
        _f.write(profiles[0][1])

    # 1. flip ON every existing permission entry (--ignore excluded).
    xml, flipped, already_on = flip_profile_on(original_copy, ignore=ignore)
    work_dir = os.path.join(out_dir, '.work')
    os.makedirs(work_dir, exist_ok=True)
    flipped_path = os.path.join(work_dir, 'onniscent-flipped-%s'
                                % meta_filename('Profile', dest_profile_name))
    with open(flipped_path, 'wb') as _f:
        _f.write(xml)
    logger.log('  OK: %d entry(ies) turned ON, %d already at full access.'
               % (sum(flipped.values()), already_on))

    # 2. grant every org element missing from the profile.
    universe = build_universe(sf, dest_org, args.api_version, logger=logger,
                              io_threads=args.io_threads_number)
    xml, granted, synthesized = grant_missing(flipped_path, universe,
                                              ignore=ignore)
    full_path = os.path.join(work_dir, 'onniscent-full-%s'
                             % meta_filename('Profile', dest_profile_name))
    with open(full_path, 'wb') as _f:
        _f.write(xml)
    logger.log('  OK: %d element(s) granted (%d default record type(s) synthesized).'
               % (sum(granted.values()), synthesized))

    # 3. read-only normalization (default on; --avoid-normalize-ro skips).
    normalized = 0
    if not args.avoid_normalize_ro:
        read_only = _read_only_field_set(sf, dest_org, dest_org, full_path,
                                         None, logger,
                                         io_threads=args.io_threads_number)
        normalized = normalize_profile_read_only(full_path, read_only)
        logger.log('  OK: %d read-only field(s) normalized to editable=false.'
                   % normalized)
    else:
        logger.log('  - read-only normalization disabled (--avoid-normalize-ro).')

    # 4. --diff: pure comparison, done here (no heal, no deliverable).
    if args.diff:
        _onniscent_diff(sf, args, out_dir, full_path, dest_profile_name,
                        dest_org, logger, ignore=ignore)

    # 5. targeted heal + final validation.
    dropped = []
    final_path = _onniscent_heal(sf, args, full_path, dest_profile_name,
                                 dest_org, out_dir, logger, dropped)

    # 6. finalize 4-merged/ + WIC.
    merge_dir = os.path.join(out_dir, '4-merged')
    os.makedirs(merge_dir, exist_ok=True)
    final_profile = os.path.join(merge_dir,
                                 meta_filename('Profile', dest_profile_name))
    shutil.copyfile(final_path, final_profile)
    normalize_xml_declaration(final_profile)
    wic_path = write_onniscent_wic(
        os.path.join(merge_dir, 'what-is-changed.txt'),
        flipped, granted, dropped, normalized, already_on, synthesized,
        dest_org)
    logger.log('  OK: what-is-changed written: %s' % _rel(out_dir, wic_path))

    # 7. optional deploy (only on explicit request, Y/N unless --sure).
    do_deploy = args.deploy_on_success
    if do_deploy and not args.sure:
        if not _confirm('--deploy-on-success will deploy the repaired profile to '
                        '%s. Deploy? [y/N] ' % dest_org):
            do_deploy = False
            print('Deploy skipped: dry-run only.', file=sys.stderr)
    outcome = 'SUCCESS'
    deploy_detail = ''
    if do_deploy:
        logger.log('  - deploying the repaired profile to %s...' % dest_org)
        ok, deploy_detail = _deploy_profile(
            sf, dest_org, final_profile, dest_profile_name, args.api_version,
            args.poll_interval, args.job_timeout, logger, out_dir,
            mdtype='Profile')
        outcome = 'SUCCESS' if ok else 'DEPLOY_FAILED'

    logger.log('========================================================')
    logger.log('  ONNISCENT RESULT')
    logger.log('========================================================')
    logger.log('FLIPPED (turned ON): %d' % sum(flipped.values()))
    logger.log('GRANTED (added ON) : %d' % sum(granted.values()))
    logger.log('DROPPED (rejected) : %d' % len(dropped))
    if deploy_detail:
        logger.log('DEPLOY: %s' % deploy_detail)
    logger.log('RESULT:%s' % outcome)
    sys.exit(0 if outcome == 'SUCCESS' else 1)


def _main():
    argv = sys.argv[1:]
    argv, repair_note = _repair_mangled_argv(argv)
    if repair_note:
        print(repair_note, file=sys.stderr)
    sys.argv[1:] = argv

    # Easter egg: print the story and exit before argparse enforces the
    # required options.
    if '--tell-me-a-story' in argv:
        print(STORY, flush=True)
        sys.exit(0)

    ap = argparse.ArgumentParser(
        prog=APP_NAME,
        description=(
            'Clean, compare or align Salesforce Profiles and PermissionSets '
            '(always dry-run; never deploy unless you ask).\n'
            'Pick the mode by what you need:\n'
            '  deploy a profile/permission set without errors  ->  plain prune (default)\n'
            '  see the differences between two                ->  --diff\n'
            '  make the destination identical to the source    ->  --align-from-source\n'
            '\n'
            'Plain prune only removes references that do not exist in the '
            'destination org; align also turns OFF destination-only permissions '
            'and turns ON source-only ones.'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )

    g_input = ap.add_argument_group('input source')
    g_input.add_argument('-pp', '--profile-path', default=None,
                         help='path of the source .profile-meta.xml file (via 1: file)')
    g_input.add_argument('-so', '--src-org', default=None,
                         help='source org alias/username to retrieve the profile from '
                              '(via 2: org, or via 3: retrieve only)')
    g_input.add_argument('-sp', '--src-profile', default=None,
                         help='name of the Profile to retrieve from --src-org (via 2/3)')
    g_input.add_argument('-sps', '--src-permissionset', default=None,
                         help='name of the PermissionSet to retrieve from --src-org '
                              '(via 2/3). Mutually exclusive with --src-profile.')
    g_input.add_argument('-fp', '--full-profile', action='store_true',
                         help='retrieve the full (non-stripped) profile; the default is a '
                              'clean retrieve (loginIpRanges/loginHours/userLicense removed)')

    g_dest = ap.add_argument_group('destination')
    g_dest.add_argument('-do', '--dest-org', default=None,
                        help='destination org alias/username (dry-run and optional deploy '
                             'target). Optional: omit it (with -so + -sp) for retrieve only.')
    g_dest.add_argument('-dp', '--dest-profile', default=None,
                        help='name to deploy the cleaned Profile under; if it does not '
                             'exist in the org it is created. Default: the source name')
    g_dest.add_argument('-dps', '--dest-permissionset', default=None,
                        help='name to deploy the cleaned PermissionSet under; if it does '
                             'not exist in the org it is created. Mutually exclusive with '
                             '--dest-profile. Enables PermissionSet source/dest, including '
                             'cross-type comparison (source Profile vs dest PermissionSet '
                             'or vice versa).')

    g_out = ap.add_argument_group('output')
    g_out.add_argument('-od', '--out-dir', default=os.getcwd(),
                       help='output directory for report/packages/logs '
                            '(default: the current directory)')

    g_tune = ap.add_argument_group('tuning')
    g_tune.add_argument('-bs', '--batch-size', type=int, default=DEFAULT_BATCH_SIZE, metavar='N',
                        help='split a section into parallel chunks of N entries '
                             '(min 100, max 10000, default 200); 0 disables splitting')
    g_tune.add_argument('-tn', '--thread-number', type=int, default=16, metavar='X',
                        help='max parallel worker threads, 1..64 (default: 16); capped to '
                             'the number of work items (chunks) to prune')
    g_tune.add_argument('-itn', '--io-threads-number', type=int,
                        default=DEFAULT_IO_THREADS, metavar='X',
                        help='max parallel threads for the read-only io calls '
                             '(list metadata, Tooling queries, sobject describe). '
                             '1..%d (default: %d); 0 or 1 disables parallelism'
                             % (MAX_IO_THREADS, DEFAULT_IO_THREADS))
    g_tune.add_argument('--job-timeout', type=int, default=0, metavar='M',
                        help='minutes to wait for a deploy job before aborting '
                             '(1..60; 0 = no timeout, the default)')
    g_tune.add_argument('-av', '--api-version', default=None,
                        help='manifest API version (default: the latest version supported '
                             'by the destination org, detected automatically)')
    g_tune.add_argument('--deploy-throttle-ms', type=int, default=500, metavar='MS',
                        help='minimum interval between deploy start submissions, in ms '
                             '(default: 500); 0 disables the throttle')
    g_tune.add_argument('--poll-interval', type=float, default=3.0,
                        help='seconds to wait between status polls of a deploy job (default: 3)')
    g_tune.add_argument('--max-iter', type=int, default=5000,
                        help='iteration cap (default: 5000)')
    g_tune.add_argument('--sf', default='sf',
                        help='command/path of the Salesforce CLI (default: sf)')

    g_act = ap.add_argument_group('behavior')
    g_act.add_argument('--ignore', default=None, metavar='PACKAGE_XML',
                       help='path of a package.xml listing metadata to IGNORE '
                            '(metadata not yet released). The ignored entries are '
                            'excluded from --diff, --align-from-source and the plain '
                            'prune; their destination value is kept (never propagated '
                            'from the source, never turned off).')
    g_act.add_argument('--align-from-source', action='store_true',
                       help='make the destination identical to the source: turns OFF '
                            'destination-only permissions and turns ON source-only ones '
                            '(source is authoritative). Works with a source file (via 1) or '
                            'a source org (via 2). Runs automatically (no prompt); '
                            're-running on the same folder deploys the cached merge.')
    g_act.add_argument('--force-license-mismatch', action='store_true',
                       help='allow --align-from-source to proceed when a PermissionSet\'s '
                            'source and destination declare DIFFERENT licenses. The guard '
                            'blocks by default because a PermissionSet license cannot be '
                            'updated once it exists (a deploy would fail with "License '
                            'can\'t be updated"); this flag lets you generate the merged '
                            'output for inspection anyway.')
    g_act.add_argument('--new-ps-name', default=None, metavar='NAME',
                       help='when --align-from-source --force-license-mismatch detects '
                            'DIFFERENT PermissionSet licenses, create the destination as a '
                            'NEW PermissionSet under this API name (no file extension) '
                            'instead of overwriting the existing one. The name must be a '
                            'valid Salesforce API name and must NOT already exist in the '
                            'destination org.')
    g_act.add_argument('--deploy-on-success', action='store_true',
                       help='deploy the cleaned profile/permission set once pruning succeeds. '
                            'Modifies the org: asks Y/N (a No runs dry-run only). Add --sure '
                            'to skip the confirmation.')
    g_act.add_argument('--sure', action='store_true',
                       help='skip the Y/N confirmation of --deploy-on-success and deploy '
                            'without asking. Has no effect without --deploy-on-success.')
    g_act.add_argument('--diff', action='store_true',
                       help='compare the source against the destination (read-only): writes '
                            'diff/diff-report.txt + diff/diff-data.json/.xml. No prune, no '
                            'dry-run, no deploy. Works with a source file (via 1) or org (via 2).')
    g_act.add_argument('--onniscent', action='store_true',
                       help='bring the destination Profile (-do + -dp) to FULL access: '
                            'every permission entry present is turned ON and every org '
                            'element missing from the profile is added ON (enumerated '
                            'from the org). No source. Entries the deploy rejects are '
                            'dropped and listed in the report. With --diff it becomes a '
                            'pure comparison between the current profile and the '
                            'computed full-access profile (no deliverable, no deploy).')
    g_act.add_argument('--fresh', action='store_true',
                       help='ignore any previous state and restart from the original')
    g_act.add_argument('--avoid-normalize-ro', action='store_true',
                       help='skip the read-only field normalization. Salesforce IGNORES '
                            'the editable flag on fields it keeps read-only (formula, '
                            'roll-up summary, auto number, compound Address/Location, '
                            'and system read-only fields such as ChatKey or AccountId), '
                            'so a true value in the file is dead metadata. By default '
                            'the tool rewrites those to editable=false in BOTH '
                            '--align-from-source and the plain prune, so the profile '
                            'matches what the org actually enforces; with this flag the '
                            'flags are left exactly as retrieved (for raw inspection).')
    g_act.add_argument('--quiet', action='store_true',
                       help='suppress the progressive stdout output')
    g_act.add_argument('--tell-me-a-story', action='store_true',
                       help='Turbolino will tell you a fascinating story about code, '
                            'algorithms and copyright.')
    g_act.add_argument('-V', '--version', action='version',
                       version='%s v%s - %s' % (APP_NAME, __version__, AUTHOR_NOTE))

    args = ap.parse_args()

    # ---------- --ignore: parse the package.xml into {type: members} ----------
    ignore = {}
    if args.ignore:
        if not os.path.isfile(args.ignore):
            print('ERROR: --ignore file not found: %s' % args.ignore)
            sys.exit(2)
        try:
            ignore = parse_ignore_package(args.ignore)
        except Exception as e:
            print('ERROR: cannot parse --ignore package.xml %r: %s' % (args.ignore, e))
            sys.exit(2)
        if not ignore:
            print('WARNING: --ignore package.xml lists no ignorable metadata type.')

    # ---------- numeric validation ----------
    if not (1 <= args.thread_number <= 64):
        print('ERROR: --thread-number must be between 1 and 64 (got %d).' % args.thread_number)
        sys.exit(2)

    # --io-threads-number: 0 or 1 = sequential; above the ceiling is clamped.
    if args.io_threads_number < 0:
        print('ERROR: --io-threads-number must be >= 0 (got %d).' % args.io_threads_number)
        sys.exit(2)
    if args.io_threads_number <= 1:
        args.io_threads_number = 1
    elif args.io_threads_number > MAX_IO_THREADS:
        print('NOTE: --io-threads-number %d is above the maximum of %d; using %d.'
              % (args.io_threads_number, MAX_IO_THREADS, MAX_IO_THREADS),
              file=sys.stderr)
        args.io_threads_number = MAX_IO_THREADS

    if args.batch_size < 0:
        print('ERROR: --batch-size must be >= 0 (got %d).' % args.batch_size)
        sys.exit(2)
    clamped = _clamp_batch_size(args.batch_size)
    if clamped != args.batch_size:
        if args.batch_size == 0:
            pass  # 0 = no chunking, kept as-is
        elif args.batch_size > MAX_CHUNK_SIZE:
            print('NOTE: --batch-size %d is above the maximum of %d; using %d.' %
                  (args.batch_size, MAX_CHUNK_SIZE, MAX_CHUNK_SIZE), file=sys.stderr)
        else:
            print('NOTE: --batch-size %d is below the minimum of %d; using %d.' %
                  (args.batch_size, MIN_CHUNK_SIZE, MIN_CHUNK_SIZE), file=sys.stderr)
        args.batch_size = clamped

    if args.job_timeout < 0 or args.job_timeout > 60:
        print('ERROR: --job-timeout must be between 0 and 60 minutes (got %d).' % args.job_timeout)
        sys.exit(2)

    # Apply the deploy-start rate limit before any worker is spawned.
    set_deploy_start_interval(args.deploy_throttle_ms / 1000.0)

    # --align-from-source needs a destination org. The source can be either a
    # file (via 1: -pp + -do) or an org retrieve (via 2: -so + -sp + -do + -dp).
    # Retrieve-only (via 3) has no destination, so it cannot align.
    if args.align_from_source and not args.dest_org:
        print('ERROR: --align-from-source requires --dest-org (-do).')
        sys.exit(2)

    # --diff is a pure comparison: it needs both a source (file or org) and a
    # destination profile, and is mutually exclusive with align/deploy.
    if args.diff and not args.dest_org:
        print('ERROR: --diff requires --dest-org (-do).')
        sys.exit(2)
    if args.diff and args.align_from_source:
        print('ERROR: --diff and --align-from-source are mutually exclusive.')
        sys.exit(2)
    if args.diff and args.deploy_on_success:
        print('ERROR: --diff and --deploy-on-success are mutually exclusive.')
        sys.exit(2)

    # --onniscent: no source, Profile-only repair (-do + -dp). Mutually
    # exclusive with the source-bearing modes; --diff turns it into a pure
    # comparison between the current profile and the computed full-access one.
    if args.onniscent:
        if not args.dest_org:
            print('ERROR: --onniscent requires --dest-org (-do).')
            sys.exit(2)
        if not args.dest_profile:
            print('ERROR: --onniscent requires --dest-profile (-dp).')
            sys.exit(2)
        if args.dest_permissionset:
            print('ERROR: --onniscent works on Profiles only (-dp), not PermissionSets.')
            sys.exit(2)
        if args.src_org or args.src_profile or args.src_permissionset or args.profile_path:
            print('ERROR: --onniscent has no source: -so/-sp/-sps/-pp are '
                  'mutually exclusive with it.')
            sys.exit(2)
        if args.align_from_source:
            print('ERROR: --onniscent and --align-from-source are mutually exclusive.')
            sys.exit(2)
        if args.new_ps_name:
            print('ERROR: --onniscent and --new-ps-name are mutually exclusive.')
            sys.exit(2)
        if args.force_license_mismatch:
            print('ERROR: --onniscent and --force-license-mismatch are mutually exclusive.')
            sys.exit(2)

    # ---------- --onniscent: full-access profile repair (no source) ----------
    # Dispatched before the source-mode detection, which would otherwise
    # reject a source-less invocation.
    if args.onniscent:
        out_dir = os.path.abspath(args.out_dir or os.getcwd())
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            print('ERROR: cannot create output directory %r: %s' % (out_dir, e))
            sys.exit(2)
        logger = Logger(args.quiet, os.path.join(out_dir, 'run.log'))
        sf = find_sf(args.sf)
        _run_onniscent(sf, args, out_dir, logger, ignore=ignore)
        sys.exit(1)  # _run_onniscent exits; safety net only

    # ---------- mode detection ----------
    # via 1 (file):           -pp + -do
    # via 2 (org):            -so + (-sp|-sps) + -do + (-dp|-dps)  (retrieve -> prune -> dry-run/deploy)
    # via 3 (retrieve only):  -so + (-sp|-sps)                     (no prune, no dry-run/deploy)
    src_name = args.src_profile or args.src_permissionset
    dest_name = args.dest_profile or args.dest_permissionset

    if args.src_profile and args.src_permissionset:
        print('ERROR: --src-profile (-sp) and --src-permissionset (-sps) are mutually exclusive.')
        sys.exit(2)
    if args.dest_profile and args.dest_permissionset:
        print('ERROR: --dest-profile (-dp) and --dest-permissionset (-dps) are mutually exclusive.')
        sys.exit(2)

    if args.src_org or src_name:
        if not args.src_org or not src_name:
            print('ERROR: --src-org (-so) must be provided with --src-profile (-sp) or '
                  '--src-permissionset (-sps).')
            sys.exit(2)
        if bool(args.dest_org) != bool(dest_name):
            print('ERROR: --dest-org (-do) must be provided with --dest-profile (-dp) or '
                  '--dest-permissionset (-dps).')
            sys.exit(2)
        if args.profile_path:
            print('ERROR: --profile-path (-pp) and --src-org (-so) are mutually exclusive.')
            sys.exit(2)
        mode = 'org' if args.dest_org else 'retrieve'
    else:
        if not args.profile_path:
            print('ERROR: provide --profile-path (-pp) [via 1], '
                  '--src-org (-so) + (--src-profile|-sp or --src-permissionset|-sps) '
                  '+ -do + (-dp|-dps) [via 2], or -so + (-sp|-sps) alone [via 3, retrieve only].')
            sys.exit(2)
        if not args.dest_org:
            print('ERROR: --dest-org (-do) is required (the dry-run/deploy target).')
            sys.exit(2)
        if not os.path.isfile(args.profile_path):
            print('ERROR: profile not found: %s' % args.profile_path)
            sys.exit(2)
        mode = 'file'

    # ---------- via 3: retrieve only (no prune, no dry-run/deploy) ----------
    if mode == 'retrieve':
        out_dir = os.path.abspath(args.out_dir or os.getcwd())
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError as e:
            print('ERROR: cannot create output directory %r: %s' % (out_dir, e))
            sys.exit(2)
        sf = find_sf(args.sf)
        username, uerr = org_username(sf, args.src_org)
        if not username:
            print('ERROR: could not resolve the org username for %r: %s' % (args.src_org, uerr))
            sys.exit(2)
        clean = not args.full_profile
        src_mdtype = 'PermissionSet' if args.src_permissionset else 'Profile'
        print('Retrieving %s %r from org %s (%s)...' %
              (src_mdtype, src_name, args.src_org, 'full' if args.full_profile else 'clean'))
        profiles, rerr = retrieve_profiles(username, [src_name], clean=clean, mdtype=src_mdtype)
        if not profiles:
            print('ERROR: source %s %r not found in org %s: %s'
                  % (src_mdtype, src_name, args.src_org, rerr or 'no profile returned'))
            sys.exit(2)
        full_name, xml = profiles[0]
        out_path = os.path.join(out_dir, meta_filename(src_mdtype, full_name))
        with open(out_path, 'wb') as _f:
            _f.write(xml)
        print('Retrieved %s -> %s (%d bytes)' % (full_name, out_path, len(xml)))
        if rerr:
            print('WARNING: %s' % rerr, file=sys.stderr)
        sys.exit(0)

    # ---------- destructive-op confirmation (deploy only) ----------
    # --align-from-source runs automatically: it only produces files (the
    # pre-align destination profile is always saved in the run folder), so no
    # prompt is needed. A 'no' on deploy downgrades to dry-run instead of
    # aborting the whole run.
    do_align = args.align_from_source
    do_deploy = args.deploy_on_success
    if do_deploy and not args.sure:
        if not _confirm('--deploy-on-success will deploy the cleaned profile to the '
                        'destination org. Deploy? [y/N] '):
            do_deploy = False
            print('Deploy skipped: dry-run only.', file=sys.stderr)

    # The alignment engine (--align-from-source) is dispatched after the shared
    # setup below (out_dir / logger / sf / API version), where the source profile
    # and destination org are known. See _run_alignment().

    if not args.quiet:
        print(BANNER, flush=True)
        print('%s v%s' % (APP_NAME, __version__), flush=True)
        print(AUTHOR_NOTE, flush=True)
        print(DISCLAIMER, flush=True)
        print(COPYLEFT, flush=True)
        print('', flush=True)
        if not args.avoid_normalize_ro and not args.diff:
            print('NOTE: read-only normalization is ON. System read-only fields '
                  '(formula, roll-up summary, auto number, compound '
                  'Address/Location, and system read-only fields such as '
                  'ChatKey/AccountId) will be normalized to editable=false in '
                  'the output. Use --avoid-normalize-ro to keep the raw flags.',
                  flush=True)
            print('', flush=True)

    # ---------- names and mode ----------
    out_dir = os.path.abspath(args.out_dir or os.getcwd())
    dest_org = args.dest_org
    if mode == 'org':
        src_org = args.src_org
        src_profile_name = src_name
        src_mdtype = 'PermissionSet' if args.src_permissionset else 'Profile'
        dest_profile_name = dest_name
        dest_mdtype = 'PermissionSet' if args.dest_permissionset else 'Profile'
        profile_path = None
    else:
        src_org = None
        profile_path = os.path.abspath(args.profile_path)
        src_mdtype = detect_metadata_type(profile_path)
        base = os.path.basename(profile_path)
        src_profile_name = re.sub(re.escape(METADATA_TYPES[src_mdtype]['suffix']) + '$', '', base)
        dest_profile_name = dest_name or src_profile_name
        dest_mdtype = ('PermissionSet' if args.dest_permissionset is not None
                       else 'Profile' if args.dest_profile is not None else src_mdtype)

    original_copy = os.path.join(out_dir, 'ORIGINAL_%s' % meta_filename(src_mdtype, src_profile_name))
    run_log = os.path.join(out_dir, 'run.log')

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print('ERROR: cannot create output directory %r: %s' % (out_dir, e))
        sys.exit(2)

    logger = Logger(args.quiet, run_log)

    logger.log('-- SETUP -----------------------------------------------')
    logger.log('  purpose: locate the CLI, resolve the API version and confirm the org context')
    logger.log('  - locating the Salesforce CLI (sf)...')
    sf = find_sf(args.sf)
    logger.file_log('sf          : %s' % sf)
    logger.log('  OK: Salesforce CLI ready')

    # Informational, non-blocking upgrade notice (stderr so it never disturbs
    # the stdout progress/board; skipped silently on any network failure).
    logger.log('  - checking for a newer sf-profile-pruner version...')
    update_lines = _check_for_update()
    for line in update_lines:
        print(line, file=sys.stderr)
    if not update_lines:
        logger.log('  OK: sf-profile-pruner is up to date (or the check was skipped)')

    # Non-blocking profile check: warn when the connecting user is not a System
    # Administrator, because prune/diff/align results can depend on the current
    # profile's permissions. Runs for every invocation mode (via 1 / via 2 / align
    # / diff / prune); never blocks or fails the run.
    _warn_if_not_admin(sf, dest_org, logger)
    if src_org and src_org != dest_org:
        _warn_if_not_admin(sf, src_org, logger)

    # PermissionSet deploy is authoritative (unlike a Profile merge): surface
    # the difference up front, for every mode that produces a PermissionSet.
    # --diff is pure comparison (no file produced/deployed), so it is excluded.
    if not args.diff:
        _warn_permissionset_deploy(logger, dest_mdtype, args.quiet,
                                   deploy=args.deploy_on_success)

    # ---------- resume a completed alignment (same folder + same parameters) ----------
    # If this folder already holds a validated alignment with identical
    # -so/-do/-sp/-dp, deploy the cached merge (or just report it) instead of
    # redoing the whole pipeline. --fresh bypasses this and restarts from scratch.
    if do_align:
        resume_code = _resume_alignment(
            args, src_org, src_profile_name, dest_profile_name, dest_org,
            do_deploy, out_dir, logger, sf, src_mdtype=src_mdtype,
            dest_mdtype=dest_mdtype)
        if resume_code is not None:
            sys.exit(resume_code)

    # ---------- API version: explicit or detected from the destination org ----------
    api_source = 'explicit'
    if args.api_version is None:
        logger.log('  - detecting the latest API version from org %s...' % dest_org)
        args.api_version, averr = org_api_version(sf, dest_org)
        if args.api_version is None:
            print('ERROR: could not detect the latest API version from org %r:' % dest_org)
            print('       %s' % (averr or 'unknown error'))
            print('       Pass --api-version <X.Y> explicitly to continue.')
            sys.exit(2)
        api_source = 'auto (latest for org)'
    logger.log('  OK: API v%s (%s)' % (args.api_version, api_source))

    # ---------- retrieve the source profile (via 2) ----------
    if mode == 'org':
        username, uerr = org_username(sf, src_org)
        if not username:
            print('ERROR: could not resolve the org username for %r: %s' % (src_org, uerr))
            sys.exit(2)
        clean = not args.full_profile
        logger.log('  - retrieving %s %r from org %s (%s)...' %
                   (src_mdtype, src_profile_name, src_org, 'full' if args.full_profile else 'clean'))
        profiles, rerr = retrieve_profiles(username, [src_profile_name], clean=clean,
                                           mdtype=src_mdtype)
        if not profiles:
            print('ERROR: source %s %r not found in org %s: %s'
                  % (src_mdtype, src_profile_name, src_org, rerr or 'no profile returned'))
            sys.exit(2)
        full_name, xml = profiles[0]
        with open(original_copy, 'wb') as _f:
            _f.write(xml)
        profile_path = original_copy
        logger.log('  OK: retrieved %s (%d bytes)' % (full_name, len(xml)))
        if rerr:
            logger.log('  WARNING: %s' % rerr)
    else:
        # File source (-pp): stage.py writes its own ORIGINAL_* copy inside the
        # stage output dir, but the TOP-LEVEL original_copy (used by
        # _finalize_prune_output -> removed_non_alignable) must also exist.
        # Copy the user's file here so manual-ops detection has an original.
        if os.path.abspath(profile_path) != os.path.abspath(original_copy):
            shutil.copyfile(profile_path, original_copy)
            normalize_xml_declaration(original_copy)

    if mode == 'org':
        logger.log('  context: source %r @ %s  ->  destination %r @ %s' %
                   (src_profile_name, src_org, dest_profile_name, dest_org))
    else:
        logger.log('  context: source file %r  ->  destination %r @ %s' %
                   (src_profile_name, dest_profile_name, dest_org))

    # ---------- diff (--diff): pure profile-to-profile comparison ----------
    if args.diff:
        _run_diff(sf, args, out_dir, profile_path, src_profile_name,
                  dest_profile_name, dest_org, src_org, src_mdtype, dest_mdtype,
                  logger, ignore=ignore)
        sys.exit(1)  # _run_diff exits; safety net only

    # ---------- alignment (--align-from-source): separate flow ----------
    if do_align:
        _run_alignment(sf, args, out_dir, profile_path, src_profile_name,
                       dest_profile_name, dest_org, src_org, src_mdtype,
                       dest_mdtype, do_deploy, logger, ignore=ignore)
        sys.exit(1)  # _run_alignment exits; safety net only

    # ---------- run the prune stage ----------
    # Coherent with the alignment flow: the prune output lives in
    # 1-source-pruned/ and the final deliverable + reports in 4-merged/.
    #
    # Rename guard (plain prune, same type only): deploying a renamed source
    # ONTO an existing destination is ambiguous — a Profile deploy would MERGE
    # (leaving dest-only permissions), a PermissionSet deploy is authoritative
    # (revoking dest-only permissions). Neither is a clean "rename", so it is
    # rejected; renaming is allowed only onto a name that does not yet exist.
    # Cross-type conversions are exempt: filling an existing PS/Profile is the
    # intended behaviour there.
    if src_mdtype == dest_mdtype:
        dest_exists = _metadata_exists(sf, dest_org, dest_mdtype, dest_profile_name)
        if dest_profile_name != src_profile_name and dest_exists:
            print('ERROR: destination %s %r already exists in org %s.'
                  % (dest_mdtype, dest_profile_name, dest_org))
            print('       Renaming onto an existing %s is not allowed:' % dest_mdtype)
            if dest_mdtype == 'PermissionSet':
                print('       a PermissionSet deploy is authoritative and would REVOKE')
                print('       the existing permissions of %r.' % dest_profile_name)
            else:
                print('       a Profile deploy is a merge and would leave the existing')
                print('       permissions of %r partially intact.' % dest_profile_name)
            print('       To rename, choose a destination name that does not yet exist;')
            print('       to reconcile two existing components, use --align-from-source.')
            sys.exit(2)
        if not dest_exists:
            _warn_destination_missing(
                logger, args.quiet, dest_mdtype, dest_profile_name, dest_org,
                'it will be created on deploy')

    src_out = os.path.join(out_dir, '1-source-pruned')
    merge_dir = os.path.join(out_dir, '4-merged')
    result = prune_stage(
        sf=sf,
        org=dest_org,
        source_profile_path=profile_path,
        profile_name=src_profile_name,
        out_dir=src_out,
        api_version=args.api_version,
        poll_interval=args.poll_interval,
        job_timeout=args.job_timeout,
        thread_number=args.thread_number,
        batch_size=args.batch_size,
        max_iter=args.max_iter,
        quiet=args.quiet,
        fresh=args.fresh,
        logger=logger,
        io_threads=args.io_threads_number,
        ignore=ignore,
    )

    # Normalize system read-only field editable flags (default; --avoid-normalize-ro
    # skips). Applied in place so both the deploy target and the 4-merged copy
    # carry the effective (dead) permission.
    if result['outcome'] == 'SUCCESS':
        _normalize_prune_read_only(sf, args, src_org, dest_org,
                                   result['common_profile_path'], logger)

    # Plain prune, MIXED and NON-MIXED alike: the option (plain vs --align),
    # not the Profile/PermissionSet combination, determines the output. A
    # cross-type source is first converted into the destination metadata type;
    # then, in every case, the deliverable states EVERY entry the destination
    # values - destination-only entries keep the DESTINATION value (never
    # revoked, never turned off). Entries ignored via --ignore were already
    # excluded from the source by the prune stage, so they become
    # destination-only and are preserved the same way.
    deliverable_path = result['common_profile_path']
    preserved = {}
    if result['outcome'] == 'SUCCESS':
        if src_mdtype != dest_mdtype:
            deliverable_path = _convert_prune_deliverable(
                sf, args, src_mdtype, dest_mdtype, result['common_profile_path'],
                dest_profile_name, dest_org, out_dir, logger)
        deliverable_path, preserved = _preserve_dest_merge(
            sf, args, deliverable_path, dest_profile_name, dest_org, out_dir,
            logger, mdtype=dest_mdtype)

    # ---------- already clean? (resume short-circuit) ----------
    if result['already_clean'] and not do_deploy:
        _finalize_prune_output(sf, result, merge_dir, src_out, original_copy,
                               dest_profile_name, dest_org, logger, args.quiet,
                               mdtype=dest_mdtype, clean_path=deliverable_path,
                               preserved=preserved)
        prev = read_report_summary(os.path.join(src_out, 'report.txt'))
        print('Profile "%s" is already clean (previous run: %s). Nothing to do.'
              % (src_profile_name, prev.get('outcome') or 'SUCCESS'))
        if prev.get('removed') is not None:
            print('Previous run removed %d metadata reference(s).' % prev['removed'])
        sys.exit(0)

    # ---------- deploy with retry-on-failure loop (only with --deploy-on-success) ----------
    # The prune always runs to completion first (it can never be interrupted by
    # a deploy). When a deploy fails, run another prune round and retry; stop and
    # report an explicit failure only when the prune result stops changing (no
    # progress). This lets a transient org change converge, without spinning
    # forever on a reference that the dry-run cannot see.
    report_result = result          # stats of the last prune round that did real work
    result_state = report_result['outcome']
    deploy_detail = ''

    if do_deploy and result_state == 'SUCCESS':
        import hashlib

        def _sig(path):
            try:
                with open(path, 'rb') as f:
                    return hashlib.sha256(f.read()).hexdigest()
            except OSError:
                return None

        if report_result['already_clean']:
            logger.log('Profile already clean; deploying %r to org %s...' % (dest_profile_name, dest_org))

        prev_sig = _sig(deliverable_path)
        round_no = 1
        while True:
            logger.log('Deploying the cleaned profile %r to org %s (round %d)...'
                       % (dest_profile_name, dest_org, round_no))
            ok, detail = _deploy_profile(sf, dest_org, deliverable_path,
                                         dest_profile_name, args.api_version,
                                         args.poll_interval, args.job_timeout,
                                         logger, out_dir, mdtype=dest_mdtype)
            if ok:
                deploy_detail = detail
                result_state = 'SUCCESS'
                break

            deploy_detail = detail
            logger.log('Deploy failed (round %d); re-running the prune...' % round_no)
            result = prune_stage(
                sf=sf,
                org=dest_org,
                source_profile_path=profile_path,
                profile_name=src_profile_name,
                out_dir=src_out,
                api_version=args.api_version,
                poll_interval=args.poll_interval,
                job_timeout=args.job_timeout,
                thread_number=args.thread_number,
                batch_size=args.batch_size,
                max_iter=args.max_iter,
                quiet=args.quiet,
                fresh=False,
                logger=logger,
                io_threads=args.io_threads_number,
                ignore=ignore,
            )
            result_state = result['outcome']
            if result_state != 'SUCCESS':
                break
            _normalize_prune_read_only(sf, args, src_org, dest_org,
                                       result['common_profile_path'], logger)
            if not result.get('already_clean'):
                report_result = result  # a prune round that actually did work
            deliverable_path = result['common_profile_path']
            if src_mdtype != dest_mdtype:
                deliverable_path = _convert_prune_deliverable(
                    sf, args, src_mdtype, dest_mdtype, result['common_profile_path'],
                    dest_profile_name, dest_org, out_dir, logger)
            # Same unified plain semantics as the main flow (mixed + non-mixed).
            deliverable_path, preserved = _preserve_dest_merge(
                sf, args, deliverable_path, dest_profile_name, dest_org,
                out_dir, logger, mdtype=dest_mdtype)
            new_sig = _sig(deliverable_path)
            if new_sig == prev_sig:
                result_state = 'DEPLOY_FAILED'
                logger.log('Deploy still failing with an identical prune result; '
                           'giving up (no progress).')
                break
            prev_sig = new_sig
            round_no += 1
            if round_no > args.max_iter:
                result_state = 'DEPLOY_FAILED'
                logger.log('Deploy retry did not converge within %d rounds.' % args.max_iter)
                break

    # ---------- final output ----------
    logger.log('')
    logger.log('========================================================')
    logger.log('  PROFILE PRUNING RESULT')
    logger.log('========================================================')
    logger.log('OUTCOME: %s' % result_state)
    if report_result.get('reason'):
        logger.log('REASON: %s' % report_result['reason'])
    logger.log('METADATA REMOVED (total): %d' % report_result['total_removed'])
    logger.log('TIME: %.1fs' % report_result['elapsed'])
    if report_result.get('final_detail'):
        logger.log('FINAL DRY-RUN: %s' % report_result['final_detail'])
    if deploy_detail:
        logger.log('DEPLOY: %s' % deploy_detail)
    logger.log('========================================================')
    logger.log('RESULT:%s' % result_state)

    # ---------- finalize 4-merged/ (clean profile + WIC + manual ops) ----------
    wic_path = _finalize_prune_output(
        sf, report_result, merge_dir, src_out, original_copy,
        dest_profile_name, dest_org, logger, args.quiet, mdtype=dest_mdtype,
        clean_path=deliverable_path, preserved=preserved)
    logger.log('  OK: what-is-changed written: %s' % _rel(out_dir, wic_path))

    # Propagate a non-zero exit code when the prune did not succeed. The
    # alignment flow already does this; without it a run ending in ABORTED
    # (e.g. a deploy lock that hard-failed a section) exits 0 and is
    # indistinguishable from SUCCESS to scripts/CI that rely on the exit code.
    sys.exit(0 if result_state == 'SUCCESS' else 1)


def _reconfigure_output_encoding():
    """Force UTF-8 with a ``replace`` fallback on stdout/stderr.

    On Windows consoles that default to a legacy code page (cp1252/cp850),
    Python's strict default encoding raises ``UnicodeEncodeError`` the moment
    the banner prints its box-drawing/arrow glyphs (and the live board would
    hit the same wall). Reconfiguring to UTF-8 keeps the banner and the board
    intact on UTF-8 terminals and makes the process crash-proof everywhere
    else: any glyph the target encoding cannot represent degrades to ``?``
    instead of aborting the whole run."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            # Not a reconfigurable text stream (e.g. piped BytesIO): leave it.
            pass


def main():
    """Entry point. Catches a Ctrl-C raised outside the worker phase (e.g.
    while `sf org display` or a final dry-run is blocked) and exits cleanly
    instead of dumping a traceback."""
    _reconfigure_output_encoding()
    try:
        _main()
    except KeyboardInterrupt:
        print('\nstopped by user', file=sys.stderr)
        sys.exit(130)
