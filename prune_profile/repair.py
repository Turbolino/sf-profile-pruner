# -*- coding: utf-8 -*-
"""``--onniscent``: full-access Profile repair.

Flip ON every permission entry already present, grant every org element
missing from the profile (enumerated via listMetadata / ``sf sobject list``
+ EntityDefinition / FieldDefinition + sobject describe ``permissionable`` /
TabDefinition / the PermissionSet schema for userPermissions), and let the
dry-run + targeted heal drop whatever the deploy rejects. Everything here is
read-only against the org; the dry-run stays authoritative.

The only Profile sections deliberately untouched are the non-permission
ones: ``layoutAssignments``, ``loginFlows``, ``loginIpRanges``,
``loginHours``, ``userLicense``, ``custom`` and ``description``. The
``default`` app / record-type children of existing entries are never
modified; a default record type is synthesized only for objects that had NO
``recordTypeVisibilities`` entry before the grant (a Profile deploy without
a default record type fails with "No default record type specified").
"""

import concurrent.futures
import re
from copy import deepcopy

from lxml import etree

from .constants import BLOCK_MAP, M, MIN_API, version_ge
from .merge import MERGE_TABLE, _element_ignored
from .sfcli import describe_object, list_metadata, soql_query, tooling_query
from .xmlio import resolve_error, tostring_xml

# ---------------------------------------------------------------------------
# ON values per permission section (full admin access). Sections absent from
# this table are deliberately never touched.
# ---------------------------------------------------------------------------
_ON_VALUES = {
    'applicationVisibilities': {'visible': 'true'},
    'classAccesses': {'enabled': 'true'},
    'pageAccesses': {'enabled': 'true'},
    'flowAccesses': {'enabled': 'true'},
    'customPermissions': {'enabled': 'true'},
    'customMetadataTypeAccesses': {'enabled': 'true'},
    'customSettingAccesses': {'enabled': 'true'},
    'externalDataSourceAccesses': {'enabled': 'true'},
    'servicePresenceStatusAccesses': {'enabled': 'true'},
    'agentAccesses': {'enabled': 'true'},
    'genComputingSummaryDefAccesses': {'enabled': 'true'},
    'userPermissions': {'enabled': 'true'},
    'categoryGroupVisibilities': {'visibility': 'VISIBLE'},
    'objectPermissions': {
        'allowRead': 'true', 'allowCreate': 'true', 'allowEdit': 'true',
        'allowDelete': 'true', 'viewAllRecords': 'true',
        'modifyAllRecords': 'true', 'viewAllFields': 'true',
    },
    'fieldPermissions': {'readable': 'true', 'editable': 'true'},
    'recordTypeVisibilities': {'visible': 'true'},
    'tabVisibilities': {'visibility': 'DefaultOn'},
}

# listMetadata type -> Profile section. Types not listable in the current
# org (AgentDefinition / GenComputingSummaryDefinition raise INVALID_TYPE)
# are skipped silently: no grants, existing entries still flipped.
_LIST_UNIVERSE = {
    'ApexClass': 'classAccesses',
    'ApexPage': 'pageAccesses',
    'Flow': 'flowAccesses',
    'CustomApplication': 'applicationVisibilities',
    'CustomTab': 'tabVisibilities',
    'CustomPermission': 'customPermissions',
    'ExternalDataSource': 'externalDataSourceAccesses',
    'RecordType': 'recordTypeVisibilities',
    'ServicePresenceStatus': 'servicePresenceStatusAccesses',
    'DataCategoryGroup': 'categoryGroupVisibilities',
    'AgentDefinition': 'agentAccesses',
    'GenComputingSummaryDefinition': 'genComputingSummaryDefAccesses',
}

# Canonical order for sections the profile does not contain at all: they are
# appended at the end of the file in this order (deterministic).
_SECTION_TAIL_ORDER = (
    'applicationVisibilities', 'classAccesses', 'customMetadataTypeAccesses',
    'customPermissions', 'customSettingAccesses', 'externalDataSourceAccesses',
    'fieldPermissions', 'flowAccesses', 'objectPermissions', 'pageAccesses',
    'recordTypeVisibilities', 'tabVisibilities', 'userPermissions',
    'categoryGroupVisibilities', 'servicePresenceStatusAccesses',
    'agentAccesses', 'genComputingSummaryDefAccesses',
)

# sObject name suffixes that CANNOT carry objectPermissions (junction /
# tracking / localization objects): the deploy rejects them, so they are
# excluded from the grant universe. The dry-run + heal remains authoritative
# for anything this list misses.
_NON_PERMISSIONABLE_SUFFIXES = ('Feed', 'Share', 'History', 'Localization')

# System-only field names the fieldPermissions schema rejects (audit /
# virtual sharing / connection fields).
_NON_PERMISSIONABLE_FIELDS = {
    'Id', 'IsDeleted', 'CreatedById', 'CreatedDate', 'LastModifiedById',
    'LastModifiedDate', 'SystemModstamp', 'RecordVisibilityId',
    'UserRecordAccessId', 'MasterRecordId', 'OwnerId',
    'ConnectionReceivedId', 'ConnectionSentId',
}

# EntityDefinition / FieldDefinition IN-clause chunk size (Tooling API).
_ENTITY_CHUNK = 40

# Extra deploy-failure patterns the prune machinery's resolve_error does not
# cover (the dry-run surfaces them when it rejects an ON entry).
_EXTRA_PROBLEM_RES = [
    re.compile(r'Unknown user permission:\s*(\S+)'),
    re.compile(r'You cannot deploy to a required field:\s*(\S+)'),
]


def _parallel_map(callables, io_threads):
    """Run `callables` in parallel, returning results in submission order
    (None where a callable failed - the caller treats that as a skip)."""
    results = [None] * len(callables)
    if not callables:
        return results
    if io_threads <= 1:
        for i, fn in enumerate(callables):
            try:
                results[i] = fn()
            except Exception:
                results[i] = None
        return results
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(io_threads, len(callables))) as ex:
        futures = [ex.submit(fn) for fn in callables]
        for i, fut in enumerate(futures):
            try:
                results[i] = fut.result()
            except Exception:
                results[i] = None
    return results


def _on_element(section, name):
    """One full-access entry in the Profile schema (key child + ON flags)."""
    el = etree.Element(M + section)
    key_child = MERGE_TABLE[section][0]
    etree.SubElement(el, M + key_child).text = name
    for child, value in _ON_VALUES[section].items():
        etree.SubElement(el, M + child).text = value
    return el


def flip_profile_on(profile_path, ignore=None):
    """Turn every permission entry already present ON, in place (new XML).

    Sections absent from ``_ON_VALUES`` (layoutAssignments, loginFlows,
    loginIpRanges, loginHours, userLicense, custom, description) and entries
    matched by ``--ignore`` are left exactly as they are; the ``default``
    app / record-type children are never written. Returns
    ``(xml_bytes, flipped_by_section, already_on)`` where ``already_on`` is
    the number of entries already at their full-access value."""
    root = etree.parse(profile_path).getroot()
    counts = {}
    already_on = 0
    for el in root:
        section = el.tag.split('}')[-1]
        if section not in _ON_VALUES:
            continue
        if _element_ignored(el, section, ignore):
            continue
        changed = False
        for child, value in _ON_VALUES[section].items():
            ce = el.find(M + child)
            if ce is None:
                ce = etree.SubElement(el, M + child)
            if (ce.text or '').strip() != value:
                ce.text = value
                changed = True
        if changed:
            counts[section] = counts.get(section, 0) + 1
        else:
            already_on += 1
    etree.indent(root, space='    ')
    return tostring_xml(root), counts, already_on


def _sobject_list(sf, org):
    """Every sObject name via ``sf sobject list``. Returns (list_or_None, err)."""
    import json as _json
    import subprocess
    cmd = [sf, 'sobject', 'list', '--target-org', org, '--json']
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, str(e)
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or b'').decode('utf-8', 'replace')[:300]
    try:
        payload = _json.loads(proc.stdout or b'{}')
    except Exception as e:
        return None, 'unparseable sobject list output: %s' % e
    result = payload.get('result')
    if isinstance(result, list):
        return [str(x) for x in result if x], None
    if isinstance(result, dict):
        names = []
        for key in ('standard', 'custom'):
            chunk = result.get(key)
            if isinstance(chunk, list):
                names.extend(str(x) for x in chunk if x)
        if names:
            return names, None
    return None, 'unexpected sobject list output shape'


def _classify_objects(sf, org, names, io_threads):
    """EntityDefinition IN-chunk probe: returns
    ``([(QualifiedApiName, IsCustomSetting), ...], err)`` for the names that
    EXIST in EntityDefinition (external objects / Data Cloud objects are not
    enumerated there and are therefore dropped)."""

    def _chunk(chunk):
        in_clause = ', '.join("'%s'" % n.replace("'", "\\'") for n in chunk)
        recs, err = tooling_query(
            sf, org,
            'SELECT QualifiedApiName, IsCustomSetting FROM EntityDefinition '
            'WHERE QualifiedApiName IN (%s)' % in_clause)
        if recs is None:
            return None
        return [(str(r['QualifiedApiName']),
                 str(r.get('IsCustomSetting')).lower() == 'true')
                for r in recs if r.get('QualifiedApiName')]

    chunks = [names[i:i + _ENTITY_CHUNK]
              for i in range(0, len(names), _ENTITY_CHUNK)]
    results = _parallel_map([(lambda c=c: _chunk(c)) for c in chunks],
                            io_threads)
    if any(r is None for r in results):
        return None, 'one or more EntityDefinition probes failed'
    out = []
    for res in results:
        out.extend(res)
    return out, None


def build_universe(sf, org, api_version, logger=None, io_threads=8):
    """Enumerate the org into a ``{section: [member, ...]}`` grant universe
    for a Profile. Read-only and best-effort per section: a failing oracle
    only skips its own section (logged), it never fails the run. Members are
    sorted for a deterministic output."""

    def _log(msg):
        if logger:
            logger.log('ONNISCENT: %s' % msg)

    def _add(section, names):
        clean = {n for n in names if n}
        if clean:
            universe.setdefault(section, set()).update(clean)

    universe = {}

    # ---------- listMetadata types (parallel) ----------
    def _list_type(mdn):
        names, err = list_metadata(sf, org, mdn)
        if names is None:
            _log('listMetadata %s skipped: %s' % (mdn, err or 'unknown error'))
            return None
        _log('%s: %d component(s)' % (mdn, len(names)))
        return names

    jobs = [mdn for mdn in _LIST_UNIVERSE
            if mdn not in MIN_API or version_ge(api_version, MIN_API[mdn])]
    listed = _parallel_map([(lambda m=mdn: _list_type(m)) for mdn in jobs],
                           io_threads)
    for mdn, names in zip(jobs, listed):
        if names:
            _add(_LIST_UNIVERSE[mdn], names)

    # ---------- standard tabs (TabDefinition) ----------
    recs, err = tooling_query(sf, org, 'SELECT Name FROM TabDefinition')
    if recs is None:
        _log('TabDefinition skipped: %s' % (err or 'unknown error'))
    else:
        _add('tabVisibilities',
             [str(r['Name']) for r in recs
              if (r.get('Name') or '').startswith('standard-')])

    # ---------- person-account record types ----------
    # listMetadata lists them under their REAL object (Account.PersonAccount)
    # while the Profile schema refers to the PersonAccount pseudo-entity:
    # rewrite to the file format. The standard RecordType object spells the
    # field `SobjectType` (lowercase 'o') in its API name.
    recs, err = soql_query(
        sf, org,
        "SELECT SObjectType, DeveloperName FROM RecordType "
        "WHERE SObjectType = 'Account' AND IsPersonType = true")
    if recs is None:
        _log('person-account record types skipped: %s' % (err or 'unknown error'))
    else:
        rt_set = universe.get('recordTypeVisibilities')
        for r in recs:
            sobj = r.get('SObjectType') or r.get('SobjectType')
            dev = r.get('DeveloperName')
            if not sobj or not dev:
                continue
            if rt_set is not None:
                rt_set.discard('%s.%s' % (sobj, dev))
            _add('recordTypeVisibilities', ['PersonAccount.%s' % dev])

    # ---------- objects / custom settings / custom metadata types ----------
    objs, err = _sobject_list(sf, org)
    if objs is None:
        _log('object enumeration skipped: %s' % (err or 'unknown error'))
    else:
        objects, custom_settings, mdts = set(), set(), set()
        classified, err = _classify_objects(sf, org, objs, io_threads)
        if classified is None:
            _log('EntityDefinition classification skipped: %s'
                 % (err or 'unknown error'))
            for q in objs:
                if q.endswith('__mdt'):
                    mdts.add(q)
                elif not q.endswith(_NON_PERMISSIONABLE_SUFFIXES):
                    objects.add(q)
        else:
            for q, is_custom_setting in classified:
                if q.endswith('__mdt'):
                    mdts.add(q)
                elif is_custom_setting:
                    custom_settings.add(q)
                elif not q.endswith(_NON_PERMISSIONABLE_SUFFIXES):
                    objects.add(q)
        _add('customMetadataTypeAccesses', mdts)
        _add('customSettingAccesses', custom_settings)
        _add('objectPermissions', objects)
        _log('objects: %d, custom settings: %d, custom metadata types: %d'
             % (len(objects), len(custom_settings), len(mdts)))

    # ---------- fieldPermissions ----------
    field_scope = sorted(objects)

    def _fields_chunk(chunk):
        in_clause = ', '.join("'%s'" % o.replace("'", "\\'") for o in chunk)
        recs, err = tooling_query(
            sf, org,
            'SELECT QualifiedApiName, EntityDefinition.QualifiedApiName '
            'FROM FieldDefinition '
            'WHERE EntityDefinition.QualifiedApiName IN (%s)' % in_clause)
        if recs is None:
            _log('FieldDefinition chunk skipped: %s' % (err or 'unknown error'))
            return []
        out = []
        for r in recs:
            entity = r.get('EntityDefinition') or {}
            entity = entity.get('QualifiedApiName') if isinstance(entity, dict) else None
            field = r.get('QualifiedApiName')
            if (entity and field and field not in _NON_PERMISSIONABLE_FIELDS):
                out.append('%s.%s' % (entity, field))
        return out

    chunks = [field_scope[i:i + _ENTITY_CHUNK]
              for i in range(0, len(field_scope), _ENTITY_CHUNK)]
    field_results = _parallel_map(
        [(lambda c=c: _fields_chunk(c)) for c in chunks], io_threads)
    field_names = []
    for res in field_results:
        if res:
            field_names.extend(res)

    # Permissionable filter (sobject describe): fieldPermissions cannot
    # include REQUIRED fields (Name, CreatedById, required custom fields...):
    # the deploy rejects them even with explicit true values. FieldDefinition
    # has no required/permissionable column, so the per-object describe's
    # `permissionable` flag is the discriminator. Best-effort: on failure (or
    # when the attribute is absent) the unfiltered list is kept and the
    # dry-run + targeted heal handle the stragglers.
    def _permissionable_fields(obj):
        desc, derr = describe_object(sf, org, obj)
        if desc is None:
            return None
        fields = desc.get('fields') or []
        if not any('permissionable' in f for f in fields):
            return None
        return {str(f['name']) for f in fields if f.get('permissionable')}

    described = _parallel_map(
        [(lambda o=o: _permissionable_fields(o)) for o in field_scope],
        io_threads)
    permissionable_by_obj = {}
    for o, res in zip(field_scope, described):
        if res is not None:
            permissionable_by_obj[o] = res
    _log('permissionable filter: %d/%d object(s) described'
         % (len(permissionable_by_obj), len(field_scope)))
    if permissionable_by_obj:
        kept = []
        for name in field_names:
            obj, _, fld = name.partition('.')
            if obj in permissionable_by_obj \
                    and fld not in permissionable_by_obj[obj]:
                continue
            kept.append(name)
        field_names = kept
    _add('fieldPermissions', field_names)
    _log('fieldPermissions: %d field(s)' % len(field_names))

    # ---------- userPermissions (PermissionSet schema) ----------
    # readMetadata does not return the standard admin Profile, so the
    # universe is the org's own PermissionSet schema: every `PermissionsXxx`
    # legacy field maps to a userPermission `Xxx`. The one field the deploy
    # does not accept as a userPermission is dropped by the targeted heal.
    desc, derr = describe_object(sf, org, 'PermissionSet')
    if desc is None:
        _log('userPermissions skipped: %s' % (derr or 'unknown error'))
    else:
        perms = [f['name'][11:] for f in desc.get('fields', [])
                 if str(f.get('name', '')).startswith('Permissions')]
        _add('userPermissions', perms)
        _log('userPermissions: %d permission(s) from the PermissionSet schema'
             % len(perms))

    return {section: sorted(names) for section, names in universe.items()}


def grant_missing(profile_path, universe, ignore=None):
    """Append a full-access entry for every universe element the profile does
    not declare. Returns ``(xml_bytes, granted_by_section, synthesized)``
    where ``synthesized`` is the number of default record types created for
    objects that had no recordTypeVisibilities entry at all."""
    root = etree.parse(profile_path).getroot()
    existing = {}
    last_index = {}
    for i, el in enumerate(root):
        section = el.tag.split('}')[-1]
        if section not in _ON_VALUES:
            continue
        key_child = MERGE_TABLE[section][0]
        key = (el.findtext(M + key_child) or '').strip()
        existing.setdefault(section, set()).add(key)
        last_index[section] = i

    pending = {}
    synthesized = 0
    synthesized_objs = set()
    existing_rt_objs = {k.split('.', 1)[0]
                        for k in existing.get('recordTypeVisibilities', ())}
    for section in _SECTION_TAIL_ORDER:
        if section not in universe:
            continue
        adds = []
        for name in universe[section]:
            if name in existing.get(section, ()):
                continue
            el = _on_element(section, name)
            if ignore and _element_ignored(el, section, ignore):
                continue
            if section == 'recordTypeVisibilities':
                obj = name.split('.', 1)[0]
                if obj not in existing_rt_objs and obj not in synthesized_objs:
                    # First granted record type for an object with no
                    # existing entry: synthesize default=true (a Profile
                    # deploy fails without a default record type). Existing
                    # defaults are never touched.
                    d = etree.Element(M + 'default')
                    d.text = 'true'
                    el.insert(1, d)
                    synthesized += 1
                    synthesized_objs.add(obj)
            adds.append(el)
        if adds:
            pending[section] = adds

    if not pending:
        return tostring_xml(root), {}, 0

    counts = {s: len(v) for s, v in pending.items()}
    new_root = etree.Element(root.tag, nsmap=root.nsmap)
    for i, el in enumerate(root):
        new_root.append(deepcopy(el))
        section = el.tag.split('}')[-1]
        if last_index.get(section) == i and section in pending:
            for on_el in pending.pop(section):
                new_root.append(on_el)
    for section in _SECTION_TAIL_ORDER:
        if section in pending:
            for on_el in pending.pop(section):
                new_root.append(on_el)
    for section in list(pending):  # safety net
        for on_el in pending[section]:
            new_root.append(on_el)
    etree.indent(new_root, space='    ')
    return tostring_xml(new_root), counts, synthesized


def entry_removals_from_failures(component_failures):
    """Map raw deploy component failures to ``(section, child, mode, member)``
    removals, reusing the prune machinery's ``resolve_error`` oracle plus the
    unknown-user-permission and required-field patterns. Returns a set of
    4-tuples; empty when no failure is entry-level."""
    out = set()
    for f in component_failures:
        problem = str((f or {}).get('problem', '') or '')
        rr = resolve_error(problem)
        if rr:
            typ, member = rr
        else:
            m = None
            for pat in _EXTRA_PROBLEM_RES:
                m = pat.search(problem)
                if m:
                    break
            if not m:
                continue
            if 'required field' in problem:
                typ, member = 'CustomField', m.group(1)
            else:
                typ, member = 'UserPermission', m.group(1)
        if typ in ('CustomField', 'StandardField'):
            out.add(('fieldPermissions', 'field', 'exact', member))
            continue
        for section, child, mode in BLOCK_MAP.get(typ, []):
            out.add((section, child, mode, member))
    return out


def strip_flagged_entries(path, removals):
    """Remove the dry-run-flagged entries from a profile, in place (single
    rewrite). ``removals`` is a set of ``(section, child, mode, member)``
    tuples. Returns the removed count."""
    root = etree.parse(path).getroot()
    by_section = {}
    for section, child, mode, member in removals:
        by_section.setdefault((section, child), []).append((mode, member))
    kept = []
    removed = 0
    for el in root:
        section = el.tag.split('}')[-1]
        drop_el = False
        for (sec, child), pairs in by_section.items():
            if sec != section:
                continue
            ce = el.find(M + child)
            val = (ce.text or '').strip() if ce is not None else ''
            for mode, member in pairs:
                if ((mode == 'exact' and val == member)
                        or (mode == 'prefix' and val.startswith(member + '.'))):
                    drop_el = True
                    break
            if drop_el:
                break
        if drop_el:
            removed += 1
        else:
            kept.append(el)
    if not removed:
        return 0
    new_root = etree.Element(root.tag, nsmap=root.nsmap)
    for el in kept:
        new_root.append(deepcopy(el))
    etree.indent(new_root, space='    ')
    with open(path, 'wb') as f:
        f.write(tostring_xml(new_root))
    return removed


def write_onniscent_wic(path, flipped, granted, dropped, normalized,
                        already_on, synthesized, dest_env):
    """Write the onniscent what-is-changed report: FLIPPED (turned ON),
    GRANTED (added ON), DROPPED (rejected by the deploy, removed),
    NORMALIZED (read-only fields rewritten to editable=false), ALREADY ON
    and SYNTHESIZED DEFAULTS. Per-section counts for the first two, full
    member list for the dropped ones. Returns the path."""
    total_flipped = sum(flipped.values())
    total_granted = sum(granted.values())
    lines = [
        'WHAT IS CHANGED - onniscent (destination: %s)' % dest_env,
        '=' * 78,
        '',
        'The profile was brought to FULL access. Every permission entry',
        'already present was turned ON (FLIPPED); every org element missing',
        'from the profile was added ON (GRANTED); entries the deploy rejected',
        'were removed (DROPPED). Entries already at full access are untouched.',
        'The org was NOT modified unless --deploy-on-success was given.',
        '',
        'FLIPPED (turned ON): %d' % total_flipped,
        '',
    ]
    for section in sorted(flipped):
        lines.append('  %s: %d' % (section, flipped[section]))
    lines.append('')
    lines.append('GRANTED (added ON): %d' % total_granted)
    if granted:
        lines.append('')
        for section in sorted(granted):
            lines.append('  %s: %d' % (section, granted[section]))
    lines.append('')
    lines.append('DROPPED (rejected by the deploy, removed): %d' % len(dropped))
    if dropped:
        lines.append('')
        for section, member in dropped:
            lines.append('  %s: %s' % (section, member))
    lines.append('')
    lines.append('NORMALIZED (read-only fields rewritten to editable=false): %d'
                 % normalized)
    lines.append('')
    lines.append('ALREADY ON: %d' % already_on)
    if synthesized:
        lines.append('')
        lines.append('SYNTHESIZED DEFAULTS: %d' % synthesized)
        lines.append('  (first granted record type per object with no existing')
        lines.append('  entry, so the Profile stays deployable)')
    lines.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines).rstrip() + '\n')
    return path
