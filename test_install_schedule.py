"""Inspect generated Windows tasks and Linux timers without installing any."""
import base64
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import install_schedule as schedule

class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='threadsatchel-SYNTHETIC-schedule-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ('capture-local.json', 'capture-export.json', 'remote-capture-config.json'):
            (self.root/name).write_text('{}')
        (self.root/'memory.sqlite3').touch()
        (self.root/'python.exe').touch()
        (self.root/'pythonw.exe').touch()
        self.enterContext(patch.object(schedule, 'ROOT', self.root))
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.process = self.enterContext(patch.object(schedule.subprocess, 'run'))

    def invoke(self, mode, platform, os_name):
        fake_sys = SimpleNamespace(executable=str(self.root/'python.exe'), platform=platform)
        with patch.object(schedule, 'sys', fake_sys), patch.object(schedule, 'os', SimpleNamespace(name=os_name)), \
             patch('sys.argv', ['install_schedule.py', mode]):
            schedule.main()

    def test_windows_sync_is_five_minutes_and_retains_limited_logon_task(self):
        for mode, minutes in (('local', 1), ('export', 1), ('sync', 5), ('inbox', 2)):
            with self.subTest(mode=mode):
                self.invoke(mode, 'win32', 'nt')
                command = base64.b64decode(self.process.call_args.args[0][-1]).decode('utf-16-le')
                self.assertIn('New-TimeSpan -Minutes ' + str(minutes), command)
                self.assertIn('AtLogOn -User $u', command)
                self.assertIn('-LogonType Interactive -RunLevel Limited', command)
                self.assertIn('-MultipleInstances IgnoreNew', command)
                self.assertIn('Task already exists; inspect it before replacing', command)
                self.assertIn('pythonw.exe', command)

    def test_linux_sync_is_five_minutes_without_changing_other_timers(self):
        with patch.object(Path, 'home', return_value=self.root):
            for mode, seconds in (('local', 60), ('export', 60), ('sync', 300), ('inbox', 120)):
                with self.subTest(mode=mode):
                    self.invoke(mode, 'linux', 'posix')
                    timer = self.root/'.config/systemd/user'/('threadsatchel-' + mode + '.timer')
                    self.assertIn('OnUnitInactiveSec=' + str(seconds) + '\n', timer.read_text())
                    self.assertIn('OnBootSec=60', timer.read_text())
                    self.assertEqual(self.process.call_args.args[0],
                                     ['systemctl', '--user', 'start', 'threadsatchel-' + mode + '.service'])

if __name__ == '__main__':
    unittest.main()
