#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Install and verify the dependencies required by Profile Pruner.

Installs the Python packages via pip, then checks the two external runtime
dependencies (Node.js and the Salesforce CLI) and reports where they are.
"""
import os
import subprocess
import sys

REQUIREMENTS = ['lxml>=4.9']

NODE_MIN = (18, 11)


def load_profile_pruner():
    """Import the installed/local package (the repo root is on sys.path when
    this script is run from the project directory)."""
    import prune_profile
    return prune_profile


def check_runtime(pp):
    """Report the state of Node.js and the Salesforce CLI. Returns ok(bool)."""
    print('')
    print('Checking runtime dependencies...')

    ok = True

    node = pp.find_node(NODE_MIN)
    if node:
        print('  Node.js      : OK  (%s)' % node)
    else:
        ok = False
        print('  Node.js      : MISSING  (need >= 18.11; install Node.js LTS '
              'or point PATH to a newer node.exe)')

    sf = pp.find_sf('sf')
    if os.path.isfile(sf):
        print('  Salesforce   : OK  (%s)' % sf)
    else:
        ok = False
        print('  Salesforce   : MISSING  (install `sf` from developer.salesforce.com '
              'or pass --sf <path>)')

    return ok


def main():
    print('Installing Profile Pruner dependencies...')
    cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade'] + REQUIREMENTS
    rc = subprocess.call(cmd)
    if rc != 0:
        print('Dependency installation failed (exit code %d).' % rc)
        sys.exit(rc)

    pp = load_profile_pruner()
    runtime_ok = check_runtime(pp)
    print('')
    if runtime_ok:
        print('All dependencies satisfied.')
    else:
        print('One or more runtime dependencies are missing (see above). '
              'Profile Pruner will keep looking for them at run time.')


if __name__ == '__main__':
    main()
