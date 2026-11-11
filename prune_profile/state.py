# -*- coding: utf-8 -*-
"""Thread-safe shared pruning state: per-section groups + cumulative errors."""

import os
import threading
from copy import deepcopy

from lxml import etree

from .constants import BLOCK_MAP, M, NS
from .xmlio import remove_from_list, write_package_xml


class SharedState:
    """Thread-safe shared results: per-section groups + cumulative errors."""

    def __init__(self, groups, original_seq, logger, err_log, good_profile_path,
                 errors_pkg_path, profile_name, api_version):
        self.lock = threading.Lock()
        self.groups = groups              # OrderedDict: block -> [lxml elements]
        self.original_seq = original_seq  # [lxml elements] in document order
        self.logger = logger
        self.err_log = err_log
        self.good_profile_path = good_profile_path
        self.errors_pkg_path = errors_pkg_path
        self.profile_name = profile_name
        self.api_version = api_version
        self.removed_by_type = {}         # type -> set(member)
        self.seq = 0
        self.section_status = {}           # block -> (ok, reason)
        self.initial_counts = {}           # block -> initial number of entries

    def record_section(self, block, ok, reason):
        with self.lock:
            self.section_status[block] = (ok, reason)

    def apply_and_record(self, block, new_elements, removals, tid):
        """Under the mutex: update the section, append the error log, and
        rewrite the GOOD profile and the ERRORS package."""
        with self.lock:
            self.groups[block] = new_elements
            for typ, member, problem in removals:
                self.removed_by_type.setdefault(typ, set()).add(member)
                self.seq += 1
                with open(self.err_log, 'a', encoding='utf-8') as f:
                    f.write('%d|%s|%s|%s\n' % (self.seq, typ, member, problem))
            self._write_outputs_locked()

    def apply_global_removals(self, removals, problem='final dry-run'):
        """Remove (type, member) pairs across every mapped block, record them
        in the error log, and rewrite outputs. Used by the final full-package
        gate when it surfaces references the per-section pass missed (e.g.
        login flows). Returns the number of entries removed.

        `removals` is a list of (type, member) tuples."""
        with self.lock:
            total = 0
            recorded = []
            for typ, member in removals:
                removed_any = False
                for block, child, mode in BLOCK_MAP.get(typ, []):
                    g = self.groups.get(block)
                    if g is None:
                        continue
                    new, n = remove_from_list(g, block, member)
                    if n:
                        self.groups[block] = new
                        total += n
                        removed_any = True
                if removed_any:
                    self.removed_by_type.setdefault(typ, set()).add(member)
                    recorded.append((typ, member))
            for typ, member in recorded:
                self.seq += 1
                with open(self.err_log, 'a', encoding='utf-8') as f:
                    f.write('%d|%s|%s|%s\n' % (self.seq, typ, member, problem))
            if total:
                self._write_outputs_locked()
            return total

    def _write_outputs_locked(self):
        surviving = set()
        for elements in self.groups.values():
            for el in elements:
                surviving.add(id(el))
        root = etree.Element(M + 'Profile', nsmap={None: NS})
        for el in self.original_seq:
            if id(el) in surviving:
                root.append(deepcopy(el))
        os.makedirs(os.path.dirname(self.good_profile_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.errors_pkg_path), exist_ok=True)
        tree = etree.ElementTree(root)
        tree.write(self.good_profile_path, encoding='utf-8', xml_declaration=True)
        write_package_xml(self.errors_pkg_path, self.removed_by_type, self.api_version)

    def totals(self):
        with self.lock:
            return sum(len(v) for v in self.removed_by_type.values())
