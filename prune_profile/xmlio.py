# -*- coding: utf-8 -*-
"""Profile XML manipulation and package/manifest file I/O.

Everything that reads or writes the .profile-meta.xml, package.xml,
sfdx-project.json, the resumable errors_log.txt and the throwaway working
area. No CLI/console logic here.
"""

import json
import os
import re
import shutil
import time
from copy import deepcopy

from lxml import etree

from .constants import BLOCK_MAP, M, NS


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
            tree.write(work_path, encoding='utf-8', xml_declaration=True)
            return len(to_remove)

    # Generic fallback: leaf whose exact text == member.
    removed = 0
    for leaf in root.iter():
        if leaf.text is not None and leaf.text.strip() == member:
            block = leaf
            parent = block.getparent()
            while parent is not None and parent.tag != M + 'Profile':
                block = parent
                parent = block.getparent()
            if parent is not None:
                parent.remove(block)
                removed += 1
    if removed:
        tree.write(work_path, encoding='utf-8', xml_declaration=True)
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


def strip_blank_lines(path):
    """Removes blank lines (left behind by the XML removals)."""
    with open(path, encoding='utf-8', errors='replace') as f:
        lines = f.readlines()
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
        with open(report, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if line.startswith('OUTCOME:'):
                    return line.split(':', 1)[1].strip()
    except OSError:
        pass
    return None


def load_log_entries(err_log):
    """Ordered, deduplicated (type, member) pairs from errors_log.txt."""
    entries = []
    seen = set()
    try:
        with open(err_log, encoding='utf-8', errors='replace') as f:
            for line in f:
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
                    while parent is not None and parent.tag != M + 'Profile':
                        block = parent
                        parent = block.getparent()
                    if parent is not None:
                        parent.remove(block)
                        removed_total += 1
                    break
    tree.write(work_path, encoding='utf-8', xml_declaration=True)
    return removed_total


def prepare_work_copy(work_path, source_path, err_log, fresh):
    """(Re)build the working profile: copy the source, then re-apply every
    removal already recorded in errors_log.txt (unless fresh)."""
    os.makedirs(os.path.dirname(work_path), exist_ok=True)
    if os.path.abspath(source_path) != os.path.abspath(work_path):
        shutil.copyfile(source_path, work_path)
    os.chmod(work_path, 0o666)
    if fresh:
        return
    removals = []
    for t, m in load_log_entries(err_log):
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


def build_subprofile(elements):
    """Serialize a list of top-level section elements into a Profile XML bytes."""
    root = etree.Element(M + 'Profile', nsmap={None: NS})
    for el in elements:
        root.append(deepcopy(el))
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8')
