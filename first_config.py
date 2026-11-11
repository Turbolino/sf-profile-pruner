#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
first_config - one-time setup for Profile Pruner.

Installs the package (dependencies + the `sfpp` command), checks Node.js and the
Salesforce CLI, and puts `sfpp` on the PATH.

Run once, from the project directory:
    python first_config.py
Then open a NEW terminal and use:
    sfpp --help

The `sfpp` command is a real console-script executable (created by pip), NOT a
.cmd batch file, so Ctrl-C reaches the program directly and never shows
cmd.exe's "Terminate batch job (Y/N)?" prompt.
"""
import glob
import os
import shutil
import subprocess
import sys
import sysconfig


def run(python, *args):
    return subprocess.call([python] + list(args))


def scripts_dirs(python):
    """Candidate directories where pip may have put the `sfpp` console script."""
    dirs = []
    s = sysconfig.get_path('scripts')
    if s:
        dirs.append(s)
    # Some managed/vendor Python installs redirect pip to a different
    # interpreter; scan the interpreter tree for a Scripts folder as a fallback.
    root = os.path.dirname(os.path.dirname(python))
    for hit in glob.glob(os.path.join(root, '**', 'Scripts'), recursive=True):
        if hit not in dirs:
            dirs.append(hit)
    return dirs


def find_sfpp(python):
    """Return the absolute path of the installed `sfpp` executable, or None."""
    for d in scripts_dirs(python):
        for name in ('sfpp.exe', 'sfpp'):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def add_to_path(dirpath, remove_old=('ProfilePruner',)):
    """Append dirpath to the user PATH (registry on Windows), without
    duplicates, and drop entries that point at the old launcher folder."""
    if os.name == 'nt':
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment',
                             0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        try:
            path, _ = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            path = ''
        entries = [p for p in path.split(';') if p]
        entries = [p for p in entries if not any(r in p for r in remove_old)]
        if dirpath not in entries:
            entries.append(dirpath)
        winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, ';'.join(entries))
        winreg.CloseKey(key)
    else:
        profile = os.path.expanduser('~/.profile')
        line = 'export PATH="%s:$PATH"' % dirpath
        if os.path.isfile(profile):
            with open(profile, encoding='utf-8') as f:
                content = f.read()
            if line not in content:
                with open(profile, 'a', encoding='utf-8') as f:
                    f.write('\n' + line + '\n')
        print('Add this line to your shell profile:')
        print('  ' + line)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    python = sys.executable

    print('Installing Profile Pruner (package + sfpp command)...')
    rc = run(python, '-m', 'pip', 'install', '-e', here)
    if rc != 0:
        print('ERROR: pip install failed (exit code %d).' % rc)
        sys.exit(rc)

    exe = find_sfpp(python)
    if exe is None:
        print('WARNING: could not locate the `sfpp` script. It may be under a')
        print('different interpreter. Run `pip show -f sf-profile-pruner` to find it.')
    else:
        add_to_path(os.path.dirname(exe))
        print('sfpp installed at: %s' % exe)
        print('Added its folder to PATH.')

    # Runtime checks (non-fatal: the pruner also auto-discovers these).
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'profile_pruner', os.path.join(here, 'prune_profile.py'))
    pp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pp)
    print('')
    node = pp.find_node((18, 11))
    print('  Node.js      : %s' % (node if node else 'MISSING (need >= 18.11)'))
    sf = pp.find_sf('sf')
    print('  Salesforce   : %s' % (sf if os.path.isfile(sf) else 'MISSING'))

    print('')
    print('Open a NEW terminal and run:  sfpp --help')


if __name__ == '__main__':
    main()
