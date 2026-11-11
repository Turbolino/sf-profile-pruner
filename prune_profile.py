#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
prune_profile.py

Removes from a Salesforce Profile every reference to metadata that does NOT
exist in the target org, using ONLY `sf project deploy start --dry-run`
(never modifying the environment).

Algorithm (per iteration):
  1. deploy dry-run of the profile
  2. remove the XML block that caused the error
  3. refresh the cumulative package.xml with the errored metadata
until the dry-run succeeds.

RESUME BEHAVIOUR
  - Default: RESUMES from where it left off (if errors_log.txt and the
    working copy exist). If there are no more errors, it removes nothing and
    leaves GOOD/ and ERRORS/ untouched.
  - With --fresh: restarts from scratch, restoring the original and clearing
    the outputs.

OUTPUT (in --out-dir)
  ORIGINAL_<name>.profile-meta.xml   untouched copy of the original
  GOOD/package.xml + GOOD/profiles/   clean, deployable profile
  ERRORS/package.xml                  cumulative package of missing metadata
  errors_log.txt                      line-by-line log (resumable)
  run.log                             progressive log copy
  report.txt                          final outcome
  .work/manifest.xml                  internal manifest (not a deliverable)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

from lxml import etree

__version__ = '1.0.0'
__author__ = "Lorenzo D'Aleo"
__email__ = 'turbolino.dev@proton.me'
AUTHOR_NOTE = "made with <3 by %s (%s)" % (__author__, __email__)

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
    ],
    'StandardObject':     [('objectPermissions', 'object', 'exact')],
}


def timestamp():
    return datetime.now().strftime('%H:%M:%S')


class Logger:
    def __init__(self, quiet, log_path=None):
        self.quiet = quiet
        self.log_path = log_path

    def log(self, msg):
        line = '[%s] %s' % (timestamp(), msg)
        if not self.quiet:
            print(line, flush=True)
        if self.log_path:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(line + '\n')


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


def dry_run(sf, manifest_path, org):
    cmd = [sf, 'project', 'deploy', 'start', '--dry-run',
           '--manifest', manifest_path, '--target-org', org, '--json']
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    try:
        return json.loads(proc.stdout)
    except Exception:
        return None


def find_sf(default='sf'):
    """Returns an `sf` executable launchable from CreateProcess (sf.cmd/sf.exe)."""
    if default and os.path.isfile(default):
        return default
    for name in ('sf.cmd', 'sf.exe', 'sf.bat', 'sf'):
        found = shutil.which(name)
        if found:
            return found
    return default


def main():
    ap = argparse.ArgumentParser(
        prog=APP_NAME,
        description='Removes from a Salesforce Profile every reference to metadata that '
                    'does not exist in the target org (dry-run only, the org is never modified).'
    )
    ap.add_argument('--profile-path', required=True,
                    help='path of the .profile-meta.xml file (original source)')
    ap.add_argument('--out-dir', required=True,
                    help='output directory (report, packages, logs)')
    ap.add_argument('--org', required=True,
                    help="alias or username of the org used for the dry-run")
    ap.add_argument('--profile-name', default=None,
                    help='name of the Profile (package member); default: derived from the file name')
    ap.add_argument('--project-dir', default=os.path.dirname(os.path.abspath(__file__)),
                    help='root of the SFDX project (default: directory of this script)')
    ap.add_argument('--api-version', default='68.0',
                    help='API version of the manifest (default: 68.0)')
    ap.add_argument('--max-iter', type=int, default=5000,
                    help='iteration cap (default: 5000)')
    ap.add_argument('--sf', default='sf',
                    help='command/path of the Salesforce CLI (default: sf)')
    ap.add_argument('--fresh', action='store_true',
                    help='ignore any previous state and restart from the original')
    ap.add_argument('--quiet', action='store_true',
                    help='suppress the progressive stdout output')
    ap.add_argument('-V', '--version', action='version',
                    version='%s v%s - %s' % (APP_NAME, __version__, AUTHOR_NOTE))
    args = ap.parse_args()

    if not args.quiet:
        print(BANNER, flush=True)
        print('%s v%s' % (APP_NAME, __version__), flush=True)
        print(AUTHOR_NOTE, flush=True)
        print('', flush=True)

    project_dir = os.path.abspath(args.project_dir)
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

    # Working copy inside the package directory of the project.
    work_path = os.path.join(project_dir, 'force-app', 'main', 'default', 'profiles',
                             '%s.profile-meta.xml' % profile_name)

    for d in (out_dir, good_dir, errors_dir, work_dir):
        os.makedirs(d, exist_ok=True)

    logger = Logger(args.quiet, run_log)
    logger.log('Profile     : %s' % profile_path)
    logger.log('Name        : %s' % profile_name)
    logger.log('Org         : %s' % args.org)
    logger.log('Project     : %s' % project_dir)
    logger.log('Output      : %s' % out_dir)
    logger.log('API version : %s' % args.api_version)
    logger.log('sf          : %s' % sf)

    # ---------- RESUME vs CLEAN RUN ----------
    def _log_lines():
        try:
            with open(err_log, encoding='utf-8', errors='replace') as f:
                return sum(1 for l in f if l.strip())
        except OSError:
            return 0

    has_log = os.path.isfile(err_log) and _log_lines() > 0

    error_members = {}   # type -> set(member)
    start_iter = 0

    if args.fresh:
        # --- clean run: restore the original and clear the outputs ---
        if os.path.abspath(profile_path) != os.path.abspath(original_copy):
            shutil.copyfile(profile_path, original_copy)
        os.makedirs(os.path.dirname(work_path), exist_ok=True)
        if os.path.abspath(profile_path) != os.path.abspath(work_path):
            shutil.copyfile(profile_path, work_path)
        os.chmod(work_path, 0o666)
        for p in (err_log, report, run_log):
            if os.path.exists(p):
                os.remove(p)
        good_profiles = os.path.join(good_dir, 'profiles')
        if os.path.isdir(good_profiles):
            shutil.rmtree(good_profiles, ignore_errors=True)
        good_pkg = os.path.join(good_dir, 'package.xml')
        if os.path.exists(good_pkg):
            os.remove(good_pkg)
        err_pkg = os.path.join(errors_dir, 'package.xml')
        if os.path.exists(err_pkg):
            os.remove(err_pkg)
        logger.log('CLEAN RUN (--fresh): original restored and outputs cleared.')

    elif has_log and os.path.isfile(work_path):
        # --- resume ---
        for line in open(err_log, encoding='utf-8', errors='replace'):
            line = line.strip()
            if not line:
                continue
            parts = line.split('|')
            if len(parts) >= 3:
                t, m = parts[1], parts[2]
                error_members.setdefault(t, set()).add(m)
            start_iter += 1
        logger.log('RESUME: %d metadata already removed, continuing from the next error.' % start_iter)

    elif has_log:
        # Log present but the working copy is missing: inconsistent state, do not destroy anything.
        print('ERROR: errors_log.txt exists but the working copy is missing:')
        print('  %s' % work_path)
        print('Pass --project-dir pointing to the correct SFDX project root, or use --fresh to restart.')
        sys.exit(3)

    else:
        # --- first run: no previous state ---
        if os.path.abspath(profile_path) != os.path.abspath(original_copy):
            shutil.copyfile(profile_path, original_copy)
        os.makedirs(os.path.dirname(work_path), exist_ok=True)
        if os.path.abspath(profile_path) != os.path.abspath(work_path):
            shutil.copyfile(profile_path, work_path)
        os.chmod(work_path, 0o666)
        logger.log('FIRST RUN: original copied.')

    # ---------- internal manifest ----------
    write_package_xml(manifest_path, {'Profile': [profile_name]}, args.api_version)

    os.chdir(project_dir)
    t0 = time.time()

    result_state = 'ABORTED'
    result_reason = ''
    iterations = start_iter
    removed_this_run = 0

    for i in range(start_iter + 1, start_iter + args.max_iter + 1):
        iterations = i
        logger.log('[ITER %d] dry-run in progress...' % i)

        res = dry_run(sf, manifest_path, args.org)
        if res is None:
            result_reason = 'deploy returned unparseable output'
            break

        result = res.get('result', {})
        if result.get('success') is True:
            result_state = 'SUCCESS'
            break

        details = result.get('details', {})
        failures = details.get('componentFailures') or []

        # Async edge case: the CLI can return the deploy job before the
        # validation details are populated, so `details` is empty although
        # errors actually exist. Retry a few times instead of aborting.
        attempts = 0
        while not failures and attempts < 3:
            attempts += 1
            logger.log('[ITER %d] no componentFailures yet, retrying dry-run (%d/3)...' % (i, attempts))
            time.sleep(5)
            retry_res = dry_run(sf, manifest_path, args.org)
            if retry_res is None:
                continue
            res = retry_res
            result = res.get('result', {})
            if result.get('success') is True:
                result_state = 'SUCCESS'
                break
            details = result.get('details', {})
            failures = details.get('componentFailures') or []

        if result_state == 'SUCCESS':
            break

        if not failures:
            # Full diagnostics: avoid aborting with {} and no explanation.
            diag = {
                'status': res.get('status'),
                'message': result.get('message'),
                'errorMessage': result.get('errorMessage'),
                'numberComponentErrors': result.get('numberComponentErrors'),
                'details': details,
            }
            result_reason = 'deploy failed without componentFailures after retries: %s' % json.dumps(diag)[:1000]
            break

        problem = str(failures[0].get('problem', ''))
        logger.log('[ITER %d] ERROR: %s' % (i, problem))

        rr = resolve_error(problem)
        if rr is None:
            result_reason = 'unmappable error: %s' % problem
            break

        what, member = rr
        candidates = BLOCK_MAP.get(what)
        if candidates is None:
            result_reason = 'unmappable type (%s): %s' % (what, problem)
            break

        removed = remove_block(work_path, member, candidates)
        error_members.setdefault(what, set()).add(member)
        removed_this_run += removed

        write_package_xml(os.path.join(errors_dir, 'package.xml'), error_members, args.api_version)

        with open(err_log, 'a', encoding='utf-8') as f:
            f.write('%d|%s|%s|%s\n' % (i, what, member, problem))

        logger.log('[ITER %d] removed %s \'%s\' (%d node(s))  [elapsed %.0fs]'
                   % (i, what, member, removed, time.time() - t0))

    elapsed = time.time() - t0

    # ---------- strip blank lines from the working profile ----------
    strip_blank_lines(work_path)

    # ---------- GOOD ----------
    good_profiles = os.path.join(good_dir, 'profiles')
    os.makedirs(good_profiles, exist_ok=True)
    shutil.copyfile(work_path, os.path.join(good_profiles, '%s.profile-meta.xml' % profile_name))
    write_package_xml(os.path.join(good_dir, 'package.xml'), {'Profile': [profile_name]}, args.api_version)

    # ---------- ERRORS (cumulative, always refreshed) ----------
    write_package_xml(os.path.join(errors_dir, 'package.xml'), error_members, args.api_version)

    # ---------- report ----------
    lines = []
    lines.append('OUTCOME: %s' % result_state)
    if result_reason:
        lines.append('REASON: %s' % result_reason)
    lines.append('ITERATIONS: %d' % iterations)
    lines.append('METADATA REMOVED (total): %d' % sum(len(v) for v in error_members.values()))
    lines.append('NODES REMOVED (this run): %d' % removed_this_run)
    lines.append('TIME: %.1fs' % elapsed)
    lines.append('')
    lines.append('Removed by type:')
    for t in sorted(error_members):
        lines.append('  %s : %d' % (t, len(error_members[t])))
    lines.append('')
    lines.append('Output:')
    lines.append('  GOOD   : %s' % good_dir)
    lines.append('  ERRORS : %s' % errors_dir)
    lines.append('  report : %s' % report)
    with open(report, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')

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
