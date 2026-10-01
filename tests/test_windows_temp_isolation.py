"""Opt-in regression for Renode's stale-PID cleanup on Windows."""

import os
from pathlib import Path
import subprocess
import time

import pytest

from escsim.renode import download
from escsim.renode.generator import renode_command
from escsim.renode.process import ProcessTree


pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("ESCSIM_TEST_RENODE") != "1",
    reason="requires Windows and ESCSIM_TEST_RENODE=1",
)


def test_protected_pid_in_shared_temp_cannot_break_startup(tmp_path):
    # PID 0 is Windows' protected Idle process. This is a synthetic stale
    # Renode directory in a test-owned root, not a real process directory.
    stale = tmp_path / "renode-0"
    stale.mkdir()
    marker = stale / "leave-alone.txt"
    marker.write_text("unrelated process data")
    env = dict(os.environ, TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path))
    renode = download.cached(download.default_cache()) or download.install_current()[0]
    command = renode_command(renode, 0, 'python "from Antmicro.Renode.Utilities import TemporaryFilesManager; '
                             'print TemporaryFilesManager.Instance.EmulatorTemporaryPath"; quit')
    logs = []
    for isolated in (False, True):
        log = tmp_path / ("isolated.log" if isolated else "shared.log")
        with log.open("w") as stream:
            tree = ProcessTree(command, isolate_temp=isolated, env=env,
                               stdin=subprocess.PIPE, stdout=stream,
                               stderr=subprocess.STDOUT)
            if isolated:
                private_root = Path(tree._temp_directory.name)
            try:
                # A failed startup command aborts the command list before
                # `quit`. Observe that error while retaining an open stdin.
                deadline = time.monotonic() + 45
                while tree.running() and time.monotonic() < deadline:
                    if not isolated and "Access is denied" in log.read_text(errors="replace"):
                        break
                    time.sleep(0.1)
            finally:
                tree.stop()
        logs.append(log.read_text(errors="replace"))
    assert "Access is denied" in logs[0], logs[0]
    assert "Access is denied" not in logs[1], logs[1]
    assert "escsim-process-" in logs[1], logs[1]
    assert "TypeInitializationException" not in logs[1], logs[1]
    assert marker.read_text() == "unrelated process data"
    # The private directory printed by Renode was cleaned with its owner.
    assert not private_root.exists()
