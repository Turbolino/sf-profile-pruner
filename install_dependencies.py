#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Install the Python dependencies required by Profile Pruner."""
import subprocess
import sys

REQUIREMENTS = ['lxml>=4.9']


def main():
    print('Installing Profile Pruner dependencies...')
    cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade'] + REQUIREMENTS
    rc = subprocess.call(cmd)
    if rc == 0:
        print('Dependencies installed successfully.')
    else:
        print('Dependency installation failed (exit code %d).' % rc)
        sys.exit(rc)


if __name__ == '__main__':
    main()
