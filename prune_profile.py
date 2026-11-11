#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prune_profile.py

Removes from a Salesforce Profile every reference to metadata that does NOT
exist in the target org. Pruning uses only `sf project deploy start --dry-run`
and never modifies the org; with `--deploy-on-success` the cleaned profile is
deployed at the end.

Algorithm:
  The profile is split by its top-level sections (`classAccesses`,
  `fieldPermissions`, `tabVisibilities`, … — discovered dynamically from the
  XML). Up to `--thread-number` workers run in parallel; each worker takes a
  section and, in a loop, builds a sub-package with only that section, starts
  an async deploy dry-run and polls its job via `sf project deploy report`
  until terminal, then removes every offending entry. GOOD/ and ERRORS/ are
  reassembled from the surviving entries in the original document order under
  a mutex. A final dry-run of the whole assembled package gates `SUCCESS`.

RESUME BEHAVIOUR
  - Default: RESUMES from where it left off using `errors_log.txt`: the
    cleaned profile is rebuilt from the original plus the recorded removals.
  - If the previous run ended with OUTCOME: SUCCESS, the tool exits
    immediately with "profile already clean".
  - With --fresh: restarts from scratch, clearing the previous outputs.

OUTPUT (in --out-dir)
  ORIGINAL_<name>.profile-meta.xml   untouched copy of the original
  GOOD/<name>.profile-meta.xml       clean, deployable profile (the only GOOD artifact)
  ERRORS/package.xml                 cumulative package of removed/missing metadata
  errors_log.txt                     line-by-line log (resumable)
  run.log                            progressive log copy
  report.txt                         final outcome
  .work/                             internal temporary working area (removed at the end)
"""

import argparse
import functools
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from copy import deepcopy
from datetime import datetime

from lxml import etree

__version__ = '1.10.3'
__author__ = "Lorenzo D'Aleo"
__email__ = 'turbolino.dev@proton.me'
AUTHOR_NOTE = "Made with <3 by %s (%s)" % (__author__, __email__)

APP_NAME = 'Profile Pruner'

BANNER = r'''
  _____   ______  _____  _______ _____        _______       _____   ______ _     _ __   _ _______  ______
 |_____] |_____/ |     | |______   |   |      |______      |_____] |_____/ |     | | \  | |______ |_____/
 |       |    \_ |_____| |       __|__ |_____ |______      |       |    \_ |_____| |  \_| |______ |    \_
'''

NS = 'http://soap.sforce.com/2006/04/metadata'
M = '{%s}' % NS

# Error-type -> list of (block, child, mode)
# mode: 'exact' | 'prefix' (X__c.*) | 'suffix' (-X)
BLOCK_MAP = {
    'CustomField':        [('fieldPermissions', 'field', 'exact')],
    'StandardField':      [('fieldPermissions', 'field', 'exact')],
    'CustomTab':          [
        ('tabVisibilities', 'tab', 'exact'),
        ('tabVisibilities', 'tab', 'prefix'),
        ('tabVisibilities', 'tab', 'suffix'),
    ],
    'ApexClass':          [('classAccesses', 'apexClass', 'exact')],
    'ApexPage':           [('pageAccesses', 'apexPage', 'exact')],
    'CustomApplication':  [('applicationVisibilities', 'application', 'exact')],
    'Flow':               [('flowAccesses', 'flow', 'exact')],
    'FlowDefinition':     [('flowAccesses', 'flow', 'exact')],
    'Layout':             [('layoutAssignments', 'layout', 'exact')],
    'RecordType':         [
        ('recordTypeVisibilities', 'recordType', 'exact'),
        ('layoutAssignments', 'recordType', 'exact'),
    ],
    'CustomPermission':   [('customPermissions', 'name', 'exact')],
    'CustomMetadataType': [('customMetadataTypeAccesses', 'name', 'exact')],
    'ExternalDataSource': [('externalDataSourceAccesses', 'externalDataSource', 'exact')],
    'CustomObject':       [
        ('objectPermissions', 'object', 'exact'),
        ('fieldPermissions', 'field', 'prefix'),
        ('customSettingAccesses', 'name', 'exact'),
    ],
    'StandardObject':     [('objectPermissions', 'object', 'exact')],
    'ServicePresenceStatus': [('servicePresenceStatusAccesses', 'servicePresenceStatus', 'exact')],
    'DataCategoryGroup':  [('categoryGroupVisibilities', 'dataCategoryGroup', 'exact')],
}


def timestamp():
    return datetime.now().strftime('%H:%M:%S')


class Logger:
    def __init__(self, quiet, log_path=None):
        self.quiet = quiet
        self.console = not quiet
        self.log_path = log_path
        self._lock = threading.Lock()

    def log(self, msg):
        line = '[%s] %s' % (timestamp(), msg)
        with self._lock:
            if self.console and not self.quiet:
                print(line, flush=True)
            if self.log_path:
                with open(self.log_path, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')


class StatusBoard:
    """One live-updating line per worker thread on the console, plus a summary
    line with the elapsed time and the tag counters (done / running / todo).

    Renders N thread lines + 1 summary line and redraws them in place on every
    update. Falls back to no-op when stdout is not a TTY (e.g. piped) or when
    --quiet is set.

    Stop lifecycle:
      - freeze(): stop the live redraw, leaving the current frame on screen
        (the rows stay visible instead of being wiped).
      - finalize_stopped(): mark non-idle rows "stopped by user" and redraw the
        frame in place, then leave it on screen.
    """

    COL_FMT = '%-7s %-9s %-28s %-22s remaining:%-9d deleted:%-9d initial:%-8d'

    def __init__(self, n_threads, quiet, progress=None, t0=None):
        self.n = n_threads
        self.progress = progress
        self.t0 = t0 if t0 is not None else time.time()
        self.state = [dict(status='idle', tag='-', op='-', initial=0, remaining=0)
                      for _ in range(n_threads)]
        self.rows = [self._fmt_row(t, **self.state[t]) for t in range(n_threads)]
        self.lock = threading.Lock()
        self.lines_on_screen = 0
        self.active = (not quiet) and bool(getattr(sys.stdout, 'isatty', lambda: False)())
        if self.active and os.name == 'nt':
            self.active = self._enable_ansi()
        self._was_active = self.active

    @staticmethod
    def _enable_ansi():
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_uint32()
            k.GetConsoleMode(h, ctypes.byref(mode))
            k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        except Exception:
            return False

    def _fmt_row(self, tid, status, tag, op, initial, remaining):
        removed = max(0, initial - remaining)
        return self.COL_FMT % ('[T%d]' % tid, status, tag or '-', op or '-',
                               remaining, removed, initial)

    @staticmethod
    def _fmt_elapsed(seconds):
        s = max(0, int(seconds))
        return '%02d:%02d:%02d' % (s // 3600, (s % 3600) // 60, s % 60)

    def set(self, tid, status, tag='', op='', initial=0, remaining=0):
        with self.lock:
            self.state[tid] = dict(status=status, tag=tag, op=op,
                                   initial=initial, remaining=remaining)
            self.rows[tid] = self._fmt_row(tid, **self.state[tid])
            if self.active:
                self._render()

    def _summary(self):
        elapsed = self._fmt_elapsed(time.time() - self.t0)
        if self.progress is None:
            return 'elapsed %s' % elapsed
        done, running, todo, _failed = self.progress.snapshot()
        return 'elapsed %s | done: %d | running: %d | todo: %d' % (elapsed, done, running, todo)

    def _clear_above(self, n):
        if n <= 0:
            return
        out = ['\x1b[%dA' % n]
        for _ in range(n):
            out.append('\x1b[2K\n')
        sys.stdout.write(''.join(out))
        sys.stdout.flush()

    def _render(self):
        out = []
        if self.lines_on_screen:
            out.append('\x1b[%dA' % self.lines_on_screen)
        for row in self.rows:
            out.append('\x1b[2K' + row + '\n')
        out.append('\x1b[2K' + self._summary() + '\n')
        sys.stdout.write(''.join(out))
        sys.stdout.flush()
        self.lines_on_screen = self.n + 1

    def freeze(self):
        """Stop live redraw; the last rendered frame stays on screen."""
        with self.lock:
            self.active = False

    def finalize_stopped(self):
        """Mark non-idle rows 'stopped by user' and redraw them in place."""
        with self.lock:
            self._mark_stopped_locked()
            if self._was_active and self.lines_on_screen:
                self._render()
            self.active = False
            self.lines_on_screen = 0

    def _mark_stopped_locked(self):
        for tid, s in enumerate(self.state):
            if s['status'] not in ('idle', 'stopped'):
                s['status'] = 'stopped'
                s['op'] = 'stopped by user'
                self.rows[tid] = self._fmt_row(tid, **s)

    def mark_stopped(self):
        with self.lock:
            self._mark_stopped_locked()

    def finish(self):
        if not self.active:
            return
        with self.lock:
            if self.lines_on_screen:
                self._clear_above(self.lines_on_screen)
            self.lines_on_screen = 0
            self.active = False


class Control:
    """Stop signal shared between the main thread and the workers."""

    def __init__(self):
        self.stop = threading.Event()


class Progress:
    """Thread-safe counters for the tag-level progress (done / running / todo)."""

    def __init__(self, total):
        self.lock = threading.Lock()
        self.total = total
        self.done = 0
        self.failed = 0
        self.running = 0

    def start(self):
        with self.lock:
            self.running += 1

    def finish(self, ok):
        with self.lock:
            self.running -= 1
            if ok:
                self.done += 1
            else:
                self.failed += 1

    def snapshot(self):
        with self.lock:
            todo = self.total - self.done - self.failed - self.running
            return self.done, self.running, todo, self.failed


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


TERMINAL_STATUSES = {'Succeeded', 'SucceededPartial', 'Failed', 'Canceled'}


def _run_sf(cmd, sf, what, cwd=None):
    """Run an sf command and parse its JSON. Returns (data_or_None, error)."""
    env = os.environ.copy()
    good_node = find_node()
    if good_node:
        # Prepend the compatible Node so the sf.cmd shim doesn't fall back to
        # an old node.exe on PATH (sf needs Node >= 18.11).
        env['PATH'] = os.path.dirname(os.path.abspath(good_node)) + os.pathsep + env.get('PATH', '')
    kwargs = dict(capture_output=True, text=True, encoding='utf-8',
                  errors='replace', env=env, cwd=cwd)
    if os.name == 'nt':
        # Put the child in its own process group so Ctrl-C reaches only us
        # (not the cmd.exe batch shim), avoiding the "Terminate batch job"
        # prompt and letting the run stop cleanly.
        kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.run(cmd, **kwargs)
    except FileNotFoundError:
        return None, ('sf executable not found: %s. Install the Salesforce CLI '
                      '(run `sf plugins install` / `python first_config.py`), or pass --sf <path>.' % sf)
    except OSError as e:
        return None, 'failed to run %s: %s' % (what, e)
    out = (proc.stdout or '').strip()
    if not out:
        return None, '%s returned no output (stderr: %s)' % (what, (proc.stderr or '').strip()[:400])
    try:
        return json.loads(out), None
    except Exception as e:
        return None, 'unparseable JSON from %s (%s). stderr: %s | stdout head: %s' % (
            what, e, (proc.stderr or '').strip()[:300], out[:300])


def start_deploy(sf, manifest_path, org, dry_run=False, cwd=None):
    """Start an async deploy. Returns (job_id, error)."""
    cmd = [sf, 'project', 'deploy', 'start', '--async']
    if dry_run:
        cmd.append('--dry-run')
    cmd += ['--manifest', manifest_path, '--target-org', org, '--json']
    res, err = _run_sf(cmd, sf, 'deploy start', cwd=cwd)
    if err:
        return None, err
    result = res.get('result', {})
    job_id = result.get('id') or result.get('jobId')
    if not job_id:
        return None, 'deploy start returned no job id: %s' % json.dumps(res)[:400]
    return job_id, None


def report_deploy(sf, job_id, org, cwd=None):
    """Fetch the status/details of a deploy job. Returns (result_dict, error)."""
    cmd = [sf, 'project', 'deploy', 'report', '--job-id', job_id,
           '--target-org', org, '--json']
    return _run_sf(cmd, sf, 'deploy report', cwd=cwd)


def poll_deploy(sf, job_id, org, poll_interval, job_timeout, logger, cwd=None,
                on_status=None, control=None):
    """Poll a deploy job until it reaches a terminal status. Returns (result_dict, error)."""
    deadline = time.time() + job_timeout * 60
    last_status = ''
    while time.time() < deadline:
        if control and control.stop.is_set():
            return None, 'stopped by user'
        res, err = report_deploy(sf, job_id, org, cwd=cwd)
        if res is None:
            return None, err
        result = res.get('result', {})
        status = str(result.get('status', ''))
        if status and status != last_status:
            logger.log('  [job %s] status: %s' % (job_id, status))
            last_status = status
            if on_status:
                on_status(humanize_status(status))
        if status in TERMINAL_STATUSES:
            return res, None
        if control:
            # Interruptible sleep: wake early on stop.
            waited = 0.0
            while waited < poll_interval:
                if control.stop.is_set():
                    break
                time.sleep(0.2)
                waited += 0.2
        else:
            time.sleep(poll_interval)
    return None, 'deploy job %s did not reach a terminal status within %d min (last status: %s)' % (
        job_id, job_timeout, last_status or '?')


def _candidate_dirs():
    """User-agnostic directories where the Salesforce CLI may live."""
    dirs = []
    for var in ('APPDATA', 'LOCALAPPDATA', 'USERPROFILE', 'PROGRAMFILES', 'PROGRAMFILES(X86)'):
        v = os.environ.get(var)
        if v:
            dirs.append(v)
    home = os.path.expanduser('~')
    dirs.append(home)
    dirs.append(os.path.join(home, 'AppData', 'Roaming'))
    dirs.append(os.path.join(home, 'AppData', 'Local'))
    out = []
    for d in dirs:
        d = os.path.normpath(d)
        if d and d not in out:
            out.append(d)
    return out


def _node_version(node_path):
    """Return (major, minor) of a Node.js executable, or None."""
    try:
        proc = subprocess.run([node_path, '--version'], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=15)
    except Exception:
        return None
    raw = (proc.stdout or '').strip().lstrip('v')
    nums = []
    for part in raw.split('.')[:2]:
        digit = part.split('-')[0]
        nums.append(int(digit) if digit.isdigit() else 0)
    if not nums:
        return None
    return (nums[0], nums[1] if len(nums) > 1 else 0)


@functools.lru_cache(maxsize=1)
def find_node(min_version=(18, 11)):
    """Locate a Node.js executable >= min_version. The Salesforce CLI needs
    Node >= 18.11 (the `constants` export of `node:fs/promises`)."""
    import glob
    candidates = []
    for name in ('node.exe', 'node'):
        w = shutil.which(name)
        if w and w not in candidates:
            candidates.append(w)
    subpaths = [
        ('Programs', 'AutoClaw*', 'resources', 'vendor', 'node', 'node.exe'),
        ('AutoClaw*', 'resources', 'vendor', 'node', 'node.exe'),
        ('nodejs', 'node.exe'),
        ('Programs', 'nodejs', 'node.exe'),
    ]
    for base in _candidate_dirs():
        for parts in subpaths:
            for hit in glob.glob(os.path.join(base, *parts)):
                if os.path.isfile(hit) and hit not in candidates:
                    candidates.append(hit)
    for c in candidates:
        v = _node_version(c)
        if v is not None and v >= min_version:
            return c
    return None


def find_sf(default='sf'):
    """Locate the Salesforce CLI (sf.cmd/sf.exe/sfdx). Returns a path or the
    fallback string if nothing is found."""
    if default and os.path.isfile(default):
        return default

    # 1) on PATH
    for name in ('sf.cmd', 'sf.exe', 'sf.bat', 'sf', 'sfdx.cmd', 'sfdx.exe', 'sfdx'):
        found = shutil.which(name)
        if found:
            return found

    # 2) common install locations + toolchain prefixes (account-agnostic glob)
    import glob
    subpaths = [
        ('sfdx', 'client', 'bin', 'sf.cmd'),
        ('sfdx', 'client', 'bin', 'sfdx.cmd'),
        ('sf', 'client', 'bin', 'sf.cmd'),
        ('Salesforce CLI', 'bin', 'sf.exe'),
        ('Salesforce CLI', 'bin', 'sf.cmd'),
        ('AutoClaw*', 'toolchain', 'node', 'prefix', 'sf.cmd'),
        ('AutoClaw*', 'toolchain', 'node', 'prefix', 'sfdx.cmd'),
    ]
    for base in _candidate_dirs():
        for parts in subpaths:
            for hit in glob.glob(os.path.join(base, *parts)):
                if os.path.isfile(hit):
                    return hit

    return default


def dry_run(sf, manifest_path, org, poll_interval, job_timeout, logger, cwd=None,
            on_status=None, control=None):
    """Start an async dry-run and poll to completion. Returns (result_dict_or_None, error)."""
    job_id, err = start_deploy(sf, manifest_path, org, dry_run=True, cwd=cwd)
    if err:
        return None, err
    return poll_deploy(sf, job_id, org, poll_interval, job_timeout, logger,
                       cwd=cwd, on_status=on_status, control=control)


def deploy(sf, manifest_path, org, poll_interval, job_timeout, logger, cwd=None,
           on_status=None):
    """Real async deploy. Returns (ok, detail)."""
    job_id, err = start_deploy(sf, manifest_path, org, dry_run=False, cwd=cwd)
    if err:
        return False, err
    res, err = poll_deploy(sf, job_id, org, poll_interval, job_timeout, logger,
                           cwd=cwd, on_status=on_status)
    if res is None:
        return False, err or 'unparseable deploy output'
    result = res.get('result', {})
    if result.get('success') is True:
        return True, 'deployed successfully'
    return False, 'deploy failed: %s' % json.dumps({
        'status': res.get('status'),
        'message': result.get('message'),
        'errorMessage': result.get('errorMessage'),
        'numberComponentErrors': result.get('numberComponentErrors'),
    })[:800]


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
# Parallel section pruning
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


def process_section(tid, block, state, board, sf, org, poll_interval, job_timeout,
                    profile_name, api_version, worker_proj, logger, max_iter, control):
    """Prune a single section until its sub-profile dry-runs clean.
    Returns (ok, reason)."""
    manifest_path = os.path.join(worker_proj, 'manifest.xml')
    profile_path = os.path.join(worker_proj, 'force-app', 'main', 'default',
                                'profiles', '%s.profile-meta.xml' % profile_name)
    os.makedirs(os.path.dirname(profile_path), exist_ok=True)
    write_package_xml(manifest_path, {'Profile': [profile_name]}, api_version)

    with state.lock:
        initial = state.initial_counts.get(block, 0)

    guard = 0
    while True:
        guard += 1
        if guard > max_iter:
            return False, 'iteration cap %d reached' % max_iter
        if control.stop.is_set():
            return False, 'stopped by user'
        with state.lock:
            elements = list(state.groups.get(block, []))
        if not elements:
            return True, 'no entries left'
        with open(profile_path, 'wb') as f:
            f.write(build_subprofile(elements))

        board.set(tid, 'dry-run', block, 'polling', initial, len(elements))
        res, err = dry_run(sf, manifest_path, org, poll_interval, job_timeout,
                           logger, cwd=worker_proj,
                           on_status=lambda s: board.set(tid, 'dry-run', block, s,
                                                         initial, len(elements)),
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
                  initial, len(elements))
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
        state.apply_and_record(block, new_elements, removals, tid)


def worker(tid, state, queue, queue_lock, board, progress, control, sf, org,
           poll_interval, job_timeout, profile_name, api_version, worker_proj,
           logger, max_iter):
    """Consume one section at a time until the queue is empty."""
    while True:
        if control.stop.is_set():
            return
        with queue_lock:
            if not queue:
                board.set(tid, 'idle')
                return
            block = queue.popleft()
        progress.start()
        board.set(tid, 'start', block, 'queued')
        ok, reason = process_section(tid, block, state, board, sf, org, poll_interval,
                                     job_timeout, profile_name, api_version,
                                     worker_proj, logger, max_iter, control)
        state.record_section(block, ok, reason)
        progress.finish(ok)
        if control.stop.is_set():
            return
        board.set(tid, 'ok' if ok else 'fail', block,
                  reason if not ok else 'clean', 0, 0)
        logger.log('[T%d][%s] %s' % (tid, block, ('DONE: ' + reason) if ok else ('FAILED: ' + reason)))


def main():
    ap = argparse.ArgumentParser(
        prog=APP_NAME,
        description='Removes from a Salesforce Profile every reference to metadata that '
                    'does not exist in the target org. Dry-run only by default; use '
                    '--deploy-on-success to deploy the cleaned profile at the end.'
    )
    ap.add_argument('--profile-path', required=True,
                    help='path of the .profile-meta.xml file (original source)')
    ap.add_argument('--out-dir', required=True,
                    help='output directory (report, packages, logs)')
    ap.add_argument('--org', required=True,
                    help="alias or username of the org used for the dry-run")
    ap.add_argument('--profile-name', default=None,
                    help='name of the Profile (package member); default: derived from the file name')
    ap.add_argument('--project-dir', default=None,
                    help='root of an existing SFDX project. If omitted, a minimal '
                         'temporary project is created inside --out-dir and removed '
                         'at the end')
    ap.add_argument('--api-version', default='68.0',
                    help='API version of the manifest (default: 68.0)')
    ap.add_argument('--max-iter', type=int, default=5000,
                    help='iteration cap (default: 5000)')
    ap.add_argument('--sf', default='sf',
                    help='command/path of the Salesforce CLI (default: sf)')
    ap.add_argument('--poll-interval', type=float, default=5.0,
                    help='seconds to wait between status polls of a deploy job (default: 5)')
    ap.add_argument('--job-timeout', type=int, default=30,
                    help='minutes to wait for a deploy job to finish before aborting (default: 30)')
    ap.add_argument('--thread-number', type=int, default=8, metavar='X',
                    help='number of parallel section jobs, 1..64 (default: 8)')
    ap.add_argument('--fresh', action='store_true',
                    help='ignore any previous state and restart from the original')
    ap.add_argument('--quiet', action='store_true',
                    help='suppress the progressive stdout output')
    ap.add_argument('--deploy-on-success', action='store_true',
                    help='deploy the cleaned profile to the org once the pruning succeeds '
                         '(default: dry-run only, the org is never modified)')
    ap.add_argument('-V', '--version', action='version',
                    version='%s v%s - %s' % (APP_NAME, __version__, AUTHOR_NOTE))
    args = ap.parse_args()

    if not (1 <= args.thread_number <= 64):
        print('ERROR: --thread-number must be between 1 and 64 (got %d).' % args.thread_number)
        sys.exit(2)

    if not args.quiet:
        print(BANNER, flush=True)
        print('%s v%s' % (APP_NAME, __version__), flush=True)
        print(AUTHOR_NOTE, flush=True)
        print('', flush=True)

    profile_path = os.path.abspath(args.profile_path)
    out_dir = os.path.abspath(args.out_dir)

    if not os.path.isfile(profile_path):
        print('ERROR: profile not found: %s' % profile_path)
        sys.exit(2)

    sf = find_sf(args.sf)

    base = os.path.basename(profile_path)
    profile_name = args.profile_name or re.sub(r'\.profile-meta\.xml$', '', base)

    good_dir = os.path.join(out_dir, 'GOOD')
    errors_dir = os.path.join(out_dir, 'ERRORS')
    work_dir = os.path.join(out_dir, '.work')
    err_log = os.path.join(out_dir, 'errors_log.txt')
    report = os.path.join(out_dir, 'report.txt')
    run_log = os.path.join(out_dir, 'run.log')
    manifest_path = os.path.join(work_dir, 'manifest.xml')
    original_copy = os.path.join(out_dir, 'ORIGINAL_%s.profile-meta.xml' % profile_name)

    # Project root: the user's own project, or a minimal temporary micro-project.
    user_project = args.project_dir is not None
    if user_project:
        project_dir = os.path.abspath(args.project_dir)
    else:
        project_dir = os.path.join(work_dir, 'project')

    # Working copy inside the package directory of the (micro-)project.
    work_path = os.path.join(project_dir, 'force-app', 'main', 'default', 'profiles',
                             '%s.profile-meta.xml' % profile_name)

    for d in (out_dir, good_dir, errors_dir, work_dir):
        os.makedirs(d, exist_ok=True)

    # Untouched copy of the source (kept for reconstruction on resume).
    if os.path.abspath(profile_path) != os.path.abspath(original_copy):
        shutil.copyfile(profile_path, original_copy)

    # Minimal project scaffolding (a single sfdx-project.json is enough).
    if not user_project and not os.path.isfile(os.path.join(project_dir, 'sfdx-project.json')):
        write_sfdx_project(project_dir, args.api_version)

    # Internal manifest used by the dry-runs and the optional deploy.
    write_package_xml(manifest_path, {'Profile': [profile_name]}, args.api_version)

    logger = Logger(args.quiet, run_log)
    logger.log('Profile     : %s' % profile_path)
    logger.log('Name        : %s' % profile_name)
    logger.log('Org         : %s' % args.org)
    logger.log('Project     : %s' % project_dir)
    logger.log('Output      : %s' % out_dir)
    logger.log('API version : %s' % args.api_version)
    logger.log('sf          : %s' % sf)

    # ---------- already clean? ----------
    prev_outcome = read_outcome(report)
    good_profile = os.path.join(good_dir, '%s.profile-meta.xml' % profile_name)
    if (not args.fresh) and prev_outcome == 'SUCCESS' and os.path.isfile(good_profile):
        if args.deploy_on_success:
            os.makedirs(os.path.dirname(work_path), exist_ok=True)
            shutil.copyfile(good_profile, work_path)
            logger.log('Profile already clean; deploying the GOOD profile as requested...')
            ok, detail = deploy(sf, manifest_path, args.org, args.poll_interval,
                                args.job_timeout, logger, cwd=project_dir)
            remove_temp_project(work_dir, out_dir)
            print('%s: %s' % ('DEPLOY OK' if ok else 'DEPLOY FAILED', detail))
            sys.exit(0 if ok else 1)
        remove_temp_project(work_dir, out_dir)
        print('Profile "%s" is already clean (previous run: SUCCESS). Nothing to do.' % profile_name)
        sys.exit(0)

    # ---------- clean run (--fresh): clear previous outputs ----------
    if args.fresh:
        for p in (err_log, report, run_log):
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(good_profile):
            os.remove(good_profile)
        err_pkg = os.path.join(errors_dir, 'package.xml')
        if os.path.exists(err_pkg):
            os.remove(err_pkg)
        logger.log('CLEAN RUN (--fresh): outputs cleared.')

    # ---------- load original, split into per-section groups ----------
    tree = etree.parse(original_copy)
    root = tree.getroot()
    original_seq = [c for c in root if isinstance(c.tag, str)]
    groups = OrderedDict()
    for el in original_seq:
        block = el.tag.split('}')[-1]
        groups.setdefault(block, []).append(el)

    # ---------- apply recorded removals (resume) ----------
    # Capture the true original section sizes first, so the board keeps showing
    # cumulative deleted/initial across resumptions instead of resetting.
    original_counts = {block: len(els) for block, els in groups.items()}
    removed_by_type = {}
    for typ, member in load_log_entries(err_log):
        removed_by_type.setdefault(typ, set()).add(member)
        for block, child, mode in BLOCK_MAP.get(typ, []):
            g = groups.get(block)
            if g is None:
                continue
            g, _n = remove_from_list(g, block, member)
            groups[block] = g

    total_resumed = sum(len(v) for v in removed_by_type.values())
    if total_resumed:
        logger.log('RESUME: %d metadata already removed.' % total_resumed)
    else:
        logger.log('FIRST RUN.')

    # ---------- queue of prunable sections that still have entries ----------
    queue = deque()
    for block in groups:
        if block in prunable_blocks() and groups[block]:
            queue.append(block)
    logger.log('Sections to prune: %s' % (', '.join(queue) if queue else '(none)'))

    # ---------- shared state + initial outputs ----------
    good_profile_path = os.path.join(good_dir, '%s.profile-meta.xml' % profile_name)
    errors_pkg = os.path.join(errors_dir, 'package.xml')
    state = SharedState(groups, original_seq, logger, err_log, good_profile_path,
                        errors_pkg, profile_name, args.api_version)
    state.removed_by_type = removed_by_type
    state.initial_counts = original_counts
    state.seq = total_resumed  # continue the error-log sequence on resume
    with state.lock:
        state._write_outputs_locked()

    t0 = time.time()
    result_reason = ''

    if not queue:
        result_state = 'SUCCESS'
        logger.log('Nothing to prune: all sections already clean.')
    else:
        # ---------- launch workers ----------
        workers_dir = os.path.join(work_dir, 'workers')
        queue_lock = threading.Lock()
        progress = Progress(total=len(queue))
        control = Control()
        board = StatusBoard(args.thread_number, args.quiet, progress=progress, t0=t0)
        logger.console = False  # board owns the console during the parallel phase
        threads = []
        for tid in range(args.thread_number):
            wp = os.path.join(workers_dir, 'w%d' % tid)
            write_sfdx_project(wp, args.api_version)
            t = threading.Thread(
                target=worker,
                args=(tid, state, queue, queue_lock, board, progress, control,
                      sf, args.org, args.poll_interval, args.job_timeout,
                      profile_name, args.api_version, wp, logger, args.max_iter),
                daemon=True)
            t.start()
            threads.append(t)

        # ---------- join workers; Ctrl-C stops gracefully (as if 'S') ----------
        # Bounded joins: a join() without timeout blocks the main thread in a
        # native wait that never checks the Ctrl-C interrupt flag on Windows,
        # so the interrupt is only raised when the join returns. Polling with a
        # short timeout keeps KeyboardInterrupt responsive.
        stopped = False
        try:
            while any(t.is_alive() for t in threads):
                for t in threads:
                    t.join(timeout=0.5)
        except KeyboardInterrupt:
            control.stop.set()
            board.freeze()
            stopped = True

        # Wait for the workers to acknowledge the stop at their next checkpoint.
        # A second Ctrl-C while shutting down must not abort this wait, or the
        # workers would be left holding their temp dirs and cleanup would fail.
        if stopped:
            while any(t.is_alive() for t in threads):
                try:
                    for t in threads:
                        t.join(timeout=0.5)
                except KeyboardInterrupt:
                    pass

        if stopped:
            board.finalize_stopped()
            logger.console = not args.quiet
            result_state = 'ABORTED'
            result_reason = 'stopped by user'
        else:
            board.finish()
            logger.console = not args.quiet

            # ---------- outcome ----------
            failed = [(b, r) for b, (ok, r) in state.section_status.items() if not ok]
            if failed:
                result_state = 'ABORTED'
                result_reason = '; '.join('%s: %s' % (b, r) for b, r in failed)
            else:
                result_state = 'SUCCESS'

    elapsed = time.time() - t0

    # ---------- final GOOD/ERRORS ----------
    with state.lock:
        state._write_outputs_locked()
    total_removed = state.totals()

    # ---------- final full dry-run of the assembled package ----------
    final_detail = ''
    if result_state == 'SUCCESS':
        logger.log('Final dry-run of the full package...')
        final_proj = os.path.join(work_dir, 'final')
        write_sfdx_project(final_proj, args.api_version)
        fmanifest = os.path.join(final_proj, 'manifest.xml')
        write_package_xml(fmanifest, {'Profile': [profile_name]}, args.api_version)
        fprofile = os.path.join(final_proj, 'force-app', 'main', 'default', 'profiles',
                                '%s.profile-meta.xml' % profile_name)
        os.makedirs(os.path.dirname(fprofile), exist_ok=True)
        shutil.copyfile(good_profile_path, fprofile)
        res, err = dry_run(sf, fmanifest, args.org, args.poll_interval,
                           args.job_timeout, logger, cwd=final_proj)
        if res is None:
            result_state = 'ABORTED'
            result_reason = 'final dry-run failed: %s' % (err or 'unparseable output')
        else:
            r = res.get('result', {})
            if r.get('success') is True:
                final_detail = 'clean'
            else:
                cf = (r.get('details', {}) or {}).get('componentFailures') or []
                probs = '; '.join(str(f.get('problem', '')) for f in cf[:5])
                result_state = 'ABORTED'
                result_reason = 'final dry-run reported %d failure(s): %s' % (len(cf), probs)

    # ---------- optional deploy (only with --deploy-on-success) ----------
    deploy_detail = ''
    if result_state == 'SUCCESS' and args.deploy_on_success:
        logger.log('Deploying the cleaned profile to org %s...' % args.org)
        os.makedirs(os.path.dirname(work_path), exist_ok=True)
        shutil.copyfile(good_profile_path, work_path)
        ok, deploy_detail = deploy(sf, manifest_path, args.org, args.poll_interval,
                                   args.job_timeout, logger, cwd=project_dir)
        if not ok:
            result_state = 'DEPLOY_FAILED'

    # ---------- report ----------
    lines = []
    lines.append('OUTCOME: %s' % result_state)
    if result_reason:
        lines.append('REASON: %s' % result_reason)
    lines.append('METADATA REMOVED (total): %d' % total_removed)
    lines.append('TIME: %.1fs' % elapsed)
    if final_detail:
        lines.append('FINAL DRY-RUN: %s' % final_detail)
    if deploy_detail:
        lines.append('DEPLOY: %s' % deploy_detail)
    lines.append('')
    lines.append('Removed by type:')
    for t in sorted(removed_by_type):
        lines.append('  %s : %d' % (t, len(removed_by_type[t])))
    lines.append('')
    lines.append('Output:')
    lines.append('  GOOD   : %s' % good_dir)
    lines.append('  ERRORS : %s' % errors_dir)
    lines.append('  report : %s' % report)
    with open(report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

    # ---------- throw away our temporary work area ----------
    remove_temp_project(work_dir, out_dir)

    # ---------- final output ----------
    logger.log('')
    logger.log('========================================================')
    logger.log('  PROFILE PRUNING RESULT')
    logger.log('========================================================')
    for l in lines:
        logger.log(l)
    logger.log('========================================================')
    logger.log('RESULT:%s' % result_state)


if __name__ == '__main__':
    main()
