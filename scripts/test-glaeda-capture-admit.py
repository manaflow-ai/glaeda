#!/usr/bin/env python3
"""Behavioral tests for whole-host protected capture admission; no real host or account."""
from __future__ import annotations

import json
import hashlib
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

import importlib.machinery
import importlib.util

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader('promotion_harness', str(ROOT / 'scripts/test-glaeda-promotion-admit.py'))
spec = importlib.util.spec_from_loader(loader.name, loader)
harness = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = harness
loader.exec_module(harness)
HELPER = ROOT / 'scripts/glaeda-capture-admit'


class CaptureTest(unittest.TestCase):
    # The same fake listener and real flock ledger exercise both admission policies.
    def setUp(self):
        harness.PromotionTest.setUp(self)
        (self.fleet / 'bin').mkdir()
        (self.fleet / 'bin/with-host-lock').write_bytes(b'fixture quarantine-aware lock helper')
    tearDown = harness.PromotionTest.tearDown
    set_tailnet = harness.PromotionTest.set_tailnet
    lease_dir = harness.PromotionTest.lease_dir
    state = harness.PromotionTest.state
    units_held = harness.PromotionTest.units_held
    wait_for = harness.PromotionTest.wait_for
    ready = harness.PromotionTest.ready
    started = harness.PromotionTest.started
    listener_alive = harness.PromotionTest.listener_alive

    def run_admit(self, request, timeout=30):
        self.state_root = self.home / '.local/state/glaeda/capture'
        return subprocess.run([sys.executable, str(HELPER), '--request-stdin'], input=json.dumps(request),
                              env=self.env, capture_output=True, text=True, timeout=timeout)

    def request(self, **overrides):
        request = harness.PromotionTest.request(self)
        request.pop('controller_url')
        request.pop('status_path')
        request.update(repository='manaflow-ai/cmuxterm-hq', workflow='pr-capture-cloud.yml', job='capture',
                       ref='refs/heads/main', runner_name='cmux-capture-' + self.lease_id,
                       runner_label='cmux-capture-' + self.lease_id + '-capture')
        request['host_lock_sha256'] = hashlib.sha256((self.fleet / 'bin/with-host-lock').read_bytes()).hexdigest()
        request['runtime_sha256'] = {name: hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
                                     for name in ('glaeda-capture-admit', 'glaeda-promotion-admit',
                                                  'glaeda-cmux-runner-hook', 'glaeda_reservation.py')}
        request.update(overrides)
        return request

    def assign(self, **overrides):
        event = self.dir / 'dispatch.json'
        event.write_text(json.dumps({'repository': {'full_name': 'manaflow-ai/cmuxterm-hq', 'fork': False},
                                    'ref': 'refs/heads/main'}))
        env = dict(GITHUB_REPOSITORY='manaflow-ai/cmuxterm-hq', GITHUB_RUN_ID='4242', GITHUB_RUN_ATTEMPT='1',
                   GITHUB_SHA=harness.SHA, GITHUB_WORKFLOW_SHA=harness.SHA, GITHUB_JOB='capture',
                   RUNNER_NAME='cmux-capture-' + self.lease_id, GITHUB_EVENT_NAME='workflow_dispatch',
                   GITHUB_REF='refs/heads/main', GITHUB_ENV=str(self.dir / 'github-env'),
                   GITHUB_WORKFLOW_REF='manaflow-ai/cmuxterm-hq/.github/workflows/pr-capture-cloud.yml@refs/heads/main',
                   GITHUB_EVENT_PATH=str(event))
        env.update(overrides)
        (self.control / 'assign.json').write_text(json.dumps({'env': env}))

    def clean_receipt(self, **overrides):
        identity = json.loads((self.lease_dir() / 'lease.json').read_text())
        fields = ('lease_id', 'repository', 'run_id', 'run_attempt', 'source_sha', 'workflow', 'job', 'runner_name')
        receipt = dict(version=1, cleaned=True, **{k: identity[k] for k in fields})
        receipt.update(overrides)
        (self.lease_dir() / 'cleanup.json').write_text(json.dumps(receipt))

    def check_hq_contract(self, workflow, job):
        consumer = harness.load('hq_capture_contract', Path(os.environ['HQ_CAPTURE_ADMISSION']))
        request = self.request(workflow=workflow, job=job,
                               runner_label='cmux-capture-' + self.lease_id + '-' + job)
        response = self.run_admit(request)
        self.assertEqual(response.returncode, 0, response.stderr)
        receipt = json.loads(response.stdout)
        hook_sha = request['runtime_sha256']['glaeda-cmux-runner-hook']
        consumer.validate_receipt(request, receipt, hook_sha, now=time.time())
        env = {'GITHUB_JOB': job, 'GITHUB_REPOSITORY': request['repository'], 'GITHUB_REF': request['ref'],
               'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': str(request['run_id']),
               'GITHUB_RUN_ATTEMPT': str(request['run_attempt']), 'GITHUB_SHA': request['source_sha'],
               'GITHUB_WORKFLOW_SHA': request['source_sha'], 'RUNNER_NAME': request['runner_name'],
               'GITHUB_WORKFLOW_REF': request['repository'] + '/.github/workflows/' + workflow + '@refs/heads/main'}
        self.assign(**env)
        self.assertEqual(self.started()['rc'], 0)
        for line in (self.dir / 'github-env').read_text().splitlines():
            key, value = line.split('=', 1)
            env[key] = value
        consumer.verify_target(dict(receipt, request=request), env, hook_sha)
        self.clean_receipt()
        (self.control / 'finish').touch()
        (self.control / 'worker-exit').touch()
        self.wait_for(lambda: self.state().get('phase') == 'released')

    @unittest.skipUnless(os.environ.get('HQ_CAPTURE_ADMISSION'), 'set HQ_CAPTURE_ADMISSION to the HQ consumer')
    def test_real_hq_mac_receipt_contract(self):
        self.check_hq_contract('pr-capture-cloud.yml', 'capture')

    @unittest.skipUnless(os.environ.get('HQ_CAPTURE_ADMISSION'), 'set HQ_CAPTURE_ADMISSION to the HQ consumer')
    def test_real_hq_ios_receipt_contract(self):
        self.check_hq_contract('ios-screen-sharing-proof.yml', 'proof')

    def test_unknown_worker_and_group_keep_quarantine(self):
        module = harness.load('capture_unknown_test', HELPER)
        from unittest import mock
        from types import SimpleNamespace
        probe = object.__new__(module.core.Supervisor)
        state = {'phase': 'quarantined', 'adopted': {'pid': 42, 'identity': 'owner'}}
        probe.lease = SimpleNamespace(state=lambda: state, runner_dir=Path('/fixture'))
        probe.policy = SimpleNamespace(cleaned=lambda lease: True)
        probe.holder_alive = lambda state: False
        probe.hook = SimpleNamespace(JobOwner=lambda *args: SimpleNamespace(gone=lambda: False))
        probe.finish = mock.Mock()
        with mock.patch.object(module.core, 'listener_group', return_value=None):
            self.assertTrue(probe.step([]))
        probe.finish.assert_not_called()

    def test_prune_preserves_cleanup_identity_while_reservation_remains(self):
        module = harness.load('capture_prune_test', HELPER)
        from unittest import mock
        from types import SimpleNamespace
        root = self.dir / 'retained'
        lease_dir = root / self.lease_id
        lease_dir.mkdir(parents=True)
        (lease_dir / 'state.json').write_text(json.dumps({'phase': 'released'}))
        (lease_dir / 'lease.json').write_text('{}')
        marker = self.fleet / 'reservation.json'
        marker.write_text('{}')
        policy = SimpleNamespace(recover=mock.Mock(return_value='cleanup unproven'),
                                 load_frozen_hook=lambda *args: SimpleNamespace(RESERVATION=str(marker)))
        module.core.prune(root, time.time() + module.core.KEEP_TERMINAL_S + 1, policy)
        self.assertTrue(lease_dir.exists())
        policy.recover.assert_called_once()
        marker.unlink()  # this test's marker only
        module.core.prune(root, time.time() + module.core.KEEP_TERMINAL_S + 1, policy)
        self.assertFalse(lease_dir.exists())

    def test_wrong_runtime_or_host_guard_pin_refuses_before_listener(self):
        request = self.request()
        request['runtime_sha256']['glaeda-promotion-admit'] = '0' * 64
        refused = self.run_admit(request)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.control / 'listener-start.json').exists())
        self.assertFalse(any(self.units_held()))

    def test_missing_host_guard_refuses_before_claim(self):
        request = self.request(host_lock_sha256='0' * 64)
        refused = self.run_admit(request)
        self.assertNotEqual(refused.returncode, 0)
        self.assertFalse((self.control / 'listener-start.json').exists())
        self.assertFalse(any(self.units_held()))

    def test_holder_loss_keeps_durable_reservation_until_cleanup(self):
        self.ready()
        self.assign()
        self.assertEqual(self.started()['rc'], 0)
        os.kill(self.state()['holder_pid'], 9)  # this test's own recorded holder only
        self.wait_for(lambda: self.state().get('phase') == 'quarantined')
        marker = self.fleet / 'reservation.json'
        self.assertEqual(json.loads(marker.read_text())['owner'], 'glaeda-capture-admit')
        probe = subprocess.run([sys.executable, '-c',
            "import importlib.machinery as m; h=m.SourceFileLoader('h', __import__('sys').argv[1]).load_module(); "
            "print(h.reservation_refusal(h.RESERVATION, h.time.time()))",
            str(ROOT / 'scripts/glaeda-cmux-runner-hook')], env=self.env,
            capture_output=True, text=True, timeout=10)
        self.assertIn('host reserved by glaeda-capture-admit', probe.stdout)
        self.clean_receipt()
        (self.control / 'finish').touch()
        self.wait_for(lambda: (self.control / 'completed.json').exists())
        time.sleep(0.7)
        self.assertEqual(self.state()['phase'], 'quarantined')
        self.assertTrue(marker.exists())
        (self.control / 'worker-exit').touch()
        self.assertTrue(marker.exists())
        self.clean_receipt()
        self.wait_for(lambda: self.state().get('phase') == 'released')
        self.wait_for(lambda: not marker.exists())

    def test_frozen_hook_survives_installed_generation_change(self):
        self.ready()
        original = self.source / 'glaeda-hooks/glaeda-cmux-runner-hook'
        original.write_text('raise RuntimeError("simulated next OTA generation")\n')
        self.assign()
        self.assertEqual(self.started()['rc'], 0)
        self.clean_receipt()
        (self.control / 'finish').touch()
        (self.control / 'worker-exit').touch()
        self.wait_for(lambda: self.state().get('phase') == 'released')

    def test_capture_holder_ignores_ordinary_maximum_deadline(self):
        path = self.source / 'glaeda-hooks/glaeda-cmux-runner-hook'
        path.write_text(path.read_text().replace('HOLDER_MAX_S = 5 * 24 * 3600', 'HOLDER_MAX_S = 1'))
        request = self.request()
        request['runtime_sha256']['glaeda-cmux-runner-hook'] = hashlib.sha256(path.read_bytes()).hexdigest()
        result = self.run_admit(request)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assign()
        self.assertEqual(self.started()['rc'], 0)
        (self.control / 'finish').touch()
        (self.control / 'worker-exit').touch()
        self.wait_for(lambda: self.state().get('phase') == 'quarantined')
        time.sleep(3)
        self.assertTrue(all(self.units_held()))
        self.clean_receipt()
        self.wait_for(lambda: self.state().get('phase') == 'released')

    def test_missing_cleanup_quarantines_host_after_worker_exit(self):
        self.ready()
        self.assign()
        self.assertEqual(self.started()['rc'], 0)
        (self.control / 'finish').touch()
        (self.control / 'worker-exit').touch()
        self.wait_for(lambda: self.state().get('phase') == 'quarantined')
        self.assertTrue(all(self.units_held()))
        self.clean_receipt(run_attempt=2)
        time.sleep(0.5)
        self.assertEqual(self.state()['phase'], 'quarantined')
        self.assertTrue(all(self.units_held()))
        self.clean_receipt()
        self.wait_for(lambda: self.state().get('phase') == 'released')
        self.assertFalse(any(self.units_held()))

    def test_supervisor_crash_cannot_release_without_cleanup(self):
        self.ready()
        self.assign()
        self.assertEqual(self.started()['rc'], 0)
        supervisor = json.loads((self.lease_dir() / 'supervisor.json').read_text())['pid']
        os.kill(supervisor, 9)  # this test's recorded supervisor only
        (self.control / 'finish').touch()
        (self.control / 'worker-exit').touch()
        self.wait_for(lambda: not self.listener_alive(), why='test worker and ephemeral listener exit')
        time.sleep(5)  # two holder looks; this is the regression against early release
        self.assertTrue(all(self.units_held()))
        refused = subprocess.run([sys.executable, str(HELPER), '--recover', self.lease_id],
                                 env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(refused.returncode, 1, refused.stdout + refused.stderr)
        self.assertTrue(all(self.units_held()))
        self.clean_receipt()
        (self.source / 'glaeda-hooks/glaeda-cmux-runner-hook').write_text('raise RuntimeError("new OTA hook")\n')
        recovered = subprocess.run([sys.executable, str(HELPER), '--recover', self.lease_id],
                                   env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.wait_for(lambda: not any(self.units_held()), why='verified cleanup permits recorded recovery')

    def test_refuses_wrong_scope_before_listener_or_claim(self):
        for change in ({'repository': 'manaflow-ai/cmux'}, {'workflow': 'nightly.yml'}, {'job': 'proof'},
                       {'ref': 'refs/heads/branch'}, {'source_sha': 'x'}, {'run_attempt': True},
                       {'runner_label': 'glaeda-ios-sim'}, {'CLOUD_CAPTURE_PASSWORD': 'no'},
                       {'expires_at': harness.rfc(time.time() + 901)},
                       {'expires_at': harness.rfc(time.time() - 5)}):
            with self.subTest(change=change):
                result = self.run_admit(self.request(**change))
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertFalse((self.control / 'listener-start.json').exists())
                self.assertFalse(any(self.units_held()))

    def test_ready_holds_every_unit_before_listener_then_adopts_exact_job(self):
        receipt = self.ready()
        self.assertEqual({key: receipt.get(key) for key in
                          ('lease_id', 'repository', 'run_id', 'run_attempt', 'source_sha', 'workflow', 'job', 'ref')},
                         {key: self.request()[key] for key in
                          ('lease_id', 'repository', 'run_id', 'run_attempt', 'source_sha', 'workflow', 'job', 'ref')})
        self.assertEqual(receipt['exclusive_claim'], self.lease_id)
        self.assertEqual(receipt['workflow'], 'pr-capture-cloud.yml')
        self.assertRegex(receipt['hook_sha256'], r'^[a-f0-9]{64}$')
        started = json.loads((self.control / 'listener-start.json').read_text())
        self.assertEqual(started['units_held'], [True] * harness.UNITS)
        for token in ('fast-0', 'fast-1', 'gateway-0', 'gateway-1', 'gui'):
            with (self.capacity / (token + '.token')).open() as locked:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (self.fleet / 'host.lock').open() as locked:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertNotIn(harness.JIT, json.dumps(started))
        self.assign()
        self.assertEqual(self.started()['rc'], 0, self.started())
        exported = (self.dir / 'github-env').read_text()
        self.assertIn('GLAEDA_JOB_CLAIM=exclusive', exported)
        self.assertIn('GLAEDA_CAPTURE_LEASE_ID=' + self.lease_id, exported)
        self.assertIn('GLAEDA_HOOK_SHA256=' + receipt['hook_sha256'], exported)
        self.clean_receipt()
        (self.control / 'finish').touch()
        (self.control / 'worker-exit').touch()
        self.wait_for(lambda: self.state().get('phase') == 'released', why='owned worker exit releases lease')
        self.assertFalse(any(self.units_held()))
        self.assertEqual(self.controller.hits, [])

    def test_dedicated_runner_refused_before_any_listener(self):
        wrapper = self.source / 'glaeda-hooks/job-started.sh'
        wrapper.write_text(wrapper.read_text().rstrip() + ' --dedicated-admit /bin/true\n')
        result = self.run_admit(self.request())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse((self.control / 'listener-start.json').exists())
        self.assertFalse(any(self.units_held()))

    def test_ios_job_adopts_only_its_workflow_and_sha(self):
        receipt = self.ready(workflow='ios-screen-sharing-proof.yml', job='proof',
                             runner_label='cmux-capture-' + self.lease_id + '-proof')
        self.assertEqual(receipt['job'], 'proof')
        self.assign(GITHUB_JOB='proof',
                    GITHUB_WORKFLOW_REF='manaflow-ai/cmuxterm-hq/.github/workflows/ios-screen-sharing-proof.yml@refs/heads/main')
        self.assertEqual(self.started()['rc'], 0, self.started())

    def test_workflow_sha_mismatch_receives_no_claim(self):
        self.ready()
        self.assign(GITHUB_WORKFLOW_SHA='b' * 40)
        self.assertNotEqual(self.started()['rc'], 0)
        self.assertFalse((self.dir / 'github-env').exists())

    def test_wrong_job_never_receives_claim(self):
        self.ready()
        self.assign(GITHUB_JOB='other')
        self.assertNotEqual(self.started()['rc'], 0)
        self.assertFalse((self.dir / 'github-env').exists())

    def test_unassigned_listener_expires_without_controller(self):
        self.ready(expires_at=harness.rfc(time.time() + 4))
        self.wait_for(lambda: self.state().get('phase') == 'expired', timeout=15, why='bounded unassigned lease')
        self.assertFalse(any(self.units_held()))
        self.assertEqual(self.controller.hits, [])

    def test_assigned_job_keeps_claim_past_admission_expiry(self):
        self.ready(expires_at=harness.rfc(time.time() + 4))
        self.assign()
        self.assertEqual(self.started()['rc'], 0)
        time.sleep(4)
        self.assertTrue(all(self.units_held()))
        self.assertTrue(self.listener_alive())


if __name__ == '__main__':
    unittest.main()
