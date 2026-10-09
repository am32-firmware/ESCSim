"""Windows firmware build and VS Code debugging support for installed ESCSim."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time

from escsim.renode.process import ProcessTree
from escsim.renode.session import generator_command, generator_environment


def cygpath(root, path):
    return subprocess.check_output(
        [str(root / "bin/cygpath.exe"), "-au", str(path)], text=True
    ).strip()


def build(args):
    repo = args.repo.resolve()
    if not re.fullmatch(r"[A-Za-z0-9_]+", args.target):
        raise ValueError("target must be an AM32 target name")
    out = repo / "build" / ("escsim-" + args.backend)
    target = "AM32_SITL_CAN" if args.backend == "sitl" else args.target
    command = [
        str(args.cygwin / "bin/bash.exe"),
        "--noprofile",
        "--norc",
        "-c",
        'export PATH=/usr/bin:/bin:$PATH; exec make --no-print-directory -B "$@"',
        "escsim-build",
        target,
        "OBJ=" + out.relative_to(repo).as_posix(),
        "SHELL=/bin/bash",
    ]
    if args.backend == "sitl":
        command += ["SITL_CC=gcc", "SITL_CROSS=", "SITL_SANITIZE=", "SITL_COVERAGE="]
    else:
        arm_gdb = (
            args.arm_gdb
            or repo
            / "tools/windows/xpack-arm-none-eabi-gcc-10.3.1-2.3/bin/arm-none-eabi-gdb.exe"
        )
        prefix = cygpath(args.cygwin, arm_gdb.parent / "arm-none-eabi-")
        # Make expands this prefix into shell commands; keep a compiler
        # installed below a user/folder name containing spaces as one word.
        command += ["ARM_SDK_PREFIX=" + shlex.quote(prefix).replace("$", "$$")]
    subprocess.run(command, cwd=repo, check=True)
    header = (repo / "Inc/version.h").read_text()
    version = ".".join(
        re.search(r"#define\s+VERSION_" + part + r"\s+(\d+)", header)[1]
        for part in ("MAJOR", "MINOR")
    )
    image = out / f"AM32_{target}_{version}.elf"
    destination = out / ("firmware.exe" if args.backend == "sitl" else "firmware.elf")
    shutil.copy2(image, destination)
    if args.backend == "sitl":
        shutil.copy2(args.cygwin / "bin/cygwin1.dll", out / "cygwin1.dll")
    print("Built:", destination, flush=True)
    return 0


def workspace(args):
    repo = args.repo.resolve()
    if not (repo / "Inc/targets.h").is_file():
        raise ValueError(
            "select the AM32 firmware source folder, containing Inc/targets.h"
        )
    destination = repo / "ESCSim.code-workspace"
    if destination.exists() and not args.force:
        raise ValueError(
            f"{destination} exists; use --force to replace only this workspace"
        )
    cygwin = args.cygwin.resolve()
    arm_gdb = (
        args.arm_gdb
        or repo
        / "tools/windows/xpack-arm-none-eabi-gcc-10.3.1-2.3/bin/arm-none-eabi-gdb.exe"
    )
    arm_gdb = arm_gdb.resolve()
    required = [
        cygwin / "bin" / (name + ".exe")
        for name in ("bash", "cygpath", "gcc", "gdb", "make")
    ]
    required += [arm_gdb, arm_gdb.with_name("arm-none-eabi-gcc.exe")]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError(
            "Missing development tools: "
            + ", ".join(missing)
            + ". See the installed VS Code setup instructions."
        )
    # Cygwin's python3 can be a Cygwin symlink, unreadable as a native Path.
    subprocess.run(
        [
            str(cygwin / "bin/bash.exe"),
            "--noprofile",
            "--norc",
            "-c",
            "export PATH=/usr/bin:/bin:$PATH; command -v python3",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    install = Path(sys.executable).parent
    cli = str(install / "ESCSim-cli.exe")
    gui = str(install / "ESCSim.exe")
    common = [
        "--repo",
        "${workspaceFolder}",
        "--cygwin",
        str(cygwin),
        "--arm-gdb",
        str(arm_gdb),
        "--target",
        args.target,
    ]
    tasks = []
    launches = []
    for backend, label in (("sitl", "SITL"), ("renode", "Renode")):
        task = "ESCSim: Build " + label
        tasks.append(
            {
                "label": task,
                "type": "process",
                "command": cli,
                "args": ["debug", "build", "--backend", backend, *common],
                "problemMatcher": "$gcc",
                "group": "build",
            }
        )
        tasks.append(
            {
                "label": "ESCSim: " + label + " motor controls",
                "type": "process",
                "command": gui,
                "args": ["control", "--backend", backend],
                "problemMatcher": [],
            }
        )
        launch = {
            "name": "ESCSim " + label,
            "type": "cppdbg",
            "request": "launch",
            "cwd": "${workspaceFolder}",
            "MIMode": "gdb",
            "preLaunchTask": task,
            "externalConsole": False,
            "avoidWindowsConsoleRedirection": True,
        }
        if backend == "sitl":
            launch.update(
                {
                    "program": "${workspaceFolder}/build/escsim-sitl/firmware.exe",
                    "miDebuggerPath": str(cygwin / "bin/gdb.exe"),
                    "sourceFileMap": {cygpath(cygwin, repo): "${workspaceFolder}"},
                    "args": [
                        "--input-type",
                        "1",
                        "--can-uri",
                        "none",
                        "--input-port",
                        "57733",
                        "--state-port",
                        "57734",
                        "--eeprom",
                        "${workspaceFolder}/build/escsim-sitl/eeprom.bin",
                        "--nosleep",
                        "--log",
                        "${workspaceFolder}/build/escsim-sitl/firmware.log",
                        "--wait-for-input",
                        "--exit-on-reset",
                    ],
                    "setupCommands": [{"text": "handle SIGUSR1 nostop noprint pass"}],
                    "stopAtEntry": True,
                }
            )
        else:
            # The native ARM compiler records Windows source paths. Mapping
            # them back to Cygwin paths makes editor breakpoints stay pending.
            elf = "${workspaceFolder}/build/escsim-renode/firmware.elf"
            server_args = [
                "debug",
                "serve",
                "--target",
                args.target,
                "--elf",
                elf,
                "--targets-file",
                "${workspaceFolder}/Inc/targets.h",
                "--outdir",
                "${workspaceFolder}/build/escsim-renode/session",
            ]
            launch.update(
                {
                    "program": elf,
                    "miDebuggerPath": str(arm_gdb),
                    "miDebuggerServerAddress": "127.0.0.1:3333",
                    "debugServerPath": cli,
                    "debugServerArgs": subprocess.list2cmdline(server_args),
                    "serverStarted": "ESCSim GDB ready",
                    "filterStdout": True,
                    "serverLaunchTimeout": 300000,
                    "stopAtConnect": True,
                    "launchCompleteCommand": "None",
                }
            )
        launches.append(launch)
    document = {
        "folders": [{"path": "."}],
        "settings": {},
        "extensions": {"recommendations": ["ms-vscode.cpptools"]},
        "tasks": {"version": "2.0.0", "tasks": tasks},
        "launch": {"version": "0.2.0", "configurations": launches},
    }
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(destination)
    return 0


def serve(args):
    from escsim.renode import download

    if not 1 <= args.gdb_port <= 65535:
        raise ValueError("GDB port must be 1..65535")
    # Fail before starting if another debug session already owns the port.
    for port in (args.gdb_port, 3334):
        with socket.socket() as probe:
            probe.bind(("0.0.0.0", port))
    renode = args.renode or download.install_current()[0]
    command = generator_command() + [
        args.target,
        "--link",
        "--gdb-server",
        "--gdb-port",
        str(args.gdb_port),
        "--monitor-port",
        "3334",
        "--elf",
        str(args.elf.resolve()),
        "--outdir",
        str(args.outdir.resolve()),
        "--renode",
        str(renode),
    ]
    if args.targets_file:
        command += ["--targets-file", str(args.targets_file.resolve())]
    tree = ProcessTree(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=generator_environment(),
    )
    try:

        def output():
            for line in tree.process.stdout:
                print(line, end="", flush=True)

        threading.Thread(target=output, daemon=True).start()
        deadline = time.monotonic() + 120
        while tree.running():
            with socket.socket() as probe:
                try:
                    # Renode binds all interfaces. Windows allows a separate
                    # loopback bind alongside that listener, so match it here.
                    probe.bind(("0.0.0.0", args.gdb_port))
                except OSError:
                    print("ESCSim GDB ready", flush=True)
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("Renode GDB server did not start within 120 seconds")
            time.sleep(0.1)
        return tree.process.wait()
    finally:
        tree.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("workspace", "build"):
        child = actions.add_parser(action)
        child.add_argument("--repo", type=Path, required=True)
        child.add_argument("--target", default="VIMDRONES_L431")
        child.add_argument(
            "--cygwin",
            type=Path,
            default=Path(os.environ.get("AM32_CYGWIN_ROOT", "C:/cygwin64")),
        )
        child.add_argument("--arm-gdb", type=Path)
        if action == "workspace":
            child.add_argument("--force", action="store_true")
        else:
            child.add_argument("--backend", choices=("sitl", "renode"), required=True)
    child = actions.add_parser("serve")
    child.add_argument("--target", required=True)
    child.add_argument("--elf", type=Path, required=True)
    child.add_argument("--targets-file", type=Path)
    child.add_argument("--outdir", type=Path, required=True)
    child.add_argument("--gdb-port", type=int, default=3333)
    child.add_argument("--renode", type=Path)
    args = parser.parse_args(argv)
    try:
        return {"workspace": workspace, "build": build, "serve": serve}[args.action](
            args
        )
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"ESCSim: {error}\n")
