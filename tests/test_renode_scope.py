"""Renode's v2 stream drives the shared DHO804 acquisition UI."""

import json
import socket
import struct
import time

import pytest

from escsim.control.backend import SimStream


def test_scope_consumes_renode_packets_and_requests_instantaneous_samples():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(3)
        sim = SimStream(port=server.getsockname()[1], period_us=50)
        try:
            sim.enabled = sim.scope_enabled = True
            sim.scope.arm(
                "Single",
                trigger="Edge",
                source="vA",
                level=12,
                span=0.0004,
                pretrigger=0.25,
            )
            packet, client = server.recvfrom(128)
            assert struct.unpack("<HBBI", packet) == (sim.MAGIC_CMD, 0, 2, 50000)
            samples = [
                sim.SAMPLE.pack(
                    k * 50000,
                    0,
                    0,
                    0,
                    1,
                    -1,
                    0,
                    0 if k < 8 else 24,
                    0,
                    24,
                    24,
                    1,
                    bytes([0, 1, 2]),
                    0,
                    0,
                )
                for k in range(20)
            ]
            server.sendto(
                struct.pack("<HBB", sim.MAGIC_DATA, 2, len(samples))
                + b"".join(samples),
                client,
            )
            deadline = time.monotonic() + 3
            while sim.scope.snapshot()[1] is None and time.monotonic() < deadline:
                time.sleep(0.01)
            _, frame, state = sim.scope.snapshot()
            assert state == "STOP"
            assert frame.trigger_time == pytest.approx(0.0004)
            assert frame.measurements(0)["sample_interval_s"] == pytest.approx(0.00005)
            assert len(frame.samples[0]) == 15
        finally:
            sim.close()


def test_renode_scope_controls_export_and_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    pytest.importorskip("pyqtgraph")
    from PySide6.QtWidgets import QApplication, QWidget
    from escsim.control import ui
    from escsim.control.scope_ui import DemagScopeWindow

    app = QApplication.instance() or QApplication([])
    container = QWidget()
    args = ui.argument_parser().parse_args(
        ["--backend", "renode", "--state-port", "39874"]
    )
    cleanup = ui.create_ui(args, app=app, container=container)
    scope = None
    try:
        runtime = container._sitl_gui_runtime
        runtime["scope_check"].setChecked(True)
        scope = next(
            w
            for w in app.topLevelWidgets()
            if isinstance(w, DemagScopeWindow) and w.stream is runtime["sim"]
        )
        assert scope.isVisible()
        assert runtime["sim"].scope_enabled
        assert runtime["sample_spin"].minimum() == 20
        assert not scope.trigger.model().item(2).isEnabled()
        source = scope.channels[0][1]
        assert not source.model().item(source.findData("eA")).isEnabled()
        scope.mode.setCurrentText("Auto")
        sim = runtime["sim"]
        sim.scope.feed(
            [
                (
                    k * 0.00005,
                    0,
                    0,
                    0,
                    1,
                    -1,
                    0,
                    24,
                    0,
                    24,
                    24,
                    1,
                    bytes([0, 1, 2]),
                    0,
                    0,
                )
                for k in range(100)
            ]
        )
        scope.refresh()
        assert scope.frame is not None
        assert "20 µs" in scope.notice.text()
        assert "Rebuild SITL" not in scope.notice.text()
        assert len(scope.curves[0].getData()[0]) > 2
        scope.save_csv(tmp_path / "capture.csv")
        metadata = json.loads((tmp_path / "capture.csv.json").read_text())
        assert metadata["backend"] == "renode"
        assert metadata["sample_version"] == 2
        scope.close()
        assert not runtime["scope_check"].isChecked()
        assert not sim.scope.enabled
        assert not sim.scope_enabled
    finally:
        cleanup()
        container.close()
    assert scope is None or not scope.timer.isActive()
