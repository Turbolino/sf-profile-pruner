# -*- coding: utf-8 -*-
"""Profile Pruner — command-line entry point.

Removes from a Salesforce Profile every reference to metadata that does NOT
exist in the destination org. Pruning combines a Metadata API + Tooling API
pre-pass with an authoritative `sf project deploy start --dry-run`, and never
modifies the org; with `--deploy-on-success` the cleaned profile is deployed at
the end.

Three input modes:
  * via 1 (file):   `--profile-path`  + `--dest-org`
  * via 2 (org):    `--src-org` + `--src-profile` + `--dest-org` + `--dest-profile`
                    (the profile is retrieved from the source org first)
  * via 3 (retrieve only): `--src-org` + `--src-profile` (no prune, no dry-run/deploy)

`--align-from-source` upgrades via 1/via 2 into an *alignment*: after pruning,
the destination profile is also retrieved and pruned, and every permission that
exists only in the destination is turned off so the destination matches the
source. It is destructive even on dry-run and asks for Y/N confirmation (skip
with a following `--sure`).

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
from .retrieve import retrieve_profiles
from .sfcli import (deploy, find_sf, org_api_version, org_username,
                    set_deploy_start_interval)
from .stage import prune_stage
from .xmlio import write_package_xml, write_sfdx_project

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

  # Retrieve only (no prune): download the profile from a source org
  sfpp -so devorg -sp "Sales Rep" -od ./profiles

  # Align the destination profile to the source (turns OFF destination-only
  # permissions); the Y/N prompt is skipped by the trailing --sure
  sfpp -so collaudo -sp "Sales Rep" -do test -dp "Sales Rep" \
      --align-from-source --sure --deploy-on-success --sure

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

    # --sure must immediately follow --align-from-source or --deploy-on-success,
    # and it applies only to that one operation (deploy and align are independent).
    sure_align = False
    sure_deploy = False
    for i, tok in enumerate(argv):
        if tok == '--sure':
            prev = argv[i - 1] if i > 0 else None
            if prev == '--align-from-source':
                sure_align = True
            elif prev == '--deploy-on-success':
                sure_deploy = True
            else:
                print('ERROR: --sure must immediately follow --align-from-source or '
                      '--deploy-on-success (e.g. --align-from-source --sure).')
                sys.exit(2)

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
    g_act.add_argument('--align-from-source', action='store_true',
                       help='align the destination profile to the source (works with a '
                            'source file via 1, or a source org via 2): prune the source '
                            'against the destination, then retrieve and prune the '
                            'destination too, and turn OFF any permission that exists only '
                            'in the destination (hidden tab/app, disabled '
                            'field/object/class/page/flow), so the destination matches the '
                            'source. Without it, destination-only permissions are left '
                            'untouched. Destructive even on dry-run: asks for Y/N '
                            'confirmation (a No skips the alignment and runs the normal '
                            'prune); skip with a following --sure.')
    g_act.add_argument('--deploy-on-success', action='store_true',
                       help='deploy the cleaned profile to --dest-org once pruning succeeds. '
                            'Modifies the org: asks for Y/N confirmation (a No runs the '
                            'dry-run only); skip with a following --sure.')
    g_act.add_argument('--sure', action='store_true',
                       help='skip the Y/N confirmation of the immediately preceding '
                            'destructive option (--align-from-source or --deploy-on-success). '
                            'Must be placed right after it, e.g. --align-from-source --sure.')
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

    # ---------- destructive-op confirmations (Y/N, skippable with --sure) ----------
    # A 'no' downgrades the operation instead of aborting the whole run:
    #   - align 'no'  -> skip the alignment, run the normal prune flow
    #   - deploy 'no' -> skip the deploy, run the dry-run only
    do_align = args.align_from_source
    if do_align and not sure_align:
        if not _confirm('--align-from-source disables destination-only permissions '
                        '(destructive, even on dry-run). Align? [y/N] '):
            do_align = False
            print('Alignment skipped: running the normal prune flow.', file=sys.stderr)
    do_deploy = args.deploy_on_success
    if do_deploy and not sure_deploy:
        if not _confirm('--deploy-on-success will deploy the cleaned profile to the '
                        'destination org. Deploy? [y/N] '):
            do_deploy = False
            print('Deploy skipped: dry-run only.', file=sys.stderr)

    # Alignment engine (retrieve+prune both sides, merge with source authoritative)
    # lands in the next step.
    if do_align:
        print('ERROR: --align-from-source is not implemented yet (the alignment engine '
              'is being built).', file=sys.stderr)
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

    # Informational, non-blocking upgrade notice (stderr so it never disturbs
    # the stdout progress/board; skipped silently on any network failure).
    logger.log('Startup: checking for a newer release...')
    for line in _check_for_update():
        print(line, file=sys.stderr)
    logger.log('Startup: update check done.')

    logger.log('Startup: locating the Salesforce CLI (sf)...')
    sf = find_sf(args.sf)
    logger.log('sf          : %s' % sf)

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
    logger.log('API version : %s (%s)' % (args.api_version, api_source))

    # ---------- retrieve the source profile (via 2) ----------
    if mode == 'org':
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

    logger.log('Deploy as   : %s' % dest_profile_name)
    logger.log('Dest org    : %s' % dest_org)
    if mode == 'org':
        logger.log('Source org  : %s' % src_org)

    # ---------- run the prune stage ----------
    result = prune_stage(
        sf=sf,
        org=dest_org,
        source_profile_path=profile_path,
        profile_name=src_profile_name,
        out_dir=out_dir,
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
    if result['already_clean']:
        if do_deploy:
            logger.log('Profile already clean; deploying %r to org %s...' % (dest_profile_name, dest_org))
            ok, detail = _deploy_profile(sf, dest_org, result['common_profile_path'],
                                         dest_profile_name, args.api_version,
                                         args.poll_interval, args.job_timeout,
                                         logger, out_dir)
            print('%s: %s' % ('DEPLOY OK' if ok else 'DEPLOY FAILED', detail))
            sys.exit(0 if ok else 1)
        print('Profile "%s" is already clean (previous run: SUCCESS). Nothing to do.' % src_profile_name)
        sys.exit(0)

    # ---------- optional deploy (only with --deploy-on-success) ----------
    result_state = result['outcome']
    deploy_detail = ''
    if result_state == 'SUCCESS' and do_deploy:
        logger.log('Deploying the cleaned profile %r to org %s...' % (dest_profile_name, dest_org))
        ok, deploy_detail = _deploy_profile(sf, dest_org, result['common_profile_path'],
                                            dest_profile_name, args.api_version,
                                            args.poll_interval, args.job_timeout,
                                            logger, out_dir)
        if not ok:
            result_state = 'DEPLOY_FAILED'

    # ---------- final output ----------
    logger.log('')
    logger.log('========================================================')
    logger.log('  PROFILE PRUNING RESULT')
    logger.log('========================================================')
    logger.log('OUTCOME: %s' % result_state)
    if result['reason']:
        logger.log('REASON: %s' % result['reason'])
    logger.log('METADATA REMOVED (total): %d' % result['total_removed'])
    logger.log('TIME: %.1fs' % result['elapsed'])
    if result['final_detail']:
        logger.log('FINAL DRY-RUN: %s' % result['final_detail'])
    if deploy_detail:
        logger.log('DEPLOY: %s' % deploy_detail)
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
