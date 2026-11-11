# -*- coding: utf-8 -*-
"""Unit tests for Profile Pruner (pure functions, no network/org required)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prune_profile as pp  # noqa: E402

SYNTH = ('<?xml version="1.0" encoding="UTF-8"?>\n'
         '<Profile xmlns="http://soap.sforce.com/2006/04/metadata">\n'
         '    <tabVisibilities><tab>standard-NotARealTab</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
         '    <fieldPermissions><field>Missing__c.Foo__c</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
         '    <classAccesses><apexClass>NoSuchClass</apexClass><enabled>true</enabled></classAccesses>\n'
         '    <objectPermissions><object>KeepMe__c</object><allowRead>true</allowRead></objectPermissions>\n'
         '</Profile>\n')


def write_text(path, text):
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)


def read_text(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def tag_names(path):
    tree = pp.etree.parse(path)
    return [c.tag.split('}')[1] for c in tree.getroot()]


class TestSfdxProject(unittest.TestCase):
    def test_minimal_project(self):
        d = tempfile.mkdtemp()
        try:
            pp.write_sfdx_project(d, '68.0')
            self.assertTrue(os.path.isfile(os.path.join(d, 'sfdx-project.json')))
            data = json.loads(read_text(os.path.join(d, 'sfdx-project.json')))
            self.assertEqual(data['packageDirectories'], [{'path': 'force-app', 'default': True}])
            self.assertEqual(data['sourceApiVersion'], '68.0')
            self.assertEqual(sorted(os.listdir(d)), ['sfdx-project.json'])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestBatchRemoval(unittest.TestCase):
    def test_apply_removals_batch(self):
        d = tempfile.mkdtemp()
        try:
            syn = os.path.join(d, 'Synth.profile-meta.xml')
            write_text(syn, SYNTH)
            removals = [
                ('NotARealTab', pp.BLOCK_MAP['CustomTab']),
                ('Missing__c.Foo__c', pp.BLOCK_MAP['CustomField']),
                ('NoSuchClass', pp.BLOCK_MAP['ApexClass']),
            ]
            n = pp.apply_removals_batch(syn, removals)
            self.assertEqual(n, 3)
            names = tag_names(syn)
            for gone in ('tabVisibilities', 'fieldPermissions', 'classAccesses'):
                self.assertNotIn(gone, names)
            self.assertIn('objectPermissions', names)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestReconstruction(unittest.TestCase):
    def test_prepare_work_copy_reapplies_log(self):
        d = tempfile.mkdtemp()
        try:
            errlog = os.path.join(d, 'errors_log.txt')
            write_text(errlog,
                       "1|CustomTab|NotARealTab|You can't edit tab settings for NotARealTab, as it's not a valid tab.\n"
                       '1|CustomField|Missing__c.Foo__c|In field: field - no CustomField named Missing__c.Foo__c found\n'
                       '1|ApexClass|NoSuchClass|In field: apexClass - no ApexClass named NoSuchClass found\n')
            src = os.path.join(d, 'Synth.profile-meta.xml')
            write_text(src, SYNTH)
            work = os.path.join(d, 'force-app', 'main', 'default', 'profiles', 'Synth.profile-meta.xml')
            pp.prepare_work_copy(work, src, errlog, fresh=False)
            names = tag_names(work)
            for gone in ('tabVisibilities', 'fieldPermissions', 'classAccesses'):
                self.assertNotIn(gone, names)
            self.assertIn('objectPermissions', names)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestLogEntries(unittest.TestCase):
    def test_order_and_dedup(self):
        d = tempfile.mkdtemp()
        try:
            errlog = os.path.join(d, 'errors_log.txt')
            write_text(errlog,
                       '1|CustomTab|A|p\n'
                       '2|CustomField|B|p\n'
                       '3|CustomTab|A|p\n'
                       '4|ApexClass|C|p\n')
            self.assertEqual(pp.load_log_entries(errlog),
                             [('CustomTab', 'A'), ('CustomField', 'B'), ('ApexClass', 'C')])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestResolveError(unittest.TestCase):
    def test_tab_error(self):
        self.assertEqual(pp.resolve_error("You can't edit tab settings for JobProfile, as it's not a valid tab."),
                         ('CustomTab', 'JobProfile'))

    def test_field_error(self):
        self.assertEqual(pp.resolve_error('In field: field - no CustomField named Missing__c.Foo__c found'),
                         ('CustomField', 'Missing__c.Foo__c'))

    def test_unknown(self):
        self.assertIsNone(pp.resolve_error('totally different error text'))


class TestPackageXml(unittest.TestCase):
    def test_no_blank_lines(self):
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, 'package.xml')
            pp.write_package_xml(p, {'Profile': ['My Profile']}, '68.0')
            lines = read_text(p).splitlines()
            self.assertNotIn('', lines)
            self.assertEqual(lines[0], '<?xml version="1.0" encoding="UTF-8"?>')
            # exactly one <name>Profile</name>
            self.assertEqual(lines.count('        <name>Profile</name>'), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestFindSf(unittest.TestCase):
    def test_returns_path_or_fallback(self):
        r = pp.find_sf('sf')
        # either a real file, or the fallback string
        self.assertTrue(os.path.isfile(r) or r == 'sf', r)


class TestFindNode(unittest.TestCase):
    def test_version_none_for_missing(self):
        self.assertIsNone(pp._node_version(r'C:\__missing__\node.exe'))

    def test_find_node_compatible_or_none(self):
        n = pp.find_node()
        if n is None:
            return
        self.assertTrue(os.path.isfile(n), n)
        v = pp._node_version(n)
        self.assertIsNotNone(v)
        self.assertGreaterEqual(v, (18, 11))


if __name__ == '__main__':
    unittest.main(verbosity=2)
