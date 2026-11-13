# -*- coding: utf-8 -*-
"""Parallel pruning engine: the per-section worker and its section loop."""

import json
import os

from .constants import LISTABLE, LIST_METADATA_NAME, M, MIN_API, is_standard_api_name, version_ge
from .sfcli import dry_run, list_metadata, tooling_query
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
        list_name = LIST_METADATA_NAME.get(mdtype, mdtype)
        _report('[%d/%d] listing %s...' % (i, n_types, list_name))
        names, err = list_metadata(sf, org, list_name)
        if names is None:
            _report('[%d/%d] %s: skipped (%s)' % (i, n_types, list_name, err))
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


def _chunked(seq, size):
    """Yield `seq` in slices of at most `size` elements."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def prefetch_standard_dangling(sf, org, groups, logger, status=None):
    """Detect standard metadata the target org does not have, via its Tooling
    API, and return (type, member, source) removals.

    Covers three profile sections with three sources:

      - ``tabVisibilities``   ``standard-*``  -> TabDefinition   (`StandardTab`)
      - ``objectPermissions`` standard object -> EntityDefinition (`StandardObject`)
      - ``fieldPermissions``  standard field  -> FieldDefinition  (`StandardField`)

    EntityDefinition does not support queryMore and FieldDefinition requires a
    reified filter, so both are queried with a ``WHERE ... IN`` on exactly the
    names the profile references (in batches), never with an unfiltered scan.
    A source that fails is skipped without aborting the other two (the dry-run
    loop below remains the authoritative fallback).

    `status` is an optional callable(message) for live console progress."""
    def _report(msg):
        logger.log('STD-PREFETCH: %s' % msg)
        if status:
            status('standards: %s' % msg)

    def _txt(el, child):
        ce = el.find(M + child)
        return (ce.text or '').strip() if ce is not None else ''

    removals = []  # (type, member, source)

    # ---------- standard tabs (tabVisibilities: standard-*) ----------
    std_tabs = set()
    for el in groups.get('tabVisibilities', []):
        v = _txt(el, 'tab')
        if v.startswith('standard-'):
            std_tabs.add(v)
    if std_tabs:
        _report('listing %d standard tab(s) via TabDefinition...' % len(std_tabs))
        recs, err = tooling_query(sf, org, 'SELECT Name FROM TabDefinition')
        if recs is None:
            _report('TabDefinition skipped: %s' % err)
        else:
            valid = set(r.get('Name', '') for r in recs
                        if (r.get('Name') or '').startswith('standard-'))
            dangling = sorted(std_tabs - valid)
            for v in dangling:
                removals.append(('StandardTab', v, 'TabDefinition'))
            _report('TabDefinition: %d valid, %d dangling' % (len(valid), len(dangling)))

    # ---------- standard objects (objectPermissions: non-custom) ----------
    std_objs = set()
    for el in groups.get('objectPermissions', []):
        v = _txt(el, 'object')
        if v and is_standard_api_name(v):
            std_objs.add(v)
    if std_objs:
        _report('listing %d standard object(s) via EntityDefinition...' % len(std_objs))
        existing = set()
        failed = False
        for batch in _chunked(sorted(std_objs), 100):
            in_clause = ', '.join("'%s'" % o for o in batch)
            recs, err = tooling_query(
                sf, org,
                'SELECT QualifiedApiName FROM EntityDefinition '
                'WHERE QualifiedApiName IN (%s)' % in_clause)
            if recs is None:
                _report('EntityDefinition skipped: %s' % err)
                failed = True
                break
            existing.update(r.get('QualifiedApiName', '') for r in recs)
        if not failed:
            dangling = sorted(std_objs - existing)
            for o in dangling:
                removals.append(('StandardObject', o, 'EntityDefinition'))
            _report('EntityDefinition: %d valid, %d dangling' %
                    (len(std_objs & existing), len(dangling)))

    # ---------- standard fields (fieldPermissions: Object.Field) ----------
    # FieldDefinition does not support queryMore(), so a full scan is out, and
    # DurableId is NOT reliably 'Object.Field' (relationship/system fields like
    # Account.ParentId use a different DurableId) — filtering on it yields false
    # negatives. EntityDefinition.QualifiedApiName + QualifiedApiName is
    # authoritative for every field (relationships included), so we query by
    # object. To keep the number of queries low (per-object round trips are
    # slow and trip the Tooling API rate limit) we batch several objects per
    # query with an IN filter, and split a batch in half when it trips the
    # single-batch row limit (EXCEEDED_ID_LIMIT).
    fields_by_obj = {}
    for el in groups.get('fieldPermissions', []):
        v = _txt(el, 'field')
        if v and '.' in v:
            obj, fname = v.split('.', 1)
            if obj and is_standard_api_name(obj):
                fields_by_obj.setdefault(obj, set()).add(fname)

    def _collect(batch):
        """Collect fields for a list of objects. Returns True on success,
        False if a batch cannot be enumerated (kept for caller diagnostics)."""
        if not batch:
            return True
        in_clause = ', '.join("'%s'" % o for o in batch)
        recs, err = tooling_query(
            sf, org,
            'SELECT EntityDefinition.QualifiedApiName, QualifiedApiName '
            'FROM FieldDefinition '
            'WHERE EntityDefinition.QualifiedApiName IN (%s)' % in_clause)
        if recs is not None:
            for r in recs:
                ed = r.get('EntityDefinition') or {}
                o = ed.get('QualifiedApiName') if isinstance(ed, dict) else ''
                existing.add('%s.%s' % (o, r.get('QualifiedApiName')))
            return True
        if err and 'EXCEEDED_ID_LIMIT' in err and len(batch) > 1:
            mid = len(batch) // 2
            return _collect(batch[:mid]) and _collect(batch[mid:])
        _report('FieldDefinition batch skipped: %s' % err)
        return False

    if fields_by_obj:
        total = sum(len(s) for s in fields_by_obj.values())
        _report('listing %d standard field(s) across %d object(s) via FieldDefinition...'
                % (total, len(fields_by_obj)))
        existing = set()  # 'Object.Field' values present in the org
        objects = sorted(fields_by_obj)
        ok = True
        for i in range(0, len(objects), 40):
            batch = objects[i:i + 40]
            if not _collect(batch):
                ok = False
                break
        if ok:
            dangling = []
            for obj in objects:
                for fname in sorted(fields_by_obj[obj]):
                    key = '%s.%s' % (obj, fname)
                    if key not in existing:
                        dangling.append(key)
            for key in dangling:
                removals.append(('StandardField', key, 'FieldDefinition'))
            _report('FieldDefinition: %d valid, %d dangling' %
                    (total - len(dangling), len(dangling)))

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

        board.set(tid, 'dry-run', block, 'polling', initial, initial - len(elements))
        res, err = dry_run(sf, manifest_path, org, poll_interval, job_timeout,
                           logger, cwd=worker_proj,
                           on_status=lambda s: board.set(tid, 'dry-run', block, s,
                                                         initial, initial - len(elements)),
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
                  initial, initial - len(elements))
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
