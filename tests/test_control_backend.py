from __future__ import annotations

import socket
import struct
import time

import pytest

from escsim.control import backend
from escsim.control.backend import SimStream


def test_can_panels_share_indexed_raw_command_vector(monkeypatch):
    monkeypatch.setattr(backend.threading.Thread, "start", lambda _self: None)
    command_group = backend.CanCommandGroup()
    first = backend.CanPanel(
        "test:multi", esc_index=0, node_id=126, command_group=command_group
    )
    third = backend.CanPanel(
        "test:multi", esc_index=2, node_id=124, command_group=command_group
    )
    isolated = backend.CanPanel("test:multi", esc_index=1, node_id=122)
    first.enabled = True
    first.throttle = 0.25
    third.enabled = True
    third.throttle = 0.75

    assert first._group_raw_commands() == [int(8191 * 0.25), 0, int(8191 * 0.75)]
    assert third._group_raw_commands() == first._group_raw_commands()
    assert isolated._group_raw_commands() == [0, 0]
    assert first.source_node_id == 126
    assert third.source_node_id == 124

    first.running = False
    third.running = False
    isolated.running = False


def _receive_command(sock, command, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data, _addr = sock.recvfrom(128)
        if len(data) >= 4:
            magic, received, _flags = struct.unpack_from("<HBB", data)
            if magic == SimStream.MAGIC_CMD and received == command:
                return data
    pytest.fail("timed out waiting for simulation command %u" % command)


def test_speedup_is_retained_and_reapplied_when_state_stream_appears():
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    server.settimeout(2.0)
    sim = SimStream(port=server.getsockname()[1])
    try:
        sim.set_speedup(1.0)
        first = _receive_command(server, 2)
        assert struct.unpack_from("<f", first, 4)[0] == pytest.approx(1.0)

        # The panel may set its default before Renode binds the state port.
        # Its first state packet must trigger the retained pacing command.
        server.sendto(
            struct.pack("<HBB", SimStream.MAGIC_DATA, 2, 0),
            sim.sock.getsockname(),
        )
        second = _receive_command(server, 2)
        assert struct.unpack_from("<f", second, 4)[0] == pytest.approx(1.0)
    finally:
        sim.close()
        server.close()


@pytest.mark.parametrize("version", [2, 3])
def test_state_stream_delivers_legacy_and_extended_scope_samples(version):
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    sim = SimStream(port=server.getsockname()[1])
    captured = []
    sim.scope.feed = captured.extend
    try:
        # The v3 suffix carries BEMF/filter/diode/duty/desync values. Two
        # samples catch an incorrect stride as well as a dropped packet.
        layout = struct.Struct("<Q11f3sBB3x" + ("7f3sxfI" if version == 3 else ""))
        suffix = (
            (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, b"\x01\x00\x01", 0.5, 19)
            if version == 3
            else ()
        )
        values = [
            (t, *range(11), b"\x00\x01\x02", 1, 0, *suffix) for t in (100000, 150000)
        ]
        packet = struct.pack("<HBB", SimStream.MAGIC_DATA, version, 2)
        server.sendto(
            packet + b"".join(layout.pack(*row) for row in values),
            sim.sock.getsockname(),
        )
        deadline = time.monotonic() + 2
        while len(captured) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(captured) == 2
        assert list(sim.samples) == captured
        for actual, expected in zip(captured, values):
            assert actual[0] == pytest.approx(expected[0] * 1e-9)
            assert actual[1:] == expected[1:]
    finally:
        sim.close()
        server.close()
