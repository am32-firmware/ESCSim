import argparse
import json

import pytest

from escsim import debug


@pytest.fixture
def workspace_args(tmp_path, monkeypatch):
    repo = tmp_path / "AM32 firmware"
    (repo / "Inc").mkdir(parents=True)
    (repo / "Inc/targets.h").touch()
    cygwin = tmp_path / "Cygwin tools"
    (cygwin / "bin").mkdir(parents=True)
    for name in ("bash", "cygpath", "gcc", "gdb", "make"):
        (cygwin / "bin" / (name + ".exe")).touch()
    arm_gdb = tmp_path / "ARM tools/bin/arm-none-eabi-gdb.exe"
    arm_gdb.parent.mkdir(parents=True)
    arm_gdb.touch()
    arm_gdb.with_name("arm-none-eabi-gcc.exe").touch()
    monkeypatch.setattr(
        debug, "cygpath", lambda root, path: "/cygdrive/c/AM32 firmware"
    )
    monkeypatch.setattr(debug.subprocess, "run", lambda *a, **kw: None)
    monkeypatch.setattr(
        debug.sys, "executable", str(tmp_path / "Installed app/ESCSim-cli.exe")
    )
    return argparse.Namespace(
        repo=repo, cygwin=cygwin, arm_gdb=arm_gdb, target="VIMDRONES_L431", force=False
    )


def test_workspace_keeps_existing_vscode_and_uses_matching_debuggers(workspace_args):
    args = workspace_args
    existing = args.repo / ".vscode/launch.json"
    existing.parent.mkdir()
    existing.write_text("user's existing configuration")
    assert debug.workspace(args) == 0
    document = json.loads((args.repo / "ESCSim.code-workspace").read_text())
    sitl, renode = document["launch"]["configurations"]
    assert sitl["miDebuggerPath"] == str(args.cygwin / "bin/gdb.exe")
    assert renode["miDebuggerPath"] == str(args.arm_gdb)
    assert sitl["sourceFileMap"] == {"/cygdrive/c/AM32 firmware": "${workspaceFolder}"}
    assert "sourceFileMap" not in renode
    assert "escsim-sitl/firmware.exe" in sitl["program"]
    assert "escsim-renode/firmware.elf" in renode["program"]
    assert renode["debugServerPath"].endswith("ESCSim-cli.exe")
    assert "--targets-file" in renode["debugServerArgs"]
    assert sitl["setupCommands"] == [{"text": "handle SIGUSR1 nostop noprint pass"}]
    # Cygwin sleeps can reduce simulation speed to a crawl; an unread GDB
    # stderr pipe must not block the firmware either.
    assert "--nosleep" in sitl["args"]
    assert sitl["args"][sitl["args"].index("--log") + 1] == (
        "${workspaceFolder}/build/escsim-sitl/firmware.log"
    )
    tasks = document["tasks"]["tasks"]
    for launch in (sitl, renode):
        task = next(task for task in tasks if task["label"] == launch["preLaunchTask"])
        assert task["type"] == "process"  # Paths containing spaces stay one argument.
        assert str(args.arm_gdb) in task["args"]
    assert existing.read_text() == "user's existing configuration"
    with pytest.raises(ValueError, match="exists"):
        debug.workspace(args)


def test_workspace_missing_compiler_leaves_no_partial_file(workspace_args):
    args = workspace_args
    (args.cygwin / "bin/gcc.exe").unlink()
    with pytest.raises(ValueError, match="Missing development tools"):
        debug.workspace(args)
    assert not (args.repo / "ESCSim.code-workspace").exists()


def test_build_rejects_make_expression_as_target(tmp_path):
    args = argparse.Namespace(repo=tmp_path, target="$(shell unexpected)")
    with pytest.raises(ValueError, match="target"):
        debug.build(args)
