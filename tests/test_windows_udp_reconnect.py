"""Opt-in native Windows regression for Renode telemetry client replacement."""

import importlib.util
import os
from pathlib import Path
import socket
import struct
import time

import pytest


pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("ESCSIM_TEST_RENODE") != "1",
    reason="requires Windows and ESCSIM_TEST_RENODE=1",
)


def test_telemetry_disconnect_keeps_listener_alive(monkeypatch):
    path = Path(__file__).parents[1] / "scripts/run-parity-tests.py"
    spec = importlib.util.spec_from_file_location("windows_reconnect_parity", path)
    parity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parity)
    wait_ready = parity.wait_ready
    executable = os.environ.get("ESCSIM_TEST_EXE")
    if executable:
        monkeypatch.setattr(
            parity, "generator_command", lambda: [executable, "--internal-generator"]
        )
        monkeypatch.setattr(parity, "built_native_library", lambda: None)

    def reconnect_after_ready(lines, tree):
        args = tree.process.args
        address = ("127.0.0.1", int(args[args.index("--gui-state-port") + 1]))
        if executable:
            # Windowed frozen executables do not emit the console readiness
            # line. Wait for the endpoint, then require actual telemetry below.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and tree.running():
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                    try:
                        probe.bind(address)
                    except OSError:
                        break
                time.sleep(0.2)
            else:
                pytest.fail("Packaged Renode did not bind its telemetry port")
        else:
            wait_ready(lines, tree)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as first:
            first.settimeout(10)
            first.sendto(struct.pack("<HBBI", 0x5353, 0, 1, 1000000), address)
            assert first.recv(4096).startswith(b"TS")
        # Ongoing samples now hit the closed port and generate a Windows
        # ICMP port-unreachable response on the emulator's receiving socket.
        time.sleep(0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as second:
            second.settimeout(0.5)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                second.sendto(struct.pack("<HBB", 0x5353, 10, 0), address)
                try:
                    reply = second.recv(4096)
                except socket.timeout:
                    continue
                assert reply.startswith(b"YS")
                break
            else:
                pytest.fail("Renode stopped answering after its subscriber disconnected")

    monkeypatch.setattr(parity, "wait_ready", reconnect_after_ready)
    repository = parity.ArtifactRepository()
    release = repository.catalog()["channels"]["stable"]["firmware"]
    renode = parity.renode_download.install_current()[0]
    result = parity.run_one(repository, renode, "VIMDRONES_L431", "bdshot", release, "elf")
    assert result["status"] == "passed"
