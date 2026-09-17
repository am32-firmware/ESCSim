"""Exercise packaged-GUI failure cleanup with real POSIX child processes."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from windows_package_test import _kill_package_processes


_CHILD = '''
import json, os, signal, socket, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('127.0.0.1', 0))
Path(sys.argv[1]).write_text(json.dumps(sock.getsockname()[1]))
time.sleep(60)
'''
_PARENT = '''
import os, subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
while not Path(sys.argv[2]).exists():
    time.sleep(.01)
if sys.argv[3] == 'crash':
    os._exit(1)
time.sleep(60)
'''


@unittest.skipUnless(os.name == 'posix', 'POSIX process-group cleanup')
class PackageCleanupTests(unittest.TestCase):
    def check_child_cleanup(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / 'child-port.json'
            proc = subprocess.Popen(
                [sys.executable, '-c', _PARENT, _CHILD, str(ready), mode],
                start_new_session=True)
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        port = json.loads(ready.read_text())
                        break
                    except (FileNotFoundError, json.JSONDecodeError):
                        if time.monotonic() >= deadline:
                            self.fail('child did not bind its UDP port')
                        time.sleep(.01)
                if mode == 'crash':
                    self.assertEqual(proc.wait(timeout=10), 1)
                else:
                    self.assertIsNone(proc.poll())
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    with self.assertRaises(OSError):
                        probe.bind(('127.0.0.1', port))
                _kill_package_processes(proc)
                # The leader may be reaped before its child has finished dying.
                deadline = time.monotonic() + 5
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    while True:
                        try:
                            probe.bind(('127.0.0.1', port))
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                self.fail('orphaned simulator still holds its UDP port')
                            time.sleep(.01)
            finally:
                # Own our group even if the implementation under test regresses.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=10)

    def test_hung_gui(self):
        self.check_child_cleanup('hang')

    def test_crashed_gui(self):
        self.check_child_cleanup('crash')

    def test_unreaped_gui(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        try:
            deadline = time.monotonic() + 10
            while True:
                # Observe the zombie without poll()/wait(), which would reap it.
                state = subprocess.check_output(
                    ['/bin/ps', '-p', str(proc.pid), '-o', 'stat='], text=True).strip()
                if state.startswith('Z'):
                    break
                if time.monotonic() >= deadline:
                    self.fail('GUI did not exit')
                time.sleep(.01)
            _kill_package_processes(proc)
            self.assertEqual(proc.returncode, 0)
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_real_permission_error_is_not_hidden(self):
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                start_new_session=True)
        try:
            with mock.patch('windows_package_test.sys.platform', 'darwin'), \
                    mock.patch('windows_package_test.os.killpg', side_effect=PermissionError):
                with self.assertRaises(PermissionError):
                    _kill_package_processes(proc)
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_already_stopped(self):
        proc = subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)
        try:
            self.assertEqual(proc.wait(timeout=10), 0)
            _kill_package_processes(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
