# -*- coding: utf-8 -*-
"""Thread-safe shared pruning state: per-section groups + cumulative errors."""

import os
import threading
from copy import deepcopy

from lxml import etree

from .constants import BLOCK_MAP, M, NS, STANDARD_TYPES
from .xmlio import extract_common_members, remove_from_list, write_package_xml


class SharedState:
    """Thread-safe shared results: per-section groups + cumulative errors."""

    def __init__(self, groups, original_seq, logger, missing_log, common_profile_path,
                 missing_pkg_path, todeploy_path, profile_name, api_version):
        self.lock = threading.Lock()
        self.groups = groups              # OrderedDict: block -> [lxml elements]
        self.original_seq = original_seq  # [lxml elements] in document order
        self.logger = logger
        self.missing_log = missing_log
        self.common_profile_path = common_profile_path
        self.missing_pkg_path = missing_pkg_path
        self.todeploy_path = todeploy_path
        self.profile_name = profile_name
        self.api_version = api_version
        self.removed_by_type = {}         # type -> set(member)
        self.seq = 0
        self.section_status = {}           # block -> (ok, reason)
        self.initial_counts = {}           # block -> initial number of entries
        self.chunks = {}                   # block -> [list-of-elements, ...]
        self.chunk_initials = {}           # block -> [initial count per chunk]
        self._chunk_results = {}           # block -> {chunk_id: (ok, reason)}

    def record_section(self, block, ok, reason):
        with self.lock:
            self.section_status[block] = (ok, reason)

    def split_chunks(self, block, max_elem):
        """Split `groups[block]` into independent chunks for parallel pruning.

        With `max_elem > 0`, a section holding more than `max_elem` entries is
        sliced into chunks of `max_elem` (the last one may be smaller); with
        `max_elem == 0` the whole section is kept as a single chunk. Returns the
        number of chunks created.
        """
        with self.lock:
            flat = list(self.groups.get(block, []))
            if max_elem and len(flat) > max_elem:
                chunks = [flat[i:i + max_elem] for i in range(0, len(flat), max_elem)]
            else:
                chunks = [flat]
            self.chunks[block] = chunks
            self.chunk_initials[block] = [len(c) for c in chunks]
            self._chunk_results.setdefault(block, {})
            return len(chunks)

    def record_chunk(self, block, chunk_id, ok, reason):
        """Record one chunk's result and aggregate the section status: a
        section is clean only when every one of its chunks is clean."""
        with self.lock:
            self._chunk_results.setdefault(block, {})[chunk_id] = (ok, reason)
            results = self._chunk_results[block]
            if all(ok_ for ok_, _r in results.values()):
                self.section_status[block] = (True, 'clean')
            else:
                for ok_, r in results.values():
                    if not ok_:
                        self.section_status[block] = (False, r)
                        break

    def apply_chunk_removals(self, block, chunk_id, new_elements, removals, tid):
        """Replace one chunk's surviving elements, rebuild the block's flat
        list, append the error log and rewrite the outputs. Only the chunk at
        `chunk_id` is replaced; the other chunks of the same block are left
        untouched, so parallel workers never clobber each other."""
        with self.lock:
            self.chunks[block][chunk_id] = new_elements
            self.groups[block] = [el for chunk in self.chunks[block] for el in chunk]
            for typ, member, problem in removals:
                self.removed_by_type.setdefault(typ, set()).add(member)
                self.seq += 1
                with open(self.missing_log, 'a', encoding='utf-8') as f:
                    f.write('%d|%s|%s|%s\n' % (self.seq, typ, member, problem))
            self._write_outputs_locked()

    def apply_and_record(self, block, new_elements, removals, tid):
        """Under the mutex: update the section, append the log, and
        rewrite the COMMON profile and the MISSING package."""
        with self.lock:
            self.groups[block] = new_elements
            for typ, member, problem in removals:
                self.removed_by_type.setdefault(typ, set()).add(member)
                self.seq += 1
                with open(self.missing_log, 'a', encoding='utf-8') as f:
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
                if not removed_any:
                    # Fallback for a brand-new metadata type (not yet in
                    # BLOCK_MAP): remove any section entry whose descendant
                    # leaf equals `member`. Same semantics as the generic
                    # fallback used when re-applying the error log on resume.
                    n = self._generic_remove(member)
                    if n:
                        total += n
                        removed_any = True
                if removed_any:
                    self.removed_by_type.setdefault(typ, set()).add(member)
                    recorded.append((typ, member))
            for typ, member in recorded:
                self.seq += 1
                with open(self.missing_log, 'a', encoding='utf-8') as f:
                    f.write('%d|%s|%s|%s\n' % (self.seq, typ, member, problem))
            if total:
                self._write_outputs_locked()
            return total

    def _generic_remove(self, member):
        """Remove, from any section, entries whose descendant leaf text equals
        `member` exactly. Returns the number of entries removed."""
        removed = 0
        for block, elements in list(self.groups.items()):
            kept = []
            dropped = 0
            for el in elements:
                if any((ch.text or '').strip() == member
                       for ch in el.iter() if ch.text):
                    dropped += 1
                else:
                    kept.append(el)
            if dropped:
                self.groups[block] = kept
                removed += dropped
        return removed

    def _write_outputs_locked(self):
        surviving = set()
        for elements in self.groups.values():
            for el in elements:
                surviving.add(id(el))
        root = etree.Element(M + 'Profile', nsmap={None: NS})
        for el in self.original_seq:
            if id(el) in surviving:
                root.append(deepcopy(el))
        os.makedirs(os.path.dirname(self.common_profile_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.missing_pkg_path), exist_ok=True)
        os.makedirs(os.path.dirname(self.todeploy_path), exist_ok=True)
        tree = etree.ElementTree(root)
        tree.write(self.common_profile_path, encoding='utf-8', xml_declaration=True)
        # Standard metadata cannot be packaged: keep it in the log/report but
        # exclude it from MISSING/package.xml (which lists deployable types).
        deployable = {t: m for t, m in self.removed_by_type.items()
                      if t not in STANDARD_TYPES}
        write_package_xml(self.missing_pkg_path, deployable, self.api_version)
        write_package_xml(self.todeploy_path, extract_common_members(self.groups),
                         self.api_version)

    def totals(self):
        with self.lock:
            return sum(len(v) for v in self.removed_by_type.values())
