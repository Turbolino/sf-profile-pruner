# -*- coding: utf-8 -*-
"""Parallel pruning engine: the per-section worker and its section loop."""

import json
import os

from .constants import LISTABLE, MIN_API, version_ge
from .sfcli import dry_run, list_metadata
from .xmlio import (
    build_subprofile,
    dangling_members,
    remove_from_list,
    resolve_error,
    write_package_xml,
)


def prefetch_dangling(sf, org, groups, api_version, logger, status=None):
    """Detect dangling references by listing existing org metadata per type.

    Returns a deduplicated list of (type, member) pairs to remove. Each `type`
    is a BLOCK_MAP key, so the removals are applied and re-applied (on resume)
    through the exact same code path as dry-run findings.

    `status` is an optional callable(message) used to show live progress on the
    console (the pre-fetch phase owns the status line while logger.console is
    off); every message is also written to the run log via `logger`."""
    def _report(msg):
        logger.log('PREFETCH: %s' % msg)
        if status:
            status('prefetch: %s' % msg)

    active = []
    for mdtype, mappings in LISTABLE.items():
        minv = MIN_API.get(mdtype)
        if minv and not version_ge(api_version, minv):
            continue
        blocks = [(b, c) for b, c in mappings if groups.get(b)]
        if blocks:
            active.append((mdtype, blocks))

    removals = []
    seen = set()
    n_types = len(active)
    _report('%d metadata type(s) to check' % n_types)
    for i, (mdtype, blocks) in enumerate(active, 1):
        _report('[%d/%d] listing %s...' % (i, n_types, mdtype))
        names, err = list_metadata(sf, org, mdtype)
        if names is None:
            _report('[%d/%d] %s: skipped (%s)' % (i, n_types, mdtype, err))
            continue
        if not names:
            # An empty list is almost always a permissions/feature problem, not
            # a genuinely empty org; removing everything would be destructive.
            _report('[%d/%d] %s: zero members, skipping' % (i, n_types, mdtype))
            continue
        existing = set(names)
        before = len(removals)
        for block, child in blocks:
            for member in dangling_members(groups[block], child, existing):
                if mdtype == 'CustomTab' and member.lower().startswith('standard-'):
                    # `standard-*` are standard tabs, not CustomTab;
                    # listMetadata --metadata-type CustomTab cannot see them, so
                    # they must never be treated as dangling.
                    continue
                key = (mdtype, member)
                if key not in seen:
                    seen.add(key)
                    removals.append(key)
        dangled = len(removals) - before
        _report('[%d/%d] %s: %d existing, %d dangling' %
                (i, n_types, mdtype, len(existing), dangled))
    return removals


def process_section(tid, block, chunk_id, state, board, sf, org, poll_interval,
                    job_timeout, profile_name, api_version, worker_proj, logger,
                    max_iter, control):
    """Prune a single chunk (a slice of a section) until its sub-profile
    dry-runs clean. Returns (ok, reason)."""
    manifest_path = os.path.join(worker_proj, 'manifest.xml')
    profile_path = os.path.join(worker_proj, 'force-app', 'main', 'default',
                                'profiles', '%s.profile-meta.xml' % profile_name)
    os.makedirs(os.path.dirname(profile_path), exist_ok=True)
    write_package_xml(manifest_path, {'Profile': [profile_name]}, api_version)

    with state.lock:
        initial = state.chunk_initials.get(block, [0])[chunk_id]

    guard = 0
    while True:
        guard += 1
        if guard > max_iter:
            return False, 'iteration cap %d reached' % max_iter
        if control.stop.is_set():
            return False, 'stopped by user'
        with state.lock:
            elements = list(state.chunks[block][chunk_id])
        if not elements:
            return True, 'no entries left'
        with open(profile_path, 'wb') as f:
            f.write(build_subprofile(elements))

        board.set(tid, 'dry-run', block, 'polling', initial, len(elements))
        res, err = dry_run(sf, manifest_path, org, poll_interval, job_timeout,
                           logger, cwd=worker_proj,
                           on_status=lambda s: board.set(tid, 'dry-run', block, s,
                                                         initial, len(elements)),
                           control=control)
        if res is None:
            if err == 'stopped by user':
                return False, 'stopped by user'
            return False, err or 'dry-run failed'
        result = res.get('result', {})
        if result.get('success') is True:
            return True, 'clean'

        details = result.get('details', {})
        failures = details.get('componentFailures') or []
        if not failures:
            diag = {'status': res.get('status'),
                    'message': result.get('message'),
                    'errorMessage': result.get('errorMessage'),
                    'numberComponentErrors': result.get('numberComponentErrors')}
            return False, json.dumps(diag)[:400]

        board.set(tid, 'removing', block, '%d errors' % len(failures),
                  initial, len(elements))
        removals = []
        for f in failures:
            problem = str(f.get('problem', ''))
            rr = resolve_error(problem)
            if rr is None:
                logger.log('[T%d][%s] unmappable, skipped: %s' % (tid, block, problem))
                continue
            removals.append((rr[0], rr[1], problem))

        if not removals:
            return False, 'no mappable error among %d failure(s)' % len(failures)

        new_elements = list(elements)
        for typ, member, _problem in removals:
            new_elements, _n = remove_from_list(new_elements, block, member)
        if len(new_elements) == len(elements):
            return False, 'no progress: reported member(s) not found in section %s' % block
        state.apply_chunk_removals(block, chunk_id, new_elements, removals, tid)


def worker(tid, state, queue, queue_lock, board, progress, control, sf, org,
           poll_interval, job_timeout, profile_name, api_version, worker_proj,
           logger, max_iter):
    """Consume one chunk at a time until the queue is empty."""
    while True:
        if control.stop.is_set():
            return
        with queue_lock:
            if not queue:
                board.set(tid, 'completed', '', 'no more work', 0, 0)
                return
            block, chunk_id = queue.popleft()
        progress.start()
        board.set(tid, 'start', block, 'queued')
        ok, reason = process_section(tid, block, chunk_id, state, board, sf, org,
                                     poll_interval, job_timeout, profile_name,
                                     api_version, worker_proj, logger, max_iter,
                                     control)
        state.record_chunk(block, chunk_id, ok, reason)
        progress.finish(ok)
        if control.stop.is_set():
            return
        board.set(tid, 'ok' if ok else 'fail', block,
                  reason if not ok else 'clean', 0, 0)
        logger.log('[T%d][%s] %s' % (tid, block, ('DONE: ' + reason) if ok else ('FAILED: ' + reason)))
