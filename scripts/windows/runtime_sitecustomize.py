"""Bundled Python import paths, independent of machine-wide long-path policy.

Installed version directories include a content hash. Deep package filenames can
therefore exceed MAX_PATH even when the user's chosen directory is reasonable.
Only normalize existing absolute local import paths; do not add search locations,
change data paths, alter Windows policy, or touch credentials.
"""
import os
import sys


def configure(system=sys, paths=os.path):
    if system.platform != 'win32':
        return
    system.path[:] = [
        '\\\\?\\' + paths.abspath(path).replace('/', '\\')
        if path and paths.isabs(path) and paths.splitdrive(path)[0]
        and not path.replace('/', '\\').startswith('\\\\') else path
        for path in system.path
    ]


if __name__ == 'sitecustomize':
    configure()
