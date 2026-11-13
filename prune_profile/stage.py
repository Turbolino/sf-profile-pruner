# -*- coding: utf-8 -*-
"""Reusable prune stage: one profile file + one org -> clean COMMON/MISSING.

`prune_stage` encapsulates the whole pruning pipeline (Metadata API pre-pass,
standard-metadata pre-pass, parallel per-section dry-run loop, self-healing
final gate and the report), so the CLI can run it more than once with different
inputs — for example the alignment flow, which prunes the source against the
destination and then prunes the destination against itself.

It is intentionally free of argument parsing and of the "deploy" step: it only
produces the clean profile and the removed-metadata logs. The caller decides
what to do with the result (deploy it, merge it, …).
"""

import os
import shutil
import sys
import threading
import time
from collections import OrderedDict, deque

from lxml import etree

from .board import Control, PrefetchStatus, Progress, StatusBoard
from .constants import BLOCK_MAP
from .engine import prefetch_dangling, prefetch_standard_dangling, worker
from .sfcli import dry_run
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


def prune_stage(*, sf, org, source_profile_path, profile_name, out_dir,
                api_version, poll_interval, job_timeout, thread_number,
                batch_size, max_iter, quiet, fresh, logger):
    """Prune `profile_name` (from `source_profile_path`) against `org`.

    Writes COMMON/, MISSING/, missing_log.txt, report.txt and run.log under
    `out_dir`. The provided `logger` receives the progress history (the caller
    owns the logger so several stages can share one run.log).

    Returns a dict:
      outcome              'SUCCESS' | 'ABORTED'
      reason               human-readable reason when ABORTED
      already_clean        True when a previous run was SUCCESS (resume short-circuit)
      total_removed        total metadata references removed
      final_detail         final dry-run verdict ('clean' or '')
      common_profile_path  path of the clean COMMON profile
      missing_dir          path of the MISSING dir
      report               path of report.txt
      elapsed              seconds spent in the prune phase
    """
    common_dir = os.path.join(out_dir, 'COMMON')
    missing_dir = os.path.join(out_dir, 'MISSING')
    work_dir = os.path.join(out_dir, '.work')
    missing_log = os.path.join(out_dir, 'missing_log.txt')
    report = os.path.join(out_dir, 'report.txt')
    original_copy = os.path.join(out_dir, 'ORIGINAL_%s.profile-meta.xml' % profile_name)
    project_dir = os.path.join(work_dir, 'project')

    for d in (out_dir, common_dir, missing_dir, work_dir):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            print('ERROR: cannot create output directory %r: %s' % (d, e))
            raise

    # Untouched copy of the source (kept for reconstruction on resume).
    if os.path.abspath(source_profile_path) != os.path.abspath(original_copy):
        shutil.copyfile(source_profile_path, original_copy)

    # Minimal project scaffolding (a single sfdx-project.json is enough).
    if not os.path.isfile(os.path.join(project_dir, 'sfdx-project.json')):
        write_sfdx_project(project_dir, api_version)

    logger.log('Profile     : %s' % source_profile_path)
    logger.log('Name        : %s' % profile_name)
    logger.log('Org         : %s' % org)
    logger.log('Project     : %s' % project_dir)
    logger.log('Output      : %s' % out_dir)
    logger.log('API version : %s' % api_version)

    # ---------- already clean? ----------
    prev_outcome = read_outcome(report)
    common_profile = os.path.join(common_dir, '%s.profile-meta.xml' % profile_name)
    if (not fresh) and prev_outcome == 'SUCCESS' and os.path.isfile(common_profile):
        remove_temp_project(work_dir, out_dir)
        logger.log('Profile already clean (previous run: SUCCESS).')
        return {
            'outcome': 'SUCCESS',
            'reason': '',
            'already_clean': True,
            'total_removed': 0,
            'final_detail': '',
            'common_profile_path': common_profile,
            'missing_dir': missing_dir,
            'report': report,
            'elapsed': 0.0,
        }

    # ---------- clean run (--fresh): clear previous outputs ----------
    if fresh:
        for p in (missing_log, report):
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
                        missing_pkg, todeploy_pkg, profile_name, api_version)
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
    ps = PrefetchStatus(quiet)
    if ps.active:
        logger.console = False
    ps.start()
    try:
        pref = prefetch_dangling(sf, org, state.groups, api_version,
                                 logger, status=ps.update)
    finally:
        ps.stop()
        if ps.active:
            logger.console = not quiet
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
    ps = PrefetchStatus(quiet)
    if ps.active:
        logger.console = False
    ps.start()
    try:
        pref = prefetch_standard_dangling(sf, org, state.groups,
                                          logger, status=ps.update)
    finally:
        ps.stop()
        if ps.active:
            logger.console = not quiet
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
            n_chunks = state.split_chunks(block, batch_size)
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
        n_workers = min(thread_number, len(queue))
        logger.log('Launching %d worker(s) across %d work item(s)...' % (n_workers, len(queue)))
        board = StatusBoard(n_workers, quiet, progress=progress, t0=t0)
        logger.console = False  # board owns the console during the parallel phase
        threads = []
        for tid in range(n_workers):
            wp = os.path.join(workers_dir, 'w%d' % tid)
            write_sfdx_project(wp, api_version)
            t = threading.Thread(
                target=worker,
                args=(tid, state, queue, queue_lock, board, progress, control,
                      sf, org, poll_interval, job_timeout,
                      profile_name, api_version, wp, logger, max_iter),
                daemon=True)
            t.start()
            threads.append(t)

        # ---------- join workers; Ctrl-C stops gracefully (as if 'S') ----------
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
        if stopped:
            while any(t.is_alive() for t in threads):
                try:
                    for t in threads:
                        t.join(timeout=0.5)
                except KeyboardInterrupt:
                    pass

        if stopped:
            board.finalize_stopped()
            logger.console = not quiet
            result_state = 'ABORTED'
            result_reason = 'stopped by user'
        else:
            board.finalize_finished()
            logger.console = not quiet

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
        write_sfdx_project(final_proj, api_version)
        fmanifest = os.path.join(final_proj, 'manifest.xml')
        write_package_xml(fmanifest, {'Profile': [profile_name]}, api_version)
        fprofile = os.path.join(final_proj, 'force-app', 'main', 'default', 'profiles',
                                '%s.profile-meta.xml' % profile_name)
        os.makedirs(os.path.dirname(fprofile), exist_ok=True)

        guard = 0
        while True:
            guard += 1
            if guard > max_iter:
                result_state = 'ABORTED'
                result_reason = 'final dry-run did not converge within %d iterations' % max_iter
                break
            shutil.copyfile(common_profile_path, fprofile)
            res, err = dry_run(sf, fmanifest, org, poll_interval,
                               job_timeout, logger, cwd=final_proj)
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

    return {
        'outcome': result_state,
        'reason': result_reason,
        'already_clean': False,
        'total_removed': total_removed,
        'final_detail': final_detail,
        'common_profile_path': common_profile_path,
        'missing_dir': missing_dir,
        'report': report,
        'elapsed': elapsed,
    }
