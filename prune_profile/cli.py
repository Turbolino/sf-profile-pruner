# -*- coding: utf-8 -*-
"""Profile Pruner — command-line entry point.

Removes from a Salesforce Profile every reference to metadata that does NOT
exist in the destination org. Pruning combines a Metadata API + Tooling API
pre-pass with an authoritative `sf project deploy start --dry-run`, and never
modifies the org; with `--deploy-on-success` the cleaned profile is deployed at
the end.

Two input modes:
  * via 1 (file):   `--profile-path`  + `--dest-org`
  * via 2 (org):    `--src-org` + `--src-profile` + `--dest-org` + `--dest-profile`
                    (the profile is retrieved from the source org first)

Algorithm:
  The profile is split by its top-level sections (`classAccesses`,
  `fieldPermissions`, `tabVisibilities`, … — discovered dynamically from the
  XML). Up to `--thread-number` workers run in parallel; each worker takes a
  section and, in a loop, builds a sub-package with only that section, starts
  an async deploy dry-run and polls its job via `sf project deploy report`
  until terminal, then removes every offending entry. COMMON/ and MISSING/ are
  reassembled from the surviving entries in the original document order under
  a mutex. A final dry-run of the whole assembled package gates `SUCCESS`.
"""

import argparse
import json
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque

from lxml import etree

from .board import Control, Logger, PrefetchStatus, Progress, StatusBoard
from .constants import (
    APP_NAME,
    AUTHOR_NOTE,
    BANNER,
    BLOCK_MAP,
    COPYLEFT,
    DEFAULT_BATCH_SIZE,
    DISCLAIMER,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    STORY,
    __version__,
    version_ge,
)
from .engine import prefetch_dangling, prefetch_standard_dangling, worker
from .retrieve import retrieve_profiles
from .sfcli import (deploy, dry_run, find_sf, org_api_version, org_username,
                    set_deploy_start_interval)
from .state import SharedState
from .xmlio import (
    load_log_entries,
    prunable_blocks,
    read_outcome,
    remove_from_list,
    remove_temp_project,
    resolve_error,
    write_package_xml,
    write_sfdx_project,
)

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
    # deprecated aliases (still accepted)
    '--org', '--profile-name', '--max-elem-per-section',
}

EXAMPLES = '''\
Examples:
  # Prune a profile file and dry-run against the destination org (output in the current dir)
  sfpp -pp "Admin.profile-meta.xml" -do mysandbox

  # Prune and deploy under the same name (creates the profile if it does not exist)
  sfpp -pp "Admin.profile-meta.xml" -do prod --deploy-on-success

  # Deploy the cleaned profile under a different name
  sfpp -pp "Admin.profile-meta.xml" -do prod -dp "Admin_Clean" --deploy-on-success

  # Retrieve from a source org, prune and deploy to a destination org
  sfpp -so devorg -sp "Sales Rep" -do mysandbox -dp "Sales Rep" --deploy-on-success

  # Same org, full (non-stripped) retrieve, dry-run only
  sfpp -so mysandbox -sp "Sales Rep" -do mysandbox -dp "Sales Rep" -fp

  # Custom output dir, batch size and threads
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
        'Update with:  python -m pip install --upgrade sf-profile-pruner',
    ]


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
                    '--deploy-on-success to deploy the cleaned profile at the end.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES,
    )

    g_input = ap.add_argument_group('input source')
    g_input.add_argument('-pp', '--profile-path', default=None,
                         help='path of the source .profile-meta.xml file (via 1: file)')
    g_input.add_argument('-so', '--src-org', default=None,
                         help='source org alias/username to retrieve the profile from '
                              '(via 2: org)')
    g_input.add_argument('-sp', '--src-profile', default=None,
                         help='name of the Profile to retrieve from --src-org (via 2)')
    g_input.add_argument('-fp', '--full-profile', action='store_true',
                         help='retrieve the full (non-stripped) profile; the default is a '
                              'clean retrieve (loginIpRanges/loginHours/userLicense removed)')

    g_dest = ap.add_argument_group('destination')
    g_dest.add_argument('-do', '--dest-org', default=None,
                        help='destination org alias/username (dry-run and optional deploy target)')
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
    g_tune.add_argument('--deploy-throttle-ms', type=int, default=1000, metavar='MS',
                        help='minimum interval between deploy start submissions, in ms '
                             '(default: 1000); 0 disables the throttle')
    g_tune.add_argument('--poll-interval', type=float, default=5.0,
                        help='seconds to wait between status polls of a deploy job (default: 5)')
    g_tune.add_argument('--max-iter', type=int, default=5000,
                        help='iteration cap (default: 5000)')
    g_tune.add_argument('--sf', default='sf',
                        help='command/path of the Salesforce CLI (default: sf)')

    g_act = ap.add_argument_group('behavior')
    g_act.add_argument('--deploy-on-success', action='store_true',
                       help='deploy the cleaned profile to --dest-org once pruning succeeds '
                            '(default: dry-run only, the org is never modified)')
    g_act.add_argument('--fresh', action='store_true',
                       help='ignore any previous state and restart from the original')
    g_act.add_argument('--quiet', action='store_true',
                       help='suppress the progressive stdout output')
    g_act.add_argument('--tell-me-a-story', action='store_true',
                       help='Turbolino will tell you a fascinating story about code, '
                            'algorithms and copyright.')
    g_act.add_argument('-V', '--version', action='version',
                       version='%s v%s - %s' % (APP_NAME, __version__, AUTHOR_NOTE))

    # Deprecated aliases: accepted for compatibility, hidden from help, warned.
    ap.add_argument('--org', dest='dep_org', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--profile-name', dest='dep_profile_name', default=None, help=argparse.SUPPRESS)
    ap.add_argument('--retrieve-full', dest='dep_retrieve_full', action='store_true',
                    default=False, help=argparse.SUPPRESS)
    ap.add_argument('--max-elem-per-section', dest='dep_max_elem', type=int, default=None,
                    help=argparse.SUPPRESS)

    args = ap.parse_args()

    # ---------- deprecated aliases -> new names (with a warning) ----------
    if args.dep_org is not None:
        print('WARNING: --org is deprecated; use --dest-org (-do).', file=sys.stderr)
        args.dest_org = args.dep_org
    if args.dep_profile_name is not None:
        print('WARNING: --profile-name is deprecated; use --dest-profile (-dp).', file=sys.stderr)
        args.dest_profile = args.dep_profile_name
    if args.dep_retrieve_full:
        print('WARNING: --retrieve-full is deprecated; use --full-profile (-fp).', file=sys.stderr)
        args.full_profile = True
    if args.dep_max_elem is not None:
        print('WARNING: --max-elem-per-section is deprecated; use --batch-size (-bs).', file=sys.stderr)
        args.batch_size = args.dep_max_elem

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

    # ---------- mode detection: via 1 (file) or via 2 (org) ----------
    via2 = bool(args.src_org or args.src_profile)
    if via2:
        missing = []
        if not args.src_org:
            missing.append('--src-org (-so)')
        if not args.src_profile:
            missing.append('--src-profile (-sp)')
        if not args.dest_org:
            missing.append('--dest-org (-do)')
        if not args.dest_profile:
            missing.append('--dest-profile (-dp)')
        if missing:
            print('ERROR: via 2 (retrieve from org) requires: %s.' % ', '.join(missing))
            sys.exit(2)
        if args.profile_path:
            print('ERROR: --profile-path (-pp) and --src-org (-so) are mutually exclusive.')
            sys.exit(2)
    else:
        if not args.profile_path:
            print('ERROR: provide either --profile-path (-pp) [via 1] or '
                  '--src-org (-so) + --src-profile (-sp) [via 2].')
            sys.exit(2)
        if not args.dest_org:
            print('ERROR: --dest-org (-do) is required (the dry-run/deploy target).')
            sys.exit(2)
        if not os.path.isfile(args.profile_path):
            print('ERROR: profile not found: %s' % args.profile_path)
            sys.exit(2)

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
    if via2:
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

    common_dir = os.path.join(out_dir, 'COMMON')
    missing_dir = os.path.join(out_dir, 'MISSING')
    work_dir = os.path.join(out_dir, '.work')
    missing_log = os.path.join(out_dir, 'missing_log.txt')
    report = os.path.join(out_dir, 'report.txt')
    run_log = os.path.join(out_dir, 'run.log')
    original_copy = os.path.join(out_dir, 'ORIGINAL_%s.profile-meta.xml' % src_profile_name)

    # Always a minimal micro-project inside --out-dir (never the caller's project).
    project_dir = os.path.join(work_dir, 'project')

    # Early logger: created before any slow step so the startup is never silent.
    for d in (out_dir, common_dir, missing_dir, work_dir):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print('ERROR: cannot create output directory %r: %s' % (d, e))
            print('       If the path ends with a quote or is otherwise malformed,')
            print('       check the --out-dir value (PowerShell may have mangled a')
            print('       trailing backslash).')
            sys.exit(2)

    logger = Logger(args.quiet, run_log)

    # Informational, non-blocking upgrade notice (stderr so it never disturbs
    # the stdout progress/board; skipped silently on any network failure).
    logger.log('Startup: checking for a newer release...')
    for line in _check_for_update():
        print(line, file=sys.stderr)
    logger.log('Startup: update check done.')

    logger.log('Startup: locating the Salesforce CLI (sf)...')
    sf = find_sf(args.sf)
    logger.log('sf          : %s' % sf)

    def deploy_cleaned(common_profile, dest_profile_name, dest_org):
        """Deploy the cleaned profile under dest_profile_name (renaming if needed).

        Salesforce creates the profile if the name does not exist and merges
        into it otherwise, so a renamed deploy is exactly "create if missing".
        """
        deploy_manifest = os.path.join(project_dir, 'manifest.xml')
        write_package_xml(deploy_manifest, {'Profile': [dest_profile_name]}, args.api_version)
        deploy_profile = os.path.join(project_dir, 'force-app', 'main', 'default',
                                      'profiles', '%s.profile-meta.xml' % dest_profile_name)
        os.makedirs(os.path.dirname(deploy_profile), exist_ok=True)
        shutil.copyfile(common_profile, deploy_profile)
        return deploy(sf, deploy_manifest, dest_org, args.poll_interval,
                      args.job_timeout, logger, cwd=project_dir)

    # ---------- API version: explicit or detected from the destination org ----------
    api_source = 'explicit'
    if args.api_version is None:
        logger.log('Startup: detecting the latest API version from org %s...' % dest_org)
        args.api_version, averr = org_api_version(sf, dest_org)
        if args.api_version is None:
            print('ERROR: could not detect the latest API version from org %r:' % dest_org)
            print('       %s' % (averr or 'unknown error'))
            print('       Pass --api-version <X.Y> explicitly to continue.')
            sys.exit(2)
        api_source = 'auto (latest for org)'

    logger.log('Startup: preparing the working copy and project scaffolding...')
    if via2:
        username, uerr = org_username(sf, src_org)
        if not username:
            print('ERROR: could not resolve the org username for %r: %s' % (src_org, uerr))
            sys.exit(2)
        clean = not args.full_profile
        logger.log('Retrieve : fetching profile %r from org %s (%s)...' %
                   (src_profile_name, src_org, 'full' if args.full_profile else 'clean'))
        profiles, rerr = retrieve_profiles(username, [src_profile_name], clean=clean)
        if not profiles:
            print('ERROR: retrieve failed: %s' % (rerr or 'no profile returned'))
            sys.exit(2)
        full_name, xml = profiles[0]
        with open(original_copy, 'wb') as _f:
            _f.write(xml)
        profile_path = original_copy
        logger.log('Retrieve : %s -> %s (%d bytes)' % (full_name, original_copy, len(xml)))
        if rerr:
            logger.log('Retrieve : warning: %s' % rerr)
    # Untouched copy of the source (kept for reconstruction on resume).
    if os.path.abspath(profile_path) != os.path.abspath(original_copy):
        shutil.copyfile(profile_path, original_copy)

    # Minimal project scaffolding (a single sfdx-project.json is enough).
    if not os.path.isfile(os.path.join(project_dir, 'sfdx-project.json')):
        write_sfdx_project(project_dir, args.api_version)

    logger.log('Profile     : %s' % profile_path)
    logger.log('Name        : %s' % src_profile_name)
    logger.log('Deploy as   : %s' % dest_profile_name)
    logger.log('Dest org    : %s' % dest_org)
    if via2:
        logger.log('Source org  : %s' % src_org)
    logger.log('Project     : %s' % project_dir)
    logger.log('Output      : %s' % out_dir)
    logger.log('API version : %s (%s)' % (args.api_version, api_source))

    # ---------- already clean? ----------
    prev_outcome = read_outcome(report)
    common_profile = os.path.join(common_dir, '%s.profile-meta.xml' % src_profile_name)
    if (not args.fresh) and prev_outcome == 'SUCCESS' and os.path.isfile(common_profile):
        if args.deploy_on_success:
            logger.log('Profile already clean; deploying %r to org %s...' % (dest_profile_name, dest_org))
            ok, detail = deploy_cleaned(common_profile, dest_profile_name, dest_org)
            remove_temp_project(work_dir, out_dir)
            print('%s: %s' % ('DEPLOY OK' if ok else 'DEPLOY FAILED', detail))
            sys.exit(0 if ok else 1)
        remove_temp_project(work_dir, out_dir)
        print('Profile "%s" is already clean (previous run: SUCCESS). Nothing to do.' % src_profile_name)
        sys.exit(0)

    # ---------- clean run (--fresh): clear previous outputs ----------
    if args.fresh:
        for p in (missing_log, report, run_log):
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(common_profile):
            os.remove(common_profile)
        missing_pkg = os.path.join(missing_dir, 'package.xml')
        if os.path.exists(missing_pkg):
            os.remove(missing_pkg)
        todeploy = os.path.join(common_dir, 'toDeploy.xml')
        if os.path.exists(todeploy):
            os.remove(todeploy)
        logger.log('CLEAN RUN (--fresh): outputs cleared.')

    # ---------- load original, split into per-section groups ----------
    logger.log('Reading the profile and splitting it into sections...')
    tree = etree.parse(original_copy)
    root = tree.getroot()
    original_seq = [c for c in root if isinstance(c.tag, str)]
    groups = OrderedDict()
    for el in original_seq:
        block = el.tag.split('}')[-1]
        groups.setdefault(block, []).append(el)
    logger.log('Read %d section(s) with %d entry(ies) in total.' % (len(groups), len(original_seq)))

    # ---------- apply recorded removals (resume) ----------
    # Capture the true original section sizes first, so the board keeps showing
    # cumulative deleted/initial across resumptions instead of resetting.
    original_counts = {block: len(els) for block, els in groups.items()}
    removed_by_type = {}
    for typ, member in load_log_entries(missing_log):
        removed_by_type.setdefault(typ, set()).add(member)
        for block, child, mode in BLOCK_MAP.get(typ, []):
            g = groups.get(block)
            if g is None:
                continue
            g, _n = remove_from_list(g, block, member)
            groups[block] = g

    total_resumed = sum(len(v) for v in removed_by_type.values())
    if total_resumed:
        logger.log('RESUME: %d metadata already removed.' % total_resumed)
    else:
        logger.log('FIRST RUN.')

    # ---------- shared state + initial outputs ----------
    logger.log('Writing the initial COMMON/MISSING outputs...')
    common_profile_path = os.path.join(common_dir, '%s.profile-meta.xml' % src_profile_name)
    missing_pkg = os.path.join(missing_dir, 'package.xml')
    todeploy_pkg = os.path.join(common_dir, 'toDeploy.xml')
    state = SharedState(groups, original_seq, logger, missing_log, common_profile_path,
                        missing_pkg, todeploy_pkg, src_profile_name, args.api_version)
    state.removed_by_type = removed_by_type
    state.initial_counts = original_counts
    state.seq = total_resumed  # continue the error-log sequence on resume
    with state.lock:
        state._write_outputs_locked()

    # ---------- Metadata API pre-pass: prune dangling references fast ----------
    # Enumerates existing metadata per type (`sf org list metadata`) and removes
    # every reference that has no matching component, before the dry-run loop.
    # This catches login flows, the newest Profile sections (agentAccesses,
    # genComputingSummaryDefAccesses) and any other listable type in one shot.
    # The pre-fetch phase owns the console via a single in-place status line
    # (message + elapsed) so it never looks frozen; the logger keeps writing
    # the full history to run.log. In non-TTY mode the status line is a no-op
    # and the per-type messages keep flowing to stdout through the logger.
    ps = PrefetchStatus(args.quiet)
    if ps.active:
        logger.console = False
    ps.start()
    try:
        pref = prefetch_dangling(sf, dest_org, state.groups, args.api_version,
                                 logger, status=ps.update)
    finally:
        ps.stop()
        if ps.active:
            logger.console = not args.quiet
    if pref:
        n = state.apply_global_removals(pref, problem='missing in org (pre-fetch)')
        logger.log('PREFETCH: removed %d dangling reference(s) via listMetadata.' % n)
    else:
        logger.log('PREFETCH: no dangling references found via listMetadata.')

    # ---------- standard pre-pass: prune dangling standard metadata ----------
    # Queries the org Tooling API (TabDefinition / EntityDefinition /
    # FieldDefinition) for the standard tabs/objects/fields the profile
    # references, and removes those that do not exist. This catches the class of
    # error the dry-run never surfaces (e.g. "can't edit tab settings for X, as
    # it's not a valid tab"), where an object exists but has no editable tab.
    ps = PrefetchStatus(args.quiet)
    if ps.active:
        logger.console = False
    ps.start()
    try:
        pref = prefetch_standard_dangling(sf, dest_org, state.groups,
                                          logger, status=ps.update)
    finally:
        ps.stop()
        if ps.active:
            logger.console = not args.quiet
    by_source = {}
    for typ, member, source in pref:
        by_source.setdefault(source, []).append((typ, member))
    for source in sorted(by_source):
        n = state.apply_global_removals(
            by_source[source],
            problem='missing in org (standard pre-fetch: %s)' % source)
        logger.log('STD-PREFETCH [%s]: removed %d dangling reference(s).' % (source, n))
    if not pref:
        logger.log('STD-PREFETCH: no dangling standard references found.')

    # ---------- queue of prunable chunks (sections split into chunks) ----------
    # No workers are running yet, so reading `state.groups` is safe here; each
    # split_chunks() call takes the state lock itself.
    queue = deque()
    section_count = 0
    for block in state.groups:
        if block in prunable_blocks() and state.groups[block]:
            section_count += 1
            n_chunks = state.split_chunks(block, args.batch_size)
            for chunk_id in range(n_chunks):
                queue.append((block, chunk_id))
    logger.log('Sections to prune: %d (split into %d work item(s))' % (section_count, len(queue)))

    t0 = time.time()
    result_reason = ''

    if not queue:
        result_state = 'SUCCESS'
        logger.log('Nothing to prune: all sections already clean.')
    else:
        # ---------- launch workers (one per workable section, capped) ----------
        workers_dir = os.path.join(work_dir, 'workers')
        queue_lock = threading.Lock()
        progress = Progress(total=len(queue))
        control = Control()
        # Never spawn more threads than there are work items to prune.
        n_workers = min(args.thread_number, len(queue))
        logger.log('Launching %d worker(s) across %d work item(s)...' % (n_workers, len(queue)))
        board = StatusBoard(n_workers, args.quiet, progress=progress, t0=t0)
        logger.console = False  # board owns the console during the parallel phase
        threads = []
        for tid in range(n_workers):
            wp = os.path.join(workers_dir, 'w%d' % tid)
            write_sfdx_project(wp, args.api_version)
            t = threading.Thread(
                target=worker,
                args=(tid, state, queue, queue_lock, board, progress, control,
                      sf, dest_org, args.poll_interval, args.job_timeout,
                      src_profile_name, args.api_version, wp, logger, args.max_iter),
                daemon=True)
            t.start()
            threads.append(t)

        # ---------- join workers; Ctrl-C stops gracefully (as if 'S') ----------
        # Bounded joins: a join() without timeout blocks the main thread in a
        # native wait that never checks the Ctrl-C interrupt flag on Windows,
        # so the interrupt is only raised when the join returns. Polling with a
        # short timeout keeps KeyboardInterrupt responsive.
        stopped = False
        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=0.5)
        except KeyboardInterrupt:
            control.stop.set()
            board.freeze()
            sys.stdout.write('Saving pending work...\n')
            sys.stdout.flush()
            stopped = True

        # Wait for the workers to acknowledge the stop at their next checkpoint.
        # A second Ctrl-C while shutting down must not abort this wait, or the
        # workers would be left holding their temp dirs and cleanup would fail.
        if stopped:
            while any(t.is_alive() for t in threads):
                try:
                    for t in threads:
                        t.join(timeout=0.5)
                except KeyboardInterrupt:
                    pass

        if stopped:
            board.finalize_stopped()
            logger.console = not args.quiet
            result_state = 'ABORTED'
            result_reason = 'stopped by user'
        else:
            board.finalize_finished()
            logger.console = not args.quiet

            # ---------- outcome ----------
            failed = [(b, r) for b, (ok, r) in state.section_status.items() if not ok]
            if failed:
                result_state = 'ABORTED'
                result_reason = '; '.join('%s: %s' % (b, r) for b, r in failed)
            else:
                result_state = 'SUCCESS'

    elapsed = time.time() - t0

    # ---------- final COMMON/MISSING ----------
    logger.log('Writing the final COMMON/MISSING outputs...')
    with state.lock:
        state._write_outputs_locked()

    # ---------- final full dry-run of the assembled package ----------
    # Loop it: when the full-package validation surfaces a reference the
    # per-section pass missed (e.g. login flows), resolve + remove it and
    # re-validate until clean (or until no further progress is possible).
    final_detail = ''
    if result_state == 'SUCCESS':
        logger.log('Final dry-run of the full package...')
        final_proj = os.path.join(work_dir, 'final')
        write_sfdx_project(final_proj, args.api_version)
        fmanifest = os.path.join(final_proj, 'manifest.xml')
        write_package_xml(fmanifest, {'Profile': [src_profile_name]}, args.api_version)
        fprofile = os.path.join(final_proj, 'force-app', 'main', 'default', 'profiles',
                                '%s.profile-meta.xml' % src_profile_name)
        os.makedirs(os.path.dirname(fprofile), exist_ok=True)

        guard = 0
        while True:
            guard += 1
            if guard > args.max_iter:
                result_state = 'ABORTED'
                result_reason = 'final dry-run did not converge within %d iterations' % args.max_iter
                break
            shutil.copyfile(common_profile_path, fprofile)
            res, err = dry_run(sf, fmanifest, dest_org, args.poll_interval,
                               args.job_timeout, logger, cwd=final_proj)
            if res is None:
                result_state = 'ABORTED'
                result_reason = 'final dry-run failed: %s' % (err or 'unparseable output')
                break
            r = res.get('result', {})
            if r.get('success') is True:
                final_detail = 'clean'
                break
            cf = (r.get('details', {}) or {}).get('componentFailures') or []
            if not cf:
                result_state = 'ABORTED'
                result_reason = 'final dry-run reported a failed deploy with no componentFailures'
                break
            removals = []
            for f in cf:
                problem = str(f.get('problem', ''))
                rr = resolve_error(problem)
                if rr is None:
                    logger.log('Final gate: unmappable problem, skipped: %s' % problem)
                    continue
                removals.append((rr[0], rr[1]))
            if not removals:
                probs = '; '.join(str(f.get('problem', '')) for f in cf[:5])
                result_state = 'ABORTED'
                result_reason = 'final dry-run reported %d failure(s), none mappable: %s' % (len(cf), probs)
                break
            removed = state.apply_global_removals(removals)
            if removed == 0:
                probs = '; '.join(str(f.get('problem', '')) for f in cf[:5])
                result_state = 'ABORTED'
                result_reason = 'final dry-run: no progress removing %d failure(s): %s' % (len(cf), probs)
                break
            logger.log('Final gate: removed %d entry(ies); re-running full dry-run...' % removed)

    total_removed = state.totals()

    # ---------- optional deploy (only with --deploy-on-success) ----------
    deploy_detail = ''
    if result_state == 'SUCCESS' and args.deploy_on_success:
        logger.log('Deploying the cleaned profile %r to org %s...' % (dest_profile_name, dest_org))
        ok, deploy_detail = deploy_cleaned(common_profile_path, dest_profile_name, dest_org)
        if not ok:
            result_state = 'DEPLOY_FAILED'

    # ---------- report ----------
    logger.log('Writing the report...')
    lines = []
    lines.append('OUTCOME: %s' % result_state)
    if result_reason:
        lines.append('REASON: %s' % result_reason)
    lines.append('METADATA REMOVED (total): %d' % total_removed)
    lines.append('TIME: %.1fs' % elapsed)
    if final_detail:
        lines.append('FINAL DRY-RUN: %s' % final_detail)
    if deploy_detail:
        lines.append('DEPLOY: %s' % deploy_detail)
    lines.append('')
    lines.append('Removed by type:')
    for t in sorted(removed_by_type):
        lines.append('  %s : %d' % (t, len(removed_by_type[t])))
    lines.append('')
    lines.append('Output:')
    lines.append('  COMMON  : %s' % common_dir)
    lines.append('  MISSING : %s' % missing_dir)
    lines.append('  toDeploy: %s' % todeploy_pkg)
    lines.append('  report : %s' % report)
    with open(report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    # ---------- throw away our temporary work area ----------
    logger.log('Cleaning up the temporary working area...')
    remove_temp_project(work_dir, out_dir)

    # ---------- final output ----------
    logger.log('')
    logger.log('========================================================')
    logger.log('  PROFILE PRUNING RESULT')
    logger.log('========================================================')
    for l in lines:
        logger.log(l)
    logger.log('========================================================')
    logger.log('RESULT:%s' % result_state)


def main():
    """Entry point. Catches a Ctrl-C raised outside the worker phase (e.g.
    while `sf org display` or a final dry-run is blocked) and exits cleanly
    instead of dumping a traceback."""
    try:
        _main()
    except KeyboardInterrupt:
        print('\nstopped by user', file=sys.stderr)
        sys.exit(130)
