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
  until terminal, then removes every offending entry. GOOD/ and ERRORS/ are
  reassembled from the surviving entries in the original document order under
  a mutex. A final dry-run of the whole assembled package gates `SUCCESS`.
"""

import argparse
import os
import re
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque

from lxml import etree

from .board import Control, Logger, Progress, StatusBoard
from .constants import APP_NAME, AUTHOR_NOTE, BANNER, BLOCK_MAP, __version__
from .engine import worker
from .sfcli import deploy, dry_run, find_sf
from .state import SharedState
from .xmlio import (
    load_log_entries,
    prunable_blocks,
    read_outcome,
    remove_from_list,
    remove_temp_project,
    write_package_xml,
    write_sfdx_project,
)


def main():
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
    ap.add_argument('--api-version', default='68.0',
                    help='API version of the manifest (default: 68.0)')
    ap.add_argument('--max-iter', type=int, default=5000,
                    help='iteration cap (default: 5000)')
    ap.add_argument('--sf', default='sf',
                    help='command/path of the Salesforce CLI (default: sf)')
    ap.add_argument('--poll-interval', type=float, default=5.0,
                    help='seconds to wait between status polls of a deploy job (default: 5)')
    ap.add_argument('--job-timeout', type=int, default=30,
                    help='minutes to wait for a deploy job to finish before aborting (default: 30)')
    ap.add_argument('--thread-number', type=int, default=8, metavar='X',
                    help='max parallel section jobs, 1..64 (default: 8); capped to '
                         'the number of sections to prune')
    ap.add_argument('--fresh', action='store_true',
                    help='ignore any previous state and restart from the original')
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

    sf = find_sf(args.sf)

    base = os.path.basename(profile_path)
    profile_name = args.profile_name or re.sub(r'\.profile-meta\.xml$', '', base)

    good_dir = os.path.join(out_dir, 'GOOD')
    errors_dir = os.path.join(out_dir, 'ERRORS')
    work_dir = os.path.join(out_dir, '.work')
    err_log = os.path.join(out_dir, 'errors_log.txt')
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

    for d in (out_dir, good_dir, errors_dir, work_dir):
        os.makedirs(d, exist_ok=True)

    # Untouched copy of the source (kept for reconstruction on resume).
    if os.path.abspath(profile_path) != os.path.abspath(original_copy):
        shutil.copyfile(profile_path, original_copy)

    # Minimal project scaffolding (a single sfdx-project.json is enough).
    if not user_project and not os.path.isfile(os.path.join(project_dir, 'sfdx-project.json')):
        write_sfdx_project(project_dir, args.api_version)

    # Internal manifest used by the dry-runs and the optional deploy.
    write_package_xml(manifest_path, {'Profile': [profile_name]}, args.api_version)

    logger = Logger(args.quiet, run_log)
    logger.log('Profile     : %s' % profile_path)
    logger.log('Name        : %s' % profile_name)
    logger.log('Org         : %s' % args.org)
    logger.log('Project     : %s' % project_dir)
    logger.log('Output      : %s' % out_dir)
    logger.log('API version : %s' % args.api_version)
    logger.log('sf          : %s' % sf)

    # ---------- already clean? ----------
    prev_outcome = read_outcome(report)
    good_profile = os.path.join(good_dir, '%s.profile-meta.xml' % profile_name)
    if (not args.fresh) and prev_outcome == 'SUCCESS' and os.path.isfile(good_profile):
        if args.deploy_on_success:
            os.makedirs(os.path.dirname(work_path), exist_ok=True)
            shutil.copyfile(good_profile, work_path)
            logger.log('Profile already clean; deploying the GOOD profile as requested...')
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
        for p in (err_log, report, run_log):
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(good_profile):
            os.remove(good_profile)
        err_pkg = os.path.join(errors_dir, 'package.xml')
        if os.path.exists(err_pkg):
            os.remove(err_pkg)
        logger.log('CLEAN RUN (--fresh): outputs cleared.')

    # ---------- load original, split into per-section groups ----------
    tree = etree.parse(original_copy)
    root = tree.getroot()
    original_seq = [c for c in root if isinstance(c.tag, str)]
    groups = OrderedDict()
    for el in original_seq:
        block = el.tag.split('}')[-1]
        groups.setdefault(block, []).append(el)

    # ---------- apply recorded removals (resume) ----------
    # Capture the true original section sizes first, so the board keeps showing
    # cumulative deleted/initial across resumptions instead of resetting.
    original_counts = {block: len(els) for block, els in groups.items()}
    removed_by_type = {}
    for typ, member in load_log_entries(err_log):
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

    # ---------- queue of prunable sections that still have entries ----------
    queue = deque()
    for block in groups:
        if block in prunable_blocks() and groups[block]:
            queue.append(block)
    logger.log('Sections to prune: %s' % (', '.join(queue) if queue else '(none)'))

    # ---------- shared state + initial outputs ----------
    good_profile_path = os.path.join(good_dir, '%s.profile-meta.xml' % profile_name)
    errors_pkg = os.path.join(errors_dir, 'package.xml')
    state = SharedState(groups, original_seq, logger, err_log, good_profile_path,
                        errors_pkg, profile_name, args.api_version)
    state.removed_by_type = removed_by_type
    state.initial_counts = original_counts
    state.seq = total_resumed  # continue the error-log sequence on resume
    with state.lock:
        state._write_outputs_locked()

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
        # Never spawn more threads than there are sections to prune.
        n_workers = min(args.thread_number, len(queue))
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
            board.finish()
            logger.console = not args.quiet

            # ---------- outcome ----------
            failed = [(b, r) for b, (ok, r) in state.section_status.items() if not ok]
            if failed:
                result_state = 'ABORTED'
                result_reason = '; '.join('%s: %s' % (b, r) for b, r in failed)
            else:
                result_state = 'SUCCESS'

    elapsed = time.time() - t0

    # ---------- final GOOD/ERRORS ----------
    with state.lock:
        state._write_outputs_locked()
    total_removed = state.totals()

    # ---------- final full dry-run of the assembled package ----------
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
        shutil.copyfile(good_profile_path, fprofile)
        res, err = dry_run(sf, fmanifest, args.org, args.poll_interval,
                           args.job_timeout, logger, cwd=final_proj)
        if res is None:
            result_state = 'ABORTED'
            result_reason = 'final dry-run failed: %s' % (err or 'unparseable output')
        else:
            r = res.get('result', {})
            if r.get('success') is True:
                final_detail = 'clean'
            else:
                cf = (r.get('details', {}) or {}).get('componentFailures') or []
                probs = '; '.join(str(f.get('problem', '')) for f in cf[:5])
                result_state = 'ABORTED'
                result_reason = 'final dry-run reported %d failure(s): %s' % (len(cf), probs)

    # ---------- optional deploy (only with --deploy-on-success) ----------
    deploy_detail = ''
    if result_state == 'SUCCESS' and args.deploy_on_success:
        logger.log('Deploying the cleaned profile to org %s...' % args.org)
        os.makedirs(os.path.dirname(work_path), exist_ok=True)
        shutil.copyfile(good_profile_path, work_path)
        ok, deploy_detail = deploy(sf, manifest_path, args.org, args.poll_interval,
                                   args.job_timeout, logger, cwd=project_dir)
        if not ok:
            result_state = 'DEPLOY_FAILED'

    # ---------- report ----------
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
    lines.append('  GOOD   : %s' % good_dir)
    lines.append('  ERRORS : %s' % errors_dir)
    lines.append('  report : %s' % report)
    with open(report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    # ---------- throw away our temporary work area ----------
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
