# -*- coding: utf-8 -*-
"""Salesforce CLI integration: run `sf` commands, poll deploy jobs, and
discover a compatible `sf`/Node installation."""

import functools
import json
import os
import shutil
import subprocess
import threading
import time

from .constants import TERMINAL_STATUSES
from .xmlio import humanize_status


# Rate-limit `sf deploy start` submissions. The Salesforce CLI serializes deploy
# operations with a per-org lock file; many workers submitting at once trip
# "Lock file is already being held". A minimum interval between submissions
# (plus a sane minimum chunk size) keeps the lock from being hammered.
_deploy_start_lock = threading.Lock()
_last_deploy_start = 0.0
_deploy_start_interval = 1.0  # seconds


def set_deploy_start_interval(seconds):
    """Set the minimum interval (seconds) between deploy start submissions.
    Zero disables the throttle."""
    global _deploy_start_interval
    _deploy_start_interval = max(0.0, float(seconds))


def _throttle_deploy_start():
    """Block until the minimum interval since the previous deploy start has
    elapsed, then record this submission as the new last start."""
    global _last_deploy_start
    if _deploy_start_interval <= 0:
        return
    with _deploy_start_lock:
        now = time.monotonic()
        wait = _last_deploy_start + _deploy_start_interval - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        _last_deploy_start = now


def _sf_invocation(cmd, node):
    """Return the argv to actually run.

    The Salesforce CLI ships `sf.cmd`, a cmd.exe batch shim. If that batch
    shares our console, a Ctrl-C also hits its cmd.exe and raises ITS
    "Terminate batch job (Y/N)?" prompt, which blocks the worker polling it and
    deadlocks our own shutdown. So on Windows we invoke node + run.js directly
    (exactly what sf.ps1 does), bypassing the batch entirely. Falls back to the
    original argv when run.js is not found next to the shim."""
    sf = cmd[0]
    if os.name == 'nt' and node and sf.lower().endswith(('.cmd', '.bat')):
        base = os.path.dirname(os.path.abspath(sf))
        run_js = os.path.join(base, 'node_modules', '@salesforce', 'cli', 'bin', 'run.js')
        if os.path.isfile(run_js):
            return [node, '--no-deprecation', run_js] + list(cmd[1:])
    return cmd


def _terminate_proc(proc):
    """Kill a spawned sf child (and its tree on Windows). Best-effort."""
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/T', '/F', '/PID', str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _run_sf(cmd, sf, what, cwd=None, control=None):
    """Run an sf command and parse its JSON. Returns (data_or_None, error).

    When `control` is provided, the child is polled in a loop so a stop request
    terminates it promptly (otherwise a slow `sf deploy report` would block the
    worker and, in turn, the Ctrl-C shutdown)."""
    env = os.environ.copy()
    good_node = find_node()
    if good_node:
        # Prepend the compatible Node so any `node` lookup on PATH resolves to a
        # version sf supports (sf needs Node >= 18.11).
        env['PATH'] = os.path.dirname(os.path.abspath(good_node)) + os.pathsep + env.get('PATH', '')
    run_cmd = _sf_invocation(cmd, good_node)
    try:
        proc = subprocess.Popen(run_cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding='utf-8', errors='replace', env=env, cwd=cwd)
    except FileNotFoundError:
        return None, ('sf executable not found: %s. Install the Salesforce CLI '
                      '(run `sf plugins install` / `python first_config.py`), or pass --sf <path>.' % sf)
    except OSError as e:
        return None, 'failed to run %s: %s' % (what, e)

    if control is None:
        # Poll with a short timeout even without a control object: `sf org
        # display` (and any other blocking call) must be interruptible, and a
        # Ctrl-C has to kill the child tree rather than leak it. We re-raise
        # the interrupt so the top-level handler can stop cleanly.
        out, err = '', ''
        while True:
            try:
                out, err = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
            except KeyboardInterrupt:
                _terminate_proc(proc)
                raise
            except OSError:
                break
    else:
        out, err = '', ''
        while True:
            if control.stop.is_set():
                _terminate_proc(proc)
                return None, 'stopped by user'
            try:
                out, err = proc.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
            except KeyboardInterrupt:
                _terminate_proc(proc)
                raise
            except OSError:
                break
    out = (out or '').strip()
    if not out:
        return None, '%s returned no output (stderr: %s)' % (what, (err or '').strip()[:400])
    try:
        return json.loads(out), None
    except Exception as e:
        return None, 'unparseable JSON from %s (%s). stderr: %s | stdout head: %s' % (
            what, e, (err or '').strip()[:300], out[:300])


def list_metadata(sf, org, metadata_type, cwd=None, control=None):
    """List the `fullName` of every component of `metadata_type` in the org.
    Returns (list_of_names_or_None, error)."""
    cmd = [sf, 'org', 'list', 'metadata', '--metadata-type', metadata_type,
           '--target-org', org, '--json']
    res, err = _run_sf(cmd, sf, 'list metadata %s' % metadata_type,
                       cwd=cwd, control=control)
    if err:
        return None, err
    result = res.get('result')
    if not isinstance(result, list):
        return None, 'unexpected list metadata result: %s' % json.dumps(res)[:200]
    names = []
    for item in result:
        if isinstance(item, dict) and item.get('fullName'):
            names.append(str(item['fullName']))
    return names, None


def tooling_query(sf, org, soql, cwd=None, control=None):
    """Run a Tooling API SOQL query and return its records.
    Returns (list_of_record_dicts_or_None, error).

    `sf data query` already follows pagination for Tooling objects that support
    it (TabDefinition, FieldDefinition, ...). For objects that reject queryMore
    (EntityDefinition) the caller must keep the result within a single batch via
    a WHERE IN filter; a truncated single batch is still returned as-is."""
    cmd = [sf, 'data', 'query', '--query', soql, '--use-tooling-api',
           '--target-org', org, '--json']
    res, err = _run_sf(cmd, sf, 'tooling query', cwd=cwd, control=control)
    if err:
        return None, err
    result = res.get('result', {})
    records = result.get('records')
    if not isinstance(records, list):
        return None, 'unexpected tooling query result: %s' % json.dumps(res)[:200]
    return records, None


def org_api_version(sf, org, cwd=None, control=None):
    """Return the org's API version (the latest it supports) via `sf org display`.
    Returns (version_string_or_None, error)."""
    cmd = [sf, 'org', 'display', '--target-org', org, '--json']
    res, err = _run_sf(cmd, sf, 'org display', cwd=cwd, control=control)
    if err:
        return None, err
    result = res.get('result', {})
    v = result.get('apiVersion')
    if not v:
        return None, 'org display returned no apiVersion'
    return str(v), None


def org_username(sf, org, cwd=None, control=None):
    """Return the resolved username (not the alias) for an org via `sf org display`.
    Returns (username_or_None, error)."""
    cmd = [sf, 'org', 'display', '--target-org', org, '--json']
    res, err = _run_sf(cmd, sf, 'org display', cwd=cwd, control=control)
    if err:
        return None, err
    u = (res.get('result') or {}).get('username')
    if not u:
        return None, 'org display returned no username'
    return str(u), None


def start_deploy(sf, manifest_path, org, dry_run=False, cwd=None, control=None):
    """Start an async deploy. Returns (job_id, error)."""
    _throttle_deploy_start()
    cmd = [sf, 'project', 'deploy', 'start', '--async']
    if dry_run:
        cmd.append('--dry-run')
    cmd += ['--manifest', manifest_path, '--target-org', org, '--json']
    res, err = _run_sf(cmd, sf, 'deploy start', cwd=cwd, control=control)
    if err:
        return None, err
    result = res.get('result', {})
    job_id = result.get('id') or result.get('jobId')
    if not job_id:
        return None, 'deploy start returned no job id: %s' % json.dumps(res)[:400]
    return job_id, None


def report_deploy(sf, job_id, org, cwd=None, control=None):
    """Fetch the status/details of a deploy job. Returns (result_dict, error)."""
    cmd = [sf, 'project', 'deploy', 'report', '--job-id', job_id,
           '--target-org', org, '--json']
    return _run_sf(cmd, sf, 'deploy report', cwd=cwd, control=control)


def poll_deploy(sf, job_id, org, poll_interval, job_timeout, logger, cwd=None,
                on_status=None, control=None):
    """Poll a deploy job until it reaches a terminal status. Returns (result_dict, error)."""
    deadline = time.time() + job_timeout * 60
    last_status = ''
    while time.time() < deadline:
        if control and control.stop.is_set():
            return None, 'stopped by user'
        res, err = report_deploy(sf, job_id, org, cwd=cwd, control=control)
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


def dry_run(sf, manifest_path, org, poll_interval, job_timeout, logger, cwd=None,
            on_status=None, control=None):
    """Start an async dry-run and poll to completion. Returns (result_dict_or_None, error)."""
    job_id, err = start_deploy(sf, manifest_path, org, dry_run=True, cwd=cwd,
                               control=control)
    if err:
        return None, err
    return poll_deploy(sf, job_id, org, poll_interval, job_timeout, logger,
                       cwd=cwd, on_status=on_status, control=control)


def deploy(sf, manifest_path, org, poll_interval, job_timeout, logger, cwd=None,
           on_status=None, control=None):
    """Real async deploy. Returns (ok, detail)."""
    job_id, err = start_deploy(sf, manifest_path, org, dry_run=False, cwd=cwd,
                               control=control)
    if err:
        return False, err
    res, err = poll_deploy(sf, job_id, org, poll_interval, job_timeout, logger,
                           cwd=cwd, on_status=on_status, control=control)
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


@functools.lru_cache(maxsize=1)
def find_sf_node_modules():
    """Locate the directory holding the Salesforce CLI's global node_modules
    (with @salesforce/core and @jsforce/jsforce-node). The readMetadata helper
    needs both to authenticate against an org. Returns a path or None."""
    candidates = []
    for var in ('SF_HOME', 'SF_DATA_DIR', 'SFXDX_HOME'):
        v = os.environ.get(var)
        if v:
            candidates.append(os.path.join(v, 'node_modules'))
    home = os.path.expanduser('~')
    candidates.append(os.path.join(home, '.local', 'share', 'sf', 'node_modules'))
    for var in ('LOCALAPPDATA', 'APPDATA'):
        v = os.environ.get(var)
        if v:
            candidates.append(os.path.join(v, 'sf', 'node_modules'))
    seen = set()
    for c in candidates:
        c = os.path.normpath(c)
        if not c or c in seen:
            continue
        seen.add(c)
        if (os.path.isfile(os.path.join(c, '@salesforce', 'core', 'package.json'))
                and os.path.isdir(os.path.join(c, '@jsforce', 'jsforce-node'))):
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
