# -*- coding: utf-8 -*-
"""Unit tests for Profile Pruner (pure functions, no network/org required)."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prune_profile as pp  # noqa: E402
from prune_profile.cli import _check_for_update, _clamp_batch_size, _repair_mangled_argv  # noqa: E402
from prune_profile.retrieve import profile_json_to_xml  # noqa: E402

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
            errlog = os.path.join(d, 'missing_log.txt')
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
            errlog = os.path.join(d, 'missing_log.txt')
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

    def test_login_flow_error(self):
        # Post-login flow referenced from <loginFlows>, not <flowAccesses>.
        self.assertEqual(
            pp.resolve_error('In field: Flow - no FlowDefinition named PostLoginClearUserAppInfo found'),
            ('FlowDefinition', 'PostLoginClearUserAppInfo'))

    def test_unknown(self):
        self.assertIsNone(pp.resolve_error('totally different error text'))


class TestRepairMangledArgv(unittest.TestCase):
    def test_swallowed_trailing_flags_recovered(self):
        # Case A: --out-dir ends in backslash+quote, so the closing quote became
        # a literal `"` and swallowed `--org Test` + `Org`.
        argv = ['--profile-path', r'C:\p\X.profile-meta.xml',
                '--out-dir', r'.\salesforce\src Test\dest Test" --org Test', 'Org']
        out, note = _repair_mangled_argv(argv)
        self.assertTrue(note)
        self.assertNotIn('"', ' '.join(out))
        self.assertEqual(out, ['--profile-path', r'C:\p\X.profile-meta.xml',
                               '--out-dir', r'.\salesforce\src Test\dest Test',
                               '--org', 'Test Org'])

    def test_trailing_quote_stripped(self):
        # Case B: --org first; --out-dir kept a literal trailing `"`.
        argv = ['--org', 'Test Org', '--profile-path', r'C:\p\X.profile-meta.xml',
                '--out-dir', r'.\salesforce\src Test\dest Test"']
        out, note = _repair_mangled_argv(argv)
        self.assertTrue(note)
        self.assertEqual(out, ['--org', 'Test Org', '--profile-path',
                               r'C:\p\X.profile-meta.xml',
                               '--out-dir', r'.\salesforce\src Test\dest Test'])

    def test_no_quote_is_untouched(self):
        argv = ['--org', 'Test Org', '--out-dir', r'.\dest']
        out, note = _repair_mangled_argv(argv)
        self.assertEqual(note, '')
        self.assertEqual(out, argv)

    def test_repair_never_leaves_literal_quote(self):
        argv = ['--out-dir', 'a" --org X', 'Y']
        out, _ = _repair_mangled_argv(argv)
        self.assertFalse(any('"' in t for t in out))


class TestOrgApiVersion(unittest.TestCase):
    def test_extracts_api_version(self):
        calls = []
        def fake_run(cmd, sf, what, cwd=None, control=None):
            calls.append((cmd, what))
            return {'status': 0, 'result': {'apiVersion': '68.0'}}, None
        orig = pp.sfcli._run_sf
        pp.sfcli._run_sf = fake_run
        try:
            v, err = pp.org_api_version('sf', 'devorg')
        finally:
            pp.sfcli._run_sf = orig
        self.assertIsNone(err)
        self.assertEqual(v, '68.0')
        self.assertIn('org', calls[0][0])
        self.assertIn('display', calls[0][0])

    def test_no_api_version_is_error(self):
        def fake_run(cmd, sf, what, cwd=None, control=None):
            return {'status': 0, 'result': {}}, None
        orig = pp.sfcli._run_sf
        pp.sfcli._run_sf = fake_run
        try:
            v, err = pp.org_api_version('sf', 'devorg')
        finally:
            pp.sfcli._run_sf = orig
        self.assertIsNone(v)
        self.assertIsNotNone(err)


class TestRunSfInterrupt(unittest.TestCase):
    def test_ctrl_c_terminates_child_and_reraises(self):
        # A Ctrl-C during a blocking sf call (control=None, e.g. `sf org
        # display`) must kill the child tree and re-raise the interrupt so the
        # top-level handler can stop cleanly instead of leaking the process.
        terminated = []

        class FakeProc:
            def __init__(self, *a, **k):
                pass

            def communicate(self, timeout=None):
                raise KeyboardInterrupt()

        def fake_terminate(proc):
            terminated.append(proc)

        with mock.patch.object(pp.sfcli.subprocess, 'Popen', FakeProc), \
                mock.patch.object(pp.sfcli, '_terminate_proc',
                                  side_effect=fake_terminate):
            with self.assertRaises(KeyboardInterrupt):
                pp.sfcli._run_sf(['sf', 'org', 'display'], 'sf', 'org display',
                                 control=None)
        self.assertEqual(len(terminated), 1)
        self.assertIsInstance(terminated[0], FakeProc)


class TestUpdateCheck(unittest.TestCase):
    def _patch_pypi(self, payload):
        class FakeResp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import urllib.request
        return mock.patch.object(
            urllib.request, 'urlopen',
            return_value=FakeResp(payload.encode('utf-8')))

    def test_newer_version_warns(self):
        import urllib.request
        with self._patch_pypi('{"info": {"version": "99.0.0"}}'):
            lines = _check_for_update()
        self.assertTrue(lines)
        self.assertIn('99.0.0', lines[0])
        self.assertIn('pip install --upgrade sf-profile-pruner', ' '.join(lines))

    def test_same_version_is_silent(self):
        with self._patch_pypi('{"info": {"version": "%s"}}' % pp.__version__):
            self.assertEqual(_check_for_update(), [])

    def test_network_failure_is_silent(self):
        import urllib.request
        with mock.patch.object(urllib.request, 'urlopen',
                               side_effect=OSError('no network')):
            self.assertEqual(_check_for_update(), [])


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


@unittest.skipUnless(os.name == 'nt', 'Windows-specific batch shim resolution')
class TestSfInvocation(unittest.TestCase):
    def test_batch_shim_resolves_to_node_runjs(self):
        d = tempfile.mkdtemp()
        runjs = os.path.join(d, 'node_modules', '@salesforce', 'cli', 'bin', 'run.js')
        os.makedirs(os.path.dirname(runjs), exist_ok=True)
        open(runjs, 'w').close()
        sf_cmd = os.path.join(d, 'sf.cmd')
        open(sf_cmd, 'w').close()
        node = r'C:\node.exe'
        out = pp._sf_invocation([sf_cmd, 'project', 'deploy', 'report'], node)
        self.assertEqual(out[0], node)
        self.assertIn('run.js', out[2])
        self.assertEqual(out[3:], ['project', 'deploy', 'report'])

    def test_batch_shim_without_runjs_falls_back(self):
        d = tempfile.mkdtemp()
        sf_cmd = os.path.join(d, 'sf.cmd')
        open(sf_cmd, 'w').close()
        cmd = [sf_cmd, 'x']
        self.assertEqual(pp._sf_invocation(cmd, r'C:\node.exe'), cmd)

    def test_non_cmd_untouched(self):
        cmd = [r'C:\sf\sf.exe', 'x']
        self.assertEqual(pp._sf_invocation(cmd, r'C:\node.exe'), cmd)


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
        # deleted = discarded so far; remaining = still to process = initial - deleted
        self.assertIn('deleted:5', b.rows[1])
        self.assertIn('remaining:5', b.rows[1])
        self.assertIn('initial:10', b.rows[1])
        self.assertIn('removing', b.rows[0])
        self.assertIn('classAccesses', b.rows[0])
        self.assertIn('deleted:18', b.rows[0])
        self.assertIn('remaining:2', b.rows[0])
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

    def test_tick_and_finish_are_safe_when_inactive(self):
        b = pp.StatusBoard(2, quiet=True)
        b.tick()
        b.finish()
        b.freeze()
        b.finalize_stopped()

    def test_clip_to_terminal_width(self):
        b = pp.StatusBoard(1, quiet=True)
        self.assertEqual(b._clip('hello'), 'hello')
        b.width = 10
        self.assertEqual(b._clip('1234567890'), '123456789')
        b.width = 0
        self.assertEqual(b._clip('hello world'), 'hello world')

    def test_visible_rows_capped_by_terminal_height(self):
        b = pp.StatusBoard(40, quiet=True)
        b.height = 20
        self.assertEqual(b._visible_rows(), 19)
        b.height = 3
        self.assertEqual(b._visible_rows(), 2)
        b.height = 0  # unknown terminal height -> no cap
        self.assertEqual(b._visible_rows(), 40)

    def test_mark_completed_only_idle_and_running(self):
        b = pp.StatusBoard(3, quiet=True)
        b.set(0, 'ok', 'tabVisibilities', 'clean', 10, 0)
        b.set(1, 'idle')
        b.set(2, 'fail', 'classAccesses', 'no progress', 5, 5)
        b._mark_completed_locked()
        self.assertIn('completed', b.rows[1])
        self.assertIn('no more work', b.rows[1])
        # already-finished rows keep their terminal state
        self.assertIn('ok', b.rows[0])
        self.assertIn('fail', b.rows[2])

    def test_finalize_finished_is_safe_when_inactive(self):
        b = pp.StatusBoard(2, quiet=True)
        b.set(1, 'idle')
        b.finalize_finished()
        self.assertIn('completed', b.rows[1])


class TestControl(unittest.TestCase):
    def test_initial_state(self):
        c = pp.Control()
        self.assertFalse(c.stop.is_set())


class TestHumanizeStatus(unittest.TestCase):
    def test_camel_case(self):
        self.assertEqual(pp.humanize_status('InProgress'), 'in progress')
        self.assertEqual(pp.humanize_status('SucceededPartial'), 'succeeded partial')

    def test_plain_lowercase(self):
        self.assertEqual(pp.humanize_status('Pending'), 'pending')
        self.assertEqual(pp.humanize_status('Queued'), 'queued')

    def test_already_spaced(self):
        self.assertEqual(pp.humanize_status('In Progress'), 'in progress')

    def test_empty(self):
        self.assertEqual(pp.humanize_status(''), '')
        self.assertEqual(pp.humanize_status(None), '')


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

    def test_login_flow_mapping(self):
        # Login flows reference a Flow (VisualWorkflow) or an ApexPage
        # (VisualForce); both must be prunable, not just flowAccesses.
        self.assertIn(('loginFlows', 'flow', 'exact'), pp.BLOCK_MAP['Flow'])
        self.assertIn(('loginFlows', 'flow', 'exact'), pp.BLOCK_MAP['FlowDefinition'])
        self.assertIn(('loginFlows', 'visualForcePage', 'exact'), pp.BLOCK_MAP['ApexPage'])
        # and loginFlows must now be a prunable section
        self.assertIn('loginFlows', pp.prunable_blocks())


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
            common = os.path.join(d, 'COMMON', 'X.profile-meta.xml')
            errpkg = os.path.join(d, 'MISSING', 'package.xml')
            todeploy = os.path.join(d, 'COMMON', 'toDeploy.xml')
            st = pp.SharedState(groups, seq, pp.Logger(True), os.path.join(d, 'e.txt'),
                                common, errpkg, todeploy, 'X', '68.0')
            st._write_outputs_locked()
            tags = [c.tag.split('}')[1] for c in pp.etree.parse(common).getroot()]
            self.assertEqual(tags, ['fieldPermissions', 'classAccesses', 'objectPermissions'])
            # no package.xml in COMMON
            self.assertFalse(os.path.isfile(os.path.join(d, 'COMMON', 'package.xml')))
            # toDeploy.xml lists the kept (COMMON) members
            self.assertTrue(os.path.isfile(todeploy))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_apply_global_removals_removes_login_flow(self):
        d = tempfile.mkdtemp()
        try:
            xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                   '<Profile xmlns="http://soap.sforce.com/2006/04/metadata">\n'
                   '  <loginFlows><flow>PostLoginClearUserAppInfo</flow>'
                   '<friendlyName>x</friendlyName><type>UI</type>'
                   '<uiLoginFlowType>VisualWorkflow</uiLoginFlowType></loginFlows>\n'
                   '  <classAccesses><apexClass>Keep</apexClass><enabled>true</enabled></classAccesses>\n'
                   '</Profile>\n')
            tree = pp.etree.fromstring(xml.encode('utf-8'))
            seq = [c for c in tree]
            from collections import OrderedDict
            groups = OrderedDict()
            for el in seq:
                groups.setdefault(el.tag.split('}')[-1], []).append(el)
            good = os.path.join(d, 'COMMON', 'X.profile-meta.xml')
            errpkg = os.path.join(d, 'MISSING', 'package.xml')
            todeploy = os.path.join(d, 'COMMON', 'toDeploy.xml')
            errlog = os.path.join(d, 'missing_log.txt')
            st = pp.SharedState(groups, seq, pp.Logger(True), errlog, good, errpkg,
                                todeploy, 'X', '68.0')
            n = st.apply_global_removals([('FlowDefinition', 'PostLoginClearUserAppInfo')])
            self.assertEqual(n, 1)
            tags = [c.tag.split('}')[1] for c in pp.etree.parse(good).getroot()]
            self.assertEqual(tags, ['classAccesses'])
            self.assertIn('FlowDefinition|PostLoginClearUserAppInfo', read_text(errlog))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_apply_global_removals_generic_fallback_unknown_type(self):
        d = tempfile.mkdtemp()
        try:
            # A brand-new Profile section whose metadata type is NOT in BLOCK_MAP
            # must still be removable by exact member name.
            xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
                   '<someNewAccesses><someThing>BrandNewMember</someThing>'
                   '<enabled>true</enabled></someNewAccesses>'
                   '<classAccesses><apexClass>Keep</apexClass><enabled>true</enabled></classAccesses>'
                   '</Profile>')
            tree = pp.etree.fromstring(xml.encode('utf-8'))
            seq = [c for c in tree]
            from collections import OrderedDict
            groups = OrderedDict()
            for el in seq:
                groups.setdefault(el.tag.split('}')[-1], []).append(el)
            good = os.path.join(d, 'COMMON', 'X.profile-meta.xml')
            errpkg = os.path.join(d, 'MISSING', 'package.xml')
            todeploy = os.path.join(d, 'COMMON', 'toDeploy.xml')
            errlog = os.path.join(d, 'missing_log.txt')
            st = pp.SharedState(groups, seq, pp.Logger(True), errlog, good, errpkg,
                                todeploy, 'X', '68.0')
            n = st.apply_global_removals([('BrandNewType', 'BrandNewMember')])
            self.assertEqual(n, 1)
            tags = [c.tag.split('}')[1] for c in pp.etree.parse(good).getroot()]
            self.assertEqual(tags, ['classAccesses'])
            self.assertIn('BrandNewType|BrandNewMember', read_text(errlog))
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestPrefetch(unittest.TestCase):
    def test_dangling_members(self):
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<flowAccesses><flow>Exists</flow></flowAccesses>'
               '<flowAccesses><flow>MissingFlow</flow></flowAccesses>'
               '<recordTypeVisibilities><recordType>*</recordType></recordTypeVisibilities>'
               '<layoutAssignments><layout>X</layout></layoutAssignments>'
               '</Profile>')
        root = pp.etree.fromstring(xml.encode('utf-8'))
        flows = [c for c in root if c.tag == pp.M + 'flowAccesses']
        self.assertEqual(pp.dangling_members(flows, 'flow', {'Exists'}), {'MissingFlow'})
        self.assertEqual(pp.dangling_members(flows, 'flow', {'Exists', 'MissingFlow'}), set())
        rts = [c for c in root if c.tag == pp.M + 'recordTypeVisibilities']
        # '*' is the master record type and is never dangling
        self.assertEqual(pp.dangling_members(rts, 'recordType', set()), set())
        # an element without the requested child is never dangling
        self.assertEqual(pp.dangling_members(flows, 'visualForcePage', set()), set())

    def test_listable_keys_are_block_map_keys(self):
        for mdtype in pp.LISTABLE:
            self.assertIn(mdtype, pp.BLOCK_MAP,
                          'LISTABLE key %r must be a BLOCK_MAP key' % mdtype)
        # every LISTABLE block is prunable
        prunable = pp.prunable_blocks()
        for mdtype, mappings in pp.LISTABLE.items():
            for block, _child in mappings:
                self.assertIn(block, prunable)

    def test_list_metadata_name_maps_profile_section_to_api_type(self):
        # The Profile section is `customMetadataTypeAccesses`, but the Metadata
        # API type to enumerate via `sf org list metadata` is `CustomMetadata`.
        self.assertEqual(pp.LIST_METADATA_NAME['CustomMetadataType'],
                         'CustomMetadata')
        # Every other LISTABLE key lists under its own name.
        for mdtype in pp.LISTABLE:
            if mdtype not in pp.LIST_METADATA_NAME:
                self.assertEqual(pp.LIST_METADATA_NAME.get(mdtype, mdtype), mdtype)

    def test_version_ge(self):
        self.assertTrue(pp.version_ge('68.0', '63.0'))
        self.assertTrue(pp.version_ge('66.0', '66.0'))
        self.assertFalse(pp.version_ge('62.0', '63.0'))
        self.assertTrue(pp.version_ge('68', '63.0'))

    def test_is_standard_api_name(self):
        self.assertTrue(pp.is_standard_api_name('Account'))
        self.assertTrue(pp.is_standard_api_name('Case'))
        self.assertTrue(pp.is_standard_api_name('AccountBrand'))
        # any double underscore marks custom metadata
        self.assertFalse(pp.is_standard_api_name('MyObj__c'))
        self.assertFalse(pp.is_standard_api_name('MyMeta__mdt'))
        self.assertFalse(pp.is_standard_api_name('ns__Thing__c'))
        self.assertFalse(pp.is_standard_api_name('Knowledge__kav'))
        self.assertFalse(pp.is_standard_api_name('BigObj__b'))

    def test_prefetch_preserves_standard_tabs(self):
        # `standard-*` are standard tabs; listMetadata CustomTab cannot see them,
        # so the pre-fetch must never report them as dangling.
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<tabVisibilities><tab>standard-Account</tab><visibility>DefaultOn</visibility></tabVisibilities>'
               '<tabVisibilities><tab>MissingCustomTab</tab><visibility>DefaultOn</visibility></tabVisibilities>'
               '</Profile>')
        root = pp.etree.fromstring(xml.encode('utf-8'))
        groups = {'tabVisibilities': [c for c in root if c.tag == pp.M + 'tabVisibilities']}

        def fake_list(sf, org, mdtype):
            if mdtype == 'CustomTab':
                return (['RealCustomTab'], None)
            return ([], None)  # empty -> skipped for every other type

        with mock.patch.object(pp.engine, 'list_metadata', side_effect=fake_list):
            removals = pp.prefetch_dangling('sf', 'org', groups, '68.0', pp.Logger(True))
        self.assertEqual(removals, [('CustomTab', 'MissingCustomTab')])

    def test_prefetch_reports_status_per_type(self):
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<flowAccesses><flow>Existing</flow></flowAccesses>'
               '<flowAccesses><flow>MissingFlow</flow></flowAccesses>'
               '</Profile>')
        root = pp.etree.fromstring(xml.encode('utf-8'))
        groups = {'flowAccesses': [c for c in root if c.tag == pp.M + 'flowAccesses']}

        def fake_list(sf, org, mdtype):
            return (['Existing'], None)

        messages = []
        with mock.patch.object(pp.engine, 'list_metadata', side_effect=fake_list):
            removals = pp.prefetch_dangling('sf', 'org', groups, '68.0',
                                            pp.Logger(True), status=messages.append)
        self.assertEqual(removals, [('Flow', 'MissingFlow')])
        self.assertTrue(any('listing Flow' in m for m in messages))
        self.assertTrue(any('1 existing, 1 dangling' in m for m in messages))

    def test_prefetch_skip_reports_status(self):
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<flowAccesses><flow>X</flow></flowAccesses>'
               '</Profile>')
        root = pp.etree.fromstring(xml.encode('utf-8'))
        groups = {'flowAccesses': [c for c in root if c.tag == pp.M + 'flowAccesses']}

        def fake_list(sf, org, mdtype):
            return (None, 'no permission')

        messages = []
        with mock.patch.object(pp.engine, 'list_metadata', side_effect=fake_list):
            removals = pp.prefetch_dangling('sf', 'org', groups, '68.0',
                                            pp.Logger(True), status=messages.append)
        self.assertEqual(removals, [])
        self.assertTrue(any('skipped' in m for m in messages))


class TestPrefetchStandards(unittest.TestCase):
    def _groups(self, xml):
        root = pp.etree.fromstring(xml.encode('utf-8'))
        groups = {}
        for el in root:
            groups.setdefault(el.tag.split('}')[-1], []).append(el)
        return groups

    def test_removes_dangling_standard_tabs_objects_fields(self):
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<tabVisibilities><tab>standard-Account</tab></tabVisibilities>'
               '<tabVisibilities><tab>standard-DataMaskJobSession</tab></tabVisibilities>'
               '<objectPermissions><object>Account</object></objectPermissions>'
               '<objectPermissions><object>GhostObject</object></objectPermissions>'
               '<objectPermissions><object>MyCustom__c</object></objectPermissions>'
               '<objectPermissions><object>MyMeta__mdt</object></objectPermissions>'
               '<fieldPermissions><field>Account.Name</field></fieldPermissions>'
               '<fieldPermissions><field>Account.GhostField__c</field></fieldPermissions>'
               '<fieldPermissions><field>MyCustom__c.AnyField__c</field></fieldPermissions>'
               '<fieldPermissions><field>MyMeta__mdt.AnyField__c</field></fieldPermissions>'
               '</Profile>')
        groups = self._groups(xml)

        def fake_query(sf, org, soql):
            if 'TabDefinition' in soql:
                return ([{'Name': 'standard-Account'}], None)
            if 'FieldDefinition' in soql:
                # per-object enumeration: only Account.Name exists
                return ([{'EntityDefinition': {'QualifiedApiName': 'Account'},
                         'QualifiedApiName': 'Name'}], None)
            if 'EntityDefinition' in soql:
                return ([{'QualifiedApiName': 'Account'}], None)
            return ([], None)

        with mock.patch.object(pp.engine, 'tooling_query', side_effect=fake_query):
            removals = pp.prefetch_standard_dangling('sf', 'org', groups,
                                                     pp.Logger(True))

        by_type = {}
        for typ, member, source in removals:
            by_type.setdefault(typ, []).append((member, source))

        self.assertEqual(by_type['StandardTab'],
                         [('standard-DataMaskJobSession', 'TabDefinition')])
        self.assertEqual(by_type['StandardObject'],
                         [('GhostObject', 'EntityDefinition')])
        self.assertEqual(by_type['StandardField'],
                         [('Account.GhostField__c', 'FieldDefinition')])
        # custom metadata types (__mdt) must NOT be classified as standard
        obj_members = [m for m, _s in by_type.get('StandardObject', [])]
        fld_members = [m for m, _s in by_type.get('StandardField', [])]
        self.assertNotIn('MyCustom__c', obj_members)
        self.assertNotIn('MyMeta__mdt', obj_members)
        self.assertNotIn('MyMeta__mdt.AnyField__c', fld_members)

    def test_source_failure_skips_only_that_source(self):
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<tabVisibilities><tab>standard-Account</tab></tabVisibilities>'
               '<objectPermissions><object>GhostObject</object></objectPermissions>'
               '</Profile>')
        groups = self._groups(xml)

        def fake_query(sf, org, soql):
            if 'TabDefinition' in soql:
                return ([{'Name': 'standard-Account'}], None)
            # EntityDefinition fails
            return (None, 'EXCEEDED_ID_LIMIT')

        with mock.patch.object(pp.engine, 'tooling_query', side_effect=fake_query):
            removals = pp.prefetch_standard_dangling('sf', 'org', groups,
                                                     pp.Logger(True))

        # TabDefinition still produced nothing (Account is valid); EntityDefinition
        # failed so GhostObject must NOT be reported as dangling.
        self.assertEqual(removals, [])

    def test_standard_types_excluded_from_missing_package(self):
        d = tempfile.mkdtemp()
        try:
            xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
                   '<objectPermissions><object>Account</object></objectPermissions>'
                   '</Profile>')
            tree = pp.etree.fromstring(xml.encode('utf-8'))
            seq = [c for c in tree]
            from collections import OrderedDict
            groups = OrderedDict()
            for el in seq:
                groups.setdefault(el.tag.split('}')[-1], []).append(el)
            good = os.path.join(d, 'COMMON', 'X.profile-meta.xml')
            errpkg = os.path.join(d, 'MISSING', 'package.xml')
            todeploy = os.path.join(d, 'COMMON', 'toDeploy.xml')
            errlog = os.path.join(d, 'missing_log.txt')
            st = pp.SharedState(groups, seq, pp.Logger(True), errlog, good, errpkg,
                                todeploy, 'X', '68.0')
            st.removed_by_type = {'StandardObject': {'Account'},
                                  'ApexClass': {'Foo'}}
            st._write_outputs_locked()
            pkg = read_text(errpkg)
            self.assertNotIn('StandardObject', pkg)
            self.assertIn('ApexClass', pkg)
            self.assertIn('Foo', pkg)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestChunking(unittest.TestCase):
    def _state(self, d, n_class=6):
        parts = ['<Profile xmlns="http://soap.sforce.com/2006/04/metadata">']
        for i in range(n_class):
            parts.append('<classAccesses><apexClass>K%d</apexClass><enabled>true</enabled></classAccesses>' % i)
        parts.append('</Profile>')
        tree = pp.etree.fromstring(''.join(parts).encode('utf-8'))
        seq = [c for c in tree]
        from collections import OrderedDict
        groups = OrderedDict()
        for el in seq:
            groups.setdefault(el.tag.split('}')[-1], []).append(el)
        good = os.path.join(d, 'COMMON', 'X.profile-meta.xml')
        errpkg = os.path.join(d, 'MISSING', 'package.xml')
        todeploy = os.path.join(d, 'COMMON', 'toDeploy.xml')
        errlog = os.path.join(d, 'missing_log.txt')
        return pp.SharedState(groups, seq, pp.Logger(True), errlog, good, errpkg,
                              todeploy, 'X', '68.0')

    def test_split_chunks(self):
        d = tempfile.mkdtemp()
        try:
            st = self._state(d, n_class=7)
            n = st.split_chunks('classAccesses', 3)
            self.assertEqual(n, 3)
            self.assertEqual([len(c) for c in st.chunks['classAccesses']], [3, 3, 1])
            self.assertEqual(st.chunk_initials['classAccesses'], [3, 3, 1])
            # the flat block list is untouched by splitting
            self.assertEqual(len(st.groups['classAccesses']), 7)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_split_chunks_zero_means_single(self):
        d = tempfile.mkdtemp()
        try:
            st = self._state(d, n_class=7)
            self.assertEqual(st.split_chunks('classAccesses', 0), 1)
            self.assertEqual([len(c) for c in st.chunks['classAccesses']], [7])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_split_chunks_small_block_single(self):
        d = tempfile.mkdtemp()
        try:
            st = self._state(d, n_class=2)
            self.assertEqual(st.split_chunks('classAccesses', 500), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_clamp_batch_size(self):
        self.assertEqual(_clamp_batch_size(75), pp.MIN_CHUNK_SIZE)
        self.assertEqual(_clamp_batch_size(100), 100)
        self.assertEqual(_clamp_batch_size(200), 200)
        self.assertEqual(_clamp_batch_size(4000), 4000)
        self.assertEqual(_clamp_batch_size(5000), pp.MAX_CHUNK_SIZE)
        self.assertEqual(_clamp_batch_size(0), 0)  # 0 = no chunking, kept
        self.assertEqual(_clamp_batch_size(1), pp.MIN_CHUNK_SIZE)

    def test_apply_chunk_removals_rebuilds_flat(self):
        d = tempfile.mkdtemp()
        try:
            st = self._state(d, n_class=5)
            st.split_chunks('classAccesses', 3)  # chunks [K0,K1,K2] + [K3,K4]
            chunk1 = list(st.chunks['classAccesses'][1])
            new1 = [chunk1[1]]  # keep K4, drop K3
            st.apply_chunk_removals('classAccesses', 1, new1,
                                    [('ApexClass', 'K3', 'no ApexClass named K3 found')], 0)
            self.assertEqual(len(st.groups['classAccesses']), 4)
            self.assertEqual(len(st.chunks['classAccesses'][1]), 1)
            self.assertIn('ApexClass|K3', read_text(os.path.join(d, 'missing_log.txt')))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_record_chunk_aggregates(self):
        d = tempfile.mkdtemp()
        try:
            st = self._state(d, n_class=4)
            st.split_chunks('classAccesses', 2)  # two chunks
            st.record_chunk('classAccesses', 0, True, 'clean')
            st.record_chunk('classAccesses', 1, False, 'no progress')
            ok, reason = st.section_status['classAccesses']
            self.assertFalse(ok)
            self.assertEqual(reason, 'no progress')
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestExtractCommonMembers(unittest.TestCase):
    def _groups(self, xml):
        tree = pp.etree.fromstring(xml.encode('utf-8'))
        from collections import OrderedDict
        groups = OrderedDict()
        for el in tree:
            groups.setdefault(el.tag.split('}')[-1], []).append(el)
        return groups

    def test_extract_common_members(self):
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<classAccesses><apexClass>Foo</apexClass></classAccesses>'
               '<flowAccesses><flow>MyFlow</flow></flowAccesses>'
               '<fieldPermissions><field>Account.Name</field></fieldPermissions>'
               '<fieldPermissions><field>MyObj__c.MyField__c</field></fieldPermissions>'
               '<objectPermissions><object>MyObj__c</object></objectPermissions>'
               '<tabVisibilities><tab>standard-Account</tab></tabVisibilities>'
               '<tabVisibilities><tab>MyTab</tab></tabVisibilities>'
               '<loginFlows><flow>LoginFlow1</flow></loginFlows>'
               '</Profile>')
        out = pp.extract_common_members(self._groups(xml))
        self.assertEqual(out['ApexClass'], {'Foo'})
        self.assertEqual(out['Flow'], {'MyFlow', 'LoginFlow1'})
        self.assertEqual(out['CustomField'], {'MyObj__c.MyField__c'})
        self.assertEqual(out['CustomObject'], {'MyObj__c'})
        self.assertEqual(out['CustomTab'], {'MyTab'})
        # standard field and standard tab are excluded
        self.assertNotIn('StandardField', out)
        self.assertNotIn('StandardTab', out)
        self.assertNotIn('StandardObject', out)


class TestProfileJsonToXml(unittest.TestCase):
    """Serializer for readMetadata Profile objects (no network required)."""

    NS = 'http://soap.sforce.com/2006/04/metadata'

    def _sections(self, xml):
        root = pp.etree.fromstring(xml)
        out = {}
        for el in root:
            if isinstance(el.tag, str):
                k = el.tag.split('}')[-1]
                out[k] = out.get(k, 0) + 1
        return out

    def test_scalar_and_list_sections(self):
        data = {
            'fullName': 'Admin',
            'custom': True,
            'userLicense': 'Salesforce',
            'fieldPermissions': [
                {'editable': False, 'field': 'Account.Name', 'readable': True},
                {'editable': True, 'field': 'MyObj__c.MyField__c', 'readable': False},
            ],
            'tabVisibilities': [{'tab': 'standard-Account', 'visibility': 'Hidden'}],
        }
        xml = profile_json_to_xml(data, clean=False)
        root = pp.etree.fromstring(xml)
        self.assertEqual(root.tag, '{%s}Profile' % self.NS)
        secs = self._sections(xml)
        self.assertEqual(secs['fieldPermissions'], 2)
        self.assertEqual(secs['tabVisibilities'], 1)
        self.assertEqual(secs['userLicense'], 1)
        self.assertEqual(secs['custom'], 1)
        # fullName must never leak into the XML
        self.assertNotIn('fullName', secs)
        # custom scalar renders as lowercase boolean text
        custom = root.find('{%s}custom' % self.NS)
        self.assertEqual(custom.text, 'true')

    def test_clean_strips_non_portable_sections(self):
        data = {
            'fullName': 'Admin',
            'custom': False,
            'userLicense': 'Salesforce',
            'loginIpRanges': [],
            'loginHours': [],
            'userPermissions': [{'enabled': True, 'name': 'ViewSetup'}],
        }
        xml = profile_json_to_xml(data, clean=True)
        secs = self._sections(xml)
        self.assertNotIn('userLicense', secs)
        self.assertNotIn('loginIpRanges', secs)
        self.assertNotIn('loginHours', secs)
        self.assertEqual(secs['userPermissions'], 1)

    def test_boolean_and_string_scalars(self):
        # jsforce types some flags inconsistently (e.g. viewAllFields as "false"
        # string); both must serialize to lowercase text.
        data = {
            'objectPermissions': [
                {'object': 'Account', 'allowRead': True, 'viewAllFields': 'false'},
            ],
        }
        xml = profile_json_to_xml(data, clean=True)
        root = pp.etree.fromstring(xml)
        op = root.find('{%s}objectPermissions' % self.NS)
        self.assertEqual(op.find('{%s}allowRead' % self.NS).text, 'true')
        self.assertEqual(op.find('{%s}viewAllFields' % self.NS).text, 'false')


if __name__ == '__main__':
    unittest.main(verbosity=2)
