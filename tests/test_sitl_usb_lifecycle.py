"""Exercise USB lifecycle races through the real SITL Qt controls."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith(("linux", "win")),
    reason="The SITL USB controls require Linux or Windows",
)


@pytest.fixture
def bench(monkeypatch, tmp_path):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    widgets = pytest.importorskip("PySide6.QtWidgets")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "SITL"))
    import sitl_gui
    import sitl_usbip
    import msp_stub_fc

    # USB lifecycle tests do not need firmware submodules or EEPROM files.
    monkeypatch.setattr(sitl_gui.sim_runner, "bundled_eeprom", lambda index: None)

    app = widgets.QApplication.instance() or widgets.QApplication([])
    ports = {9}  # Another application's USB device.
    next_ports = iter(range(8))
    stubs = []
    detached = []

    class Endpoint:
        unix_path = "@test"
        host = "127.0.0.1"
        port = 3240

        def __init__(self, **kwargs):
            pass

        def close(self):
            pass

    class Stub:
        def __init__(self, **kwargs):
            self.closed = False
            stubs.append(self)

        def close(self):
            self.closed = True

    def attach(**kwargs):
        port = next(next_ports)
        ports.add(port)
        return port

    def detach(port):
        detached.append(port)
        ports.remove(port)
        return True

    monkeypatch.setattr(sitl_usbip, "UsbipServer", Endpoint)
    monkeypatch.setattr(msp_stub_fc, "MspStubFC", Stub)
    monkeypatch.setattr(sitl_usbip, "attach", attach)
    monkeypatch.setattr(sitl_usbip, "detach", detach)
    monkeypatch.setattr(sitl_usbip, "port_attached", lambda port: port in ports)
    monkeypatch.setattr(sitl_usbip, "find_tty", lambda *a, **k: "test-tty")
    args = SimpleNamespace(
        host="127.0.0.1",
        port=39863,
        state_port=39864,
        can_uri="mcast:91",
        poles=14,
        control_port=0,
        log=None,
        replay=None,
        esc_count=2,
    )
    fleet = sitl_gui.EscFleet(args, app)
    panel = fleet.panels[0]

    def wait(predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            app.processEvents()
            assert time.monotonic() < deadline, panel.usb_status.text()
            time.sleep(0.001)

    def start():
        panel.usb_mode.setCurrentIndex(sitl_gui.USB_BETAFLIGHT)
        wait(lambda: panel.usb_mode.isEnabled())
        assert panel.usb_sleep.imports, panel.usb_status.text()

    try:
        yield SimpleNamespace(
            app=app,
            fleet=fleet,
            panel=panel,
            ports=ports,
            stubs=stubs,
            raw=sitl_usbip,
            gui=sitl_gui,
            wait=wait,
            start=start,
            detached=detached,
        )
    finally:
        panel.usb_sleep.close()
        fleet.close()
        fleet.win.close()


def test_resume_between_attach_return_and_publication(bench, monkeypatch):
    usb = bench.panel.usb_sleep
    attach = usb.attach

    def interrupted_attach(**kwargs):
        attachment = attach(**kwargs)
        usb.prepare()
        bench.ports.add(0)  # Old port is reused before the caller sees its handle.
        usb.resume()
        return attachment

    monkeypatch.setattr(usb, "attach", interrupted_attach)
    bench.start()
    bench.panel.usb_mode.setCurrentIndex(bench.gui.USB_OFF)
    assert not usb.imports
    assert bench.ports == {0, 9}
    assert bench.detached == [0, 1]
    assert bench.stubs[0].closed


def test_usb_off_cancels_sleeping_attach_without_gui_resume(bench, monkeypatch):
    usb = bench.panel.usb_sleep
    usb.prepare()
    entered = threading.Event()
    wait = usb.condition.wait

    def waiting(*args, **kwargs):
        entered.set()
        return wait(*args, **kwargs)

    monkeypatch.setattr(usb.condition, "wait", waiting)
    bench.panel.usb_mode.setCurrentIndex(bench.gui.USB_BETAFLIGHT)
    assert entered.wait(2)
    # Rescue a regressed implementation, so this test fails instead of hanging
    # the whole suite. The Qt thread must return before the rescue is needed.
    rescued = threading.Event()

    def rescue():
        rescued.set()
        usb.close()

    timer = threading.Timer(2, rescue)
    timer.start()
    try:
        bench.panel.usb_mode.setCurrentIndex(bench.gui.USB_OFF)
        assert not rescued.is_set()
        assert usb.sleeping.is_set()
        assert bench.ports == {9}
        assert bench.stubs[0].closed
        # The cancelled worker's queued result must not change the new mode.
        usb.resume()
        bench.start()
        bench.wait(lambda: bench.panel.usb_status.text() == "test-tty")
        assert bench.panel.usb_mode.currentIndex() == bench.gui.USB_BETAFLIGHT
    finally:
        timer.cancel()
        timer.join()


@pytest.mark.parametrize("fleet_close", [False, True])
def test_detach_failure_still_closes_exporter_and_esc_runners(
    bench, monkeypatch, capsys, fleet_close
):
    bench.start()
    monkeypatch.setattr(bench.raw, "detach", lambda port: False)
    stops = []
    for panel in bench.fleet.panels:
        stop = Mock(wraps=panel.runner.stop)
        monkeypatch.setattr(panel.runner, "stop", stop)
        stops.append(stop)
    if fleet_close:
        bench.fleet.close()
        assert all(stop.called for stop in stops)
    else:
        bench.panel.close()
        stops[0].assert_called_once()
    assert bench.stubs[0].closed
    assert bench.ports == {0, 9}
    assert "USB detach was refused" in capsys.readouterr().err
