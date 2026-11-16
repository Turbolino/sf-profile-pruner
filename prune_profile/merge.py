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


def _txt(el, child):
    ce = el.find(M + child)
    return (ce.text or '').strip() if ce is not None else ''


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


def _describe_entry(section, el):
    """Human description of a non-alignable entry, for the manual-ops report."""
    if section == 'layoutAssignments':
        layout = _txt(el, 'layout')
        rt = _txt(el, 'recordType')
        item = layout if not rt else '%s (record type: %s)' % (layout, rt)
        obj = layout.split('-', 1)[0] if '-' in layout else layout
        return {
            'section': section,
            'item': item,
            'operation': 'Remove the page layout assignment',
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


def _changed_fields(el, off_fields):
    """Map field -> old value for the off-fields that will actually change
    (old value present and different from the off value)."""
    changed = {}
    for child, off_val in off_fields.items():
        old = _txt(el, child)
        if old and old != off_val:
            changed[child] = old
    return changed


def _element_xml(el):
    """Serialize a single element with 4-space indentation, no declaration and
    no trailing tail (so a comment can be appended on the same line)."""
    el = deepcopy(el)
    el.tail = None  # drop the whitespace tail inherited from the parent tree
    etree.indent(el, space='    ')
    return etree.tostring(el, encoding='unicode').rstrip('\n')


def _change_comment_line(dest_env, old_value):
    """Single inline comment recording one field's old value."""
    return ' <!-- old value from %s: %s -->' % (dest_env, old_value)


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


def _turned_off_lines(change, dest_env):
    """XML block for one turned-off entry, with the old-value comment placed
    inline right after each field that actually changed."""
    section = change['section']
    key_child, off_fields = MERGE_TABLE[section]
    old_values = change['old_values']
    out = ['<%s>' % section]
    out.append('    <%s>%s</%s>' % (key_child, change['key'], key_child))
    for field, off_val in off_fields.items():
        line = '    <%s>%s</%s>' % (field, off_val, field)
        if field in old_values:
            line += _change_comment_line(dest_env, old_values[field])
        out.append(line)
    out.append('</%s>' % section)
    return out


def _removed_block(section, entries, dest_env):
    """One grouped block per non-alignable section: a single <section> wrapper
    listing every removed key, plus a comment enumerating them."""
    keys = _keys_for(section)
    labels = [_entry_label(section, e['element']) for e in entries]
    labels = [l for l in labels if l]
    comment = '%s removed in destination org because not present in source org' % ', '.join(labels)

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


def merge_profiles(source_path, dest_path, dest_env='destination org'):
    """Merge a pruned source profile with a pruned destination profile.

    Source is authoritative: every source entry is kept verbatim; every
    destination-only entry is turned off (alignable sections) or recorded as a
    manual operation (non-alignable sections). Destination-only entries that
    are already off are skipped entirely (no change to report).

    Returns ``(merged_xml_bytes, manual_ops, turned_off_count, changes)`` where
    ``changes`` is a list of dicts describing every turned-off / removed entry
    (kind, section, key, element, old_values) for the what-is-changed report.
    """
    src_root = etree.parse(source_path).getroot()
    dst_root = etree.parse(dest_path).getroot()
    src_groups = _groups(src_root)
    dst_groups = _groups(dst_root)

    out = etree.Element(M + 'Profile', nsmap={None: NS})
    manual_ops = []
    changes = []
    turned_off = 0

    sections = list(src_groups)
    for s in dst_groups:
        if s not in sections:
            sections.append(s)

    for section in sections:
        src_els = src_groups.get(section, [])
        dst_els = dst_groups.get(section, [])

        if section in MERGE_TABLE:
            key_child, off_fields = MERGE_TABLE[section]
            src_keys = {_entry_key(el, [key_child]) for el in src_els}
            for el in src_els:
                out.append(deepcopy(el))
            for el in dst_els:
                if _entry_key(el, [key_child]) not in src_keys:
                    old_values = _changed_fields(el, off_fields)
                    if not old_values:
                        # Already off in the destination: nothing to change.
                        # Neither the merged profile nor the report needs it.
                        continue
                    off_el = _make_off_element(section, key_child, off_fields, el)
                    out.append(off_el)
                    turned_off += 1
                    changes.append({
                        'kind': 'turned_off',
                        'section': section,
                        'key': _txt(el, key_child),
                        'element': off_el,
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
                    changes.append({
                        'kind': 'removed',
                        'section': section,
                        'key': _txt(el, keys[0]) if keys else '',
                        'element': deepcopy(el),
                        'old_values': {},
                    })
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
    return xml, manual_ops, turned_off, changes


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


def format_ops_table(ops):
    """Render manual operations as an aligned plain-text table."""
    header = ('SECTION', 'ITEM', 'MANUAL OPERATION', 'WHERE')
    rows = [(o['section'], o['item'], o['operation'], o['where']) for o in ops]
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0))
              for i, h in enumerate(header)]

    def line(values):
        return ' | '.join(str(v).ljust(widths[i]) for i, v in enumerate(values))

    out = [line(header), '-+-'.join('-' * w for w in widths)]
    out.extend(line(r) for r in rows)
    return '\n'.join(out)


def write_manual_ops(path, ops, profile_name):
    """Write the manual-operations file. Returns its path."""
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
        header.append(format_ops_table(ops))
    header.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(header) + '\n')
    return path


def write_what_is_changed(path, changes, dest_env):
    """Write the what-is-changed report: only the changed (turned-off) and
    removed rows, each followed by an XML comment recording the old value in
    the destination org (or that the entry was removed because it is not
    present in the source org). Returns the path."""
    turned = [c for c in changes if c['kind'] == 'turned_off']
    removed = [c for c in changes if c['kind'] == 'removed']

    lines = [
        'WHAT IS CHANGED - alignment (destination: %s)' % dest_env,
        '=' * 60,
        '',
        'Only the entries of the merged profile that differ from the destination',
        'org are listed. Each turned-off field carries an inline XML comment',
        'with its old value in the destination; removed entries are grouped with',
        'a comment listing what was removed because it is not in the source org.',
        '',
        'TURNED OFF: %d' % len(turned),
        'REMOVED (not present in source org): %d' % len(removed),
        '',
    ]

    def _emit_turned():
        if not turned:
            return
        lines.append('=' * 60)
        lines.append('TURNED OFF')
        lines.append('=' * 60)
        for c in turned:
            lines.extend(_turned_off_lines(c, dest_env))
            lines.append('')

    def _emit_removed():
        if not removed:
            return
        lines.append('=' * 60)
        lines.append('REMOVED (not present in source org)')
        lines.append('=' * 60)
        grouped = {}
        for c in removed:
            grouped.setdefault(c['section'], []).append(c)
        for section, entries in grouped.items():
            lines.extend(_removed_block(section, entries, dest_env))
            lines.append('')

    _emit_turned()
    _emit_removed()

    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines).rstrip() + '\n')
    return path
