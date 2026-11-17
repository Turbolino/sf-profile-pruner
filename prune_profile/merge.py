# -*- coding: utf-8 -*-
"""Alignment merge: combine a pruned source profile with a pruned destination
profile so the destination matches the source.

Deploy semantics (verified empirically against API 68.0):

  * A Profile deploy is a per-element MERGE/upsert. Omitting an entry from the
    deployed file is a silent no-op — it never removes or turns off anything in
    the target org.
  * The only way to turn a permission OFF is an explicit off value, e.g.
    ``<visible>false</visible>``, ``<visibility>Hidden</visibility>``,
    ``<enabled>false</enabled>``.
  * A few sections have NO expressible off value (``layoutAssignments``,
    ``loginFlows``) and therefore cannot be turned off by a Profile deploy at
    all. They are reported as manual operations.

``merge_profiles`` is the core: source is authoritative, every source entry is
kept verbatim, every destination-only entry is turned off (alignable sections)
or reported as a manual operation (non-alignable sections).
"""

from collections import Counter
from copy import deepcopy

from lxml import etree

from .constants import M, NS

# ---------------------------------------------------------------------------
# section -> (key_child, {child: off_text})
#
# `key_child` is the child element whose text identifies the entry (the name /
# reference). `off` maps the fields that must be set to their "off" value to
# actually turn the entry off in the target org. Sections absent from this table
# (or listed in NON_ALIGNABLE) have no expressible off value.
# ---------------------------------------------------------------------------
MERGE_TABLE = {
    'applicationVisibilities': ('application', {'default': 'false', 'visible': 'false'}),
    'classAccesses': ('apexClass', {'enabled': 'false'}),
    'pageAccesses': ('apexPage', {'enabled': 'false'}),
    'flowAccesses': ('flow', {'enabled': 'false'}),
    'objectPermissions': ('object', {
        'allowCreate': 'false', 'allowRead': 'false', 'allowEdit': 'false',
        'allowDelete': 'false', 'viewAllRecords': 'false',
        'modifyAllRecords': 'false'}),
    'fieldPermissions': ('field', {'readable': 'false', 'editable': 'false'}),
    'recordTypeVisibilities': ('recordType', {'default': 'false', 'visible': 'false'}),
    'tabVisibilities': ('tab', {'visibility': 'Hidden'}),
    'customPermissions': ('name', {'enabled': 'false'}),
    'customMetadataTypeAccesses': ('name', {'enabled': 'false'}),
    'customSettingAccesses': ('name', {'enabled': 'false'}),
    'externalDataSourceAccesses': ('externalDataSource', {'enabled': 'false'}),
    'servicePresenceStatusAccesses': ('servicePresenceStatus', {'enabled': 'false'}),
    'categoryGroupVisibilities': ('dataCategoryGroup', {'visibility': 'NONE'}),
    'userPermissions': ('name', {'enabled': 'false'}),
    'agentAccesses': ('agent', {'enabled': 'false'}),
    'genComputingSummaryDefAccesses': ('name', {'enabled': 'false'}),
}

# Sections with no expressible "off" value: they can be omitted (a no-op) but
# never turned off by a Profile deploy.
NON_ALIGNABLE = ('layoutAssignments', 'loginFlows', 'loginIpRanges', 'loginHours')

# Field types whose `editable` flag is meaningless: Salesforce always treats
# these as read-only, so an `editable=true` in the source profile is dead
# metadata (the effective value is false). The alignment normalizes them to
# `false` instead of reporting a fake "grant" that the platform will ignore.
#
# These prefixes are a FAST heuristic only: the authoritative check (SObject
# describe `updateable`) is applied on top for the remaining editable=true
# fields (see cli._read_only_field_set / retrieve.describe_field_updateable).
READ_ONLY_FIELD_TYPE_PREFIXES = ('Formula',)
READ_ONLY_FIELD_TYPE_EXACT = frozenset({'Roll-Up Summary', 'Auto Number'})


def _txt(el, child):
    ce = el.find(M + child)
    return (ce.text or '').strip() if ce is not None else ''


def _set_child_text(el, child, value):
    """Set (or create) a child element's text on `el`."""
    ce = el.find(M + child)
    if ce is None:
        ce = etree.SubElement(el, M + child)
    ce.text = value


def field_permissions_names(path):
    """Field full names referenced by ``fieldPermissions`` in `path` (in file
    order, duplicates kept). Used to look up their DataType in the org."""
    root = etree.parse(path).getroot()
    names = []
    for el in root:
        if isinstance(el.tag, str) and el.tag.split('}')[-1] == 'fieldPermissions':
            names.append(_txt(el, 'field'))
    return names


def field_permissions_editable(path):
    """Map ``fieldPermissions`` field full name -> editable bool (``True`` only
    when the source literally says ``<editable>true</editable>``). Used to know
    which fields can carry a phantom grant when the org marks them read-only."""
    root = etree.parse(path).getroot()
    out = {}
    for el in root:
        if isinstance(el.tag, str) and el.tag.split('}')[-1] == 'fieldPermissions':
            name = _txt(el, 'field')
            if name:
                out[name] = _txt(el, 'editable') == 'true'
    return out


def _keys_for(section):
    """Child elements that identify an entry of a non-alignable section."""
    if section == 'layoutAssignments':
        return ['layout', 'recordType']
    if section == 'loginFlows':
        return ['flow', 'visualForcePage']
    if section == 'loginIpRanges':
        return ['startAddress', 'endAddress']
    if section == 'loginHours':
        return []
    return []


def _entry_key(el, keys):
    return tuple(_txt(el, k) for k in keys)


def _groups(root):
    out = {}
    for el in root:
        if isinstance(el.tag, str):
            out.setdefault(el.tag.split('}')[-1], []).append(el)
    return out


def _make_off_element(section, key_child, off_fields, template):
    el = etree.Element(M + section)
    etree.SubElement(el, M + key_child).text = _txt(template, key_child)
    for child, value in off_fields.items():
        etree.SubElement(el, M + child).text = value
    return el


def _layout_object(el):
    """Object API name a layoutAssignments entry refers to (the part before
    the first '-' of the layout full name)."""
    layout = _txt(el, 'layout')
    return layout.split('-', 1)[0] if '-' in layout else layout


def _describe_entry(section, el):
    """Human description of a non-alignable entry, for the manual-ops report."""
    if section == 'layoutAssignments':
        layout = _txt(el, 'layout')
        rt = _txt(el, 'recordType')
        item = layout if not rt else '%s (record type: %s)' % (layout, rt)
        obj = _layout_object(el)
        return {
            'section': section,
            'object': obj,
            'item': item,
            'operation': 'Align the page layout assignment with the source',
            'where': ('Setup > Object Manager > %s > Page Layouts > '
                      'Page Layout Assignment' % obj),
        }
    if section == 'loginFlows':
        flow = _txt(el, 'flow') or _txt(el, 'visualForcePage')
        friendly = _txt(el, 'friendlyName')
        item = friendly or flow
        return {
            'section': section,
            'item': item,
            'operation': 'Remove the login flow association for this profile',
            'where': 'Setup > Login Flows > %s' % (friendly or flow),
        }
    if section == 'loginIpRanges':
        start = _txt(el, 'startAddress')
        end = _txt(el, 'endAddress')
        item = ('%s - %s' % (start, end)) if (start or end) else (_txt(el, 'description') or 'unnamed range')
        return {
            'section': section,
            'item': item,
            'operation': 'Delete the login IP range',
            'where': 'Setup > Profiles > this profile > Login IP Ranges',
        }
    if section == 'loginHours':
        return {
            'section': section,
            'item': 'login hours restriction',
            'operation': 'Remove the login hours restriction',
            'where': 'Setup > Profiles > this profile > Login Hours',
        }
    return {'section': section, 'item': '', 'operation': 'Remove manually',
            'where': 'Setup > Profiles > this profile'}


_VISIBILITY_ORDER = {'Hidden': 0, 'DefaultOff': 1, 'DefaultOn': 2}


def _field_direction(field, old, new, off_val):
    """Direction of one field change: ``'grant'`` (turned ON) or ``'revoke'``
    (turned OFF). ``old`` is the destination value or ``None`` when the entry
    (or field) is absent; ``new`` is the merged (aligned) value."""
    if new == off_val:
        return 'revoke'
    if old == off_val or old is None:
        return 'grant'
    # Both are non-off and different (multi-state fields only, e.g.
    # tabVisibilities.visibility: Hidden / DefaultOff / DefaultOn).
    if field == 'visibility':
        order = _VISIBILITY_ORDER
        return 'grant' if order.get(new, 0) > order.get(old, 0) else 'revoke'
    return 'grant'


def _element_xml(el):
    """Serialize a single element with 4-space indentation, no declaration and
    no trailing tail (so a comment can be appended on the same line)."""
    el = deepcopy(el)
    el.tail = None  # drop the whitespace tail inherited from the parent tree
    etree.indent(el, space='    ')
    return etree.tostring(el, encoding='unicode').rstrip('\n')


def _entry_label(section, el):
    """Human name of a non-alignable entry, used for the removed list."""
    if section == 'layoutAssignments':
        layout = _txt(el, 'layout')
        rt = _txt(el, 'recordType')
        return layout if not rt else '%s (record type: %s)' % (layout, rt)
    if section == 'loginFlows':
        return _txt(el, 'flow') or _txt(el, 'visualForcePage') or _txt(el, 'friendlyName')
    if section == 'loginIpRanges':
        start = _txt(el, 'startAddress')
        end = _txt(el, 'endAddress')
        return '%s - %s' % (start, end) if (start or end) else (_txt(el, 'description') or 'unnamed range')
    if section == 'loginHours':
        return 'login hours restriction'
    return _txt(el, 'name') or ''


def _render_alignable_change(change, dest_env):
    """XML block for one granted/revoked entry, with the old destination value
    inline right after each field that actually changed (``not present`` when
    the field did not exist in the destination)."""
    section = change['section']
    key_child, off_fields = MERGE_TABLE[section]
    el = change['element']
    old_values = change['old_values']
    out = ['<%s>' % section]
    out.append('    <%s>%s</%s>' % (key_child, change['key'], key_child))
    for field, _off in off_fields.items():
        new = _txt(el, field)
        if new == '':
            continue
        line = '    <%s>%s</%s>' % (field, new, field)
        if field in old_values:
            old = old_values[field]
            if old is None:
                line += ' <!-- old value from %s: not present -->' % dest_env
            else:
                line += ' <!-- old value from %s: %s -->' % (dest_env, old)
        out.append(line)
    out.append('</%s>' % section)
    return out


def _removed_block(section, entries, dest_env, verdict_note=None):
    """One grouped block per non-alignable section: a single <section> wrapper
    listing every removed key, plus a comment enumerating them. ``verdict_note``
    (when given) replaces the default "removed because not present" tail so the
    layoutAssignment redundancy/check classification is readable inline."""
    keys = _keys_for(section)
    labels = [_entry_label(section, e['element']) for e in entries]
    labels = [l for l in labels if l]
    tail = verdict_note or 'removed in destination org because not present in source org'
    comment = '%s %s' % (', '.join(labels), tail)

    if not keys:
        # No key children to list (e.g. loginHours): emit the element verbatim.
        lines = [_element_xml(e['element']) for e in entries]
        lines[-1] = lines[-1] + '  <!-- ' + comment + ' -->'
        return lines

    lines = ['<%s>' % section]
    for e in entries:
        el = e['element']
        for k in keys:
            v = _txt(el, k)
            if v:
                lines.append('    <%s>%s</%s>' % (k, v, k))
    lines.append('</%s>  <!-- %s -->' % (section, comment))
    return lines


def merge_profiles(source_path, dest_path, dest_env='destination org',
                   read_only_fields=None):
    """Merge a pruned source profile with a pruned destination profile.

    Source is authoritative: every source entry is kept verbatim; every
    destination-only entry is turned off (alignable sections) or recorded as a
    manual operation (non-alignable sections). Destination-only entries that
    are already off are skipped (no change).

    ``read_only_fields`` (optional) is a set of ``fieldPermissions`` field full
    names whose type is inherently read-only (formula / roll-up summary /
    auto number). Their ``editable`` flag is forced to ``false`` so the merged
    profile and the what-is-changed report reflect the effective permission
    instead of a meaningless ``true``.

    Returns ``(merged_xml_bytes, manual_ops, changes)`` where ``changes`` is the
    full list of differences the deploy will apply, each ``kind`` being
    ``grant`` (turned ON in the destination), ``revoke`` (turned OFF) or
    ``removed`` (non-alignable, manual action).
    """
    src_root = etree.parse(source_path).getroot()
    dst_root = etree.parse(dest_path).getroot()
    src_groups = _groups(src_root)
    dst_groups = _groups(dst_root)

    read_only_fields = read_only_fields or set()

    out = etree.Element(M + 'Profile', nsmap={None: NS})
    manual_ops = []
    changes = []

    sections = list(src_groups)
    for s in dst_groups:
        if s not in sections:
            sections.append(s)

    for section in sections:
        src_els = src_groups.get(section, [])
        dst_els = dst_groups.get(section, [])

        if section in MERGE_TABLE:
            key_child, off_fields = MERGE_TABLE[section]
            src_by_key = {_entry_key(el, [key_child]): el for el in src_els}
            dst_by_key = {_entry_key(el, [key_child]): el for el in dst_els}

            # Merged section: source entries verbatim (minus no-op entries) + an
            # explicit off element for each destination-only entry.
            merged_section = []
            for el in src_els:
                m_el = deepcopy(el)
                if (section == 'fieldPermissions' and read_only_fields
                        and _txt(m_el, key_child) in read_only_fields):
                    # Normalize read-only fields: `editable=true` is dead
                    # metadata (Salesforce always keeps them read-only).
                    _set_child_text(m_el, 'editable', 'false')
                key = _entry_key(m_el, [key_child])
                # Skip source entries that are already fully OFF and absent from
                # the destination: they are already aligned (both deny) and
                # deploying an explicit off value is a no-op that never persists,
                # so it would be reported forever without converging.
                if (key not in dst_by_key
                        and all(_txt(m_el, f) in ('', off_val)
                                for f, off_val in off_fields.items())):
                    continue
                merged_section.append(m_el)
            for key, el in dst_by_key.items():
                if key in src_by_key:
                    continue
                # Skip destination-only entries that are already fully off:
                # turning them off again is a no-op, not a change to report.
                if all(_txt(el, f) in ('', off_val) for f, off_val in off_fields.items()):
                    continue
                merged_section.append(_make_off_element(section, key_child, off_fields, el))
            for m_el in merged_section:
                out.append(m_el)

            # Field-level diff between the merged section and the destination:
            # every difference is a deploy-time change (grant or revoke).
            merged_by_key = {_entry_key(e, [key_child]): e for e in merged_section}
            for key in sorted(set(merged_by_key) | set(dst_by_key)):
                m_el = merged_by_key.get(key)
                d_el = dst_by_key.get(key)
                old_values = {}
                directions = Counter()
                for field, off_val in off_fields.items():
                    new = _txt(m_el, field) if m_el is not None else ''
                    if new == '':
                        continue  # source omits the field: deploy omits -> no-op
                    old = _txt(d_el, field) if d_el is not None else None
                    if old == '':
                        old = None
                    if old == new:
                        continue
                    old_values[field] = old
                    directions[_field_direction(field, old, new, off_val)] += 1
                if not old_values:
                    continue
                kind = 'revoke' if directions['revoke'] >= directions['grant'] else 'grant'
                changes.append({
                    'kind': kind,
                    'section': section,
                    'key': key[0],
                    'element': deepcopy(m_el) if m_el is not None else deepcopy(d_el),
                    'old_values': old_values,
                })
        elif section in NON_ALIGNABLE:
            keys = _keys_for(section)
            src_keys = {_entry_key(el, keys) for el in src_els}
            for el in src_els:
                out.append(deepcopy(el))
            for el in dst_els:
                if _entry_key(el, keys) not in src_keys:
                    manual_ops.append(_describe_entry(section, el))
                    change = {
                        'kind': 'removed',
                        'section': section,
                        'key': _txt(el, keys[0]) if keys else '',
                        'element': deepcopy(el),
                        'old_values': {},
                    }
                    if section == 'layoutAssignments':
                        change['object'] = _layout_object(el)
                    changes.append(change)
        else:
            # Unknown or scalar section (custom, userLicense, ...): source is
            # authoritative; keep the destination value only when source has
            # none (identity scalars are not permissions to turn off).
            if src_els:
                for el in src_els:
                    out.append(deepcopy(el))
            else:
                for el in dst_els:
                    out.append(deepcopy(el))

    etree.indent(out, space='    ')
    xml = etree.tostring(out, xml_declaration=True, encoding='UTF-8')
    return xml, manual_ops, changes


def removed_non_alignable(original_path, clean_path):
    """Manual operations for entries in non-alignable sections that were removed
    between ``original_path`` (untouched source) and ``clean_path`` (pruned).

    The prune removes such entries from the deliverable file, but a Profile
    deploy cannot remove them from the destination org (merge semantics), so
    they are surfaced as manual operations.
    """
    orig_root = etree.parse(original_path).getroot()
    clean_root = etree.parse(clean_path).getroot()
    orig_groups = _groups(orig_root)
    clean_groups = _groups(clean_root)

    ops = []
    for section in NON_ALIGNABLE:
        keys = _keys_for(section)
        clean_keys = {_entry_key(el, keys) for el in clean_groups.get(section, [])}
        for el in orig_groups.get(section, []):
            if _entry_key(el, keys) not in clean_keys:
                op = _describe_entry(section, el)
                op['operation'] = ('%s (removed from the clean profile; a '
                                   'Profile deploy cannot remove it from the '
                                   'destination org)' % op['operation'])
                ops.append(op)
    return ops


def classify_layout_ops(manual_ops, changes, layout_count):
    """Annotate layoutAssignments entries with a redundancy verdict.

    ``layout_count`` maps an object API name -> number of Layout components it
    has in the destination org. A destination-only explicit layout assignment
    is **redundant/ignorable** when the object has exactly one layout (it can
    only point at the single default); with zero or several layouts the
    assignment must be **checked** against the default by hand. The verdict is
    written onto both the manual-ops dicts and the ``removed`` change dicts so
    every report (MANUAL-OPERATIONS.txt and what-is-changed.txt) shows it.
    """
    for op in manual_ops:
        if op.get('section') != 'layoutAssignments':
            continue
        n = layout_count.get(op.get('object'), 0)
        if n == 1:
            op['verdict'] = 'REDUNDANT (ignorable)'
            op['operation'] = ('Explicit assignment is the object\'s ONLY layout '
                               '(= its default); source inherits the same default')
        else:
            op['verdict'] = 'NEEDS CHECK'
            op['operation'] = ('Object has %d layouts; verify this assignment '
                               'matches the default and change it if it does not' % n)
    for c in changes:
        if c.get('section') != 'layoutAssignments':
            continue
        n = layout_count.get(c.get('object'), 0)
        c['verdict'] = 'redundant' if n == 1 else 'check'
    return manual_ops, changes


def format_ops_table(ops, show_verdict=None):
    """Render manual operations as an aligned plain-text table. When the ops
    carry a ``verdict`` (layoutAssignments redundancy/check classification) an
    extra VERDICT column is added, unless ``show_verdict`` is explicitly False
    (e.g. when the section header already states the verdict)."""
    if show_verdict is None:
        show_verdict = any('verdict' in o for o in ops)
    header = ['SECTION', 'ITEM', 'MANUAL OPERATION']
    if show_verdict:
        header.append('VERDICT')
    header.append('WHERE')

    def _row(o):
        r = [o['section'], o['item'], o['operation']]
        if show_verdict:
            r.append(o.get('verdict', '-'))
        r.append(o['where'])
        return r

    rows = [_row(o) for o in ops]
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
              for i, h in enumerate(header)]

    def line(values):
        return ' | '.join(str(v).ljust(widths[i]) for i, v in enumerate(values))

    out = [line(header), '-+-'.join('-' * w for w in widths)]
    out.extend(line(r) for r in rows)
    return '\n'.join(out)


def write_manual_ops(path, ops, profile_name):
    """Write the manual-operations file, split into REDUNDANT (ignorable) and
    NEEDS CHECK (real action) groups. Returns its path."""
    header = [
        'MANUAL OPERATIONS REQUIRED FOR A COMPLETE ALIGNMENT',
        '==================================================',
        '',
        'Profile: %s' % profile_name,
        '',
        'These sections have no expressible "off" value in the Metadata API, so',
        'they cannot be turned off or removed by a Profile deploy (omitting an',
        'entry is a silent no-op). Perform the actions below in the destination',
        'org to finish the alignment.',
        '',
    ]
    if not ops:
        header.append('(none - every section was handled automatically)')
    else:
        redundant = [o for o in ops if (o.get('verdict') or '').startswith('REDUNDANT')]
        check = [o for o in ops if (o.get('verdict') or '') == 'NEEDS CHECK']
        other = [o for o in ops if (o.get('verdict') or '') not in
                 ('REDUNDANT (ignorable)', 'NEEDS CHECK')]

        def _section(title, note, items):
            if not items:
                return
            header.append('=' * 60)
            header.append(title + ' (%d)' % len(items))
            header.append('=' * 60)
            if note:
                header.append(note)
                header.append('')
            header.append(format_ops_table(items, show_verdict=False))
            header.append('')

        _section(
            'REDUNDANT (ignorable)',
            'These explicit assignments point at the object\'s ONLY layout '
            '(= its default), which the source inherits implicitly. Removing or '
            'ignoring them changes nothing functional.',
            redundant)
        _section(
            'NEEDS CHECK',
            'These need a real decision: verify each assignment against the '
            'object\'s default layout and change it if it differs.',
            check)
        _section(
            'OTHER MANUAL OPERATIONS',
            'Non-alignable sections with no redundancy classification.',
            other)
    header.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(header).rstrip() + '\n')
    return path


def write_what_is_changed(path, changes, dest_env):
    """Write the what-is-changed report: every difference the alignment deploy
    will apply, grouped into GRANTED (turned ON), REVOKED (turned OFF) and
    MANUAL (non-alignable) sections. Each changed field is annotated inline
    with its old destination value. Returns the path."""
    granted = [c for c in changes if c['kind'] == 'grant']
    revoked = [c for c in changes if c['kind'] == 'revoke']
    removed = [c for c in changes if c['kind'] == 'removed']

    lines = [
        'WHAT IS CHANGED - alignment (destination: %s)' % dest_env,
        '=' * 78,
        '',
        'Every entry below is a difference between the destination org and the',
        'aligned (merged) profile. Each changed field is followed by an inline',
        'XML comment recording its OLD value in the destination:',
        '',
        '    <!-- old value from %s: <old value> -->' % dest_env,
        '    <!-- old value from %s: not present -->   (entry/field did not exist)' % dest_env,
        '',
        'Sections',
        '  GRANTED : the destination was OFF (or missing) and is turned ON.',
        '  REVOKED : the destination was ON and is turned OFF.',
        '  MANUAL  : cannot be changed by a Profile deploy - do it in Setup.',
        '',
        'Layout assignments in MANUAL are split into:',
        '  REDUNDANT  : the object has ONE layout, so the explicit assignment IS',
        '               the default - redundant, you can ignore it.',
        '  NEEDS CHECK: the object has several layouts - verify the assignment',
        '               matches the default and correct it if it does not.',
        '',
        'GRANTED: %d' % len(granted),
        'REVOKED: %d' % len(revoked),
        'MANUAL: %d' % len(removed),
        '',
    ]

    def _emit_change_group(group, title):
        if not group:
            return
        lines.append('=' * 78)
        lines.append(title)
        lines.append('=' * 78)
        for c in group:
            lines.extend(_render_alignable_change(c, dest_env))
            lines.append('')

    _emit_change_group(granted, 'GRANTED (turned ON in the destination)')
    _emit_change_group(revoked, 'REVOKED (turned OFF in the destination)')

    if removed:
        lines.append('=' * 78)
        lines.append('MANUAL (cannot be changed by a Profile deploy)')
        lines.append('=' * 78)
        layouts = [c for c in removed if c.get('section') == 'layoutAssignments']
        others = [c for c in removed if c.get('section') != 'layoutAssignments']
        has_verdict = any('verdict' in c for c in layouts)

        if has_verdict:
            redundant = [c for c in layouts if c.get('verdict') == 'redundant']
            check = [c for c in layouts if c.get('verdict') != 'redundant']
            if redundant:
                lines.append('')
                lines.append('REDUNDANT (ignorable): the object has a single layout, so this')
                lines.append('explicit assignment is the default; the source inherits the same')
                lines.append('default. Nothing to do.')
                lines.extend(_removed_block(
                    'layoutAssignments', redundant, dest_env,
                    verdict_note='explicit assignment = the object\'s only layout '
                                 '(its default) - redundant, ignorable'))
                lines.append('')
            if check:
                lines.append('')
                lines.append('NEEDS CHECK: the object has several layouts - verify this explicit')
                lines.append('assignment matches the default and correct it if it does not.')
                lines.extend(_removed_block(
                    'layoutAssignments', check, dest_env,
                    verdict_note='object has several layouts - verify it matches the default'))
                lines.append('')
        else:
            grouped = {}
            for c in removed:
                grouped.setdefault(c['section'], []).append(c)
            for section, entries in grouped.items():
                lines.extend(_removed_block(section, entries, dest_env))
                lines.append('')

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines).rstrip() + '\n')
    return path
