# -*- coding: utf-8 -*-
"""Retrieve a complete Salesforce Profile via the Metadata API readMetadata().

The Salesforce CLI's `project retrieve` (and the raw SOAP MDAPI retrieve) return
a *sparse* Profile — only the sections that match the local project. The only
way to get the complete profile (every fieldPermission, tabVisibility, object
permission, layout assignment, …) is the CRUD `readMetadata('Profile', …)`
call. This module invokes a small owned Node helper (which reuses the CLI's
authentication through @salesforce/core + jsforce) and serialises the returned
object back to Profile source XML with lxml.

This is an independent re-implementation of a documented Salesforce API call:
it does not copy or depend on any third-party plugin.
"""

import json
import os
import subprocess

from lxml import etree

from .sfcli import find_node, find_sf_node_modules

METADATA_NS = 'http://soap.sforce.com/2006/04/metadata'

# Org-specific, non-portable Profile sections. Stripped by default so the
# retrieved profile moves cleanly between sandbox and production.
CLEAN_SECTIONS = frozenset({'loginIpRanges', 'loginHours', 'userLicense'})

# Keys that jsforce adds to SOAP results but that are not part of the Profile
# XML schema (fullName is metadata, `$`/`type` are jsforce housekeeping).
_SKIP_KEYS = frozenset({'fullName', '$', 'type'})


def _helper_path():
    """Path of the bundled Node helper (shipped as package data)."""
    return os.path.join(os.path.dirname(__file__), 'node', 'retrieve_profile.cjs')


def _scalar_text(value):
    """Render a scalar (bool/str/int) as its XML text value."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return ''
    return str(value)


def _sub(parent, name):
    """Create a child element in the Metadata API namespace.

    A plain `SubElement(parent, name)` would leave the child in *no* namespace
    inside the lxml tree (even though the root declares the default namespace),
    which breaks both the `--clean` filtering and namespace-correct round-trips.
    Tagging children explicitly keeps the in-memory tree consistent.
    """
    return etree.SubElement(parent, '{%s}%s' % (METADATA_NS, name))


def _build(parent, obj):
    """Recursively append Profile XML children for a readMetadata object.

    Order is preserved: jsforce returns object keys in WSDL order, so the XML
    matches the natural Profile section/field order without a hand-written map.
    New Profile sections added by Salesforce flow through automatically.
    """
    if isinstance(obj, list):
        for item in obj:
            _build(parent, item)
        return
    for key, value in obj.items():
        if key in _SKIP_KEYS:
            continue
        if isinstance(value, list):
            for item in value:
                el = _sub(parent, key)
                if isinstance(item, dict):
                    _build(el, item)
                else:
                    el.text = _scalar_text(item)
        elif isinstance(value, dict):
            el = _sub(parent, key)
            _build(el, value)
        else:
            el = _sub(parent, key)
            el.text = _scalar_text(value)


def profile_json_to_xml(data, clean=True):
    """Serialise a readMetadata Profile object to source-format XML (bytes)."""
    root = etree.Element('Profile', nsmap={None: METADATA_NS})
    _build(root, data)
    if clean:
        for section in CLEAN_SECTIONS:
            for el in root.findall('{%s}%s' % (METADATA_NS, section)):
                root.remove(el)
    return etree.tostring(root, encoding='UTF-8', xml_declaration=True,
                          pretty_print=True)


def retrieve_profiles(username, profile_names, clean=True,
                      sf_node_modules=None, node=None, timeout=300):
    """Retrieve complete profiles via readMetadata().

    Returns (profiles, error) where `profiles` is a list of
    ``(fullName, xml_bytes)`` and `error` is None on full success, otherwise a
    human-readable string (possibly alongside a partial `profiles` list).
    """
    helper = _helper_path()
    if not os.path.isfile(helper):
        return None, 'retrieve helper not found: %s' % helper

    node = node or find_node()
    if not node:
        return None, 'no compatible Node.js installation found'

    env = os.environ.copy()
    nm = sf_node_modules or find_sf_node_modules()
    if not nm:
        return None, ('could not locate the Salesforce CLI node_modules '
                      '(@salesforce/core + @jsforce/jsforce-node)')
    existing = env.get('NODE_PATH', '')
    env['NODE_PATH'] = nm + (os.pathsep + existing if existing else '')

    cmd = [node, helper, '--username', username]
    for name in profile_names:
        cmd += ['--profile', name]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding='utf-8', errors='replace',
                              env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, 'retrieve timed out after %ds' % timeout
    except OSError as e:
        return None, 'failed to run the retrieve helper: %s' % e

    if proc.returncode != 0:
        return None, 'retrieve helper failed: %s' % (proc.stderr or '').strip()[:400]

    out = (proc.stdout or '').strip()
    if not out:
        return None, 'retrieve helper returned no output'

    try:
        payload = json.loads(out)
    except Exception as e:
        return None, 'unparseable retrieve helper JSON (%s): %s' % (e, out[:300])

    profiles = []
    for p in payload.get('profiles', []):
        xml = profile_json_to_xml(p['data'], clean=clean)
        profiles.append((p['fullName'], xml))

    errs = payload.get('errors', [])
    if errs and not profiles:
        return None, 'retrieve failed: %s' % '; '.join(
            e.get('error', '') for e in errs[:3])
    err_text = None
    if errs:
        err_text = '; '.join(e.get('error', '') for e in errs)
    return profiles, err_text
