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

from collections import Counter, OrderedDict
from copy import deepcopy

import json
import os

from lxml import etree

from .constants import M, NS, __version__, meta_root, tab_section

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
    'tabSettings': ('tab', {'visibility': 'None'}),
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

# Sections that exist on a Profile but have NO PermissionSet counterpart. In a
# mixed (Profile <-> PermissionSet) run they are not carried across the type
# boundary: a Profile source cannot move its layoutAssignments / loginFlows /
# loginIpRanges / loginHours / userLicense into a PermissionSet, and a
# PermissionSet source has nothing to align against the destination Profile's
# own values there.
PROFILE_EXCLUSIVE_SECTIONS = (
    'layoutAssignments', 'loginFlows', 'loginIpRanges', 'loginHours',
    'userLicense', 'custom',
)

# Field types whose `editable` flag is meaningless: Salesforce always treats
# these as read-only, so an `editable=true` in the source profile is dead
# metadata (the effective value is false). The alignment normalizes them to
# `false` instead of reporting a fake "grant" that the platform will ignore.
#
# Detected from FieldDefinition.DataType, which is user-independent and
# reliable (unlike the SObject describe `updateable` flag, which reflects the
# running user's field-level security and produces false positives on ordinary
# editable fields).
READ_ONLY_FIELD_TYPE_PREFIXES = ('Formula',)
READ_ONLY_FIELD_TYPE_EXACT = frozenset(
    {'Roll-Up Summary', 'Auto Number', 'Address', 'Location'})


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
    """Return ``{fieldFullName: editable_text}`` for the ``fieldPermissions``
    entries in `path` (editable text is ``'true'``/``'false'``, or ``''`` when
    absent)."""
    root = etree.parse(path).getroot()
    out = {}
    for el in root:
        if isinstance(el.tag, str) and el.tag.split('}')[-1] == 'fieldPermissions':
            out[_txt(el, 'field')] = _txt(el, 'editable')
    return out


def normalize_profile_read_only(path, read_only_fields):
    """Force ``editable=false`` on the ``fieldPermissions`` entries of `path`
    whose field is in ``read_only_fields``, in place. Returns the number of
    entries normalized (0 when nothing changed).

    Used by the plain prune (no merge) so the deliverable profile reflects the
    effective permission instead of a dead ``editable=true`` on system read-only
    fields (formula / roll-up summary / auto number / compound address &
    location / system read-only).
    """
    if not read_only_fields:
        return 0
    tree = etree.parse(path)
    root = tree.getroot()
    count = 0
    for el in root:
        if isinstance(el.tag, str) and el.tag.split('}')[-1] == 'fieldPermissions':
            field = _txt(el, 'field')
            if field in read_only_fields and _txt(el, 'editable') != 'false':
                _set_child_text(el, 'editable', 'false')
                count += 1
    if count:
        etree.indent(root, space='    ')
        tree.write(path, encoding='utf-8', xml_declaration=True)
    return count


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


def _layout_assignment_key(el):
    """Identity of a ``layoutAssignments`` entry: ``(object, recordType)``.

    A profile holds at most one layout per object+recordType, so two layouts
    for the same slot (e.g. ``Account-LayoutA`` vs ``Account-LayoutB``) are the
    SAME assignment with a different value — a CHANGE the deploy applies, not
    two independent entries. Keying by the full layout name would misclassify
    that change as a destination-only removal (a manual op the deploy actually
    resolves on its own).
    """
    return (_layout_object(el), _txt(el, 'recordType'))


# ---------------------------------------------------------------------------
# Metadata type + tab-section helpers (Profile vs PermissionSet).
#
# A Profile names its tab section ``tabVisibilities`` with visibility values
# DefaultOn / DefaultOff / Hidden; a PermissionSet names it ``tabSettings`` with
# Visible / Available / None. The two schemas are otherwise identical for every
# permission-bearing section, so the merge/diff only special-case the tab part.
# ---------------------------------------------------------------------------

def _mdtype(root):
    """Metadata type ('Profile' | 'PermissionSet') declared by a parsed root."""
    name = root.tag.split('}')[-1]
    return name if name in ('Profile', 'PermissionSet') else 'Profile'


def metadata_license(path):
    """Return the ``license`` scalar of a PermissionSet file (``''`` when absent).

    A Profile has no ``license`` element, so this also returns ``''`` for a
    Profile path. Used to guard a PS -> PS alignment from silently overwriting
    the destination's license with a different one.
    """
    root = etree.parse(path).getroot()
    return _txt(root, 'license')


def license_mismatch(src_license, dst_license):
    """True when a source and destination PermissionSet declare DIFFERENT,
    non-empty licenses (case-insensitive). Empty on either side is not a
    mismatch: an absent source license keeps the destination's own, and a
    missing destination has no license to overwrite.
    """
    return bool(src_license and dst_license
                and src_license.strip().lower() != dst_license.strip().lower())


# Tab visibility has THREE semantic states, each with its own name per type:
#
#   level 2  "on / visible"          Profile DefaultOn  <->  PS Visible
#   level 1  "off but available"     Profile DefaultOff <->  PS Available
#   level 0  "unavailable / hidden"  Profile Hidden     <->  PS None
#
# The 1:1 level mapping is what keeps a Profile->PS->Profile round-trip
# coherent: DefaultOff is "off but available", not "off", so it must map to
# Available (never to None). Collapsing to a binary on/off would silently
# degrade DefaultOff/Available into Hidden/None and destroy the "available"
# state when the metadata is carried into the other type.
_TAB_LEVELS = {
    'Profile': {'DefaultOn': 2, 'DefaultOff': 1, 'Hidden': 0},
    'PermissionSet': {'Visible': 2, 'Available': 1, 'None': 0},
}
_TAB_VALUE = {
    'Profile': {2: 'DefaultOn', 1: 'DefaultOff', 0: 'Hidden'},
    'PermissionSet': {2: 'Visible', 1: 'Available', 0: 'None'},
}


def _tab_level(value, mdtype):
    """Semantic level (0/1/2) of a tab visibility value. Unknown values fall
    back to level 0 (treated as off) so a malformed file never propagates a
    wrong vocabulary value."""
    return _TAB_LEVELS.get(mdtype, {}).get(value or '', 0)


def _tab_value(level, mdtype):
    """The native visibility value for `mdtype` at `level` (0/1/2)."""
    return _TAB_VALUE.get(mdtype, _TAB_VALUE['Profile'])[level]


def _convert_tab_visibility(value, from_mdtype, to_mdtype):
    """Map a tab visibility value between Profile and PermissionSet vocabularies
    by semantic level, so the destination always receives a native, coherent
    value (DefaultOn<->Visible, DefaultOff<->Available, Hidden<->None)."""
    if from_mdtype == to_mdtype:
        return value
    return _tab_value(_tab_level(value, from_mdtype), to_mdtype)


def _convert_tab_element(el, from_mdtype, to_mdtype):
    """Re-materialise a tab entry into the destination schema (section name +
    visibility vocabulary)."""
    ne = etree.Element(M + tab_section(to_mdtype))
    etree.SubElement(ne, M + 'tab').text = _txt(el, 'tab')
    etree.SubElement(ne, M + 'visibility').text = _convert_tab_visibility(
        _txt(el, 'visibility'), from_mdtype, to_mdtype)
    return ne


def _make_tab_off_element(tab, mdtype):
    """Build an explicit OFF tab entry in the destination schema (level 0:
    Hidden for Profile, None for PermissionSet)."""
    ne = etree.Element(M + tab_section(mdtype))
    etree.SubElement(ne, M + 'tab').text = tab
    etree.SubElement(ne, M + 'visibility').text = _tab_value(0, mdtype)
    return ne


def _merge_tabs(out, src_groups, dst_groups, src_mdtype, dst_mdtype, changes):
    """Merge the tab section (tabVisibilities / tabSettings) into `out`.

    Source is authoritative and re-materialised in the destination schema; a
    destination-only tab that is not already off (level 0) is turned OFF. A
    source-only tab at level 0 is skipped (absence already means "off"), while
    a source-only tab at level 1 or 2 is carried over (a real grant / available
    state to replicate). Returns nothing (appends to `out` and `changes`)."""
    src_els = src_groups.get(tab_section(src_mdtype), [])
    dst_els = dst_groups.get(tab_section(dst_mdtype), [])
    src_by_tab = {_txt(el, 'tab'): el for el in src_els}
    dst_by_tab = {_txt(el, 'tab'): el for el in dst_els}

    merged = []
    for tab, s_el in src_by_tab.items():
        if tab not in dst_by_tab and _tab_level(_txt(s_el, 'visibility'), src_mdtype) == 0:
            continue  # source-only + off: already aligned with absence
        merged.append(_convert_tab_element(s_el, src_mdtype, dst_mdtype))
    for tab, d_el in dst_by_tab.items():
        if tab in src_by_tab:
            continue
        if _tab_level(_txt(d_el, 'visibility'), dst_mdtype) == 0:
            continue  # already off in the destination: no change to report
        merged.append(_make_tab_off_element(tab, dst_mdtype))
    for m_el in merged:
        out.append(m_el)

    # Level-based diff: a change is recorded only when the semantic level moves.
    merged_by_tab = {_txt(e, 'tab'): e for e in merged}
    for tab in sorted(set(merged_by_tab) | set(dst_by_tab)):
        m_el = merged_by_tab.get(tab)
        d_el = dst_by_tab.get(tab)
        new = _txt(m_el, 'visibility') if m_el is not None else ''
        old = _txt(d_el, 'visibility') if d_el is not None else None
        new_level = _tab_level(new, dst_mdtype)
        old_level = _tab_level(old or '', dst_mdtype)
        if new_level == old_level:
            continue
        kind = 'grant' if new_level > old_level else 'revoke'
        changes.append({
            'kind': kind,
            'section': tab_section(dst_mdtype),
            'key': tab,
            'element': deepcopy(m_el) if m_el is not None else deepcopy(d_el),
            'old_values': {'visibility': old},
        })


def _normalize_tab_section(groups, mdtype):
    """Rewrite a tab section into a canonical ``tabVisibilities`` section whose
    ``visibility`` values are a level-preserving 'on'/'available'/'off' (for
    cross-type comparison only). The three-state model keeps
    DefaultOff<->Available distinct from Hidden<->None, so a real semantic
    difference is never flattened into "both off"."""
    entries = groups.pop(tab_section(mdtype), [])
    norm = []
    for el in entries:
        ne = etree.Element(M + 'tabVisibilities')
        etree.SubElement(ne, M + 'tab').text = _txt(el, 'tab')
        level = _tab_level(_txt(el, 'visibility'), mdtype)
        etree.SubElement(ne, M + 'visibility').text = {2: 'on', 1: 'available', 0: 'off'}[level]
        norm.append(ne)
    if norm:
        groups['tabVisibilities'] = norm


# ---------------------------------------------------------------------------
# Profile diff (pure profile-to-profile comparison; no prune, no deploy).
# ---------------------------------------------------------------------------

# Sentinel returned by _diff_key_spec for layoutAssignments: their structural
# key is (object, recordType), derived from the layout full name, not a direct
# child element.
_LAYOUT_ASSIGNMENT_KEY = object()


def _diff_key_spec(section):
    """Child elements that identify an entry of `section` for diffing. ``None``
    means "no structural key": fall back to the element's serialized form.
    ``_LAYOUT_ASSIGNMENT_KEY`` means the (object, recordType) key."""
    if section in MERGE_TABLE:
        return [MERGE_TABLE[section][0]]
    if section == 'layoutAssignments':
        return _LAYOUT_ASSIGNMENT_KEY
    if section in NON_ALIGNABLE:
        keys = _keys_for(section)
        return keys or None
    return None


def _diff_key(el, spec):
    if spec is _LAYOUT_ASSIGNMENT_KEY:
        return _layout_assignment_key(el)
    if spec:
        return tuple(_txt(el, k) for k in spec)
    return etree.tostring(el, encoding='unicode')


def _element_leaf_fields(el):
    """Ordered flat map of child local-name -> text for a profile entry."""
    out = OrderedDict()
    for child in el:
        if isinstance(child.tag, str):
            out[child.tag.split('}')[-1]] = (child.text or '').strip()
    return out


def _diff_entry_fields(src_el, dst_el):
    """List of ``(field, source_value, dest_value)`` for fields that differ
    between two entries of the same section."""
    sf = _element_leaf_fields(src_el)
    df = _element_leaf_fields(dst_el)
    diffs = []
    for field, sv in sf.items():
        if sv != df.get(field):
            diffs.append((field, sv, df.get(field, '')))
    for field in df:
        if field not in sf:
            diffs.append((field, '', df[field]))
    return diffs


def diff_profiles(source_path, dest_path):
    """Compare two profiles section by section.

    Returns ``{section: {...}}`` where each section maps to
    ``{source_count, dest_count, only_source, only_dest, common, different,
    key_spec}``. ``only_source`` / ``only_dest`` are lists of
    ``{'key': ..., 'values': {...}}`` for entries present in exactly one
    profile; ``common`` is the same shape for entries present in both profiles
    with IDENTICAL values; ``different`` is a list of
    ``{'key': ..., 'source': {...}, 'dest': {...},
    'diffs': [(field, src, dst), ...]}`` for entries present in both profiles
    whose values differ. Pure comparison: no org access, no mutation, no
    deploy.
    """
    src_root = etree.parse(source_path).getroot()
    dst_root = etree.parse(dest_path).getroot()
    src_groups = _groups(src_root)
    dst_groups = _groups(dst_root)

    # Cross-type (Profile <-> PermissionSet): the tab section is named
    # differently on each side, so normalise both to a common 'tabVisibilities'
    # section with on/off visibility before the generic comparison below.
    src_mdtype = _mdtype(src_root)
    dst_mdtype = _mdtype(dst_root)
    if src_mdtype != dst_mdtype:
        _normalize_tab_section(src_groups, src_mdtype)
        _normalize_tab_section(dst_groups, dst_mdtype)

    sections = []
    for s in list(src_groups):
        if s not in sections:
            sections.append(s)
    for s in dst_groups:
        if s not in sections:
            sections.append(s)

    out = {}
    for s in sections:
        spec = _diff_key_spec(s)
        src_els = src_groups.get(s, [])
        dst_els = dst_groups.get(s, [])
        src_by_key = {}
        for el in src_els:
            src_by_key.setdefault(_diff_key(el, spec), el)
        dst_by_key = {}
        for el in dst_els:
            dst_by_key.setdefault(_diff_key(el, spec), el)
        only_source = []
        only_dest = []
        common = []
        different = []
        for k in src_by_key:
            if k not in dst_by_key:
                only_source.append({'key': k, 'values': _element_leaf_fields(src_by_key[k])})
        for k in dst_by_key:
            if k not in src_by_key:
                only_dest.append({'key': k, 'values': _element_leaf_fields(dst_by_key[k])})
        for k in set(src_by_key) & set(dst_by_key):
            diffs = _diff_entry_fields(src_by_key[k], dst_by_key[k])
            if diffs:
                different.append({'key': k,
                                  'source': _element_leaf_fields(src_by_key[k]),
                                  'dest': _element_leaf_fields(dst_by_key[k]),
                                  'diffs': diffs})
            else:
                common.append({'key': k, 'values': _element_leaf_fields(src_by_key[k])})
        # Deterministic order (set iteration order is unstable across runs).
        _rk = _render_diff_key
        only_source.sort(key=lambda x: _rk(x['key']))
        only_dest.sort(key=lambda x: _rk(x['key']))
        common.sort(key=lambda x: _rk(x['key']))
        different.sort(key=lambda x: _rk(x['key']))
        out[s] = {
            'source_count': len(src_els),
            'dest_count': len(dst_els),
            'only_source': only_source,
            'only_dest': only_dest,
            'common': common,
            'different': different,
            'key_spec': spec,
        }
    return out


def _render_diff_key(key):
    """Render a diff key tuple as a readable string."""
    if not isinstance(key, tuple):
        return str(key)
    if len(key) == 1:
        return str(key[0])
    return ' / '.join(str(x) for x in key if x)


def _diff_summary_table(sections):
    """Render only the per-section summary counts as a multi-line string."""
    lines = []
    lines.append('SUMMARY')
    lines.append('=' * 66)
    lines.append('%-30s %8s %8s %10s %10s %8s %8s' % (
        'section', 'source', 'dest', 'only_src', 'only_dst', 'common', 'diff'))
    lines.append('-' * 82)
    for s, d in sections.items():
        lines.append('%-30s %8d %8d %10d %10d %8d %8d' % (
            s, d['source_count'], d['dest_count'],
            len(d['only_source']), len(d['only_dest']),
            len(d['common']), len(d['different'])))
    return '\n'.join(lines)


def render_diff_summary(sections, src_label, dst_label, tool_version=None):
    """Render only the header + summary counts (no per-entry details)."""
    tv = tool_version or __version__
    return '\n'.join([
        'PROFILE DIFF - source -> destination',
        '=' * 66,
        'source      : %s' % src_label,
        'destination : %s' % dst_label,
        'tool        : sf-profile-pruner v%s' % tv,
        '',
        _diff_summary_table(sections),
    ])


def render_diff_report(sections, src_label, dst_label, tool_version=None):
    """Render the full diff report as a string."""
    tv = tool_version or __version__
    lines = []
    lines.append('PROFILE DIFF - source -> destination')
    lines.append('=' * 66)
    lines.append('source      : %s' % src_label)
    lines.append('destination : %s' % dst_label)
    lines.append('tool        : sf-profile-pruner v%s' % tv)
    lines.append('')
    lines.append(_diff_summary_table(sections))
    lines.append('')
    lines.append('DETAILS')
    lines.append('=' * 66)
    for s, d in sections.items():
        lines.append('')
        lines.append('[%s]  source=%d dest=%d only_src=%d only_dst=%d common=%d diff=%d'
                     % (s, d['source_count'], d['dest_count'],
                        len(d['only_source']), len(d['only_dest']),
                        len(d['common']), len(d['different'])))
        if d['only_source']:
            lines.append('  only in source (%d):' % len(d['only_source']))
            for item in sorted(d['only_source'], key=lambda x: _render_diff_key(x['key'])):
                lines.append('    - %s' % _render_diff_key(item['key']))
        if d['only_dest']:
            lines.append('  only in destination (%d):' % len(d['only_dest']))
            for item in sorted(d['only_dest'], key=lambda x: _render_diff_key(x['key'])):
                lines.append('    - %s' % _render_diff_key(item['key']))
        if d['common']:
            lines.append('  identical in both (%d): see diff-data.json / diff-data.xml'
                        % len(d['common']))
        if d['different']:
            lines.append('  different (%d):' % len(d['different']))
            for item in sorted(d['different'], key=lambda x: _render_diff_key(x['key'])):
                lines.append('    %s:' % _render_diff_key(item['key']))
                for field, sv, dv in item['diffs']:
                    lines.append('      %s: source=%r dest=%r' % (field, sv, dv))
    lines.append('')
    return '\n'.join(lines)


def write_diff_report(sections, src_label, dst_label, diff_dir,
                      tool_version=None):
    """Write the diff report into ``diff_dir`` and return its path."""
    text = render_diff_report(sections, src_label, dst_label, tool_version)
    os.makedirs(diff_dir, exist_ok=True)
    path = os.path.join(diff_dir, 'diff-report.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text + '\n')
    return path


def _diff_data_structure(sections, src_label, dst_label, tool_version=None):
    """Build the full machine-readable diff structure (identical entries
    included with their values)."""
    tv = tool_version or __version__
    out = {
        'tool': 'sf-profile-pruner',
        'tool_version': tv,
        'source': src_label,
        'destination': dst_label,
        'sections': {},
    }
    for s, d in sections.items():
        def _entries(items):
            return [{'key': _render_diff_key(it['key']), 'values': it['values']}
                    for it in items]
        def _diff_entries(items):
            res = []
            for it in items:
                res.append({
                    'key': _render_diff_key(it['key']),
                    'source': it['source'],
                    'dest': it['dest'],
                    'diffs': {f: {'source': sv, 'dest': dv}
                              for f, sv, dv in it['diffs']},
                })
            return res
        out['sections'][s] = {
            'source_count': d['source_count'],
            'dest_count': d['dest_count'],
            'only_source_count': len(d['only_source']),
            'only_dest_count': len(d['only_dest']),
            'common_count': len(d['common']),
            'different_count': len(d['different']),
            'only_source': _entries(d['only_source']),
            'only_dest': _entries(d['only_dest']),
            'common': _entries(d['common']),
            'different': _diff_entries(d['different']),
        }
    return out


def write_diff_json(sections, src_label, dst_label, diff_dir,
                    tool_version=None):
    """Write the diff as a machine-readable JSON file and return its path."""
    data = _diff_data_structure(sections, src_label, dst_label, tool_version)
    os.makedirs(diff_dir, exist_ok=True)
    path = os.path.join(diff_dir, 'diff-data.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write('\n')
    return path


def write_diff_xml(sections, src_label, dst_label, diff_dir,
                   tool_version=None):
    """Write the diff as an XML file and return its path."""
    data = _diff_data_structure(sections, src_label, dst_label, tool_version)
    os.makedirs(diff_dir, exist_ok=True)

    root = etree.Element('profile-diff')
    etree.SubElement(root, 'tool').text = data['tool']
    etree.SubElement(root, 'tool_version').text = data['tool_version']
    etree.SubElement(root, 'source').text = data['source']
    etree.SubElement(root, 'destination').text = data['destination']

    def _values(parent, tag, values):
        node = etree.SubElement(parent, tag)
        for name, val in values.items():
            f = etree.SubElement(node, 'field', name=name)
            f.text = val
        return node

    def _entries(parent, tag, items):
        node = etree.SubElement(parent, tag)
        for it in items:
            e = etree.SubElement(node, 'entry', key=it['key'])
            _values(e, 'values', it['values'])
        return node

    for s, d in data['sections'].items():
        sec = etree.SubElement(root, 'section', name=s)
        for attr in ('source_count', 'dest_count', 'only_source_count',
                     'only_dest_count', 'common_count', 'different_count'):
            sec.set(attr, str(d[attr]))
        _entries(sec, 'common', d['common'])
        _entries(sec, 'only_source', d['only_source'])
        _entries(sec, 'only_dest', d['only_dest'])
        node = etree.SubElement(sec, 'different')
        for it in d['different']:
            e = etree.SubElement(node, 'entry', key=it['key'])
            _values(e, 'source', it['source'])
            _values(e, 'dest', it['dest'])
            df = etree.SubElement(e, 'diffs')
            for f, vals in it['diffs'].items():
                fd = etree.SubElement(df, 'field', name=f)
                fd.set('source', vals['source'])
                fd.set('dest', vals['dest'])

    etree.indent(root, space='    ')
    path = os.path.join(diff_dir, 'diff-data.xml')
    with open(path, 'wb') as f:
        f.write(etree.tostring(root, encoding='utf-8', xml_declaration=True,
                               pretty_print=True))
    return path


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


def _render_layout_change(change, dest_env):
    """Render one layout CHANGE (same object+recordType slot, different layout):
    the source entry that will be deployed, annotated with the destination's old
    layout. Applied by the deploy — not a manual action."""
    el = change['element']
    old = change['old_values']
    out = ['<layoutAssignments>']
    out.append('    <layout>%s</layout>' % _txt(el, 'layout'))
    rt = _txt(el, 'recordType')
    if rt:
        out.append('    <recordType>%s</recordType>' % rt)
    old_layout = old.get('layout')
    old_note = 'not present' if old_layout is None else old_layout
    out.append('</layoutAssignments>  <!-- old layout from %s: %s -->'
               % (dest_env, old_note))
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
                   read_only_fields=None, keep_user_permissions=None,
                   keep_object_permissions=None, preserve_dest_label=False):
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

    ``keep_user_permissions`` (optional) is a set of ``userPermissions`` names
    that must be preserved verbatim from the destination instead of turned off:
    Salesforce rejects turning off some license-mandated userPermissions ("You
    may not turn off permission ..."). Used by the alignment self-heal to keep
    those from being re-turned-off after the heal removed the explicit OFF.

    ``keep_object_permissions`` (optional) is a set of object API names whose
    ``objectPermissions`` must be preserved verbatim from the destination
    instead of turned off: a preserved (license-bound) userPermission may depend
    on an objectPermission the merge would otherwise turn off ("Permission X
    depends on permission(s): ..."). Used by the self-heal to keep those ON.

    ``preserve_dest_label`` (optional) keeps the destination PermissionSet's
    ``label`` instead of the source's. Used when the destination is a NEW
    PermissionSet being created (its label is synthesised from the unique new
    name), so the source's label - which may already be in use - does not
    collide with the org's unique-label constraint.

    Returns ``(merged_xml_bytes, manual_ops, changes)`` where ``changes`` is the
    full list of differences the deploy will apply, each ``kind`` being
    ``grant`` (turned ON in the destination), ``revoke`` (turned OFF),
    ``layout_change`` (a layout assignment for the same object+recordType slot
    changed to the source value — applied by the deploy), or ``removed``
    (non-alignable, manual action).
    """
    src_root = etree.parse(source_path).getroot()
    dst_root = etree.parse(dest_path).getroot()
    src_groups = _groups(src_root)
    dst_groups = _groups(dst_root)

    read_only_fields = read_only_fields or set()
    keep_user_permissions = keep_user_permissions or set()
    keep_object_permissions = keep_object_permissions or set()

    # The merged file is deployed to the DESTINATION org, so it must use the
    # destination's metadata type (Profile or PermissionSet).
    src_mdtype = _mdtype(src_root)
    dst_mdtype = _mdtype(dst_root)
    out = etree.Element(M + meta_root(dst_mdtype), nsmap={None: NS})
    manual_ops = []
    changes = []

    sections = list(src_groups)
    for s in dst_groups:
        if s not in sections:
            sections.append(s)

    # The tab section is the one schema difference between Profile and
    # PermissionSet: handle it once (special-case), before the generic loop.
    tab_sections_present = [s for s in sections if s in ('tabVisibilities', 'tabSettings')]
    sections = [s for s in sections if s not in ('tabVisibilities', 'tabSettings')]
    if tab_sections_present:
        _merge_tabs(out, src_groups, dst_groups, src_mdtype, dst_mdtype, changes)

    # Profile-only sections have no PermissionSet counterpart: they must never
    # be injected into a PermissionSet output (an invalid PS). In a Profile ->
    # PermissionSet merge they are dropped (not carried across the boundary);
    # the reverse direction needs no filtering because a PermissionSet source
    # simply has none of these sections (the destination Profile keeps its own
    # values, reported as manual ops by the branches below).
    if dst_mdtype == 'PermissionSet':
        sections = [s for s in sections if s not in PROFILE_EXCLUSIVE_SECTIONS]

    # Cross-type PermissionSet -> Profile: the source PermissionSet carries no
    # Profile-only sections, so the destination Profile's own values there are
    # OUT OF SCOPE for the alignment (nothing to compare or turn off). Keep them
    # verbatim — present and unchanged — instead of reporting them as manual
    # removals. (Profile -> Profile keeps the normal align behavior.)
    if src_mdtype != dst_mdtype and dst_mdtype == 'Profile':
        for section in PROFILE_EXCLUSIVE_SECTIONS:
            for el in dst_groups.get(section, []):
                out.append(deepcopy(el))
        sections = [s for s in sections if s not in PROFILE_EXCLUSIVE_SECTIONS]
        # PermissionSet-only scalars (label / hasActivationRequired / license)
        # have no Profile counterpart and must not leak into the Profile output.
        sections = [s for s in sections if s not in _PS_SCALAR_SECTIONS]

    for section in sections:
        src_els = src_groups.get(section, [])
        dst_els = dst_groups.get(section, [])

        if section in MERGE_TABLE:
            key_child, off_fields = MERGE_TABLE[section]
            # Profile entries carry a `default` child that the PermissionSet
            # schema does not allow: drop it from both the copied entries and
            # the off-value map.
            if section in _PS_DROP_DEFAULT_SECTIONS and dst_mdtype == 'PermissionSet':
                off_fields = {k: v for k, v in off_fields.items() if k != 'default'}
            # userPermissions are license-bound on a Profile and a PermissionSet
            # has no license: in a PS -> Profile align, dest-only userPermissions
            # cannot be turned off, so they are preserved verbatim. Source-only /
            # shared entries still align normally.
            preserve_dest_only = (
                section == 'userPermissions'
                and src_mdtype == 'PermissionSet'
                and dst_mdtype == 'Profile')
            src_by_key = {_entry_key(el, [key_child]): el for el in src_els}
            dst_by_key = {_entry_key(el, [key_child]): el for el in dst_els}

            # Merged section: source entries verbatim (minus no-op entries) + an
            # explicit off element for each destination-only entry.
            merged_section = []
            for el in src_els:
                m_el = deepcopy(el)
                if section in _PS_DROP_DEFAULT_SECTIONS and dst_mdtype == 'PermissionSet':
                    d = m_el.find(M + 'default')
                    if d is not None:
                        m_el.remove(d)
                # PS -> Profile: a PermissionSet has no `default` concept, so the
                # destination's default is preserved (source is authoritative
                # for the other fields, e.g. `visible`).
                if (section in _PS_DROP_DEFAULT_SECTIONS
                        and src_mdtype == 'PermissionSet'
                        and dst_mdtype == 'Profile'):
                    key = _entry_key(m_el, [key_child])
                    d_el = dst_by_key.get(key)
                    dst_default = _txt(d_el, 'default') if d_el is not None else ''
                    _set_child_text(m_el, 'default', dst_default or 'false')
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
                # In PS -> Profile, userPermissions are license-bound and cannot
                # be turned off: preserve a dest-only userPermission verbatim.
                if preserve_dest_only:
                    merged_section.append(deepcopy(el))
                    continue
                # A PermissionSet source has no notion of the Profile's default
                # app / default record type; Salesforce forbids removing the only
                # default ("No default record type specified", "can't remove the
                # only default app"). Keep a destination entry marked default
                # verbatim instead of turning it off.
                if (section in _PS_DROP_DEFAULT_SECTIONS
                        and src_mdtype == 'PermissionSet'
                        and dst_mdtype == 'Profile'
                        and _txt(el, 'default') == 'true'):
                    m_el = deepcopy(el)
                    merged_section.append(m_el)
                    continue
                # License-mandated userPermissions cannot be turned off; keep
                # them verbatim from the destination instead of emitting an OFF.
                if (section == 'userPermissions' and key[0] in keep_user_permissions):
                    merged_section.append(deepcopy(el))
                    continue
                # A preserved userPermission may depend on this objectPermission
                # ("Permission X depends on permission(s): ... Object"): keep it
                # verbatim from the destination instead of turning it off.
                if (section == 'objectPermissions' and key[0] in keep_object_permissions):
                    merged_section.append(deepcopy(el))
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
                    # `editable` is meaningless for read-only fields (the org
                    # ignores it on both sides): never diff it, or the
                    # normalization (forced false) would surface a phantom
                    # revoke against a destination that has the dead true value.
                    if (section == 'fieldPermissions' and field == 'editable'
                            and key[0] in read_only_fields):
                        continue
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
        elif section == 'layoutAssignments':
            # Keyed by (object, recordType): a profile holds at most one layout
            # per slot, so a different layout for the same slot is a CHANGE the
            # deploy applies (source is authoritative), not a manual removal.
            src_by_slot = {_layout_assignment_key(el): el for el in src_els}
            dst_by_slot = {_layout_assignment_key(el): el for el in dst_els}
            for el in src_els:
                out.append(deepcopy(el))
            for slot, d_el in dst_by_slot.items():
                if slot in src_by_slot:
                    s_el = src_by_slot[slot]
                    if _txt(s_el, 'layout') != _txt(d_el, 'layout'):
                        # CHANGE: the merged profile already carries the source
                        # entry verbatim, so the deploy upserts the source layout
                        # over the destination one. Reported as a deploy-applied
                        # change, not a manual operation.
                        changes.append({
                            'kind': 'layout_change',
                            'section': section,
                            'key': _txt(s_el, 'layout'),
                            'object': slot[0],
                            'element': deepcopy(s_el),
                            'old_values': {
                                'layout': _txt(d_el, 'layout'),
                                'recordType': _txt(d_el, 'recordType'),
                            },
                        })
                else:
                    # REMOVE: destination-only slot. A Profile deploy cannot turn
                    # an assignment off, so this is a manual operation.
                    manual_ops.append(_describe_entry(section, d_el))
                    changes.append({
                        'kind': 'removed',
                        'section': section,
                        'key': _txt(d_el, 'layout'),
                        'object': slot[0],
                        'element': deepcopy(d_el),
                        'old_values': {},
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
                    changes.append(change)
        else:
            # Unknown or scalar section (custom, userLicense, ...): source is
            # authoritative; keep the destination value only when source has
            # none (identity scalars are not permissions to turn off).
            if (section == 'label' and preserve_dest_label
                    and dst_mdtype == 'PermissionSet' and dst_els):
                # A NEW PermissionSet keeps its own (unique, name-derived)
                # label: copying the source's label here would collide with
                # the org's unique-label constraint on creation.
                for el in dst_els:
                    out.append(deepcopy(el))
            elif src_els:
                for el in src_els:
                    out.append(deepcopy(el))
            else:
                for el in dst_els:
                    out.append(deepcopy(el))

    etree.indent(out, space='    ')
    xml = etree.tostring(out, xml_declaration=True, encoding='UTF-8')
    return xml, manual_ops, changes


# PermissionSet-only scalar sections (no Profile counterpart). They are never
# copied across the type boundary: a Profile has no label/license, a
# PermissionSet has no userLicense/custom. `description` IS shared, so it is
# NOT listed here.
_PS_SCALAR_SECTIONS = ('label', 'hasActivationRequired', 'license')

# Sections whose entries carry a `default` child in a Profile but NOT in a
# PermissionSet (the PS schema forbids it). When the output is a PermissionSet,
# the child is dropped and `default` is excluded from the off-value map.
_PS_DROP_DEFAULT_SECTIONS = ('applicationVisibilities', 'recordTypeVisibilities')


def convert_metadata_type(src_path, dst_path=None, label=None):
    """Convert a pruned source (Profile or PermissionSet) into the OTHER metadata
    type, source-authoritative and WITHOUT turning off or removing anything in
    the destination (this is the cross-type plain-prune "promote" step, not the
    alignment merge):

      * shared permission sections are copied verbatim (structurally identical);
      * the tab section is re-materialised in the destination vocabulary
        (tabVisibilities <-> tabSettings);
      * Profile-only sections are dropped when the destination is a PermissionSet;
      * when the destination is a Profile, its Profile-only sections are kept
        verbatim from ``dst_path`` (so they remain present and unchanged).

    Returns ``(xml_bytes, notes)`` where ``notes`` is a list of human-readable
    strings describing what was converted / kept / dropped.
    """
    src_root = etree.parse(src_path).getroot()
    src_mdtype = _mdtype(src_root)
    dst_mdtype = 'PermissionSet' if src_mdtype == 'Profile' else 'Profile'
    src_groups = _groups(src_root)

    # Destination Profile, when provided: used both to keep its Profile-only
    # sections and to preserve its `default` record type / default app, which a
    # PermissionSet source cannot express (a Profile without a default record
    # type fails the deploy with "No default record type specified").
    dst_groups = None
    if dst_mdtype == 'Profile' and dst_path and os.path.isfile(dst_path):
        dst_groups = _groups(etree.parse(dst_path).getroot())

    out = etree.Element(M + meta_root(dst_mdtype), nsmap={None: NS})
    notes = []

    # 1. tab section -> destination vocabulary (source entries only, no revoke).
    src_tab = src_groups.get(tab_section(src_mdtype), [])
    for el in src_tab:
        out.append(_convert_tab_element(el, src_mdtype, dst_mdtype))
    if src_tab:
        notes.append('tab section remapped %s -> %s (%d entr(y/ies))'
                     % (tab_section(src_mdtype), tab_section(dst_mdtype), len(src_tab)))

    # 2. shared sections verbatim.
    copied = 0
    for section, els in src_groups.items():
        if section == tab_section(src_mdtype):
            continue
        if section in PROFILE_EXCLUSIVE_SECTIONS:
            continue
        if section in _PS_SCALAR_SECTIONS:
            continue
        for el in els:
            m_el = deepcopy(el)
            if section in _PS_DROP_DEFAULT_SECTIONS:
                if dst_mdtype == 'PermissionSet':
                    # Profile's applicationVisibilities / recordTypeVisibilities
                    # carry a `default` child the PermissionSet schema forbids.
                    d = m_el.find(M + 'default')
                    if d is not None:
                        m_el.remove(d)
                else:
                    # PS -> Profile: the source has no `default`; preserve the
                    # destination's default (fallback 'false') so the resulting
                    # Profile keeps a valid default record type / app.
                    key_child = MERGE_TABLE[section][0]
                    key = _txt(m_el, key_child)
                    dst_default = 'false'
                    if dst_groups is not None:
                        for d_el in dst_groups.get(section, []):
                            if _txt(d_el, key_child) == key:
                                dst_default = _txt(d_el, 'default') or 'false'
                                break
                    _set_child_text(m_el, 'default', dst_default)
            out.append(m_el)
            copied += 1
    if copied:
        notes.append('%d shared-section entr(y/ies) copied verbatim' % copied)

    # 3. destination-specific structure.
    if dst_mdtype == 'PermissionSet':
        dropped = [s for s in PROFILE_EXCLUSIVE_SECTIONS if src_groups.get(s)]
        if dropped:
            notes.append('Profile-only sections dropped: %s' % ', '.join(dropped))
        lbl = label or ''
        etree.SubElement(out, M + 'label').text = lbl
        notes.append('label set to %r' % lbl)
    else:
        kept = []
        if dst_groups is not None:
            for section in PROFILE_EXCLUSIVE_SECTIONS:
                for el in dst_groups.get(section, []):
                    out.append(deepcopy(el))
                    kept.append(section)
            # Keep the destination's default record types / default apps that
            # the source PermissionSet does not mention: dropping them would
            # make the Profile invalid ("No default record type specified").
            for section in _PS_DROP_DEFAULT_SECTIONS:
                key_child = MERGE_TABLE[section][0]
                src_keys = {_txt(e, key_child) for e in src_groups.get(section, [])}
                for d_el in dst_groups.get(section, []):
                    if (_txt(d_el, key_child) not in src_keys
                            and _txt(d_el, 'default') == 'true'):
                        out.append(deepcopy(d_el))
                        kept.append(section)
        if kept:
            notes.append('destination Profile-only sections kept as-is: %s'
                         % ', '.join(sorted(set(kept))))
        else:
            notes.append('no destination Profile provided: Profile-only sections left empty')

    etree.indent(out, space='    ')
    return etree.tostring(out, xml_declaration=True, encoding='UTF-8'), notes


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
        # Only a REMOVED assignment (destination-only slot) is a manual action
        # worth classifying; a layout CHANGE is applied by the deploy and has no
        # "verify the default" question.
        if c.get('section') != 'layoutAssignments' or c.get('kind') != 'removed':
            continue
        n = layout_count.get(c.get('object'), 0)
        c['verdict'] = 'redundant' if n == 1 else 'check'
    return manual_ops, changes


def format_ops_table(ops, show_verdict=None, omit_operation=False):
    """Render manual operations as an aligned plain-text table. When the ops
    carry a ``verdict`` (layoutAssignments redundancy/check classification) an
    extra VERDICT column is added, unless ``show_verdict`` is explicitly False
    (e.g. when the section header already states the verdict). ``omit_operation``
    drops the MANUAL OPERATION column (used when the explanation is stated once
    in the section header instead of being repeated on every row)."""
    if show_verdict is None:
        show_verdict = any('verdict' in o for o in ops)
    header = ['SECTION', 'ITEM']
    if not omit_operation:
        header.append('MANUAL OPERATION')
    if show_verdict:
        header.append('VERDICT')
    header.append('WHERE')

    def _row(o):
        r = [o['section'], o['item']]
        if not omit_operation:
            r.append(o['operation'])
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


def write_manual_ops(path, ops, profile_name, mdtype='Profile'):
    """Write the manual-operations file, split into REDUNDANT (ignorable) and
    NEEDS CHECK (real action) groups. Returns its path."""
    label = meta_root(mdtype)
    header = [
        'MANUAL OPERATIONS REQUIRED FOR A COMPLETE ALIGNMENT',
        '==================================================',
        '',
        '%s: %s' % (label, profile_name),
        '',
        'These sections have no expressible "off" value in the Metadata API, so',
        'they cannot be turned off or removed by a %s deploy (omitting an' % label,
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

        def _section(title, note, items, omit_operation=False):
            if not items:
                return
            header.append('=' * 60)
            header.append(title + ' (%d)' % len(items))
            header.append('=' * 60)
            if note:
                header.append(note)
                header.append('')
            header.append(format_ops_table(items, show_verdict=False,
                                           omit_operation=omit_operation))
            header.append('')

        _section(
            'REDUNDANT (ignorable)',
            'Explicit assignment is the object\'s ONLY layout (= its default); '
            'source inherits the same default. Removing or ignoring these '
            'entries changes nothing functional.',
            redundant,
            omit_operation=True)
        _section(
            'NEEDS CHECK',
            'Align the page layout assignment with the source: the object has '
            'several layouts in the destination org, so verify each assignment '
            'matches the default and change it if it does not.',
            check,
            omit_operation=True)
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
    layout_changes = [c for c in changes if c['kind'] == 'layout_change']

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
        '  LAYOUT  : the profile\'s layout assignment for an object+recordType',
        '            is CHANGED to the source value by the deploy.',
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
        'LAYOUT CHANGED: %d' % len(layout_changes),
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

    if layout_changes:
        lines.append('=' * 78)
        lines.append('LAYOUT CHANGED (applied by the deploy)')
        lines.append('=' * 78)
        for c in layout_changes:
            lines.extend(_render_layout_change(c, dest_env))
            lines.append('')

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


def write_prune_what_is_changed(path, removed_by_type, manual_ops, dest_env):
    """Write the what-is-changed report for a plain prune (no merge): the
    dangling references removed from the profile and the non-alignable manual
    operations. Returns the path.

    ``removed_by_type`` maps a metadata type name to an iterable of removed
    member names. The report groups them by type so the removed references are
    easy to review without touching the org.
    """
    total_removed = sum(len(v) for v in removed_by_type.values())
    lines = [
        'WHAT IS CHANGED - prune (destination: %s)' % dest_env,
        '=' * 78,
        '',
        'The prune removed %d reference(s) that do not exist in %s.'
        % (total_removed, dest_env),
        'The cleaned profile simply omits them. Omitting a reference in a',
        'Profile deploy is a silent no-op, so the destination org is NOT',
        'modified by these removals - they are listed here only for review.',
        '',
        'REMOVED (dangling references): %d' % total_removed,
        '',
    ]
    for typ in sorted(removed_by_type):
        members = sorted(removed_by_type[typ])
        lines.append('  %s: %d' % (typ, len(members)))
        for m in members:
            lines.append('    - %s' % m)
        lines.append('')
    lines.append('MANUAL (non-alignable sections): %d' % len(manual_ops))
    if manual_ops:
        lines.append('')
        lines.append('  These sections have no deploy-able "off" value; perform the')
        lines.append('  actions below in the destination org if you need to remove them.')
        lines.append('')
        lines.append(format_ops_table(manual_ops))
    lines.append('')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines).rstrip() + '\n')
    return path
