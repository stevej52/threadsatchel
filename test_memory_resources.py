"""Synthetic resource/idle-worker regressions; no real models or memory files."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import memory_ai as worker
from codex_capture import capture_lock
from memory_resources import ResourceMonitor, resource_guard, resource_reason


class ResourcesTests(unittest.TestCase):
    def test_delta_cpu_ram_refresh_and_first_sample_fail_closed(self):
        reader=Mock(side_effect=[(100,200,12000),(190,300,11000),(195,320,500),(240,400,10000)])
        clock=Mock(side_effect=[0,1,1.2,2])
        monitor=ResourceMonitor(reader,clock)
        self.assertFalse(monitor.sample()['available'])
        self.assertEqual(monitor.sample()['cpu_percent'],10)
        cached=monitor.sample()
        self.assertEqual(cached['cpu_percent'],10)
        self.assertEqual(cached['available_mb'],500)
        self.assertEqual(monitor.sample()['cpu_percent'],50)

    def test_counter_errors_and_reset_fail_closed(self):
        monitor=ResourceMonitor(Mock(side_effect=[(100,200,10000),(190,300,10000),
            OSError('private details'),(200,400,10000),(100,200,10000)]),Mock(side_effect=[0,1,3,4]))
        self.assertFalse(monitor.sample()['available'])
        self.assertTrue(monitor.sample()['available'])
        self.assertEqual(monitor.sample(),dict(available=False,cpu_percent=None,available_mb=None))
        self.assertFalse(monitor.sample()['available'])
        self.assertFalse(monitor.sample()['available'])

    def test_guard_busy_low_ram_unknown_and_invalid(self):
        config=dict(idle_max_cpu_percent=25,idle_min_available_mb=8192)
        sample=dict(available=True,cpu_percent=20,available_mb=10000)
        self.assertTrue(resource_guard(config,sample))
        self.assertEqual(resource_reason(config,dict(sample,cpu_percent=30)),'cpu_busy')
        self.assertEqual(resource_reason(config,dict(sample,available_mb=8191)),'low_memory')
        for invalid in ({},dict(sample,available=False),dict(sample,cpu_percent=float('nan')),
                        dict(sample,cpu_percent=True),dict(sample,available_mb=-1)):
            self.assertEqual(resource_reason(config,invalid),'resource_unavailable')


class IdleWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='synthetic-idle-memory-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.config=dict(enabled=True,embeddings=True,idle_embeddings=True,idle_max_cpu_percent=25,
                         idle_min_available_mb=8192,idle_max_chunks=100,idle_budget_seconds=20)
        self.monitor=Mock()
        self.monitor.sample.return_value=dict(available=True,cpu_percent=2,available_mb=30000)
        self.client=Mock(timeout=20)
        self.load=patch.object(worker,'load_config',side_effect=lambda root:dict(self.config))
        self.load.start();self.addCleanup(self.load.stop)

    def run_pass(self):
        return worker.run_idle_embeddings(self.root,self.config,self.monitor,self.client)

    def test_pass_is_cpu_only_bounded_and_timeout_restored(self):
        def process(root,**kwargs):
            self.assertEqual(root,self.root)
            self.assertTrue(kwargs['embeddings_only'])
            self.assertEqual(kwargs['max_records'],100)
            self.assertEqual(kwargs['budget_seconds'],20)
            self.assertIs(kwargs['embedding_client'],self.client)
            self.assertEqual(self.client.timeout,5)
            self.assertTrue(kwargs['should_continue']())
            return dict(state='partial',embedded_chunks=7,indexed_chunks=2,analyzed_chunks=0,
                        errors=[],private_text='NEVER INCLUDED',diagnostics=['NEVER INCLUDED'])
        with patch.object(worker,'process',side_effect=process):
            report=self.run_pass()
        self.assertEqual(report['embedded_chunks'],7)
        self.assertEqual(report['analyzed_chunks'],0)
        self.assertEqual(self.client.timeout,20)
        saved=(self.root/'.ai-cache'/'last-idle-run.json').read_text()
        self.assertNotIn('NEVER INCLUDED',saved)
        self.assertEqual(json.loads(saved),report)

    def test_off_busy_low_memory_unknown_do_not_start_processing(self):
        with patch.object(worker,'process') as process:
            for sample,reason in [(dict(available=True,cpu_percent=90,available_mb=30000),'cpu_busy'),
                    (dict(available=True,cpu_percent=2,available_mb=500),'low_memory'),
                    (dict(available=False,cpu_percent=None,available_mb=30000),'resource_unavailable')]:
                self.monitor.sample.return_value=sample
                report=self.run_pass()
                self.assertEqual(report['state'],'deferred')
                self.assertEqual(report['reason'],reason)
            self.config['idle_embeddings']=False
            self.assertEqual(self.run_pass()['reason'],'disabled')
            process.assert_not_called()

    def test_disable_and_cpu_pressure_are_observed_between_chunks(self):
        def process(root,**kwargs):
            guard=kwargs['should_continue']
            self.assertTrue(guard())
            self.monitor.sample.return_value=dict(available=True,cpu_percent=90,available_mb=30000)
            self.assertFalse(guard())
            self.config['enabled']=False
            self.assertFalse(guard())
            return dict(state='deferred',embedded_chunks=1,errors=[])
        with patch.object(worker,'process',side_effect=process):
            report=self.run_pass()
        self.assertEqual(report['reason'],'disabled')
        self.assertEqual(report['embedded_chunks'],1)

    def test_writer_busy_defers_but_other_errors_remain_visible(self):
        def process(root,**kwargs):
            with capture_lock(root/'.ai-cache'):raise AssertionError('must not acquire')
        with capture_lock(self.root/'.ai-cache'),patch.object(worker,'process',side_effect=process):
            report=self.run_pass()
            self.assertEqual(report['reason'],'writer_busy')
            self.assertEqual(report['state'],'deferred')
        with patch.object(worker,'process',side_effect=PermissionError('secret exception details')):
            report=self.run_pass()
        self.assertEqual(report['state'],'failed')
        self.assertEqual(report['error_type'],'PermissionError')
        self.assertNotIn('secret',json.dumps(report))
        self.assertEqual(self.client.timeout,20)
        def inner_failure(root,**kwargs):
            with capture_lock(root/'.ai-cache'):
                raise PermissionError('private source read denied')
        with patch.object(worker,'process',side_effect=inner_failure):
            report=self.run_pass()
        self.assertEqual(report['state'],'failed')
        self.assertEqual(report['error_type'],'PermissionError')

    def test_normal_job_waits_for_idle_writer_and_has_bounded_defer(self):
        lock=self.root/'.ai-cache'
        try:
            with capture_lock(lock),capture_lock(lock):pass
        except OSError as error:
            busy=error
        with patch.object(worker,'process',side_effect=[busy,dict(state='ok')]) as process,\
                patch.object(worker.time,'sleep') as sleep:
            self.assertEqual(worker.process_with_lock_retry(self.root)['state'],'ok')
            self.assertEqual(process.call_count,2)
            sleep.assert_called_once()
        with patch.object(worker,'process',side_effect=busy),patch.object(worker.time,'sleep') as sleep:
            self.assertEqual(worker.process_with_lock_retry(self.root,retry_seconds=0),
                             dict(enabled=True,state='deferred',reason='writer_busy'))
            sleep.assert_not_called()
        with patch.object(worker,'process',side_effect=PermissionError('not contention')):
            with self.assertRaises(PermissionError):worker.process_with_lock_retry(self.root)

    def test_health_checks_are_independent_guarded_and_due_only(self):
        import memory_health
        self.config.update(idle_embeddings=False,health_checks_enabled=True)
        with patch.object(memory_health,'health_due',return_value=True) as due,\
                patch.object(memory_health,'run_health',return_value={'state':'ok'}) as health:
            worker.run_due_health(self.root,self.config,self.monitor)
            health.assert_called_once_with(self.root,budget_seconds=8)
            due.return_value=False
            worker.run_due_health(self.root,self.config,self.monitor)
            self.assertEqual(health.call_count,1)
            self.monitor.sample.return_value=dict(available=True,cpu_percent=90,available_mb=30000)
            worker.run_due_health(self.root,self.config,self.monitor)
            self.assertEqual(due.call_count,2)

    def test_existing_daemon_closes_owned_client_on_switch_off_after_failed_pass(self):
        import ai_embeddings
        sleeps=[]
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps)==2:self.config['enabled']=False
        with patch.object(ai_embeddings,'EmbeddingClient',return_value=self.client),\
                patch.object(worker,'ResourceMonitor',return_value=self.monitor),\
                patch.object(worker,'lower_process_priority',return_value=True) as priority,\
                patch.object(worker.time,'sleep',side_effect=sleep),\
                patch.object(worker,'process',side_effect=ValueError('private data')),\
                patch.object(worker,'run_due_health') as health:
            worker.embeddings_daemon(self.root)
        priority.assert_called_once()
        self.assertEqual(self.client.ensure_ready.call_count,2)
        self.client.close.assert_called_once()
        self.assertEqual(self.client.timeout,20)
        report=json.loads((self.root/'.ai-cache'/'last-idle-run.json').read_text())
        self.assertEqual(report['state'],'failed')
        self.assertEqual(report['error_type'],'ValueError')
        self.assertNotIn('private',json.dumps(report))


if __name__=='__main__':unittest.main()
