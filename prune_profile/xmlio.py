# -*- coding: utf-8 -*-
"""Profile XML manipulation and package/manifest file I/O.

Everything that reads or writes the .profile-meta.xml, package.xml,
sfdx-project.json, the resumable missing_log.txt and the throwaway working
area. No CLI/console logic here.
"""

import json
import os
import re
import shutil
import time
from copy import deepcopy

from lxml import etree

from .constants import BLOCK_MAP, M, NS, METADATA_TYPES, decode_bytes, is_standard_api_name, meta_root


def tostring_xml(root, encoding='UTF-8', pretty_print=False):
    """Serialize ``root`` with a DOUBLE-QUOTED XML declaration.

    lxml's built-in declaration uses single quotes
    (``<?xml version='1.0' encoding='UTF-8'?>``); Salesforce metadata and the
    rest of the toolchain conventionally write double quotes
    (``<?xml version="1.0" encoding="UTF-8"?>``). Returns UTF-8 bytes.
    """
    body = etree.tostring(root, encoding=encoding, xml_declaration=False,
                          pretty_print=pretty_print)
    return ('<?xml version="1.0" encoding="%s"?>\n' % encoding).encode('ascii') + body


def write_xml(path, root, encoding='UTF-8', pretty_print=False):
    """Write ``root`` to ``path`` with a double-quoted XML declaration."""
    with open(path, 'wb') as f:
        f.write(tostring_xml(root, encoding=encoding, pretty_print=pretty_print))


_SINGLE_QUOTED_DECL_RE = re.compile(
    r"^(\ufeff)?<\?xml version='([^']*)' encoding='([^']*)'\?>(\r?\n)?")


def normalize_xml_declaration(path):
    """Rewrite a SINGLE-quoted XML declaration to double quotes in place.

    lxml used to emit ``<?xml version='1.0' encoding='UTF-8'?>``; user-supplied
    ``-pp`` source files may still carry it. Every file the tool writes uses
    ``tostring_xml``/``write_xml`` (double quotes), but verbatim copies of an
    INPUT file (``ORIGINAL_*``, work copies) would keep the input's header —
    this normalizes them so no tool-produced file ships a single-quoted
    declaration. Returns True when the file was rewritten.
    """
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except OSError:
        return False
    text = decode_bytes(data)
    m = _SINGLE_QUOTED_DECL_RE.match(text)
    if not m:
        return False
    bom, ver, enc, nl = m.groups()
    new_text = ('%s<?xml version="%s" encoding="%s"?>%s%s'
                % (bom or '', ver, enc, nl or '', text[m.end():]))
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(new_text)
    return True


def resolve_error(problem):
    m = re.match(
        r'^In\s+([^:]+):\s*(\S+)\s*-\s*no\s+(\S+)\s+named\s+(.+)\s+found\s*$',
        problem,
    )
    if m:
        return m.group(3), m.group(4)

    m2 = re.search(r"can't edit tab settings for\s+(.+?),\s*as it's not a valid tab", problem)
    if m2:
        return 'CustomTab', m2.group(1)

    # A permission can be license-mandated for a given license type, so the
    # deploy refuses to turn it off ("You may not turn off permission X for
    # this License Type"). That is not a dangling reference: the right fix is to
    # stop turning it off, i.e. drop the explicit OFF entry from the merged
    # output (a Profile deploy omitting a userPermission leaves it untouched).
    # The message carries the permission LABEL; the API name is the label
    # without spaces.
    m3 = re.search(r"You may not turn off permission\s+(.+?)\s+for this License Type", problem)
    if m3:
        return 'UserPermission', m3.group(1).replace(' ', '')

    return None


def humanize_status(status):
    """Turn a Salesforce deploy status into readable, lower-case words with
    spaces (e.g. 'InProgress' -> 'in progress', 'Pending' -> 'pending')."""
    s = (status or '').strip()
    if not s:
        return s
    # CamelCase -> spaced words: 'InProgress' -> 'In Progress';
    # 'SucceededPartial' -> 'Succeeded Partial'.
    s = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', s)
    s = re.sub(r'(?<=[A-Z])(?=[A-Z][a-z])', ' ', s)
    s = s.replace('_', ' ').replace('-', ' ')
    s = re.sub(r'\s+', ' ', s).strip().lower()
    return s


def remove_block(work_path, member, candidates):
    tree = etree.parse(work_path)
    root = tree.getroot()

    for block, child, mode in candidates:
        to_remove = []
        for el in root.iter(M + block):
            child_el = el.find(M + child)
            if child_el is None or child_el.text is None:
                continue
            val = child_el.text.strip()
            if mode == 'exact' and val == member:
                to_remove.append(el)
            elif mode == 'prefix' and val.startswith(member + '.'):
                to_remove.append(el)
            elif mode == 'suffix' and val.endswith('-' + member):
                to_remove.append(el)
        if to_remove:
            for el in to_remove:
                el.getparent().remove(el)
            write_xml(work_path, root)
            return len(to_remove)

    # Generic fallback: leaf whose exact text == member.
    removed = 0
    for leaf in root.iter():
        if leaf.text is not None and leaf.text.strip() == member:
            block = leaf
            parent = block.getparent()
            while parent is not None and parent.getparent() is not None:
                block = parent
                parent = block.getparent()
            if parent is not None:
                parent.remove(block)
                removed += 1
    if removed:
        write_xml(work_path, root)
        return removed

    raise RuntimeError("NO BLOCK FOUND for '%s'" % member)


def write_package_xml(path, entries, api_version):
    """entries = { type: iterable of members }. Clean output, no blank lines."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<Package xmlns="http://soap.sforce.com/2006/04/metadata">')
    for t in sorted(entries):
        members = sorted(set(entries[t]))
        members = [m for m in members if m and m.strip()]
        if not members:
            continue
        lines.append('    <types>')
        for m in members:
            lines.append('        <members>%s</members>' % m)
        lines.append('        <name>%s</name>' % t)
        lines.append('    </types>')
    lines.append('    <version>%s</version>' % api_version)
    lines.append('</Package>')
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write('\n'.join(lines) + '\n')


def parse_ignore_package(path):
    """Parse a package.xml into ``{BLOCK_MAP_type: set(member)}`` for `--ignore`.

    Only metadata types with a ``BLOCK_MAP`` entry are kept (the ones a
    Profile/PermissionSet can reference); others are skipped. The package.xml
    name ``CustomMetadata`` maps to BLOCK_MAP's ``CustomMetadataType``. A
    ``<members>*</members>`` entry is kept as ``'*'`` (ignore the whole type).
    """
    root = etree.parse(path).getroot()
    out = {}

    def _txt(el, child):
        ce = el.find(M + child)
        return (ce.text or '').strip() if ce is not None else ''

    for types_el in root.findall(M + 'types'):
        typ = _txt(types_el, 'name')
        if typ == 'CustomMetadata':
            typ = 'CustomMetadataType'
        if typ not in BLOCK_MAP:
            continue
        members = set()
        for m_el in types_el.findall(M + 'members'):
            v = (m_el.text or '').strip()
            if v:
                members.add(v)
        if members:
            out.setdefault(typ, set()).update(members)
    return out


def filter_ignored_groups(groups, ignore):
    """Return a copy of ``groups`` with the entries matched by ``ignore``
    removed. ``ignore`` is ``{BLOCK_MAP_type: set(member)}`` (from
    :func:`parse_ignore_package`); a ``'*'`` member drops the whole mapped
    block. Returns ``(filtered_groups, removed_count)``."""
    out = {block: list(els) for block, els in groups.items()}
    removed = 0
    for typ, members in ignore.items():
        for member in members:
            for block, _child, _mode in BLOCK_MAP.get(typ, []):
                els = out.get(block)
                if els is None:
                    continue
                if member == '*':
                    removed += len(els)
                    out[block] = []
                else:
                    kept, n = remove_from_list(els, block, member)
                    out[block] = kept
                    removed += n
    return out, removed


def strip_blank_lines(path):
    """Removes blank lines (left behind by the XML removals)."""
    with open(path, 'rb') as f:
        lines = decode_bytes(f.read()).splitlines(keepends=True)
    kept = [l for l in lines if l.strip()]
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.writelines(kept)


def write_sfdx_project(project_dir, api_version):
    """Write a minimal sfdx-project.json (all `sf project deploy` needs)."""
    os.makedirs(project_dir, exist_ok=True)
    data = {
        'packageDirectories': [{'path': 'force-app', 'default': True}],
        'namespace': '',
        'sfdcLoginUrl': 'https://login.salesforce.com',
        'sourceApiVersion': api_version,
    }
    with open(os.path.join(project_dir, 'sfdx-project.json'), 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def read_outcome(report):
    """Reads the OUTCOME: line from a previous report, or None."""
    try:
        with open(report, 'rb') as f:
            text = decode_bytes(f.read())
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('OUTCOME:'):
                return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return None


def read_report_summary(report):
    """Read ``OUTCOME`` and ``METADATA REMOVED (total)`` from a previous
    ``report.txt``. Returns a dict with keys ``outcome`` (str or None) and
    ``removed`` (int or None); both are None when the file is unreadable or
    lacks the line."""
    out = {'outcome': None, 'removed': None}
    try:
        with open(report, 'rb') as f:
            text = decode_bytes(f.read())
        for line in text.splitlines():
            line = line.strip()
            if line.startswith('OUTCOME:'):
                out['outcome'] = line.split(':', 1)[1].strip()
            elif line.startswith('METADATA REMOVED (total):'):
                try:
                    out['removed'] = int(line.split(':', 1)[1].strip())
                except ValueError:
                    pass
    except OSError:
        pass
    return out


def load_log_entries(missing_log):
    """Ordered, deduplicated (type, member) pairs from missing_log.txt."""
    entries = []
    seen = set()
    try:
        with open(missing_log, 'rb') as f:
            text = decode_bytes(f.read())
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split('|')
            if len(parts) >= 3:
                key = (parts[1], parts[2])
                if key not in seen:
                    seen.add(key)
                    entries.append(key)
    except OSError:
        pass
    return entries


def apply_removals_batch(work_path, removals):
    """Remove many blocks from the working profile in a single parse/write.
    removals = list of (member, candidates)."""
    tree = etree.parse(work_path)
    root = tree.getroot()
    removed_total = 0
    for member, candidates in removals:
        found = False
        for block, child, mode in candidates:
            to_remove = []
            for el in root.iter(M + block):
                child_el = el.find(M + child)
                if child_el is None or child_el.text is None:
                    continue
                val = child_el.text.strip()
                if mode == 'exact' and val == member:
                    to_remove.append(el)
                elif mode == 'prefix' and val.startswith(member + '.'):
                    to_remove.append(el)
                elif mode == 'suffix' and val.endswith('-' + member):
                    to_remove.append(el)
            if to_remove:
                for el in to_remove:
                    el.getparent().remove(el)
                    removed_total += 1
                found = True
                break
        if not found:
            # Generic fallback: leaf whose exact text == member.
            for leaf in list(root.iter()):
                if leaf.text is not None and leaf.text.strip() == member:
                    block = leaf
                    parent = block.getparent()
                    while parent is not None and parent.getparent() is not None:
                        block = parent
                        parent = block.getparent()
                    if parent is not None:
                        parent.remove(block)
                        removed_total += 1
                    break
    write_xml(work_path, root)
    return removed_total


def prepare_work_copy(work_path, source_path, missing_log, fresh):
    """(Re)build the working profile: copy the source, then re-apply every
    removal already recorded in missing_log.txt (unless fresh)."""
    os.makedirs(os.path.dirname(work_path), exist_ok=True)
    if os.path.abspath(source_path) != os.path.abspath(work_path):
        shutil.copyfile(source_path, work_path)
        normalize_xml_declaration(work_path)
    os.chmod(work_path, 0o666)
    if fresh:
        return
    removals = []
    for t, m in load_log_entries(missing_log):
        candidates = BLOCK_MAP.get(t)
        if candidates:
            removals.append((m, candidates))
    if removals:
        apply_removals_batch(work_path, removals)


def remove_temp_project(project_dir, out_dir):
    """Delete the temporary micro-project. Windows cannot remove the CWD, and a
    just-finished CLI call can briefly hold a handle to the directory, so
    retry a few times and skip files that stay locked. Never raises: the temp
    area is throwaway and a leftover folder is harmless."""
    def _onerror(func, path, exc_info):
        try:
            os.chmod(path, 0o777)
            func(path)
        except OSError:
            pass
    try:
        os.chdir(out_dir)
    except OSError:
        pass
    for _ in range(8):
        try:
            shutil.rmtree(project_dir, onerror=_onerror)
            return
        except KeyboardInterrupt:
            return
        except OSError:
            try:
                time.sleep(0.5)
            except KeyboardInterrupt:
                return
    try:
        os.rmdir(project_dir)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Parallel section pruning helpers
# ---------------------------------------------------------------------------

def prunable_blocks():
    """The top-level Profile tags that carry a reference to other metadata."""
    blocks = set()
    for cands in BLOCK_MAP.values():
        for block, _child, _mode in cands:
            blocks.add(block)
    return blocks


def candidates_for_block(block):
    """(type, child, mode) tuples relevant to a given top-level section tag."""
    out = []
    for typ, cands in BLOCK_MAP.items():
        for b, child, mode in cands:
            if b == block:
                out.append((typ, child, mode))
    return out


def _matches(val, member, mode):
    if mode == 'exact':
        return val == member
    if mode == 'prefix':
        return val.startswith(member + '.')
    if mode == 'suffix':
        return val.endswith('-' + member)
    return False


def remove_from_list(elements, block, member):
    """Filter `member` out of a list of section elements. Returns (kept, removed)."""
    cands = candidates_for_block(block)
    kept = []
    removed = 0
    for el in elements:
        hit = False
        for _typ, child, mode in cands:
            child_el = el.find(M + child)
            val = (child_el.text or '').strip() if child_el is not None else ''
            if _matches(val, member, mode):
                hit = True
                break
        if hit:
            removed += 1
        else:
            kept.append(el)
    return kept, removed


def dangling_members(elements, child, existing):
    """Return the set of values referenced via `child` in `elements` that are
    absent from `existing`. Empty values and the '*' master record type are
    never treated as dangling."""
    out = set()
    for el in elements:
        ce = el.find(M + child)
        if ce is None or ce.text is None:
            continue
        val = ce.text.strip()
        if val and val != '*' and val not in existing:
            out.add(val)
    return out


def build_subprofile(elements, mdtype='Profile', label=None):
    """Serialize a list of top-level section elements into metadata source XML
    of type `mdtype` (Profile or PermissionSet).

    A PermissionSet REQUIRES a ``<label>`` even for a partial dry-run
    (deploying a PermissionSet without it fails with
    ``Required field is missing: label``). A Profile derives its name from the
    file name, so it needs nothing extra. ``label`` is used for a PermissionSet
    (falls back to a placeholder when not given)."""
    root = etree.Element(M + meta_root(mdtype), nsmap={None: NS})
    if mdtype == 'PermissionSet':
        lbl = etree.SubElement(root, M + 'label')
        lbl.text = label or 'Pruner'
    for el in elements:
        root.append(deepcopy(el))
    return tostring_xml(root)


def root_metadata_type(root):
    """Metadata type ('Profile' | 'PermissionSet') declared by a parsed root,
    defaulting to 'Profile' for unknown roots."""
    name = root.tag.split('}')[-1]
    return name if name in METADATA_TYPES else 'Profile'


def detect_metadata_type(path):
    """Metadata type of the source file at `path`, read from its root element.
    Falls back to 'Profile' when the file is unreadable/unparseable."""
    try:
        root = etree.parse(path).getroot()
        return root_metadata_type(root)
    except (OSError, etree.LxmlError):
        return 'Profile'


def extract_common_members(groups):
    """Extract the metadata referenced by the surviving profile, grouped by
    metadata type: {type: set(member)}. This is the COMMON set — every
    reference that exists in the target org and is therefore deployable.

    Standard objects/fields/tabs are intentionally excluded: they always exist
    and would only bloat the manifest. The type names match BLOCK_MAP so the
    result stays consistent with the MISSING package.xml."""
    def _txt(el, child):
        ce = el.find(M + child)
        return (ce.text or '').strip() if ce is not None else ''

    out = {}

    def add(typ, member):
        if member:
            out.setdefault(typ, set()).add(member)

    for block, els in groups.items():
        if not els:
            continue
        if block == 'classAccesses':
            for el in els:
                add('ApexClass', _txt(el, 'apexClass'))
        elif block == 'pageAccesses':
            for el in els:
                add('ApexPage', _txt(el, 'apexPage'))
        elif block == 'applicationVisibilities':
            for el in els:
                add('CustomApplication', _txt(el, 'application'))
        elif block == 'tabVisibilities' or block == 'tabSettings':
            for el in els:
                v = _txt(el, 'tab')
                if v and not v.startswith('standard-'):
                    add('CustomTab', v)
        elif block == 'flowAccesses':
            for el in els:
                add('Flow', _txt(el, 'flow'))
        elif block == 'loginFlows':
            for el in els:
                v = _txt(el, 'visualForcePage') or _txt(el, 'flow')
                add('ApexPage' if _txt(el, 'visualForcePage') else 'Flow', v)
        elif block == 'layoutAssignments':
            for el in els:
                add('Layout', _txt(el, 'layout'))
                add('RecordType', _txt(el, 'recordType'))
        elif block == 'recordTypeVisibilities':
            for el in els:
                add('RecordType', _txt(el, 'recordType'))
        elif block == 'customPermissions':
            for el in els:
                add('CustomPermission', _txt(el, 'name'))
        elif block == 'customMetadataTypeAccesses':
            for el in els:
                add('CustomMetadataType', _txt(el, 'name'))
        elif block == 'externalDataSourceAccesses':
            for el in els:
                add('ExternalDataSource', _txt(el, 'externalDataSource'))
        elif block == 'objectPermissions':
            for el in els:
                v = _txt(el, 'object')
                # Custom = any metadata whose API name carries a namespace and/or
                # a custom suffix (`__`): custom objects, big/external objects,
                # platform events, custom settings, knowledge (`__kav`), etc. This
                # matches engine.py's is_standard_api_name() criterion instead of
                # the old `endswith('__c')` which misclassified `__b`/`__e`/`__x`.
                add('CustomObject' if not is_standard_api_name(v) else 'StandardObject', v)
        elif block == 'fieldPermissions':
            for el in els:
                v = _txt(el, 'field')
                # `v` is `Object.Field`. A field is custom only when the FIELD
                # component carries `__` (e.g. Account.MyField__c); a standard
                # field on a custom object (MyObj__c.CreatedDate) is standard.
                # The old `'__c' in v` matched the object part and misclassified
                # those standard fields as CustomField.
                field = v.rsplit('.', 1)[-1]
                add('CustomField' if not is_standard_api_name(field) else 'StandardField', v)
        elif block == 'customSettingAccesses':
            for el in els:
                add('CustomObject', _txt(el, 'name'))
        elif block == 'servicePresenceStatusAccesses':
            for el in els:
                add('ServicePresenceStatus', _txt(el, 'servicePresenceStatus'))
        elif block == 'categoryGroupVisibilities':
            for el in els:
                add('DataCategoryGroup', _txt(el, 'dataCategoryGroup'))
        elif block == 'agentAccesses':
            for el in els:
                add('AgentDefinition', _txt(el, 'agentName') or _txt(el, 'agent'))
        elif block == 'genComputingSummaryDefAccesses':
            for el in els:
                add('GenComputingSummaryDefinition',
                    _txt(el, 'genComputingSummaryDef') or _txt(el, 'name'))

    # Drop standard types (they are always present in every org).
    out.pop('StandardObject', None)
    out.pop('StandardField', None)
    return out
