# -*- coding: utf-8 -*-
"""Profile Pruner — command-line entry point.

Removes from a Salesforce Profile every reference to metadata that does NOT
exist in the target org. Pruning uses only `sf project deploy start --dry-run`
and never modifies the org; with `--deploy-on-success` the cleaned profile is
deployed at the end.

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
    __version__,
    version_ge,
)
from .engine import prefetch_dangling, worker
from .sfcli import deploy, dry_run, find_sf, org_api_version
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
    '--profile-path', '--out-dir', '--org', '--profile-name',
    '--project-dir', '--api-version', '--max-iter', '--sf',
    '--poll-interval', '--job-timeout', '--thread-number',
}


def _repair_mangled_argv(argv):
    """Reconstruct argv after PowerShell/Windows mangled a quoted argument.

    When a quoted argument ends with a backslash immediately before its closing
    quote (e.g. ``--out-dir "...\\src Test\\dest Test\\"``), the Windows command
    line parser treats the trailing ``\"`` as an escaped quote: the quote becomes
    a literal ``"``, the argument stays open, and every following token is
    swallowed into it. Python then sees either

      ['--out-dir', '...\\dest Test" --org Test', 'Org']       (case A)
      ['--out-dir', '...\\dest Test"']                          (case B)

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

    ap = argparse.ArgumentParser(
        prog=APP_NAME,
        description='Removes from a Salesforce Profile every reference to metadata that '
                    'does not exist in the target org. Dry-run only by default; use '
                    '--deploy-on-success to deploy the cleaned profile at the end.'
    )
    ap.add_argument('--profile-path', required=True,
                    help='path of the .profile-meta.xml file (original source)')
    ap.add_argument('--out-dir', required=True,
                    help='output directory (report, packages, logs)')
    ap.add_argument('--org', required=True,
                    help="alias or username of the org used for the dry-run")
    ap.add_argument('--profile-name', default=None,
                    help='name of the Profile (package member); default: derived from the file name')
    ap.add_argument('--project-dir', default=None,
                    help='root of an existing SFDX project. If omitted, a minimal '
                         'temporary project is created inside --out-dir and removed '
                         'at the end')
    ap.add_argument('--api-version', default=None,
                    help='manifest API version. Default: the latest version supported '
                         'by the target org, detected automatically via `sf org display`; '
                         'pass an explicit version (e.g. 66.0) to override')
    ap.add_argument('--max-iter', type=int, default=5000,
                    help='iteration cap (default: 5000)')
    ap.add_argument('--sf', default='sf',
                    help='command/path of the Salesforce CLI (default: sf)')
    ap.add_argument('--poll-interval', type=float, default=5.0,
                    help='seconds to wait between status polls of a deploy job (default: 5)')
    ap.add_argument('--job-timeout', type=int, default=30,
                    help='minutes to wait for a deploy job to finish before aborting (default: 30)')
    ap.add_argument('--thread-number', type=int, default=8, metavar='X',
                    help='max parallel worker threads, 1..64 (default: 8); capped to '
                         'the number of work items (chunks) to prune')
    ap.add_argument('--max-elem-per-section', type=int, default=500, metavar='N',
                    help='split a section into parallel chunks when it holds more than N '
                         'entries (default: 500); 0 disables splitting (one worker per section)')
    ap.add_argument('--fresh', action='store_true',
                    help='ignore any previous state and restart from the original')
    ap.add_argument('--no-prefetch', action='store_true',
                    help='skip the Metadata API pre-pass (sf org list metadata) and '
                         'discover dangling references only through dry-runs')
    ap.add_argument('--quiet', action='store_true',
                    help='suppress the progressive stdout output')
    ap.add_argument('--deploy-on-success', action='store_true',
                    help='deploy the cleaned profile to the org once the pruning succeeds '
                         '(default: dry-run only, the org is never modified)')
    ap.add_argument('-V', '--version', action='version',
                    version='%s v%s - %s' % (APP_NAME, __version__, AUTHOR_NOTE))
    args = ap.parse_args()

    if not (1 <= args.thread_number <= 64):
        print('ERROR: --thread-number must be between 1 and 64 (got %d).' % args.thread_number)
        sys.exit(2)

    if args.max_elem_per_section < 0:
        print('ERROR: --max-elem-per-section must be >= 0 (got %d).' % args.max_elem_per_section)
        sys.exit(2)

    if not args.quiet:
        print(BANNER, flush=True)
        print('%s v%s' % (APP_NAME, __version__), flush=True)
        print(AUTHOR_NOTE, flush=True)
        print('', flush=True)

    profile_path = os.path.abspath(args.profile_path)
    out_dir = os.path.abspath(args.out_dir)

    if not os.path.isfile(profile_path):
        print('ERROR: profile not found: %s' % profile_path)
        sys.exit(2)

    base = os.path.basename(profile_path)
    profile_name = args.profile_name or re.sub(r'\.profile-meta\.xml$', '', base)

    common_dir = os.path.join(out_dir, 'COMMON')
    missing_dir = os.path.join(out_dir, 'MISSING')
    work_dir = os.path.join(out_dir, '.work')
    missing_log = os.path.join(out_dir, 'missing_log.txt')
    report = os.path.join(out_dir, 'report.txt')
    run_log = os.path.join(out_dir, 'run.log')
    manifest_path = os.path.join(work_dir, 'manifest.xml')
    original_copy = os.path.join(out_dir, 'ORIGINAL_%s.profile-meta.xml' % profile_name)

    # Project root: the user's own project, or a minimal temporary micro-project.
    user_project = args.project_dir is not None
    if user_project:
        project_dir = os.path.abspath(args.project_dir)
    else:
        project_dir = os.path.join(work_dir, 'project')

    # Working copy inside the package directory of the (micro-)project.
    work_path = os.path.join(project_dir, 'force-app', 'main', 'default', 'profiles',
                             '%s.profile-meta.xml' % profile_name)

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

    # ---------- API version: explicit or detected from the org ----------
    api_source = 'explicit'
    if args.api_version is None:
        logger.log('Startup: detecting the latest API version from org %s...' % args.org)
        args.api_version, averr = org_api_version(sf, args.org)
        if args.api_version is None:
            print('ERROR: could not detect the latest API version from org %r:' % args.org)
            print('       %s' % (averr or 'unknown error'))
            print('       Pass --api-version <X.Y> explicitly to continue.')
            sys.exit(2)
        api_source = 'auto (latest for org)'

    logger.log('Startup: preparing the working copy and project scaffolding...')
    # Untouched copy of the source (kept for reconstruction on resume).
    if os.path.abspath(profile_path) != os.path.abspath(original_copy):
        shutil.copyfile(profile_path, original_copy)

    # Minimal project scaffolding (a single sfdx-project.json is enough).
    if not user_project and not os.path.isfile(os.path.join(project_dir, 'sfdx-project.json')):
        write_sfdx_project(project_dir, args.api_version)

    # Internal manifest used by the dry-runs and the optional deploy.
    write_package_xml(manifest_path, {'Profile': [profile_name]}, args.api_version)

    logger.log('Profile     : %s' % profile_path)
    logger.log('Name        : %s' % profile_name)
    logger.log('Org         : %s' % args.org)
    logger.log('Project     : %s' % project_dir)
    logger.log('Output      : %s' % out_dir)
    logger.log('API version : %s (%s)' % (args.api_version, api_source))

    # ---------- already clean? ----------
    prev_outcome = read_outcome(report)
    common_profile = os.path.join(common_dir, '%s.profile-meta.xml' % profile_name)
    if (not args.fresh) and prev_outcome == 'SUCCESS' and os.path.isfile(common_profile):
        if args.deploy_on_success:
            os.makedirs(os.path.dirname(work_path), exist_ok=True)
            shutil.copyfile(common_profile, work_path)
            logger.log('Profile already clean; deploying the COMMON profile as requested...')
            ok, detail = deploy(sf, manifest_path, args.org, args.poll_interval,
                                args.job_timeout, logger, cwd=project_dir)
            remove_temp_project(work_dir, out_dir)
            print('%s: %s' % ('DEPLOY OK' if ok else 'DEPLOY FAILED', detail))
            sys.exit(0 if ok else 1)
        remove_temp_project(work_dir, out_dir)
        print('Profile "%s" is already clean (previous run: SUCCESS). Nothing to do.' % profile_name)
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
    common_profile_path = os.path.join(common_dir, '%s.profile-meta.xml' % profile_name)
    missing_pkg = os.path.join(missing_dir, 'package.xml')
    todeploy_pkg = os.path.join(common_dir, 'toDeploy.xml')
    state = SharedState(groups, original_seq, logger, missing_log, common_profile_path,
                        missing_pkg, todeploy_pkg, profile_name, args.api_version)
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
    if not args.no_prefetch:
        # The pre-fetch phase owns the console via a single in-place status line
        # (message + elapsed) so it never looks frozen; the logger keeps writing
        # the full history to run.log. In non-TTY mode the status line is a no-op
        # and the per-type messages keep flowing to stdout through the logger.
        ps = PrefetchStatus(args.quiet)
        if ps.active:
            logger.console = False
        ps.start()
        try:
            pref = prefetch_dangling(sf, args.org, state.groups, args.api_version,
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

    # ---------- queue of prunable chunks (sections split into chunks) ----------
    # No workers are running yet, so reading `state.groups` is safe here; each
    # split_chunks() call takes the state lock itself.
    queue = deque()
    section_count = 0
    for block in state.groups:
        if block in prunable_blocks() and state.groups[block]:
            section_count += 1
            n_chunks = state.split_chunks(block, args.max_elem_per_section)
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
                      sf, args.org, args.poll_interval, args.job_timeout,
                      profile_name, args.api_version, wp, logger, args.max_iter),
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
        write_package_xml(fmanifest, {'Profile': [profile_name]}, args.api_version)
        fprofile = os.path.join(final_proj, 'force-app', 'main', 'default', 'profiles',
                                '%s.profile-meta.xml' % profile_name)
        os.makedirs(os.path.dirname(fprofile), exist_ok=True)

        guard = 0
        while True:
            guard += 1
            if guard > args.max_iter:
                result_state = 'ABORTED'
                result_reason = 'final dry-run did not converge within %d iterations' % args.max_iter
                break
            shutil.copyfile(common_profile_path, fprofile)
            res, err = dry_run(sf, fmanifest, args.org, args.poll_interval,
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
        logger.log('Deploying the cleaned profile to org %s...' % args.org)
        os.makedirs(os.path.dirname(work_path), exist_ok=True)
        shutil.copyfile(common_profile_path, work_path)
        ok, deploy_detail = deploy(sf, manifest_path, args.org, args.poll_interval,
                                   args.job_timeout, logger, cwd=project_dir)
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
