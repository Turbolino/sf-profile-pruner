# -*- coding: utf-8 -*-
"""Profile Pruner — command-line entry point.

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

from .board import Logger
from .constants import (
    APP_NAME,
    AUTHOR_NOTE,
    BANNER,
    COPYLEFT,
    DEFAULT_BATCH_SIZE,
    DISCLAIMER,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    STORY,
    __version__,
    version_ge,
)
from .merge import (READ_ONLY_FIELD_TYPE_EXACT, READ_ONLY_FIELD_TYPE_PREFIXES,
                    classify_layout_ops, diff_profiles,
                    field_permissions_editable, field_permissions_names,
                    merge_profiles, removed_non_alignable,
                    render_diff_report, render_diff_summary, write_diff_json,
                    write_diff_xml, write_manual_ops,
                    write_prune_what_is_changed, write_what_is_changed)
from .retrieve import retrieve_profiles
from .sfcli import (deploy, describe_object, dry_run, field_data_types,
                    find_sf, list_metadata, org_api_version, org_username,
                    set_deploy_start_interval)
from .stage import prune_stage
from .xmlio import load_log_entries, write_package_xml, write_sfdx_project


def _rel(base, path):
    """Render `path` relative to `base` for the user-facing console output.

    The output directory itself is always shown absolute; every other path
    (profile, project, report, …) is shown relative to it so the console stays
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
    '--dest-org', '-do',
    '--dest-profile', '-dp',
    '--batch-size', '-bs',
    '--api-version', '-av',
    '--thread-number', '-tn',
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
                         logger):
    """Detect ``fieldPermissions`` fields whose ``editable`` flag is dead
    metadata (the org always keeps them read-only), so the alignment normalizes
    them to ``false`` instead of reporting a fake grant/revoke.

    Two layers:

    1. ``FieldDefinition.DataType`` (user-independent, batched) catches the
       self-evident read-only types: Formula, Roll-Up Summary, Auto Number,
       Address, Location.
    2. ``sobject describe`` on both orgs catches the remaining system read-only
       fields whose DataType looks normal (e.g. ``LiveChatTranscript.ChatKey``
       is ``Text(200)`` and ``ServiceAppointment.AccountId`` is
       ``Lookup(Account)``). A field is read-only only when ``updateable=False``
       on BOTH orgs: a field that is merely FLS-locked on one org (e.g.
       ``Campaign.Status`` shows ``updateable=False`` only on the dev org whose
       Campaign object is locked) reports ``True`` on the other side and is kept
       as a real grant/revoke. Describe is scoped to the few objects whose
       fields actually differ on ``editable``, so it stays fast.
    """
    names = field_permissions_names(src_clean_path)
    entities = []
    for n in names:
        ent = n.split('.', 1)[0]
        if ent and ent not in entities:
            entities.append(ent)

    # --- layer 1: DataType (batched FieldDefinition query) ---
    logger.log('  ▸ fetching field types for %d object(s) to normalize '
               'read-only fields...' % len(entities))
    progress = {'last': -1}

    def _on_progress(done, total):
        pct = (done * 100) // total if total else 100
        if pct != progress['last']:
            progress['last'] = pct
            logger.log('  ▸ field types: %d/%d object(s) (%d%%)' % (done, total, pct))

    types, terr = field_data_types(sf, dest_org, entities, on_progress=_on_progress)
    if types is None:
        logger.log('  ⚠ could not fetch field types (%s); editable flags left as-is.' % terr)
        return set()
    read_only = set()
    for n in names:
        dt = types.get(n, '')
        if dt.startswith(READ_ONLY_FIELD_TYPE_PREFIXES) or dt in READ_ONLY_FIELD_TYPE_EXACT:
            read_only.add(n)
    if read_only:
        logger.log('  ✓ %d field(s) read-only by type (formula/roll-up/auto-number/address); '
                   'editable normalized to false.' % len(read_only))

    # --- layer 2: describe on both orgs for editable-differing non-formula fields ---
    src_edit = field_permissions_editable(src_clean_path)
    dst_edit = field_permissions_editable(dst_clean_path)
    candidates = [n for n in names
                  if n not in read_only
                  and n in dst_edit
                  and src_edit.get(n, '') != dst_edit.get(n)]
    if not candidates:
        return read_only
    cand_entities = []
    for n in candidates:
        ent = n.split('.', 1)[0]
        if ent and ent not in cand_entities:
            cand_entities.append(ent)
    logger.log('  ▸ describing %d object(s) on both orgs to disambiguate %d '
               'read-only system field(s)...' % (len(cand_entities), len(candidates)))

    def _updatable(org, entity):
        res, err = describe_object(sf, org, entity)
        if res is None:
            logger.log('  ⚠ describe %s on %s failed (%s); those fields left as-is.'
                       % (entity, org, err))
            return None
        fields = res.get('fields') or []
        return {
            'object_updateable': bool(res.get('updateable')),
            'fields': {str(f.get('name')): bool(f.get('updateable')) for f in fields
                       if f.get('name')},
        }

    src_describe = {}
    dst_describe = {}
    for ent in cand_entities:
        d = _updatable(dest_org, ent)
        if d is not None:
            dst_describe[ent] = d
        if src_org:
            s = _updatable(src_org, ent)
            if s is not None:
                src_describe[ent] = s

    added = []
    for n in candidates:
        ent = n.split('.', 1)[0]
        field = n.split('.', 1)[1]
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
            # File source: no source org to describe. Trust dest-only read-only
            # only when the object itself is updateable there (otherwise the
            # whole object being FLS-locked makes every field look read-only).
            read_only_on_both = (dst['object_updateable'] is True
                                 and dst['fields'][field] is False)
        if read_only_on_both:
            read_only.add(n)
            added.append(n)
    if added:
        logger.log('  ✓ %d system read-only field(s) (updateable=false on both orgs) '
                   'normalized: %s' % (len(added), ', '.join(sorted(added))))
    return read_only


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
                    poll_interval, job_timeout, logger, out_dir):
    """Deploy `profile_path` to `org` under `profile_name`.

    Salesforce creates the profile if the name does not exist and merges into it
    otherwise, so a renamed deploy is exactly "create if missing". A fresh
    micro-project is created under `.work/deploy` and removed afterwards.
    """
    proj = os.path.join(out_dir, '.work', 'deploy')
    try:
        write_sfdx_project(proj, api_version)
        manifest = os.path.join(proj, 'manifest.xml')
        write_package_xml(manifest, {'Profile': [profile_name]}, api_version)
        dst = os.path.join(proj, 'force-app', 'main', 'default', 'profiles',
                           '%s.profile-meta.xml' % profile_name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(profile_path, dst)
        return deploy(sf, manifest, org, poll_interval, job_timeout, logger, cwd=proj)
    finally:
        shutil.rmtree(proj, ignore_errors=True)


def _write_and_report_manual_ops(manual_ops, out_dir, profile_name, logger, quiet):
    """Write MANUAL-OPERATIONS.txt and, when there is work to do by hand, print
    the table at the end of the run. Returns the file path, or None when there
    are no manual operations."""
    if not manual_ops:
        logger.log('No manual operations required (every removed/off section is deploy-safe).')
        return None
    path = write_manual_ops(os.path.join(out_dir, 'MANUAL-OPERATIONS.txt'),
                            manual_ops, profile_name)
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


def _finalize_prune_output(result, merge_dir, src_out, original_copy,
                           dest_profile_name, dest_org, logger, quiet):
    """Assemble the ``4-merged/`` folder for a plain prune run: copy the clean
    profile, write ``what-is-changed.txt`` (removed dangling references +
    manual operations) and ``MANUAL-OPERATIONS.txt``. Returns the
    what-is-changed path."""
    os.makedirs(merge_dir, exist_ok=True)
    final_profile = os.path.join(merge_dir, '%s.profile-meta.xml' % dest_profile_name)
    shutil.copyfile(result['common_profile_path'], final_profile)
    manual_ops = removed_non_alignable(original_copy, result['common_profile_path'])
    removed_by_type = {}
    missing_log = os.path.join(src_out, 'missing_log.txt')
    for typ, member in load_log_entries(missing_log):
        removed_by_type.setdefault(typ, set()).add(member)
    wic_path = write_prune_what_is_changed(
        os.path.join(merge_dir, 'what-is-changed.txt'),
        removed_by_type, manual_ops, dest_org)
    _write_and_report_manual_ops(manual_ops, merge_dir, dest_profile_name,
                                 logger, quiet)
    return wic_path


def _validate_profile_dry_run(sf, org, profile_path, profile_name, api_version,
                              poll_interval, job_timeout, logger, out_dir):
    """Dry-run a single Profile package. Returns (validation, component_failures)
    where ``validation`` is ``'clean'`` on success or a failure description, and
    ``component_failures`` is the list of raw component-failure dicts (empty on
    clean)."""
    proj = os.path.join(out_dir, '.work', 'align-final')
    write_sfdx_project(proj, api_version)
    manifest = os.path.join(proj, 'manifest.xml')
    write_package_xml(manifest, {'Profile': [profile_name]}, api_version)
    dst = os.path.join(proj, 'force-app', 'main', 'default', 'profiles',
                       '%s.profile-meta.xml' % profile_name)
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
                       n_manual):
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
        'api_version': args.api_version,
        'merged_profile': os.path.relpath(merged_path, out_dir),
        'validation': validation,
        'granted': n_granted,
        'revoked': n_revoked,
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


def _resume_alignment(args, src_org, src_profile_name, dest_profile_name,
                      dest_org, do_deploy, out_dir, logger, sf):
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
    }
    current = {
        'src_org': src_org,
        'dest_org': dest_org,
        'src_profile': src_profile_name,
        'dest_profile': dest_profile_name,
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
        '4-merged', '%s.profile-meta.xml' % dest_profile_name)
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
                                 logger, out_dir)
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
              dest_profile_name, dest_org, src_org, logger):
    """Run --diff: retrieve the destination profile and compare the two profiles
    section by section (pure comparison; no prune, dry-run or deploy). Writes
    ``diff/diff-report.txt`` and prints a summary table. Exits with 0."""
    logger.log('')
    logger.log('── DIFF ───────────────────────────────────────────────')
    logger.log('  purpose: compare the source profile against the destination profile')

    username, uerr = org_username(sf, dest_org)
    if not username:
        print('ERROR: could not resolve the destination org %r: %s' % (dest_org, uerr))
        sys.exit(2)
    clean = not args.full_profile
    logger.log('  ▸ retrieving destination profile %r from %s (%s)...'
               % (dest_profile_name, dest_org, 'full' if args.full_profile else 'clean'))
    dest_profiles, derr = retrieve_profiles(username, [dest_profile_name], clean=clean)
    if not dest_profiles:
        print('ERROR: destination retrieve failed: %s' % (derr or 'no profile returned'))
        sys.exit(2)

    diff_dir = os.path.join(out_dir, 'diff')
    os.makedirs(diff_dir, exist_ok=True)
    dest_path = os.path.join(diff_dir, '%s.profile-meta.xml' % dest_profile_name)
    with open(dest_path, 'wb') as _f:
        _f.write(dest_profiles[0][1])

    src_label = '%s "%s"' % (src_org or 'file', src_profile_name)
    dst_label = '%s "%s"' % (dest_org, dest_profile_name)
    sections = diff_profiles(source_profile_path, dest_path)
    report_text = render_diff_report(sections, src_label, dst_label)
    report_path = os.path.join(diff_dir, 'diff-report.txt')
    with open(report_path, 'w', encoding='utf-8') as _f:
        _f.write(report_text + '\n')
    json_path = write_diff_json(sections, src_label, dst_label, diff_dir)
    xml_path = write_diff_xml(sections, src_label, dst_label, diff_dir)
    logger.log('  ✓ destination retrieved (%d bytes)' % len(dest_profiles[0][1]))
    logger.log('  ✓ diff report written: %s' % _rel(out_dir, report_path))
    logger.log('  ✓ diff data (JSON):     %s' % _rel(out_dir, json_path))
    logger.log('  ✓ diff data (XML):      %s' % _rel(out_dir, xml_path))

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
                   dest_profile_name, dest_org, src_org, do_deploy, logger):
    """Run --align-from-source: prune the source against the destination,
    retrieve + prune the destination, merge (source authoritative, destination-
    only entries turned off), and optionally deploy. Exits with the result code."""
    # 1. prune the source against the destination (drop source references that
    #    do not exist in the destination org).
    src_out = os.path.join(out_dir, '1-source-pruned')
    logger.log('')
    logger.log('── PHASE 1/4 · PRUNE SOURCE ───────────────────────────')
    logger.log('  purpose: drop every reference in %r that does not exist in %s'
               % (src_profile_name, dest_org))
    logger.log('  ▸ pruning the source profile against %s...' % dest_org)
    src_res = prune_stage(
        sf=sf, org=dest_org, source_profile_path=source_profile_path,
        profile_name=src_profile_name, out_dir=src_out,
        api_version=args.api_version, poll_interval=args.poll_interval,
        job_timeout=args.job_timeout, thread_number=args.thread_number,
        batch_size=args.batch_size, max_iter=args.max_iter,
        quiet=args.quiet, fresh=args.fresh, logger=logger)
    if src_res['outcome'] != 'SUCCESS':
        print('ERROR: source prune failed: %s' % (src_res.get('reason') or 'unknown error'))
        sys.exit(2)
    src_clean = src_res['common_profile_path']
    logger.log('  ✓ source pruned (%d reference(s) removed)' % src_res['total_removed'])

    # 2. retrieve the destination profile (same clean/full choice as the source).
    logger.log('')
    logger.log('── PHASE 2/4 · RETRIEVE + PRUNE DESTINATION ───────────')
    logger.log('  purpose: get a copy of the destination profile and strip its own dangling')
    logger.log('           references, so the merge starts from two valid profiles')
    username, uerr = org_username(sf, dest_org)
    if not username:
        print('ERROR: could not resolve the destination org %r: %s' % (dest_org, uerr))
        sys.exit(2)
    dest_dir = os.path.join(out_dir, '2-destination-retrieved')
    os.makedirs(dest_dir, exist_ok=True)
    logger.log('  ▸ retrieving the destination profile %r from %s...'
               % (dest_profile_name, dest_org))
    clean = not args.full_profile
    dest_profiles, derr = retrieve_profiles(username, [dest_profile_name], clean=clean)
    if not dest_profiles:
        print('ERROR: destination retrieve failed: %s' % (derr or 'no profile returned'))
        sys.exit(2)
    dest_raw = os.path.join(dest_dir, '%s.profile-meta.xml' % dest_profile_name)
    with open(dest_raw, 'wb') as _f:
        _f.write(dest_profiles[0][1])
    logger.log('  ✓ destination retrieved (%d bytes)' % len(dest_profiles[0][1]))

    # 3. prune the destination against itself (drop its own dangling references).
    dst_out = os.path.join(out_dir, '3-destination-pruned')
    logger.log('  ▸ pruning the destination profile against itself...')
    dst_res = prune_stage(
        sf=sf, org=dest_org, source_profile_path=dest_raw,
        profile_name=dest_profile_name, out_dir=dst_out,
        api_version=args.api_version, poll_interval=args.poll_interval,
        job_timeout=args.job_timeout, thread_number=args.thread_number,
        batch_size=args.batch_size, max_iter=args.max_iter,
        quiet=args.quiet, fresh=args.fresh, logger=logger)
    if dst_res['outcome'] != 'SUCCESS':
        print('ERROR: destination prune failed: %s' % (dst_res.get('reason') or 'unknown error'))
        sys.exit(2)
    dst_clean = dst_res['common_profile_path']
    logger.log('  ✓ destination pruned (%d reference(s) removed)' % dst_res['total_removed'])

    # 4. merge: source authoritative; destination-only entries turned off.
    logger.log('')
    logger.log('── PHASE 3/4 · MERGE (source authoritative) ───────────')
    logger.log('  purpose: keep every source entry verbatim and turn OFF every destination-only entry')
    logger.log('  ▸ merging source → destination...')
    if not src_org:
        logger.log('  ⚠ NOTE (-pp file source): the source has no org to describe, so read-only '
                   'detection checks only the destination. A field editable in the source but '
                   'read-only-looking in the destination could be misclassified as read-only; '
                   'use -so/-sp to verify both sides.')
    read_only_fields = _read_only_field_set(
        sf, src_org, dest_org, src_clean, dst_clean, logger)
    merged_xml, manual_ops, changes = merge_profiles(
        src_clean, dst_clean, dest_env=dest_org, read_only_fields=read_only_fields)
    merge_dir = os.path.join(out_dir, '4-merged')
    os.makedirs(merge_dir, exist_ok=True)
    merged_path = os.path.join(merge_dir, '%s.profile-meta.xml' % dest_profile_name)
    with open(merged_path, 'wb') as _f:
        _f.write(merged_xml)

    # 5. validate the merged profile BEFORE writing any report. If the dry-run
    #    surfaces residual dangling references, self-heal by re-pruning the
    #    merged profile and recomputing the diff, so MANUAL-OPERATIONS.txt and
    #    what-is-changed.txt always describe the FINAL (deployable) profile.
    logger.log('')
    logger.log('── PHASE 4/4 · VALIDATE + DEPLOY ──────────────────────')
    logger.log('  purpose: confirm the merged profile is deployable before writing reports (and deploying)')
    logger.log('  ▸ validating the merged profile with a dry-run...')
    validation, _cf = _validate_profile_dry_run(
        sf, dest_org, merged_path, dest_profile_name, args.api_version,
        args.poll_interval, args.job_timeout, logger, out_dir)
    if validation != 'clean':
        logger.log('  ⚠ dry-run not clean (%s); self-healing the merged profile...' % validation)
        heal_out = os.path.join(out_dir, '.work', 'align-heal')
        heal_res = prune_stage(
            sf=sf, org=dest_org, source_profile_path=merged_path,
            profile_name=dest_profile_name, out_dir=heal_out,
            api_version=args.api_version, poll_interval=args.poll_interval,
            job_timeout=args.job_timeout, thread_number=args.thread_number,
            batch_size=args.batch_size, max_iter=args.max_iter,
            quiet=args.quiet, fresh=True, logger=logger)
        if heal_res['outcome'] != 'SUCCESS':
            print('ERROR: merged self-heal prune failed: %s'
                  % (heal_res.get('reason') or 'unknown error'))
            sys.exit(2)
        shutil.copyfile(heal_res['common_profile_path'], merged_path)
        shutil.rmtree(heal_out, ignore_errors=True)
        # Recompute the diff against the healed (final) merged profile.
        merged_xml, manual_ops, changes = merge_profiles(
            merged_path, dst_clean, dest_env=dest_org, read_only_fields=read_only_fields)
        with open(merged_path, 'wb') as _f:
            _f.write(merged_xml)
        validation, _cf = _validate_profile_dry_run(
            sf, dest_org, merged_path, dest_profile_name, args.api_version,
            args.poll_interval, args.job_timeout, logger, out_dir)
    logger.log('  ✓ merged dry-run: %s' % validation)

    n_granted = sum(1 for c in changes if c['kind'] == 'grant')
    n_revoked = sum(1 for c in changes if c['kind'] == 'revoke')
    logger.log('  ✓ merge result: %d granted (ON), %d revoked (OFF), %d manual'
               % (n_granted, n_revoked, len(manual_ops)))

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
            logger.log('  ⚠ could not list Layout metadata (%s); '
                       'layoutAssignments marked for manual check.' % lerr)
        classify_layout_ops(manual_ops, changes, layout_count)

    # 7. reports (only now, on the final validated merged profile).
    logger.log('  ▸ writing reports...')
    manual_ops_path = _write_and_report_manual_ops(
        manual_ops, merge_dir, dest_profile_name, logger, args.quiet)
    change_path = write_what_is_changed(
        os.path.join(merge_dir, 'what-is-changed.txt'), changes, dest_org)
    logger.log('  ✓ what-is-changed written: %s' % _rel(out_dir, change_path))

    # 7b. persist the run identity + validated merge, so a later invocation on
    #     this folder can deploy the cache instead of redoing the pipeline.
    state_path = None
    if validation == 'clean':
        state_path = _write_align_state(
            out_dir, args, src_profile_name, dest_profile_name, dest_org,
            merged_path, validation, n_granted, n_revoked, len(manual_ops))
        logger.log('  ✓ run state saved: %s' % _rel(out_dir, state_path))

    # 8. optional deploy of the merged profile (only after a clean validation).
    deploy_detail = ''
    outcome = 'SUCCESS'
    if validation != 'clean':
        outcome = 'VALIDATION_FAILED'
    elif do_deploy:
        logger.log('  ▸ deploying the merged profile to %s...' % dest_org)
        ok, deploy_detail = _deploy_profile(sf, dest_org, merged_path, dest_profile_name,
                                            args.api_version, args.poll_interval,
                                            args.job_timeout, logger, out_dir)
        outcome = 'SUCCESS' if ok else 'DEPLOY_FAILED'

    logger.log('')
    logger.log('========================================================')
    logger.log('  ALIGNMENT RESULT')
    logger.log('========================================================')
    logger.log('OUTCOME: %s' % outcome)
    logger.log('GRANTED (turned ON): %d' % n_granted)
    logger.log('REVOKED (turned OFF): %d' % n_revoked)
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
        description='Removes from a Salesforce Profile every reference to metadata that '
                    'does not exist in the destination org. Dry-run only by default; use '
                    '--deploy-on-success to deploy the cleaned profile at the end '
                    '(add --sure to skip the Y/N confirmation).',
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
    g_input.add_argument('-fp', '--full-profile', action='store_true',
                         help='retrieve the full (non-stripped) profile; the default is a '
                              'clean retrieve (loginIpRanges/loginHours/userLicense removed)')

    g_dest = ap.add_argument_group('destination')
    g_dest.add_argument('-do', '--dest-org', default=None,
                        help='destination org alias/username (dry-run and optional deploy '
                             'target). Optional: omit it (with -so + -sp) for retrieve only.')
    g_dest.add_argument('-dp', '--dest-profile', default=None,
                        help='name to deploy the cleaned profile under; if it does not exist '
                             'in the org it is created. Default: the source profile name')

    g_out = ap.add_argument_group('output')
    g_out.add_argument('-od', '--out-dir', default=os.getcwd(),
                       help='output directory for report/packages/logs '
                            '(default: the current directory)')

    g_tune = ap.add_argument_group('tuning')
    g_tune.add_argument('-bs', '--batch-size', type=int, default=DEFAULT_BATCH_SIZE, metavar='N',
                        help='split a section into parallel chunks of N entries '
                             '(min 100, max 4000, default 200); 0 disables splitting')
    g_tune.add_argument('-tn', '--thread-number', type=int, default=16, metavar='X',
                        help='max parallel worker threads, 1..64 (default: 16); capped to '
                             'the number of work items (chunks) to prune')
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
    g_act.add_argument('--align-from-source', action='store_true',
                       help='align the destination profile to the source (works with a '
                            'source file via 1, or a source org via 2): prune the source '
                            'against the destination, then retrieve and prune the '
                            'destination too, and turn OFF any permission that exists only '
                            'in the destination (hidden tab/app, disabled '
                            'field/object/class/page/flow), so the destination matches the '
                            'source. Without it, destination-only permissions are left '
                            'untouched. Runs automatically (no prompt): the pre-align '
                            'destination profile is saved in the run folder. Re-running '
                            'on the same output folder with the same -so/-do/-sp/-dp '
                            'deploys the cached merge; use --fresh to restart from '
                            'scratch.')
    g_act.add_argument('--deploy-on-success', action='store_true',
                       help='deploy the cleaned profile to --dest-org once pruning succeeds. '
                            'Modifies the org: asks for Y/N confirmation (a No runs the '
                            'dry-run only). Use as [--deploy-on-success [--sure]]: with '
                            '--sure the deploy runs immediately without the Y/N prompt.')
    g_act.add_argument('--sure', action='store_true',
                       help='skip the Y/N confirmation of --deploy-on-success and deploy '
                            'without asking. Has no effect without --deploy-on-success.')
    g_act.add_argument('--diff', action='store_true',
                       help='compare the source profile against the destination profile and '
                            'write a diff report (diff/diff-report.txt): what is only in one, '
                            'only in the other, and what differs for entries present in both. '
                            'Pure read-only comparison: no prune, no dry-run, no deploy. '
                            'Works with a source file (via 1) or a source org (via 2).')
    g_act.add_argument('--fresh', action='store_true',
                       help='ignore any previous state and restart from the original')
    g_act.add_argument('--quiet', action='store_true',
                       help='suppress the progressive stdout output')
    g_act.add_argument('--tell-me-a-story', action='store_true',
                       help='Turbolino will tell you a fascinating story about code, '
                            'algorithms and copyright.')
    g_act.add_argument('-V', '--version', action='version',
                       version='%s v%s - %s' % (APP_NAME, __version__, AUTHOR_NOTE))

    args = ap.parse_args()

    # ---------- numeric validation ----------
    if not (1 <= args.thread_number <= 64):
        print('ERROR: --thread-number must be between 1 and 64 (got %d).' % args.thread_number)
        sys.exit(2)

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

    # ---------- mode detection ----------
    # via 1 (file):           -pp + -do
    # via 2 (org):            -so + -sp + -do + -dp   (retrieve -> prune -> dry-run/deploy)
    # via 3 (retrieve only):  -so + -sp                (no prune, no dry-run/deploy)
    if args.src_org or args.src_profile:
        if not args.src_org or not args.src_profile:
            print('ERROR: --src-org (-so) and --src-profile (-sp) must be provided together.')
            sys.exit(2)
        if bool(args.dest_org) != bool(args.dest_profile):
            print('ERROR: --dest-org (-do) and --dest-profile (-dp) must be provided together.')
            sys.exit(2)
        if args.profile_path:
            print('ERROR: --profile-path (-pp) and --src-org (-so) are mutually exclusive.')
            sys.exit(2)
        mode = 'org' if args.dest_org else 'retrieve'
    else:
        if not args.profile_path:
            print('ERROR: provide --profile-path (-pp) [via 1], '
                  '--src-org (-so) + --src-profile (-sp) + -do + -dp [via 2], '
                  'or -so + -sp alone [via 3, retrieve only].')
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
        print('Retrieving profile %r from org %s (%s)...' %
              (args.src_profile, args.src_org, 'full' if args.full_profile else 'clean'))
        profiles, rerr = retrieve_profiles(username, [args.src_profile], clean=clean)
        if not profiles:
            print('ERROR: retrieve failed: %s' % (rerr or 'no profile returned'))
            sys.exit(2)
        full_name, xml = profiles[0]
        out_path = os.path.join(out_dir, '%s.profile-meta.xml' % full_name)
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

    # ---------- names and mode ----------
    out_dir = os.path.abspath(args.out_dir or os.getcwd())
    dest_org = args.dest_org
    if mode == 'org':
        src_org = args.src_org
        src_profile_name = args.src_profile
        dest_profile_name = args.dest_profile
        profile_path = None
    else:
        src_org = None
        profile_path = os.path.abspath(args.profile_path)
        base = os.path.basename(profile_path)
        src_profile_name = re.sub(r'\.profile-meta\.xml$', '', base)
        dest_profile_name = args.dest_profile or src_profile_name

    original_copy = os.path.join(out_dir, 'ORIGINAL_%s.profile-meta.xml' % src_profile_name)
    run_log = os.path.join(out_dir, 'run.log')

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        print('ERROR: cannot create output directory %r: %s' % (out_dir, e))
        sys.exit(2)

    logger = Logger(args.quiet, run_log)

    logger.log('── SETUP ───────────────────────────────────────────────')
    logger.log('  purpose: locate the CLI, resolve the API version and confirm the org context')
    logger.log('  ▸ locating the Salesforce CLI (sf)...')
    sf = find_sf(args.sf)
    logger.file_log('sf          : %s' % sf)
    logger.log('  ✓ Salesforce CLI ready')

    # Informational, non-blocking upgrade notice (stderr so it never disturbs
    # the stdout progress/board; skipped silently on any network failure).
    for line in _check_for_update():
        print(line, file=sys.stderr)

    # ---------- resume a completed alignment (same folder + same parameters) ----------
    # If this folder already holds a validated alignment with identical
    # -so/-do/-sp/-dp, deploy the cached merge (or just report it) instead of
    # redoing the whole pipeline. --fresh bypasses this and restarts from scratch.
    if do_align:
        resume_code = _resume_alignment(
            args, src_org, src_profile_name, dest_profile_name, dest_org,
            do_deploy, out_dir, logger, sf)
        if resume_code is not None:
            sys.exit(resume_code)

    # ---------- API version: explicit or detected from the destination org ----------
    api_source = 'explicit'
    if args.api_version is None:
        logger.log('  ▸ detecting the latest API version from org %s...' % dest_org)
        args.api_version, averr = org_api_version(sf, dest_org)
        if args.api_version is None:
            print('ERROR: could not detect the latest API version from org %r:' % dest_org)
            print('       %s' % (averr or 'unknown error'))
            print('       Pass --api-version <X.Y> explicitly to continue.')
            sys.exit(2)
        api_source = 'auto (latest for org)'
    logger.log('  ✓ API v%s (%s)' % (args.api_version, api_source))

    # ---------- retrieve the source profile (via 2) ----------
    if mode == 'org':
        username, uerr = org_username(sf, src_org)
        if not username:
            print('ERROR: could not resolve the org username for %r: %s' % (src_org, uerr))
            sys.exit(2)
        clean = not args.full_profile
        logger.log('  ▸ retrieving profile %r from org %s (%s)...' %
                   (src_profile_name, src_org, 'full' if args.full_profile else 'clean'))
        profiles, rerr = retrieve_profiles(username, [src_profile_name], clean=clean)
        if not profiles:
            print('ERROR: retrieve failed: %s' % (rerr or 'no profile returned'))
            sys.exit(2)
        full_name, xml = profiles[0]
        with open(original_copy, 'wb') as _f:
            _f.write(xml)
        profile_path = original_copy
        logger.log('  ✓ retrieved %s (%d bytes)' % (full_name, len(xml)))
        if rerr:
            logger.log('  ⚠ %s' % rerr)

    if mode == 'org':
        logger.log('  context: source %r @ %s  →  destination %r @ %s' %
                   (src_profile_name, src_org, dest_profile_name, dest_org))
    else:
        logger.log('  context: source file %r  →  destination %r @ %s' %
                   (src_profile_name, dest_profile_name, dest_org))

    # ---------- diff (--diff): pure profile-to-profile comparison ----------
    if args.diff:
        _run_diff(sf, args, out_dir, profile_path, src_profile_name,
                  dest_profile_name, dest_org, src_org, logger)
        sys.exit(1)  # _run_diff exits; safety net only

    # ---------- alignment (--align-from-source): separate flow ----------
    if do_align:
        _run_alignment(sf, args, out_dir, profile_path, src_profile_name,
                       dest_profile_name, dest_org, src_org, do_deploy, logger)
        sys.exit(1)  # _run_alignment exits; safety net only

    # ---------- run the prune stage ----------
    # Coherent with the alignment flow: the prune output lives in
    # 1-source-pruned/ and the final deliverable + reports in 4-merged/.
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
    )

    # ---------- already clean? (resume short-circuit) ----------
    if result['already_clean'] and not do_deploy:
        _finalize_prune_output(result, merge_dir, src_out, original_copy,
                               dest_profile_name, dest_org, logger, args.quiet)
        print('Profile "%s" is already clean (previous run: SUCCESS). Nothing to do.' % src_profile_name)
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

        prev_sig = _sig(report_result['common_profile_path'])
        round_no = 1
        while True:
            logger.log('Deploying the cleaned profile %r to org %s (round %d)...'
                       % (dest_profile_name, dest_org, round_no))
            ok, detail = _deploy_profile(sf, dest_org, report_result['common_profile_path'],
                                         dest_profile_name, args.api_version,
                                         args.poll_interval, args.job_timeout,
                                         logger, out_dir)
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
            )
            result_state = result['outcome']
            if result_state != 'SUCCESS':
                break
            if not result.get('already_clean'):
                report_result = result  # a prune round that actually did work
            new_sig = _sig(result['common_profile_path'])
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
        report_result, merge_dir, src_out, original_copy,
        dest_profile_name, dest_org, logger, args.quiet)
    logger.log('  ✓ what-is-changed written: %s' % _rel(out_dir, wic_path))


def main():
    """Entry point. Catches a Ctrl-C raised outside the worker phase (e.g.
    while `sf org display` or a final dry-run is blocked) and exits cleanly
    instead of dumping a traceback."""
    try:
        _main()
    except KeyboardInterrupt:
        print('\nstopped by user', file=sys.stderr)
        sys.exit(130)
