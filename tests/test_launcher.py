"""Real TCP preflight and bounded launcher child lifecycle regression checks."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import errno
import io
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from paper_alpha.server import launcher


class LauncherTests(unittest.TestCase):
    def free_port(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            return listener.getsockname()[1]

    def time_wait_port(self):
        # The server actively closes the accepted connection, so TIME_WAIT is
        # attached to the listening port rather than the client's ephemeral port.
        with ExitStack() as stack:
            listener = stack.enter_context(socket.socket())
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            listener.settimeout(2)
            port = listener.getsockname()[1]
            client = stack.enter_context(socket.create_connection(('127.0.0.1', port), timeout=2))
            accepted, _ = listener.accept()
            stack.enter_context(accepted)
            accepted.settimeout(2)
            accepted.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(1), b'')
            client.close()
            self.assertEqual(accepted.recv(1), b'')
        return port

    def launch_helpers(self, port, *, stop_immediately=False, first_exits=False, spawn_fails=False):
        created = []
        popen = subprocess.Popen
        stopped = threading.Event()
        if stop_immediately:
            stopped.set()

        def create_helper(command, **kwargs):
            if created and spawn_fails:
                raise OSError('Controlled second-service launch failure')
            program = 'raise SystemExit(7)' if first_exits and not created else 'import time; time.sleep(30)'
            child = popen([sys.executable, '-c', program], **kwargs)
            created.append(child)
            return child

        try:
            with tempfile.TemporaryDirectory() as temporary, \
                    patch.object(launcher.Path, 'is_file', return_value=True), \
                    patch.object(launcher.subprocess, 'Popen', side_effect=create_helper), \
                    patch.object(launcher.threading, 'Event', return_value=stopped), \
                    patch.object(launcher.signal, 'signal'), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if spawn_fails:
                    with self.assertRaisesRegex(OSError, 'Controlled second-service'):
                        launcher.main(['--home', str(Path(temporary) / 'workspace'), '--port', str(port)])
                    result = None
                else:
                    result = launcher.main(['--home', str(Path(temporary) / 'workspace'), '--port', str(port)])
            self.assertTrue(created)
            self.assertTrue(all(child.poll() is not None for child in created), 'Launcher left a service running')
            return result, [child.returncode for child in created]
        finally:
            # A failing assertion must never leave test children behind.
            for child in created:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)

    def test_recently_closed_tcp_connection_does_not_block_restart(self):
        port = self.time_wait_port()
        with socket.socket() as bare_probe:
            try:
                bare_probe.bind(('127.0.0.1', port))
            except OSError as exc:
                self.assertEqual(exc.errno, errno.EADDRINUSE)
            else:
                # Some TCP implementations do not keep this local tuple in
                # TIME_WAIT. Do not invent coverage when the precondition is absent.
                self.skipTest('This platform did not retain the closed server tuple in TIME_WAIT')
        result, children = self.launch_helpers(port, stop_immediately=True)
        self.assertEqual(result, 0)
        self.assertEqual(len(children), 2)
        # Prove an actual subsequent listener can bind, not merely the probe.
        with socket.socket() as restarted:
            restarted.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            restarted.bind(('127.0.0.1', port))
            restarted.listen(1)

    def test_live_listener_still_rejects_before_starting_any_service(self):
        for host in ('127.0.0.1', '0.0.0.0'):
            with self.subTest(host=host), socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind((host, 0))
                occupied.listen(1)
                port = occupied.getsockname()[1]
                with patch.object(launcher.Path, 'is_file', return_value=True), \
                        patch.object(launcher.subprocess, 'Popen') as popen, \
                        redirect_stderr(io.StringIO()) as message:
                    with self.assertRaises(SystemExit) as raised:
                        launcher.main(['--port', str(port)])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn(f'Port {port} is already occupied', message.getvalue())
                    popen.assert_not_called()
                # The preflight sends no application bytes and does not close
                # the original listener. Its accepted connection sees only EOF.
                occupied.settimeout(1)
                observed, _ = occupied.accept()
                with observed:
                    observed.settimeout(1)
                    self.assertEqual(observed.recv(1), b'')

    def test_inconclusive_connection_probe_does_not_start_services(self):
        with patch.object(launcher.Path, 'is_file', return_value=True), \
                patch.object(launcher.socket, 'socket') as socket_factory, \
                patch.object(launcher.subprocess, 'Popen') as popen, \
                redirect_stderr(io.StringIO()):
            probe = socket_factory.return_value.__enter__.return_value
            probe.connect_ex.return_value = errno.ETIMEDOUT
            with self.assertRaises(SystemExit) as raised:
                launcher.main(['--port', '18765'])
            self.assertEqual(raised.exception.code, 2)
            probe.settimeout.assert_called_once_with(.25)
            popen.assert_not_called()

    def test_service_exit_stops_and_reaps_the_other_service(self):
        result, statuses = self.launch_helpers(self.free_port(), first_exits=True)
        self.assertEqual(result, 1)
        self.assertEqual(statuses[0], 7)
        self.assertEqual(statuses[1], -signal.SIGTERM)

    def test_stopping_launcher_stops_and_reaps_both_services(self):
        result, statuses = self.launch_helpers(self.free_port(), stop_immediately=True)
        self.assertEqual(result, 0)
        self.assertEqual(statuses, [-signal.SIGTERM, -signal.SIGTERM])

    def test_partial_launch_failure_reaps_the_already_started_service(self):
        _, statuses = self.launch_helpers(self.free_port(), spawn_fails=True)
        self.assertEqual(statuses, [-signal.SIGTERM])


if __name__ == '__main__':
    unittest.main()
