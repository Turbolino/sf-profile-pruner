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


def merge_profiles(source_path, dest_path):
    """Merge a pruned source profile with a pruned destination profile.

    Source is authoritative: every source entry is kept verbatim; every
    destination-only entry is turned off (alignable sections) or recorded as a
    manual operation (non-alignable sections).

    Returns ``(merged_xml_bytes, manual_ops, turned_off_count)``.
    """
    src_root = etree.parse(source_path).getroot()
    dst_root = etree.parse(dest_path).getroot()
    src_groups = _groups(src_root)
    dst_groups = _groups(dst_root)

    out = etree.Element(M + 'Profile', nsmap={None: NS})
    manual_ops = []
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
                    out.append(_make_off_element(section, key_child, off_fields, el))
                    turned_off += 1
        elif section in NON_ALIGNABLE:
            keys = _keys_for(section)
            src_keys = {_entry_key(el, keys) for el in src_els}
            for el in src_els:
                out.append(deepcopy(el))
            for el in dst_els:
                if _entry_key(el, keys) not in src_keys:
                    manual_ops.append(_describe_entry(section, el))
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

    xml = etree.tostring(out, xml_declaration=True, encoding='UTF-8',
                         pretty_print=True)
    return xml, manual_ops, turned_off


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
