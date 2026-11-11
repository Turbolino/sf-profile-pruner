#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
first_config - one-time setup for Profile Pruner.

Installs the dependencies and makes `prune_profile.py` callable as `sfpp`
from any terminal.

Run once:
    python first_config.py
Then open a NEW terminal and use:
    sfpp --help
"""
import os
import subprocess
import sys

REQUIREMENTS = ['lxml>=4.9']


def install_dependencies(python):
    print('Installing dependencies (lxml)...')
    return subprocess.call([python, '-m', 'pip', 'install', '--upgrade'] + REQUIREMENTS)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, 'prune_profile.py')
    python = sys.executable

    if not os.path.isfile(script):
        print('ERROR: prune_profile.py not found next to this script.')
        sys.exit(2)

    rc = install_dependencies(python)
    if rc != 0:
        print('ERROR: dependency installation failed (exit code %d).' % rc)
        sys.exit(rc)

    if os.name == 'nt':
        bin_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')),
                               'ProfilePruner', 'bin')
        os.makedirs(bin_dir, exist_ok=True)
        wrapper = os.path.join(bin_dir, 'sfpp.cmd')
        with open(wrapper, 'w', encoding='utf-8', newline='\r\n') as f:
            f.write('@echo off\r\n')
            f.write('"%s" "%s" %%*\r\n' % (python, script))
        # Add bin_dir to the user PATH (registry), without duplicates.
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment',
                             0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        try:
            path, _ = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            path = ''
        entries = [p for p in path.split(';') if p]
        if bin_dir not in entries:
            entries.append(bin_dir)
            winreg.SetValueEx(key, 'Path', 0, winreg.REG_EXPAND_SZ, ';'.join(entries))
        winreg.CloseKey(key)
    else:
        bin_dir = os.path.join(os.path.expanduser('~'), '.local', 'bin')
        os.makedirs(bin_dir, exist_ok=True)
        wrapper = os.path.join(bin_dir, 'sfpp')
        with open(wrapper, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n')
            f.write('exec "%s" "%s" "$@"\n' % (python, script))
        os.chmod(wrapper, 0o755)

    print('')
    print('Created launcher: %s' % wrapper)
    print('Added to PATH:    %s' % bin_dir)
    print('')
    print('Open a NEW terminal and run:  sfpp --help')


if __name__ == '__main__':
    main()
