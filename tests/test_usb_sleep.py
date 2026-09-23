from __future__ import annotations

import threading
from unittest.mock import Mock

import pytest

from escsim.control.usb_sleep import UsbSleep


class Host:
    def __init__(self):
        self.ports = {7}  # Another application's USB device.
        self.attach_calls = []
        self.detach_calls = []
        self.next_port = 0

    def attach(self, **kwargs):
        self.attach_calls.append(kwargs)
        port = self.next_port
        self.next_port += 1
        self.ports.add(port)
        return port

    def detach(self, port):
        self.detach_calls.append(port)
        self.ports.remove(port)
        return True

    def port_attached(self, port):
        return port in self.ports


def session():
    host = Host()
    reconnected = Mock()
    usb = UsbSleep(host, reconnected, Mock())
    return host, usb, reconnected


def test_sleep_detaches_only_owned_ports_and_resume_updates_port():
    host, usb, reconnected = session()
    assert usb.attach(unix_path="@our-device", busid="1-1") == 0
    usb.prepare()
    assert host.ports == {7}
    assert usb.port_attached(0)  # Stop still owns the suspended lease.
    usb.resume()
    reconnected.assert_called_once_with(0, 1)
    assert host.ports == {1, 7}
    assert host.attach_calls == [{"unix_path": "@our-device", "busid": "1-1"}] * 2
    assert usb.detach(1)
    assert host.ports == {7}


def test_stop_while_asleep_cancels_reconnect():
    host, usb, reconnected = session()
    usb.attach(host="localhost", port=3240)
    usb.prepare()
    assert usb.detach(0)
    usb.resume()
    assert host.ports == {7}
    assert len(host.attach_calls) == 1
    reconnected.assert_not_called()


def test_failed_resume_forgets_old_port_without_detaching_its_new_owner():
    host, usb, reconnected = session()
    usb.attach(host="localhost", busid="1-0")
    usb.prepare()
    host.ports.add(0)  # Another instance acquired the former port.
    host.attach = Mock(side_effect=OSError("exporter unavailable"))
    usb.resume()
    reconnected.assert_called_once_with(0, None)
    assert host.ports == {0, 7}
    assert not usb.imports
    assert not usb.sleeping.is_set()


def test_detach_failure_does_not_duplicate_connection_on_resume():
    host, usb, reconnected = session()
    usb.attach(unix_path="@test")
    host.detach = Mock(return_value=False)
    with pytest.raises(RuntimeError, match="detach refused"):
        usb.prepare()
    usb.resume()
    assert len(host.attach_calls) == 1
    assert host.ports == {0, 7}
    reconnected.assert_not_called()


def test_close_while_asleep_does_not_reconnect():
    host, usb, reconnected = session()
    usb.attach(unix_path="@test")
    usb.prepare()
    usb.close()
    usb.resume()
    assert host.ports == {7}
    reconnected.assert_not_called()


def test_already_disconnected_exporter_is_not_revived():
    host, usb, reconnected = session()
    usb.attach(unix_path="@test")
    host.ports.remove(0)
    usb.prepare()
    usb.resume()
    assert not usb.imports
    reconnected.assert_not_called()


def test_inflight_attach_is_detached_before_prepare_returns():
    host, usb, _ = session()
    entering = threading.Event()
    proceed = threading.Event()
    attach = host.attach

    def slow_attach(**kwargs):
        entering.set()
        assert proceed.wait(2)
        return attach(**kwargs)

    host.attach = slow_attach
    worker = threading.Thread(target=lambda: usb.attach(unix_path="@test"))
    sleeper = threading.Thread(target=usb.prepare)
    worker.start()
    assert entering.wait(2)
    sleeper.start()
    assert usb.sleeping.wait(2)
    proceed.set()
    worker.join(2)
    sleeper.join(2)
    assert not worker.is_alive() and not sleeper.is_alive()
    assert host.ports == {7}
    usb.resume()


def test_start_during_sleep_waits_until_resume():
    host, usb, _ = session()
    usb.prepare()
    entered = threading.Event()
    attached = threading.Event()

    def start():
        entered.set()
        usb.attach(unix_path="@test")
        attached.set()

    worker = threading.Thread(target=start)
    worker.start()
    assert entered.wait(2)
    assert not attached.wait(0.05)
    usb.resume()
    worker.join(2)
    assert attached.is_set()
    usb.detach(0)


def test_prepare_waits_for_kernel_disconnect(monkeypatch):
    host, usb, _ = session()
    usb.attach(unix_path="@test")
    checks = iter([True, True, False])
    host.port_attached = lambda port: next(checks)
    wait = Mock()
    monkeypatch.setattr("escsim.control.usb_sleep.time.sleep", wait)
    usb.prepare()
    wait.assert_called_once_with(0.02)


def test_kernel_disconnect_timeout_is_reported(monkeypatch):
    host, usb, _ = session()
    usb.attach(unix_path="@test")
    host.port_attached = lambda port: True
    clock = iter([0, 3])
    monkeypatch.setattr("escsim.control.usb_sleep.time.monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="did not disconnect"):
        usb.prepare()


def test_no_blanket_detach():
    _, usb, _ = session()
    with pytest.raises(RuntimeError, match="owned port"):
        usb.detach(None)


def test_logind_delay_is_released_after_detach_and_reacquired_on_resume(monkeypatch):
    import os
    import sys
    import time
    from types import SimpleNamespace

    if not sys.platform.startswith("linux"):
        pytest.skip("Qt D-Bus sleep support is Linux-only")
    qt = pytest.importorskip("PySide6.QtDBus")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    from escsim.control.usb_sleep import watch_system_sleep

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    read_fd, write_fd = os.pipe()
    calls = []

    class Interface:
        def __init__(self, *args):
            pass

        def setTimeout(self, value):
            pass

        def call(self, *args):
            calls.append(args)
            return SimpleNamespace(
                type=lambda: qt.QDBusMessage.ReplyMessage,
                arguments=lambda: [qt.QDBusUnixFileDescriptor(read_fd)],
            )

    bus = Mock()
    bus.connect.return_value = True
    monkeypatch.setattr(qt.QDBusConnection, "systemBus", lambda: bus)
    monkeypatch.setattr(qt, "QDBusInterface", Interface)
    host, usb, restored = session()
    usb.attach(unix_path="@test")
    watcher = watch_system_sleep(app, usb)

    def wait(predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            app.processEvents()
            assert time.monotonic() < deadline
            time.sleep(0.001)

    entered, proceed = threading.Event(), threading.Event()
    original = host.detach

    def detach(port):
        entered.set()
        assert proceed.wait(2)
        return original(port)

    host.detach = detach
    try:
        assert calls[0][-1] == "delay"
        held_fd = watcher.fd.fileDescriptor()
        watcher.prepare(True)
        assert entered.wait(2)
        assert os.fstat(held_fd)  # The lock must remain held during detach.
        proceed.set()
        wait(lambda: watcher.worker is None)
        assert watcher.fd is None
        with pytest.raises(OSError):
            os.fstat(held_fd)
        assert host.ports == {7}
        watcher.prepare(False)
        wait(lambda: watcher.worker is None)
        assert watcher.fd.isValid()
        assert len(calls) == 2
        restored.assert_called_once_with(0, 1)

        # Resume/cancel can arrive before the worker's completion signal.
        watcher.prepare(True)
        watcher.prepare(False)
        wait(lambda: watcher.worker is None and not usb.sleeping.is_set())
        assert len(calls) == 3
        assert restored.call_count == 2
    finally:
        proceed.set()
        watcher.close()
        os.close(read_fd)
        os.close(write_fd)
    assert watcher.fd is None
    bus.disconnect.assert_called_once()
