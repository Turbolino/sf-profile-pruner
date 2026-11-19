# -*- coding: utf-8 -*-
"""Unit tests for Profile Pruner (pure functions, no network/org required)."""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import prune_profile as pp  # noqa: E402
from prune_profile.cli import (_check_for_update, _clamp_batch_size,
                              _load_align_state, _repair_mangled_argv,
                              _reconfigure_output_encoding,
                              _resume_alignment, _validate_profile_dry_run)  # noqa: E402
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

    def test_read_report_summary(self):
        d = tempfile.mkdtemp()
        try:
            report = os.path.join(d, 'report.txt')
            write_text(report,
                       'OUTCOME: SUCCESS\n'
                       'METADATA REMOVED (total): 2261\n'
                       'TIME: 385.2s\n')
            s = pp.read_report_summary(report)
            self.assertEqual(s['outcome'], 'SUCCESS')
            self.assertEqual(s['removed'], 2261)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_read_report_summary_missing(self):
        s = pp.read_report_summary(os.path.join(tempfile.mkdtemp(), 'nope.txt'))
        self.assertIsNone(s['outcome'])
        self.assertIsNone(s['removed'])


class TestAlignResumeSummary(unittest.TestCase):
    def test_summary_lists_completed_phases(self):
        from prune_profile import cli as cli
        d = tempfile.mkdtemp()
        try:
            for sub, outcome, removed in (('1-source-pruned', 'SUCCESS', 0),
                                          ('3-destination-pruned', 'SUCCESS', 116)):
                os.makedirs(os.path.join(d, sub))
                write_text(os.path.join(d, sub, 'report.txt'),
                           'OUTCOME: %s\nMETADATA REMOVED (total): %d\n' % (outcome, removed))
            state = {'granted': 0, 'revoked': 14, 'layout_changed': 2,
                     'manual': 16, 'validation': 'clean'}
            lines = cli._align_resume_summary(d, state)
            text = '\n'.join(lines)
            self.assertIn('PHASE 1/4 · PRUNE SOURCE: SUCCESS (0 reference(s) removed)', text)
            self.assertIn('PHASE 2/4 · PRUNE DESTINATION: SUCCESS (116 reference(s) removed)', text)
            self.assertIn('PHASE 3/4 · MERGE: 0 granted, 14 revoked, 2 layout changed, 16 manual', text)
            self.assertIn('PHASE 4/4 · VALIDATE: clean', text)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestRunParallel(unittest.TestCase):
    def test_preserves_input_order_and_captures_errors(self):
        from prune_profile.parallel import run_parallel

        def fn(x):
            if x == 'boom':
                raise ValueError('bad')
            return x * 2

        results, errors = run_parallel(['a', 'boom', 'c'], fn, max_workers=4)
        self.assertEqual(results, ['aa', None, 'cc'])
        self.assertEqual(errors, [None, 'bad', None])

    def test_sequential_when_one_or_zero_threads(self):
        from prune_profile.parallel import run_parallel
        seen = []

        def fn(x):
            seen.append(x)
            return x

        # max_workers=1 (and 0) must run strictly in order, no threads.
        results, errors = run_parallel([3, 1, 2], fn, max_workers=1)
        self.assertEqual(seen, [3, 1, 2])
        self.assertEqual(results, [3, 1, 2])
        self.assertEqual(errors, [None, None, None])

    def test_single_item_runs_inline(self):
        from prune_profile.parallel import run_parallel
        results, errors = run_parallel([42], lambda x: x + 1, max_workers=8)
        self.assertEqual(results, [43])
        self.assertEqual(errors, [None])


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
        # a literal `"` and swallowed `--dest-org Test` + `Org`.
        argv = ['--profile-path', r'C:\p\X.profile-meta.xml',
                '--out-dir', r'.\salesforce\src Test\dest Test" --dest-org Test', 'Org']
        out, note = _repair_mangled_argv(argv)
        self.assertTrue(note)
        self.assertNotIn('"', ' '.join(out))
        self.assertEqual(out, ['--profile-path', r'C:\p\X.profile-meta.xml',
                               '--out-dir', r'.\salesforce\src Test\dest Test',
                               '--dest-org', 'Test Org'])

    def test_trailing_quote_stripped(self):
        # Case B: --dest-org first; --out-dir kept a literal trailing `"`.
        argv = ['--dest-org', 'Test Org', '--profile-path', r'C:\p\X.profile-meta.xml',
                '--out-dir', r'.\salesforce\src Test\dest Test"']
        out, note = _repair_mangled_argv(argv)
        self.assertTrue(note)
        self.assertEqual(out, ['--dest-org', 'Test Org', '--profile-path',
                               r'C:\p\X.profile-meta.xml',
                               '--out-dir', r'.\salesforce\src Test\dest Test'])

    def test_no_quote_is_untouched(self):
        argv = ['--dest-org', 'Test Org', '--out-dir', r'.\dest']
        out, note = _repair_mangled_argv(argv)
        self.assertEqual(note, '')
        self.assertEqual(out, argv)

    def test_repair_never_leaves_literal_quote(self):
        argv = ['--out-dir', 'a" --dest-org X', 'Y']
        out, _ = _repair_mangled_argv(argv)
        self.assertFalse(any('"' in t for t in out))


class TestOrgApiVersion(unittest.TestCase):
    def test_field_data_types_batches_entities(self):
        calls = []
        def fake_tooling(sf, org, soql, cwd=None, control=None):
            calls.append(soql)
            # echo back the entity names as fake records (standard-object shape:
            # EntityDefinitionId is the name; the relationship is also present).
            import re
            ids = re.findall(r"'([^']+)'", soql)
            return [{'EntityDefinitionId': i,
                     'EntityDefinition': {'QualifiedApiName': i},
                     'QualifiedApiName': 'F__c',
                     'DataType': 'Text(80)'} for i in ids], None
        orig = pp.sfcli.tooling_query
        pp.sfcli.tooling_query = fake_tooling
        progress = []
        try:
            entities = ['Obj%02d' % i for i in range(85)]
            out, err = pp.sfcli.field_data_types(
                'sf', 'devorg', entities, batch=40,
                on_progress=lambda d, t: progress.append((d, t)))
        finally:
            pp.sfcli.tooling_query = orig
        self.assertIsNone(err)
        self.assertEqual(len(out), 85)
        self.assertTrue(all(k.startswith('Obj') and k.endswith('.F__c') for k in out))
        # 85 entities at batch 40 -> 3 queries
        self.assertEqual(len(calls), 3)
        self.assertTrue(all('EntityDefinitionId IN (' in c for c in calls))
        # progress: (40, 85), (80, 85), (85, 85)
        self.assertEqual(progress, [(40, 85), (80, 85), (85, 85)])

    def test_field_data_types_reconstructs_custom_object_names(self):
        # Custom objects: EntityDefinitionId resolves to a DurableId (01I...),
        # NOT the object API name. The full field name must come from
        # EntityDefinition.QualifiedApiName or it never matches the Profile XML.
        def fake_tooling(sf, org, soql, cwd=None, control=None):
            return [{'EntityDefinitionId': '01Icz00000Fa2Z0',
                     'EntityDefinition': {'QualifiedApiName': 'et4ae5__Automated_Send__c'},
                     'QualifiedApiName': 'et4ae5__Bounce_Rate_del__c',
                     'DataType': 'Formula (Percent)'}], None
        orig = pp.sfcli.tooling_query
        pp.sfcli.tooling_query = fake_tooling
        try:
            out, err = pp.sfcli.field_data_types('sf', 'devorg',
                                                 ['et4ae5__Automated_Send__c'])
        finally:
            pp.sfcli.tooling_query = orig
        self.assertIsNone(err)
        self.assertEqual(
            out.get('et4ae5__Automated_Send__c.et4ae5__Bounce_Rate_del__c'),
            'Formula (Percent)')
        self.assertNotIn('01Icz00000Fa2Z0', next(iter(out)))

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


class TestAlignResume(unittest.TestCase):
    def _write_state(self, d, merged='4-merged/Sales Rep.profile-meta.xml', **over):
        state = {
            'version': 1,
            'src_org': 'mysandbox',
            'dest_org': 'devorg',
            'src_profile': 'Sales Rep',
            'dest_profile': 'Sales Rep',
            'api_version': '68.0',
            'merged_profile': merged,
            'validation': 'clean',
        }
        state.update(over)
        with open(os.path.join(d, 'align-state.json'), 'w', encoding='utf-8') as f:
            json.dump(state, f)

    def _args(self, fresh=False):
        return types.SimpleNamespace(
            fresh=fresh, api_version='68.0', poll_interval=3.0, job_timeout=0)

    def test_resume_matching_params_deploys_cached_merge(self):
        with tempfile.TemporaryDirectory() as d:
            merged = os.path.join(d, '4-merged', 'Sales Rep.profile-meta.xml')
            os.makedirs(os.path.dirname(merged), exist_ok=True)
            write_text(merged, '<Profile/>')
            self._write_state(d)
            with mock.patch.object(pp.cli, '_deploy_profile',
                                   return_value=(True, 'deployed')) as dp:
                code = _resume_alignment(
                    self._args(), 'mysandbox', 'Sales Rep', 'Sales Rep', 'devorg',
                    True, d, pp.Logger(True), 'sf')
            self.assertEqual(code, 0)
            dp.assert_called_once()
            self.assertEqual(dp.call_args[0][2], merged)

    def test_resume_matching_params_without_deploy_reports_done(self):
        with tempfile.TemporaryDirectory() as d:
            merged = os.path.join(d, '4-merged', 'Sales Rep.profile-meta.xml')
            os.makedirs(os.path.dirname(merged), exist_ok=True)
            write_text(merged, '<Profile/>')
            self._write_state(d)
            code = _resume_alignment(
                self._args(), 'mysandbox', 'Sales Rep', 'Sales Rep', 'devorg',
                False, d, pp.Logger(True), 'sf')
            self.assertEqual(code, 0)

    def test_resume_mismatched_params_errors(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_state(d)
            code = _resume_alignment(
                self._args(), 'OTHER_ORG', 'Sales Rep', 'Sales Rep', 'devorg',
                True, d, pp.Logger(True), 'sf')
            self.assertEqual(code, 2)

    def test_resume_fresh_skips_to_full_run(self):
        with tempfile.TemporaryDirectory() as d:
            self._write_state(d)
            code = _resume_alignment(
                self._args(fresh=True), 'mysandbox', 'Sales Rep', 'Sales Rep',
                'devorg', True, d, pp.Logger(True), 'sf')
            self.assertIsNone(code)

    def test_resume_no_state_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            code = _resume_alignment(
                self._args(), 'mysandbox', 'Sales Rep', 'Sales Rep', 'devorg',
                True, d, pp.Logger(True), 'sf')
            self.assertIsNone(code)

    def test_load_align_state_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_load_align_state(d))


class TestRunSfInterrupt(unittest.TestCase):
    def test_validate_profile_dry_run_clean(self):
        with tempfile.TemporaryDirectory() as d:
            profile = os.path.join(d, 'X.profile-meta.xml')
            write_text(profile, '<Profile/>')
            with mock.patch.object(pp.cli, 'dry_run',
                                   return_value=({'result': {'success': True}}, None)):
                val, cf = _validate_profile_dry_run(
                    'sf', 'org', profile, 'X', '68.0', 3.0, 0, pp.Logger(True), d)
        self.assertEqual(val, 'clean')
        self.assertEqual(cf, [])

    def test_validate_profile_dry_run_failure_lists_problems(self):
        with tempfile.TemporaryDirectory() as d:
            profile = os.path.join(d, 'X.profile-meta.xml')
            write_text(profile, '<Profile/>')
            res = {'result': {'success': False,
                              'details': {'componentFailures': [
                                  {'problem': 'field not found: X'}]}}}
            with mock.patch.object(pp.cli, 'dry_run', return_value=(res, None)):
                val, cf = _validate_profile_dry_run(
                    'sf', 'org', profile, 'X', '68.0', 3.0, 0, pp.Logger(True), d)
        self.assertIn('FAILED', val)
        self.assertEqual(len(cf), 1)

    def test_validate_profile_dry_run_error(self):
        with tempfile.TemporaryDirectory() as d:
            profile = os.path.join(d, 'X.profile-meta.xml')
            write_text(profile, '<Profile/>')
            with mock.patch.object(pp.cli, 'dry_run', return_value=(None, 'boom')):
                val, cf = _validate_profile_dry_run(
                    'sf', 'org', profile, 'X', '68.0', 3.0, 0, pp.Logger(True), d)
        self.assertIn('FAILED', val)
        self.assertIn('boom', val)

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


class TestNetworkRetry(unittest.TestCase):
    def test_is_network_error_detection(self):
        self.assertTrue(pp.sfcli._is_network_error({'message': 'fetch failed'}, None))
        self.assertTrue(pp.sfcli._is_network_error(
            {'name': 'TypeError', 'message': 'fetch failed', 'exitCode': 10}, None))
        self.assertTrue(pp.sfcli._is_network_error(None, 'connection reset by peer'))
        self.assertTrue(pp.sfcli._is_network_error(None, 'getaddrinfo ENOTFOUND login.salesforce.com'))
        # org rate limits are transient and must be retried
        self.assertTrue(pp.sfcli._is_network_error(
            {'message': 'REQUEST_LIMIT_EXCEEDED: TotalRequests Limit exceeded'}, None))
        self.assertTrue(pp.sfcli._is_network_error(None, 'too many requests'))
        # EXCEEDED_ID_LIMIT (Tooling single-batch ceiling) is NOT a rate limit:
        # it must not be caught here (the caller splits the batch instead).
        self.assertFalse(pp.sfcli._is_network_error(
            {'message': 'EXCEEDED_ID_LIMIT: EntityDefinition does not support queryMore'}, None))
        self.assertFalse(pp.sfcli._is_network_error({'message': 'No such column'}, None))
        self.assertFalse(pp.sfcli._is_network_error(None, 'profile not found'))

    def test_network_retry_recovers(self):
        with mock.patch.object(pp.sfcli, 'find_node', return_value=r'C:\node.exe'), \
                mock.patch.object(pp.sfcli.time, 'sleep'), \
                mock.patch.object(pp.sfcli, '_run_sf_once',
                                  side_effect=[(None, 'fetch failed'),
                                               (None, 'connection reset'),
                                               ({'result': {'id': 'j1'}}, None)]) as once:
            res, err = pp.sfcli._run_sf(['sf', 'x'], 'sf', 'deploy start')
        self.assertEqual(res, {'result': {'id': 'j1'}})
        self.assertIsNone(err)
        self.assertEqual(once.call_count, 3)

    def test_network_retry_gives_up_with_message(self):
        with mock.patch.object(pp.sfcli, 'find_node', return_value=r'C:\node.exe'), \
                mock.patch.object(pp.sfcli.time, 'sleep'), \
                mock.patch.object(pp.sfcli, '_run_sf_once',
                                  return_value=(None, 'fetch failed')) as once:
            res, err = pp.sfcli._run_sf(['sf', 'x'], 'sf', 'deploy start')
        self.assertIsNone(res)
        self.assertIn('NETWORK ERROR', err)
        self.assertIn('5 attempts', err)
        self.assertEqual(once.call_count, pp.sfcli.NETWORK_RETRIES)

    def test_non_network_error_not_retried(self):
        with mock.patch.object(pp.sfcli, 'find_node', return_value=r'C:\node.exe'), \
                mock.patch.object(pp.sfcli.time, 'sleep'), \
                mock.patch.object(pp.sfcli, '_run_sf_once',
                                  return_value=(None, 'No such column on entity')) as once:
            res, err = pp.sfcli._run_sf(['sf', 'x'], 'sf', 'deploy start')
        self.assertIsNone(res)
        self.assertEqual(err, 'No such column on entity')
        self.assertEqual(once.call_count, 1)

    def test_is_lock_error_detection(self):
        self.assertTrue(pp.sfcli._is_lock_error(
            {'message': 'Lock file is already being held. This likely means '
                        'another sf command is in progress.'}, None))
        self.assertTrue(pp.sfcli._is_lock_error(
            None, 'Lock file is already being held'))
        # lock is NOT a network error (and vice versa)
        self.assertFalse(pp.sfcli._is_lock_error({'message': 'fetch failed'}, None))
        self.assertFalse(pp.sfcli._is_network_error(
            {'message': 'Lock file is already being held'}, None))

    def test_lock_retry_recovers(self):
        with mock.patch.object(pp.sfcli, 'find_node', return_value=r'C:\node.exe'), \
                mock.patch.object(pp.sfcli.time, 'sleep'), \
                mock.patch.object(pp.sfcli, '_run_sf_once',
                                  side_effect=[(None, 'Lock file is already being held'),
                                               ({'result': {'id': 'j1'}}, None)]) as once:
            res, err = pp.sfcli._run_sf(['sf', 'x'], 'sf', 'deploy start')
        self.assertEqual(res, {'result': {'id': 'j1'}})
        self.assertIsNone(err)
        self.assertEqual(once.call_count, 2)

    def test_lock_retry_gives_up_with_message(self):
        with mock.patch.object(pp.sfcli, 'find_node', return_value=r'C:\node.exe'), \
                mock.patch.object(pp.sfcli.time, 'sleep'), \
                mock.patch.object(pp.sfcli, '_run_sf_once',
                                  return_value=(None, 'Lock file is already being held')) as once:
            res, err = pp.sfcli._run_sf(['sf', 'x'], 'sf', 'deploy start')
        self.assertIsNone(res)
        self.assertIn('LOCK ERROR', err)
        self.assertIn('%d attempts' % pp.sfcli.LOCK_RETRIES, err)
        self.assertEqual(once.call_count, pp.sfcli.LOCK_RETRIES)

    def test_lock_retry_recovers(self):
        with mock.patch.object(pp.sfcli, 'find_node', return_value=r'C:\node.exe'), \
                mock.patch.object(pp.sfcli.time, 'sleep'), \
                mock.patch.object(pp.sfcli, '_run_sf_once',
                                  side_effect=[(None, 'Lock file is already being held'),
                                               (None, 'Lock file is already being held'),
                                               ({'result': {'id': 'j1'}}, None)]) as once:
            res, err = pp.sfcli._run_sf(['sf', 'x'], 'sf', 'deploy start')
        self.assertEqual(res, {'result': {'id': 'j1'}})
        self.assertIsNone(err)
        self.assertEqual(once.call_count, 3)

    def test_start_deploy_uses_serialization_slot(self):
        """start_deploy acquires the deploy slot before the sf call and always
        releases it afterwards (so two workers never submit a deploy start at
        the same time)."""
        from prune_profile import sfcli as s
        order = []
        def fake_slot(control):
            s._deploy_mutex.acquire()  # take the real mutex, like the real helper
            order.append('acquire')
            return True
        def fake_run(*a, **k):
            order.append('sf')
            return ({'result': {'id': 'j1'}}, None)
        with mock.patch.object(s, '_acquire_deploy_slot', side_effect=fake_slot), \
                mock.patch.object(s, '_run_sf', side_effect=fake_run), \
                mock.patch.object(s, '_throttle_deploy_start'):
            job_id, err = s.start_deploy('sf', 'manifest.xml', 'org', dry_run=True)
        self.assertEqual(job_id, 'j1')
        self.assertIsNone(err)
        self.assertEqual(order, ['acquire', 'sf'])
        # the real mutex must have been released after the call
        self.assertTrue(s._deploy_mutex.acquire(blocking=False))
        s._deploy_mutex.release()

    def test_acquire_deploy_slot_releases_on_stop(self):
        """The deploy slot wait is interruptible: a stop request makes it
        return False instead of blocking forever on a busy mutex."""
        from prune_profile import sfcli as s
        control = types.SimpleNamespace(stop=types.SimpleNamespace(is_set=lambda: True))
        self.assertFalse(s._acquire_deploy_slot(control))


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
        self.assertIn('pip install --upgrade --upgrade-strategy eager', ' '.join(lines))

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
        self.assertIn('[T1', b.rows[1])
        self.assertIn('dry-run', b.rows[1])
        self.assertIn('tabVisibilities', b.rows[1])
        # cell shows the [deleted/initial] ratio only (remaining dropped for 2 cols)
        self.assertIn('[5/10]', b.rows[1])
        self.assertIn('removing', b.rows[0])
        self.assertIn('classAccesses', b.rows[0])
        self.assertIn('[18/20]', b.rows[0])
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

    def test_ratio_column_grows_with_large_counters(self):
        b = pp.StatusBoard(2, quiet=True)
        b.set(0, 'dry-run', 'fieldPermissions', 'polling', initial=100, deleted=0)
        b.set(1, 'dry-run', 'tabVisibilities', 'polling', initial=10000, deleted=5000)
        self.assertEqual(len(b.rows[0]), len(b.rows[1]))
        self.assertTrue(b.rows[0].endswith('[0/100]     '))
        self.assertTrue(b.rows[1].endswith('[5000/10000]'))

    def test_visible_rows_capped_by_terminal_height(self):
        b = pp.StatusBoard(40, quiet=True)
        b.height = 20
        self.assertEqual(b._visible_rows(), 16)  # height - 4 (2 blank bottom rows)
        b.height = 4
        self.assertEqual(b._visible_rows(), 1)
        b.height = 0  # unknown terminal height -> no cap (pairs = ceil(n/2))
        self.assertEqual(b._visible_rows(), 20)

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

    def test_decode_bytes_utf8_first(self):
        # Valid UTF-8 'à' (U+00E0 = C3 A0) stays 'à'.
        self.assertEqual(pp.decode_bytes('specialità'.encode('utf-8')),
                         'specialità')
        # A str passes through unchanged.
        self.assertEqual(pp.decode_bytes('x'), 'x')

    def test_decode_bytes_ansi_fallback(self):
        # Windows-1252 'à' is a lone 0xE0 byte; strict UTF-8 would fail and
        # errors='replace' would turn it into U+FFFD. The fallback keeps 'à'.
        self.assertEqual(pp.decode_bytes(b'specialit\xe0'), 'specialit\xe0'.encode('cp1252').decode('cp1252'))
        self.assertEqual(pp.decode_bytes(b'specialit\xe0'), 'specialità')
        # 'attività' in cp1252
        self.assertEqual(pp.decode_bytes('attivit\xe0'.encode('cp1252')), 'attività')

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
        self.assertTrue(any('Flow: 1 existing, 1 dangling' in m for m in messages))
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
        self.assertEqual(_clamp_batch_size(10000), 10000)
        self.assertEqual(_clamp_batch_size(15000), pp.MAX_CHUNK_SIZE)
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

    def test_extract_common_members_custom_vs_standard(self):
        """Regression: manifest classification must treat any `__` API name as
        custom (big/external objects, platform events) and a standard field on a
        custom object as standard — not the old `endswith('__c')` / `'__c' in`
        substring heuristics."""
        xml = ('<Profile xmlns="http://soap.sforce.com/2006/04/metadata">'
               '<fieldPermissions><field>Account.MyField__c</field></fieldPermissions>'
               '<fieldPermissions><field>MyObj__c.CreatedDate</field></fieldPermissions>'
               '<fieldPermissions><field>ns__Obj__c.ns__F__c</field></fieldPermissions>'
               '<objectPermissions><object>Account</object></objectPermissions>'
               '<objectPermissions><object>MyObj__c</object></objectPermissions>'
               '<objectPermissions><object>BigObj__b</object></objectPermissions>'
               '<objectPermissions><object>Event__e</object></objectPermissions>'
               '<objectPermissions><object>ExtObj__x</object></objectPermissions>'
               '</Profile>')
        out = pp.extract_common_members(self._groups(xml))
        # custom fields (field component carries `__`)
        self.assertEqual(out['CustomField'], {'Account.MyField__c', 'ns__Obj__c.ns__F__c'})
        # a standard field on a custom object stays STANDARD (was CustomField)
        self.assertNotIn('MyObj__c.CreatedDate', out.get('CustomField', set()))
        # custom objects with any custom suffix, not just `__c`
        self.assertEqual(out['CustomObject'],
                         {'MyObj__c', 'BigObj__b', 'Event__e', 'ExtObj__x'})
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


class TestMerge(unittest.TestCase):
    """Tests for the alignment merge module (pure functions)."""

    NS = 'http://soap.sforce.com/2006/04/metadata'

    @classmethod
    def _profile(cls, sections_xml):
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<Profile xmlns="%s">\n%s\n</Profile>\n'
                % (cls.NS, sections_xml))

    @staticmethod
    def _merge(src_sections, dst_sections):
        return TestMerge._merge_with_read_only(src_sections, dst_sections, None)

    @staticmethod
    def _merge_with_read_only(src_sections, dst_sections, read_only):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src.xml')
            dst = os.path.join(d, 'dst.xml')
            write_text(src, TestMerge._profile(src_sections))
            write_text(dst, TestMerge._profile(dst_sections))
            return pp.merge_profiles(src, dst, read_only_fields=read_only)

    def test_dest_only_tab_is_turned_off(self):
        xml, manual_ops, changes = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<tabVisibilities><tab>TabB__c</tab><visibility>DefaultOn</visibility></tabVisibilities>'),
        )
        root = pp.etree.fromstring(xml)
        tabs = {el.find('{%s}tab' % self.NS).text: el.find('{%s}visibility' % self.NS).text
                for el in root.findall('{%s}tabVisibilities' % self.NS)}
        self.assertEqual(tabs['TabA__c'], 'DefaultOn')   # source value wins
        self.assertEqual(tabs['TabB__c'], 'Hidden')      # dest-only -> off
        self.assertEqual(manual_ops, [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'revoke')
        self.assertEqual(changes[0]['old_values'], {'visibility': 'DefaultOn'})

    def test_dest_only_entry_already_off_is_skipped(self):
        xml, manual_ops, changes = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<tabVisibilities><tab>TabB__c</tab><visibility>Hidden</visibility></tabVisibilities>'),
        )
        # TabB is destination-only but already Hidden: no change, not in output.
        root = pp.etree.fromstring(xml)
        tabs = [el.find('{%s}tab' % self.NS).text
                for el in root.findall('{%s}tabVisibilities' % self.NS)]
        self.assertEqual(tabs, ['TabA__c'])
        self.assertEqual(changes, [])
        self.assertEqual(manual_ops, [])

    def test_source_only_entry_already_off_is_skipped(self):
        xml, manual_ops, changes = self._merge(
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<tabVisibilities><tab>TabB__c</tab><visibility>Hidden</visibility></tabVisibilities>'),
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
        )
        # TabB exists only in the source and is already Hidden (off): the
        # destination already denies it implicitly, so there is nothing to
        # change and it must not be reported as a revoke.
        root = pp.etree.fromstring(xml)
        tabs = [el.find('{%s}tab' % self.NS).text
                for el in root.findall('{%s}tabVisibilities' % self.NS)]
        self.assertEqual(tabs, ['TabA__c'])
        self.assertEqual(changes, [])
        self.assertEqual(manual_ops, [])

    def test_read_only_field_editable_not_diffed(self):
        # A read-only field (e.g. compound address) with editable=true on BOTH
        # sides: normalization forces false in the merged, but the editable
        # flag must NOT be diffed against the destination's dead true value
        # (it would surface a phantom revoke).
        xml, manual_ops, changes = TestMerge._merge_with_read_only(
            '<fieldPermissions><field>Account.BillingAddress</field>'
            '<readable>true</readable><editable>true</editable></fieldPermissions>',
            '<fieldPermissions><field>Account.BillingAddress</field>'
            '<readable>true</readable><editable>true</editable></fieldPermissions>',
            {'Account.BillingAddress'},
        )
        self.assertEqual(changes, [])
        self.assertEqual(manual_ops, [])
        # The merged still normalizes editable to false (correct effective value).
        root = pp.etree.fromstring(xml)
        fp = root.findall('{%s}fieldPermissions' % self.NS)[0]
        self.assertEqual(fp.find('{%s}editable' % self.NS).text, 'false')

    def test_read_only_field_readable_still_diffed(self):
        # `readable` remains meaningful for read-only fields: a readable
        # difference must still be reported (as a grant, here).
        xml, manual_ops, changes = TestMerge._merge_with_read_only(
            '<fieldPermissions><field>Account.BillingAddress</field>'
            '<readable>true</readable><editable>false</editable></fieldPermissions>',
            '<fieldPermissions><field>Account.BillingAddress</field>'
            '<readable>false</readable><editable>true</editable></fieldPermissions>',
            {'Account.BillingAddress'},
        )
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'grant')
        self.assertEqual(changes[0]['old_values'], {'readable': 'false'})

    def test_diff_profiles(self):
        src = TestMerge._profile(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
            '<tabVisibilities><tab>TabB__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
            '<fieldPermissions><field>Acc.F__c</field><readable>true</readable><editable>true</editable></fieldPermissions>\n'
            '<fieldPermissions><field>Acc.E__c</field><readable>false</readable><editable>false</editable></fieldPermissions>')
        dst = TestMerge._profile(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
            '<tabVisibilities><tab>TabC__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
            '<fieldPermissions><field>Acc.F__c</field><readable>true</readable><editable>false</editable></fieldPermissions>')
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, 's.xml'); dp = os.path.join(d, 'd.xml')
            write_text(sp, src); write_text(dp, dst)
            res = pp.diff_profiles(sp, dp)
        tv = res['tabVisibilities']
        self.assertEqual(tv['source_count'], 2)
        self.assertEqual(tv['dest_count'], 2)
        self.assertEqual([x['key'] for x in tv['only_source']], [('TabB__c',)])
        self.assertEqual([x['key'] for x in tv['only_dest']], [('TabC__c',)])
        self.assertEqual(tv['different'], [])
        self.assertEqual([x['key'] for x in tv['common']], [('TabA__c',)])
        fp = res['fieldPermissions']
        self.assertEqual([x['key'] for x in fp['only_source']], [('Acc.E__c',)])
        self.assertEqual(len(fp['common']), 0)
        self.assertEqual(len(fp['different']), 1)
        self.assertEqual(fp['different'][0]['key'], ('Acc.F__c',))
        self.assertEqual(fp['different'][0]['diffs'], [('editable', 'true', 'false')])
        self.assertEqual(fp['different'][0]['source']['readable'], 'true')
        self.assertEqual(fp['different'][0]['dest']['editable'], 'false')

    def test_diff_profiles_common_entries_with_values(self):
        src = TestMerge._profile(
            '<fieldPermissions><field>Acc.F__c</field><readable>true</readable><editable>true</editable></fieldPermissions>\n'
            '<fieldPermissions><field>Acc.G__c</field><readable>false</readable><editable>false</editable></fieldPermissions>')
        dst = TestMerge._profile(
            '<fieldPermissions><field>Acc.F__c</field><readable>true</readable><editable>true</editable></fieldPermissions>\n'
            '<fieldPermissions><field>Acc.G__c</field><readable>false</readable><editable>false</editable></fieldPermissions>')
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, 's.xml'); dp = os.path.join(d, 'd.xml')
            write_text(sp, src); write_text(dp, dst)
            res = pp.diff_profiles(sp, dp)
        fp = res['fieldPermissions']
        self.assertEqual(fp['different'], [])
        self.assertEqual(len(fp['common']), 2)
        common_by_key = {x['key']: x['values'] for x in fp['common']}
        self.assertEqual(common_by_key[('Acc.F__c',)],
                         {'field': 'Acc.F__c', 'readable': 'true', 'editable': 'true'})
        self.assertEqual(common_by_key[('Acc.G__c',)],
                         {'field': 'Acc.G__c', 'readable': 'false', 'editable': 'false'})

    def test_diff_json_and_xml_written(self):
        sections = {
            'fieldPermissions': {
                'source_count': 3, 'dest_count': 2,
                'only_source': [{'key': ('Acc.X__c',), 'values': {'field': 'Acc.X__c', 'readable': 'true', 'editable': 'true'}}],
                'only_dest': [{'key': ('Acc.Y__c',), 'values': {'field': 'Acc.Y__c', 'readable': 'true', 'editable': 'true'}}],
                'common': [{'key': ('Acc.Z__c',), 'values': {'field': 'Acc.Z__c', 'readable': 'false', 'editable': 'false'}}],
                'different': [{'key': ('Acc.W__c',),
                               'source': {'field': 'Acc.W__c', 'editable': 'true'},
                               'dest': {'field': 'Acc.W__c', 'editable': 'false'},
                               'diffs': [('editable', 'true', 'false')]}],
                'key_spec': ['field'],
            },
        }
        with tempfile.TemporaryDirectory() as d:
            jp = pp.write_diff_json(sections, 'a', 'b', d)
            xp = pp.write_diff_xml(sections, 'a', 'b', d)
            import json as _json
            data = _json.load(open(jp, encoding='utf-8'))
            self.assertEqual(data['sections']['fieldPermissions']['common_count'], 1)
            self.assertEqual(
                data['sections']['fieldPermissions']['common'][0]['values']['editable'],
                'false')
            xml_text = open(xp, encoding='utf-8').read()
            self.assertIn('Acc.Z__c', xml_text)
            self.assertIn('common', xml_text)

    def test_diff_report_renders_summary_and_details(self):
        sections = {
            'fieldPermissions': {
                'source_count': 3, 'dest_count': 2,
                'only_source': [{'key': ('Acc.X__c',), 'values': {}}],
                'only_dest': [{'key': ('Acc.Y__c',), 'values': {}}],
                'common': [{'key': ('Acc.C__c',), 'values': {}}],
                'different': [{'key': ('Acc.Z__c',), 'source': {}, 'dest': {},
                               'diffs': [('editable', 'true', 'false')]}],
                'key_spec': ['field'],
            },
        }
        text = pp.render_diff_report(sections, 'mysandbox "Admin"', 'devsandbox "Admin"')
        self.assertIn('PROFILE DIFF', text)
        self.assertIn('fieldPermissions', text)
        self.assertIn('only in source (1)', text)
        self.assertIn('Acc.Z__c', text)
        self.assertIn("source='true' dest='false'", text)

    def test_source_only_entry_is_granted(self):
        xml, manual_ops, changes = self._merge(
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<tabVisibilities><tab>TabB__c</tab><visibility>DefaultOn</visibility></tabVisibilities>'),
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
        )
        # TabB exists only in the source and is ON: deploying the merged
        # profile turns it ON in the destination -> grant.
        root = pp.etree.fromstring(xml)
        tabs = {el.find('{%s}tab' % self.NS).text: el.find('{%s}visibility' % self.NS).text
                for el in root.findall('{%s}tabVisibilities' % self.NS)}
        self.assertEqual(tabs['TabB__c'], 'DefaultOn')
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'grant')
        self.assertEqual(changes[0]['old_values'], {'visibility': None})

    def test_shared_entry_source_off_dest_on_is_revoked(self):
        xml, manual_ops, changes = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>Hidden</visibility></tabVisibilities>',
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
        )
        # Same tab in both, source OFF (Hidden), dest ON: merged keeps the
        # source OFF value -> revoke.
        root = pp.etree.fromstring(xml)
        tab = root.findall('{%s}tabVisibilities' % self.NS)[0]
        self.assertEqual(tab.find('{%s}visibility' % self.NS).text, 'Hidden')
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'revoke')
        self.assertEqual(changes[0]['old_values'], {'visibility': 'DefaultOn'})

    def test_shared_entry_source_on_dest_off_is_granted(self):
        xml, manual_ops, changes = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
            '<tabVisibilities><tab>TabA__c</tab><visibility>Hidden</visibility></tabVisibilities>',
        )
        # Same tab in both, source ON, dest OFF (Hidden): merged keeps the
        # source ON value -> grant.
        root = pp.etree.fromstring(xml)
        tab = root.findall('{%s}tabVisibilities' % self.NS)[0]
        self.assertEqual(tab.find('{%s}visibility' % self.NS).text, 'DefaultOn')
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'grant')
        self.assertEqual(changes[0]['old_values'], {'visibility': 'Hidden'})

    def test_dest_only_layout_assignment_is_manual(self):
        xml, manual_ops, changes = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<layoutAssignments><layout>Account-Account Layout</layout></layoutAssignments>'),
        )
        self.assertEqual(len(manual_ops), 1)
        self.assertEqual(manual_ops[0]['section'], 'layoutAssignments')
        self.assertIn('Account-Account Layout', manual_ops[0]['item'])
        self.assertIn('page layout assignment', manual_ops[0]['operation'])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'removed')

    def test_layout_change_same_slot_is_deploy_change_not_manual(self):
        """Same object+recordType, DIFFERENT layout: this is a CHANGE the deploy
        applies (source-authoritative), not a destination-only manual removal."""
        xml, manual_ops, changes = self._merge(
            '<layoutAssignments><layout>Account-Account Layout</layout></layoutAssignments>',
            '<layoutAssignments><layout>Account-Other Layout</layout></layoutAssignments>',
        )
        # no manual operation: the deploy resolves it
        self.assertEqual(manual_ops, [])
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'layout_change')
        self.assertEqual(changes[0]['object'], 'Account')
        self.assertEqual(changes[0]['key'], 'Account-Account Layout')
        self.assertEqual(changes[0]['old_values']['layout'], 'Account-Other Layout')
        # merged profile carries the SOURCE layout
        root = pp.etree.fromstring(xml)
        layouts = [el.find('{%s}layout' % self.NS).text
                   for el in root.findall('{%s}layoutAssignments' % self.NS)]
        self.assertEqual(layouts, ['Account-Account Layout'])

    def test_layout_change_record_type_scopes_slots(self):
        """Same object but different recordType = separate slots: one is a
        change, the other is a destination-only removal."""
        xml, manual_ops, changes = self._merge(
            '<layoutAssignments><layout>Account-Account Layout</layout><recordType>Foo.Bar</recordType></layoutAssignments>',
            ('<layoutAssignments><layout>Account-Other Layout</layout><recordType>Foo.Bar</recordType></layoutAssignments>\n'
             '<layoutAssignments><layout>Account-Account Layout</layout><recordType>Foo.Baz</recordType></layoutAssignments>'),
        )
        kinds = sorted(c['kind'] for c in changes)
        self.assertEqual(kinds, ['layout_change', 'removed'])
        # the removal is the destination-only recordType slot
        removed = next(c for c in changes if c['kind'] == 'removed')
        self.assertEqual(removed['object'], 'Account')
        self.assertEqual(len(manual_ops), 1)

    def test_what_is_changed_reports_layout_change(self):
        xml, _, changes = self._merge(
            '<layoutAssignments><layout>Account-Account Layout</layout></layoutAssignments>',
            '<layoutAssignments><layout>Account-Other Layout</layout></layoutAssignments>',
        )
        with tempfile.TemporaryDirectory() as d:
            path = pp.write_what_is_changed(os.path.join(d, 'w.txt'), changes, 'devorg')
            s = read_text(path)
        self.assertIn('LAYOUT CHANGED (applied by the deploy)', s)
        self.assertIn('LAYOUT CHANGED: 1', s)
        self.assertIn('<layout>Account-Account Layout</layout>', s)
        self.assertIn('old layout from devorg: Account-Other Layout', s)

    def test_named_layout_and_default_coexist_not_a_change(self):
        """The User object's default layout (`User-User Layout`) and its named
        layout (`UserAlt-User Profile Layout`) are TWO independent assignment
        slots that coexist in a profile. A destination-only named layout is a
        removal (manual op), not a change of the default layout."""
        xml, manual_ops, changes = self._merge(
            '<layoutAssignments><layout>User-User Layout</layout></layoutAssignments>',
            ('<layoutAssignments><layout>User-User Layout</layout></layoutAssignments>\n'
             '<layoutAssignments><layout>UserAlt-User Profile Layout</layout></layoutAssignments>'),
        )
        # source keeps its default layout; the dest-only named layout is removed.
        root = pp.etree.fromstring(xml)
        layouts = sorted(el.find('{%s}layout' % self.NS).text
                         for el in root.findall('{%s}layoutAssignments' % self.NS))
        self.assertEqual(layouts, ['User-User Layout'])
        kinds = [c['kind'] for c in changes]
        self.assertEqual(kinds, ['removed'])
        self.assertEqual(len(manual_ops), 1)
        self.assertIn('UserAlt-User Profile Layout', manual_ops[0]['item'])


    def test_removed_non_alignable_detects_dropped_layout(self):
        with tempfile.TemporaryDirectory() as d:
            orig = os.path.join(d, 'orig.xml')
            clean = os.path.join(d, 'clean.xml')
            write_text(orig, self._profile(
                '<layoutAssignments><layout>Lead-Lead Layout</layout></layoutAssignments>'))
            write_text(clean, self._profile(''))
            ops = pp.removed_non_alignable(orig, clean)
            self.assertEqual(len(ops), 1)
            self.assertEqual(ops[0]['section'], 'layoutAssignments')
            self.assertIn('Lead-Lead Layout', ops[0]['item'])

    def test_format_ops_table_is_aligned(self):
        ops = [{'section': 'layoutAssignments', 'item': 'X-Y Layout',
                'operation': 'Remove the page layout assignment',
                'where': 'Setup > Object Manager'}]
        table = pp.format_ops_table(ops)
        self.assertIn('SECTION', table)
        self.assertIn('X-Y Layout', table)
        self.assertIn('Remove the page layout assignment', table)

    def test_classify_layout_ops_redundant_vs_check(self):
        ops = [
            {'section': 'layoutAssignments', 'object': 'SingleObj',
             'item': 'SingleObj-One Layout', 'operation': 'x', 'where': 'y'},
            {'section': 'layoutAssignments', 'object': 'MultiObj',
             'item': 'MultiObj-Picked Layout', 'operation': 'x', 'where': 'y'},
        ]
        changes = [
            {'kind': 'removed', 'section': 'layoutAssignments', 'object': 'SingleObj'},
            {'kind': 'removed', 'section': 'layoutAssignments', 'object': 'MultiObj'},
            {'kind': 'removed', 'section': 'loginFlows'},  # untouched
        ]
        pp.classify_layout_ops(ops, changes, {'SingleObj': 1, 'MultiObj': 4})
        self.assertEqual(ops[0]['verdict'], 'REDUNDANT (ignorable)')
        self.assertEqual(ops[1]['verdict'], 'NEEDS CHECK')
        self.assertEqual(changes[0]['verdict'], 'redundant')
        self.assertEqual(changes[1]['verdict'], 'check')
        self.assertNotIn('verdict', changes[2])

    def test_format_ops_table_adds_verdict_column(self):
        ops = [{'section': 'layoutAssignments', 'object': 'A', 'item': 'A-Layout',
                'operation': 'Explicit assignment is the only layout',
                'verdict': 'REDUNDANT (ignorable)',
                'where': 'Setup > Object Manager'}]
        table = pp.format_ops_table(ops)
        self.assertIn('VERDICT', table)
        self.assertIn('REDUNDANT (ignorable)', table)
        # explicit show_verdict=False drops the column
        table2 = pp.format_ops_table(ops, show_verdict=False)
        self.assertNotIn('VERDICT', table2)

    def test_write_manual_ops_splits_redundant_vs_check(self):
        ops = [
            {'section': 'layoutAssignments', 'object': 'Single',
             'item': 'Single-One Layout', 'operation': 'only layout',
             'verdict': 'REDUNDANT (ignorable)', 'where': 'a'},
            {'section': 'layoutAssignments', 'object': 'Multi',
             'item': 'Multi-Picked Layout', 'operation': 'check default',
             'verdict': 'NEEDS CHECK', 'where': 'b'},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = pp.write_manual_ops(os.path.join(d, 'm.txt'), ops, 'X')
            s = read_text(p)
        self.assertIn('REDUNDANT (ignorable) (1)', s)
        self.assertIn('NEEDS CHECK (1)', s)
        # the redundant row is under the redundant section, not the check one
        red_pos = s.index('REDUNDANT (ignorable) (1)')
        check_pos = s.index('NEEDS CHECK (1)')
        self.assertLess(red_pos, check_pos)
        self.assertIn('Single-One Layout', s)
        self.assertIn('Multi-Picked Layout', s)

    def test_write_manual_ops_layout_note_not_repeated_per_row(self):
        ops = [
            {'section': 'layoutAssignments', 'object': 'Single',
             'item': 'Single-One Layout',
             'operation': "Explicit assignment is the object's ONLY layout (= its default); source inherits the same default",
             'verdict': 'REDUNDANT (ignorable)', 'where': 'a'},
            {'section': 'layoutAssignments', 'object': 'Multi',
             'item': 'Multi-Picked Layout',
             'operation': 'Object has 2 layouts; verify this assignment matches the default and change it if it does not',
             'verdict': 'NEEDS CHECK', 'where': 'b'},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = pp.write_manual_ops(os.path.join(d, 'm.txt'), ops, 'X')
            s = read_text(p)
        # The explanation lives once in each section header, not on every row.
        self.assertIn("Explicit assignment is the object's ONLY layout", s)
        self.assertIn('Align the page layout assignment with the source', s)
        # The MANUAL OPERATION table column is dropped (header text keeps
        # "MANUAL OPERATIONS REQUIRED", so assert on the column separator).
        self.assertNotIn('MANUAL OPERATION |', s)
        # Rows no longer carry the repeated operation text.
        self.assertNotIn('only layout', s)
        self.assertNotIn('verify this assignment matches the default and change', s)

    def test_write_prune_what_is_changed(self):
        removed = {'ApexClass': {'Foo', 'Bar'}, 'CustomTab': {'MyTab__c'}}
        ops = [{'section': 'layoutAssignments', 'object': 'A',
                'item': 'A-Layout', 'operation': 'x', 'where': 'Setup > X'}]
        with tempfile.TemporaryDirectory() as d:
            p = pp.write_prune_what_is_changed(
                os.path.join(d, 'wic.txt'), removed, ops, 'devorg')
            s = read_text(p)
        self.assertIn('WHAT IS CHANGED - prune (destination: devorg)', s)
        self.assertIn('REMOVED (dangling references): 3', s)
        self.assertIn('ApexClass: 2', s)
        self.assertIn('- Foo', s)
        self.assertIn('MANUAL (non-alignable sections): 1', s)
        self.assertIn('A-Layout', s)

    def test_normalize_profile_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'p.xml')
            write_text(p, self._profile(
                '<fieldPermissions><field>Obj.Formula__c</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Account.BillingAddress</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Case.Status</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            n = pp.normalize_profile_read_only(
                p, {'Obj.Formula__c', 'Account.BillingAddress'})
            self.assertEqual(n, 2)
            root = pp.etree.parse(p).getroot()
            def editable(field):
                for el in root:
                    if el.find('{%s}field' % pp.NS).text == field:
                        return el.find('{%s}editable' % pp.NS).text
                return None
            self.assertEqual(editable('Obj.Formula__c'), 'false')
            self.assertEqual(editable('Account.BillingAddress'), 'false')
            # untouched: not in the read-only set
            self.assertEqual(editable('Case.Status'), 'true')
            # idempotent: no further change
            self.assertEqual(pp.normalize_profile_read_only(
                p, {'Obj.Formula__c', 'Account.BillingAddress'}), 0)

    def test_normalize_profile_read_only_empty_set(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'p.xml')
            write_text(p, self._profile(
                '<fieldPermissions><field>Obj.Formula__c</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            self.assertEqual(pp.normalize_profile_read_only(p, set()), 0)

    def test_merged_output_indented_one_entry_per_line(self):
        xml, _, _ = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<tabVisibilities><tab>TabB__c</tab><visibility>DefaultOn</visibility></tabVisibilities>'),
        )
        s = xml.decode('utf-8')
        # every section tag sits on its own line (no concatenation)
        lines = [ln for ln in s.split('\n') if '<tabVisibilities>' in ln]
        self.assertEqual(len(lines), 2)
        for ln in lines:
            self.assertEqual(ln.count('<tabVisibilities>'), 1)
        # 4-space indentation for sections, 8 for their children
        self.assertIn('\n    <tabVisibilities>', s)
        self.assertIn('\n        <tab>TabB__c</tab>', s)

    def test_field_permissions_names_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'p.xml')
            write_text(p, self._profile(
                '<fieldPermissions><field>User.F__c</field></fieldPermissions>\n'
                '<fieldPermissions><field>et4ae5__O__c.et4ae5__F__c</field></fieldPermissions>\n'
                '<tabVisibilities><tab>T__c</tab></tabVisibilities>'))
            self.assertEqual(pp.field_permissions_names(p),
                             ['User.F__c', 'et4ae5__O__c.et4ae5__F__c'])

    def test_field_permissions_editable_extraction(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'p.xml')
            write_text(p, self._profile(
                '<fieldPermissions><field>User.F__c</field><editable>true</editable></fieldPermissions>\n'
                '<fieldPermissions><field>O__c.G__c</field><editable>false</editable></fieldPermissions>\n'
                '<fieldPermissions><field>O__c.H__c</field></fieldPermissions>'))
            self.assertEqual(pp.field_permissions_editable(p),
                             {'User.F__c': 'true', 'O__c.G__c': 'false',
                              'O__c.H__c': ''})

    def test_read_only_field_set_describe_layer_marks_system_fields(self):
        from prune_profile import cli as cli
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src.xml')
            dst = os.path.join(d, 'dst.xml')
            write_text(src, self._profile(
                '<fieldPermissions><field>LiveChatTranscript.ChatKey</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Campaign.Status</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Obj.Formula__c</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            write_text(dst, self._profile(
                '<fieldPermissions><field>LiveChatTranscript.ChatKey</field><editable>false</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Campaign.Status</field><editable>false</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Obj.Formula__c</field><editable>false</editable><readable>true</readable></fieldPermissions>'))
            dt_map = {
                'LiveChatTranscript.ChatKey': 'Text(200)',
                'Campaign.Status': 'Picklist',
                'Obj.Formula__c': 'Formula (Text)',
            }

            def describe(_sf, org, entity):
                if entity == 'LiveChatTranscript':
                    return {'updateable': True,
                            'fields': [{'name': 'ChatKey', 'updateable': False}]}, None
                if entity == 'Campaign':
                    # devorg: whole object locked -> Status looks read-only there;
                    # mysandbox: editable -> must NOT be classified read-only.
                    if org == 'devorg':
                        return {'updateable': False,
                                'fields': [{'name': 'Status', 'updateable': False}]}, None
                    return {'updateable': True,
                            'fields': [{'name': 'Status', 'updateable': True}]}, None
                return {'updateable': True, 'fields': []}, None

            logger = types.SimpleNamespace(log=lambda *a: None)
            with mock.patch.object(cli, 'field_data_types',
                                   return_value=(dt_map, None)), \
                    mock.patch.object(cli, 'standard_entities',
                                      return_value=({'LiveChatTranscript', 'Campaign', 'Obj'}, None)), \
                    mock.patch.object(cli, 'describe_object', side_effect=describe):
                ro = cli._read_only_field_set(
                    'sf', 'mysandbox', 'devorg', src, dst, logger)

        # Formula -> DataType layer; ChatKey -> describe layer (false on both).
        self.assertIn('Obj.Formula__c', ro)
        self.assertIn('LiveChatTranscript.ChatKey', ro)
        # Campaign.Status is editable on mysandbox -> NOT read-only.
        self.assertNotIn('Campaign.Status', ro)

    def test_read_only_field_set_file_source_uses_object_guard(self):
        from prune_profile import cli as cli
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src.xml')
            dst = os.path.join(d, 'dst.xml')
            write_text(src, self._profile(
                '<fieldPermissions><field>LiveChatTranscript.ChatKey</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Campaign.Status</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            write_text(dst, self._profile(
                '<fieldPermissions><field>LiveChatTranscript.ChatKey</field><editable>false</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Campaign.Status</field><editable>false</editable><readable>true</readable></fieldPermissions>'))
            dt_map = {'LiveChatTranscript.ChatKey': 'Text(200)',
                      'Campaign.Status': 'Picklist'}

            def describe(_sf, org, entity):
                if entity == 'LiveChatTranscript':
                    return {'updateable': True,
                            'fields': [{'name': 'ChatKey', 'updateable': False}]}, None
                # Campaign locked at object level on devorg -> guard must reject it.
                return {'updateable': False,
                        'fields': [{'name': 'Status', 'updateable': False}]}, None

            logger = types.SimpleNamespace(log=lambda *a: None)
            with mock.patch.object(cli, 'field_data_types',
                                   return_value=(dt_map, None)), \
                    mock.patch.object(cli, 'standard_entities',
                                      return_value=({'LiveChatTranscript', 'Campaign'}, None)), \
                    mock.patch.object(cli, 'describe_object', side_effect=describe):
                ro = cli._read_only_field_set(
                    'sf', None, 'devorg', src, dst, logger)

        # ChatKey: object updateable + field updateable=False -> read-only.
        self.assertIn('LiveChatTranscript.ChatKey', ro)
        # Campaign.Status: object locked -> rejected despite field False.
        self.assertNotIn('Campaign.Status', ro)

    def test_read_only_field_set_plain_prune_describes_system_fields(self):
        """Plain prune (no destination profile) must ALSO normalize system
        read-only fields via describe, not just DataType. Custom fields are not
        candidates; a field editable in the source org is never normalized even
        when the destination is FLS-locked."""
        from prune_profile import cli as cli
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src.xml')
            write_text(src, self._profile(
                '<fieldPermissions><field>WorkOrder.IsClosed</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>WorkOrder.Subject</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Case.Status</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>Account.MyField__c</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            dt_map = {
                'WorkOrder.IsClosed': 'Checkbox',
                'WorkOrder.Subject': 'Text(80)',
                'Case.Status': 'Picklist',
                'Account.MyField__c': 'Text(200)',
            }
            described = []

            def describe(_sf, org, entity):
                described.append((org, entity))
                if entity == 'WorkOrder':
                    return {'updateable': True,
                            'fields': [{'name': 'IsClosed', 'updateable': False},
                                       {'name': 'Subject', 'updateable': True}]}, None
                if entity == 'Case':
                    # source: editable; dest: object FLS-locked -> must NOT be
                    # classified read-only.
                    if org == 'src':
                        return {'updateable': True,
                                'fields': [{'name': 'Status', 'updateable': True}]}, None
                    return {'updateable': False,
                            'fields': [{'name': 'Status', 'updateable': False}]}, None
                return {'updateable': True, 'fields': []}, None

            logger = types.SimpleNamespace(log=lambda *a: None)
            with mock.patch.object(cli, 'field_data_types',
                                   return_value=(dt_map, None)), \
                    mock.patch.object(cli, 'standard_entities',
                                      return_value=({'WorkOrder', 'Case', 'Account'}, None)), \
                    mock.patch.object(cli, 'describe_object', side_effect=describe):
                ro = cli._read_only_field_set('sf', 'src', 'dst', src, None, logger)

        # system read-only: updateable=False on both orgs -> normalized
        self.assertIn('WorkOrder.IsClosed', ro)
        # editable in source -> kept
        self.assertNotIn('WorkOrder.Subject', ro)
        # FLS-locked on destination only -> kept (not a system read-only field)
        self.assertNotIn('Case.Status', ro)
        # custom field -> not a describe candidate at all
        self.assertNotIn('Account.MyField__c', ro)
        # source-first: Case is never described on dest (source said editable),
        # and the custom field's object is never described.
        self.assertNotIn(('dst', 'Case'), described)
        self.assertNotIn(('src', 'Account'), described)

    def test_read_only_field_set_skips_custom_and_absent_objects(self):
        """Custom objects (BenefitAssignment is custom=True in both orgs) must
        not be described for the system-read-only pass — the describe would
        answer NOT_FOUND. The standard_entities filter keeps only standard
        destination objects, dropping both custom and one-org-only objects."""
        from prune_profile import cli as cli
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'src.xml')
            write_text(src, self._profile(
                '<fieldPermissions><field>WorkOrder.IsClosed</field><editable>true</editable><readable>true</readable></fieldPermissions>\n'
                '<fieldPermissions><field>BenefitAssignment.Status</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            dt_map = {
                'WorkOrder.IsClosed': 'Checkbox',
                'BenefitAssignment.Status': 'Picklist',
            }
            described = []

            def describe(_sf, org, entity):
                described.append((org, entity))
                return {'updateable': True,
                        'fields': [{'name': 'IsClosed', 'updateable': False},
                                   {'name': 'Status', 'updateable': False}]}, None

            logger = types.SimpleNamespace(log=lambda *a: None)
            with mock.patch.object(cli, 'field_data_types',
                                   return_value=(dt_map, None)), \
                    mock.patch.object(cli, 'standard_entities',
                                      # only WorkOrder is a standard object
                                      return_value=({'WorkOrder'}, None)), \
                    mock.patch.object(cli, 'describe_object', side_effect=describe):
                ro = cli._read_only_field_set('sf', 'src', 'dst', src, None, logger)

        self.assertIn('WorkOrder.IsClosed', ro)
        # custom/absent object: not described, not normalized
        self.assertNotIn('BenefitAssignment.Status', ro)
        self.assertNotIn(('dst', 'BenefitAssignment'), described)
        self.assertNotIn(('src', 'BenefitAssignment'), described)


    # --- the 4 --avoid-normalize-ro scenarios (align/prune x on/off) ---

    def test_align_default_normalizes_read_only(self):
        """Scenario 1: --align-from-source WITHOUT the flag -> normalization runs."""
        from prune_profile import cli as cli
        args = types.SimpleNamespace(avoid_normalize_ro=False, io_threads_number=8)
        logger = types.SimpleNamespace(log=lambda *a: None)
        with mock.patch.object(cli, '_read_only_field_set',
                               return_value={'Obj.Formula__c'}) as det:
            ro = cli._align_read_only_fields(
                args, 'sf', 'src', 'dst', 's.xml', 'd.xml', logger)
        self.assertEqual(ro, {'Obj.Formula__c'})
        self.assertEqual(det.call_count, 1)

    def test_align_avoid_normalize_ro_skips(self):
        """Scenario 2: --align-from-source WITH --avoid-normalize-ro -> empty set."""
        from prune_profile import cli as cli
        args = types.SimpleNamespace(avoid_normalize_ro=True, io_threads_number=8)
        logger = types.SimpleNamespace(log=lambda *a: None)
        with mock.patch.object(cli, '_read_only_field_set') as det:
            ro = cli._align_read_only_fields(
                args, 'sf', 'src', 'dst', 's.xml', 'd.xml', logger)
        self.assertEqual(ro, set())
        det.assert_not_called()

    def test_prune_default_normalizes_read_only(self):
        """Scenario 3: plain prune WITHOUT the flag -> normalize in place."""
        from prune_profile import cli as cli
        args = types.SimpleNamespace(avoid_normalize_ro=False, io_threads_number=8)
        logger = types.SimpleNamespace(log=lambda *a: None)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'p.xml')
            write_text(p, self._profile(
                '<fieldPermissions><field>Obj.Formula__c</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            with mock.patch.object(cli, '_read_only_field_set',
                                   return_value={'Obj.Formula__c'}):
                n = cli._normalize_prune_read_only(
                    'sf', args, 'src', 'dst', p, logger)
            self.assertEqual(n, 1)
            root = pp.etree.parse(p).getroot()
            el = root[0]
            self.assertEqual(el.find('{%s}editable' % pp.NS).text, 'false')

    def test_prune_avoid_normalize_ro_skips(self):
        """Scenario 4: plain prune WITH --avoid-normalize-ro -> no change, no detect."""
        from prune_profile import cli as cli
        args = types.SimpleNamespace(avoid_normalize_ro=True, io_threads_number=8)
        logger = types.SimpleNamespace(log=lambda *a: None)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'p.xml')
            write_text(p, self._profile(
                '<fieldPermissions><field>Obj.Formula__c</field><editable>true</editable><readable>true</readable></fieldPermissions>'))
            with mock.patch.object(cli, '_read_only_field_set') as det:
                n = cli._normalize_prune_read_only(
                    'sf', args, 'src', 'dst', p, logger)
            self.assertEqual(n, 0)
            det.assert_not_called()
            # flag left exactly as retrieved
            root = pp.etree.parse(p).getroot()
            self.assertEqual(root[0].find('{%s}editable' % pp.NS).text, 'true')

    # --- stdout encoding robustness (legacy Windows code pages) ---

    def test_reconfigure_output_encoding_forces_utf8(self):
        """cp1252/cp850 consoles must not crash the banner: reconfigure to UTF-8."""
        import io
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        sys.stderr = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        try:
            _reconfigure_output_encoding()
            self.assertEqual(sys.stdout.encoding.lower(), 'utf-8')
            self.assertEqual(sys.stderr.encoding.lower(), 'utf-8')
        finally:
            sys.stdout, sys.stderr = old_out, old_err

    def test_banner_survives_cp1252_console(self):
        """Regression: print(BANNER) raised UnicodeEncodeError on cp1252 stdout."""
        import io
        from prune_profile.cli import BANNER
        old_out = sys.stdout
        buf = io.BytesIO()
        sys.stdout = io.TextIOWrapper(buf, encoding='cp1252')
        try:
            _reconfigure_output_encoding()
            print(BANNER, flush=True)  # must not raise UnicodeEncodeError
            sys.stdout.flush()
        finally:
            sys.stdout = old_out

    def test_read_only_fields_normalized_to_false(self):
        xml, _, changes = self._merge_with_read_only(
            '<fieldPermissions><field>Obj.F__c</field><editable>true</editable><readable>true</readable></fieldPermissions>',
            '<fieldPermissions><field>Obj.F__c</field><editable>false</editable><readable>true</readable></fieldPermissions>',
            read_only={'Obj.F__c'},
        )
        root = pp.etree.fromstring(xml)
        fp = root.findall('{%s}fieldPermissions' % self.NS)[0]
        self.assertEqual(fp.find('{%s}editable' % self.NS).text, 'false')
        # editable now matches the destination -> no fake grant reported
        self.assertEqual(changes, [])

    def test_editable_fields_unchanged_when_not_read_only(self):
        xml, _, changes = self._merge_with_read_only(
            '<fieldPermissions><field>Obj.F__c</field><editable>true</editable><readable>true</readable></fieldPermissions>',
            '<fieldPermissions><field>Obj.F__c</field><editable>false</editable><readable>true</readable></fieldPermissions>',
            read_only=set(),
        )
        root = pp.etree.fromstring(xml)
        fp = root.findall('{%s}fieldPermissions' % self.NS)[0]
        self.assertEqual(fp.find('{%s}editable' % self.NS).text, 'true')
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['kind'], 'grant')

    def test_what_is_changed_annotates_old_values(self):
        xml, _, changes = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<tabVisibilities><tab>TabB__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<layoutAssignments><layout>Lead-Lead Layout</layout></layoutAssignments>'),
        )
        with tempfile.TemporaryDirectory() as d:
            path = pp.write_what_is_changed(os.path.join(d, 'w.txt'), changes, 'devorg')
            s = read_text(path)
        # revoked: comment inline on the changed field line
        self.assertIn('<visibility>Hidden</visibility> <!-- old value from devorg: DefaultOn -->', s)
        self.assertIn('REVOKED (turned OFF in the destination)', s)
        # removed: grouped list with an enumerating comment
        self.assertIn('<layout>Lead-Lead Layout</layout>', s)
        self.assertIn('Lead-Lead Layout removed in destination org because not present in source org', s)
        self.assertIn('REVOKED: 1', s)
        self.assertIn('MANUAL: 1', s)

    def test_what_is_changed_reports_grants(self):
        xml, _, changes = self._merge(
            '<fieldPermissions><field>Acc.Skill__c</field><editable>true</editable><readable>true</readable></fieldPermissions>',
            '',
        )
        with tempfile.TemporaryDirectory() as d:
            path = pp.write_what_is_changed(os.path.join(d, 'w.txt'), changes, 'devorg')
            s = read_text(path)
        self.assertIn('GRANTED (turned ON in the destination)', s)
        self.assertIn('<editable>true</editable> <!-- old value from devorg: not present -->', s)
        self.assertIn('GRANTED: 1', s)

    def test_what_is_changed_splits_layout_verdicts(self):
        xml, _, changes = self._merge(
            '<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>',
            ('<tabVisibilities><tab>TabA__c</tab><visibility>DefaultOn</visibility></tabVisibilities>\n'
             '<layoutAssignments><layout>Single-One Layout</layout></layoutAssignments>\n'
             '<layoutAssignments><layout>Multi-Picked Layout</layout></layoutAssignments>'),
        )
        ops = [c for c in changes if c.get('section') == 'layoutAssignments']
        # set objects + verdicts as classify_layout_ops would in the real flow
        for c, obj in zip(ops, ['Single', 'Multi']):
            c['object'] = obj
        pp.classify_layout_ops([], changes, {'Single': 1, 'Multi': 3})
        with tempfile.TemporaryDirectory() as d:
            path = pp.write_what_is_changed(os.path.join(d, 'w.txt'), changes, 'devorg')
            s = read_text(path)
        self.assertIn('REDUNDANT (ignorable)', s)
        self.assertIn('NEEDS CHECK', s)
        self.assertIn('Single-One Layout explicit assignment = the object\'s only layout', s)
        self.assertIn('Multi-Picked Layout object has several layouts - verify it matches the default', s)


if __name__ == '__main__':
    unittest.main(verbosity=2)
