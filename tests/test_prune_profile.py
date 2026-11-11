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

    def test_custom_setting_error(self):
        self.assertEqual(
            pp.resolve_error('In field: customSetting - no CustomObject named genui__GenCanvas_Input_Settings__c found'),
            ('CustomObject', 'genui__GenCanvas_Input_Settings__c'))

    def test_service_presence_error(self):
        self.assertEqual(
            pp.resolve_error('In field: servicePresenceStatus - no ServicePresenceStatus named Available_Phone found'),
            ('ServicePresenceStatus', 'Available_Phone'))

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


class TestStatusBoard(unittest.TestCase):
    def test_rows_update_per_thread(self):
        b = pp.StatusBoard(3, quiet=True)  # quiet -> inactive render, rows still update
        b.set(1, 'dry-run', 'tabVisibilities', 'polling', 10, 5)
        b.set(0, 'removing', 'classAccesses', '2 errors', 20, 18)
        self.assertIn('[T1]', b.rows[1])
        self.assertIn('dry-run', b.rows[1])
        self.assertIn('tabVisibilities', b.rows[1])
        self.assertIn('remaining:5', b.rows[1])
        self.assertIn('deleted:5', b.rows[1])
        self.assertIn('initial:10', b.rows[1])
        self.assertIn('removing', b.rows[0])
        self.assertIn('classAccesses', b.rows[0])
        # thread 2 still idle
        self.assertIn('idle', b.rows[2])

    def test_mark_stopped_only_active(self):
        b = pp.StatusBoard(3, quiet=True)
        b.set(0, 'dry-run', 'tabVisibilities', 'polling', 10, 5)
        b.set(1, 'idle')
        b.mark_stopped()
        self.assertIn('stopped', b.rows[0])
        self.assertIn('stopped by user', b.rows[0])
        self.assertIn('idle', b.rows[1])
        self.assertIn('idle', b.rows[2])

    def test_finish_idempotent(self):
        b = pp.StatusBoard(2, quiet=True)
        b.finish()
        b.finish()

    def test_summary_uses_progress(self):
        p = pp.Progress(total=10)
        p.start()
        p.start()
        p.finish(True)
        b = pp.StatusBoard(2, quiet=True, progress=p)
        self.assertIn('done: 1', b._summary())
        self.assertIn('running: 1', b._summary())
        self.assertIn('todo: 8', b._summary())


class TestControl(unittest.TestCase):
    def test_initial_state(self):
        c = pp.Control()
        self.assertFalse(c.stop.is_set())


class TestProgress(unittest.TestCase):
    def test_counters(self):
        p = pp.Progress(total=5)
        self.assertEqual(p.snapshot(), (0, 0, 5, 0))
        p.start()
        self.assertEqual(p.snapshot(), (0, 1, 4, 0))
        p.finish(True)
        self.assertEqual(p.snapshot(), (1, 0, 4, 0))
        p.start()
        p.finish(False)
        self.assertEqual(p.snapshot(), (1, 0, 3, 1))


class TestSectionHelpers(unittest.TestCase):
    def test_prunable_blocks(self):
        blocks = pp.prunable_blocks()
        for b in ('classAccesses', 'fieldPermissions', 'tabVisibilities', 'objectPermissions',
                  'customSettingAccesses', 'servicePresenceStatusAccesses'):
            self.assertIn(b, blocks)

    def test_custom_setting_mapping(self):
        cands = pp.BLOCK_MAP['CustomObject']
        self.assertIn(('customSettingAccesses', 'name', 'exact'), cands)

    def test_remove_from_list(self):
        tree = pp.etree.fromstring(SYNTH.encode('utf-8'))
        els = [c for c in tree if c.tag == pp.M + 'tabVisibilities']
        kept, removed = pp.remove_from_list(els, 'tabVisibilities', 'NotARealTab')
        self.assertEqual(removed, 1)
        self.assertEqual(kept, [])

    def test_build_subprofile(self):
        tree = pp.etree.fromstring(SYNTH.encode('utf-8'))
        els = [c for c in tree if c.tag == pp.M + 'classAccesses']
        xml = pp.build_subprofile(els)
        root = pp.etree.fromstring(xml)
        self.assertEqual(root.tag, pp.M + 'Profile')
        self.assertEqual([c.tag for c in root], [pp.M + 'classAccesses'])


class TestSharedState(unittest.TestCase):
    def test_write_outputs_preserves_order_and_drops_removed(self):
        d = tempfile.mkdtemp()
        try:
            tree = pp.etree.fromstring(SYNTH.encode('utf-8'))
            seq = [c for c in tree]
            from collections import OrderedDict
            groups = OrderedDict()
            for el in seq:
                groups.setdefault(el.tag.split('}')[-1], []).append(el)
            # drop the tabVisibilities entry
            tab = groups['tabVisibilities']
            groups['tabVisibilities'] = []
            good = os.path.join(d, 'GOOD', 'X.profile-meta.xml')
            errpkg = os.path.join(d, 'ERRORS', 'package.xml')
            st = pp.SharedState(groups, seq, pp.Logger(True), os.path.join(d, 'e.txt'),
                                good, errpkg, 'X', '68.0')
            st._write_outputs_locked()
            tags = [c.tag.split('}')[1] for c in pp.etree.parse(good).getroot()]
            self.assertEqual(tags, ['fieldPermissions', 'classAccesses', 'objectPermissions'])
            # no package.xml in GOOD
            self.assertFalse(os.path.isfile(os.path.join(d, 'GOOD', 'package.xml')))
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
