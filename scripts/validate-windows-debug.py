#!/usr/bin/env python3
"""Exercise a generated workspace with VS Code's installed C++ debug adapter.

Run on Windows after building both workspace firmware tasks. The adapter and
all its children are owned by ProcessTree, with a finite timeout per operation.
"""

import argparse
import json
from pathlib import Path
import queue
import subprocess
import threading
import time

from escsim.renode.process import ProcessTree


class Adapter:
    def __init__(self, executable):
        self.tree = ProcessTree([str(executable)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.messages = queue.Queue()
        self.pending = []
        self.sequence = 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        stream = self.tree.process.stdout
        while True:
            header = stream.readline()
            if not header:
                return
            if not header.startswith(b"Content-Length:"):
                continue
            size = int(header.split(b":", 1)[1])
            while stream.readline().strip():
                pass
            message = json.loads(stream.read(size))
            print(json.dumps(message), flush=True)
            self.messages.put(message)

    def send(self, command, arguments=None):
        self.sequence += 1
        payload = json.dumps({"seq": self.sequence, "type": "request",
                              "command": command, "arguments": arguments or {}}).encode()
        self.tree.process.stdin.write(f"Content-Length: {len(payload)}\r\n\r\n".encode() + payload)
        self.tree.process.stdin.flush()
        return self.sequence

    def wait(self, predicate, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.pending):
                if message.get("type") == "response" and message.get("success") is False:
                    raise RuntimeError(message)
                if predicate(message):
                    return self.pending.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("debug adapter response timed out")
            self.pending.append(self.messages.get(timeout=remaining))

    def response(self, sequence):
        reply = self.wait(lambda message: message.get("request_seq") == sequence)
        if not reply.get("success"):
            raise RuntimeError(reply)
        return reply.get("body", {})

    def request(self, command, arguments=None):
        return self.response(self.send(command, arguments))

    def event(self, name):
        return self.wait(lambda message: message.get("event") == name).get("body", {})


def validate(executable, workspace, backend, build=False):
    workspace = workspace.resolve()
    document = json.loads(workspace.read_text())
    launch = next(item for item in document["launch"]["configurations"]
                  if item["name"] == "ESCSim " + backend)
    launch = json.loads(json.dumps(launch).replace("${workspaceFolder}",
                                                 workspace.parent.as_posix()))
    task_name = launch.pop("preLaunchTask", None)
    if build:
        task = next(item for item in document["tasks"]["tasks"] if item["label"] == task_name)
        command = [task["command"], *task["args"]]
        command = [part.replace("${workspaceFolder}", workspace.parent.as_posix()) for part in command]
        subprocess.run(command, cwd=workspace.parent, check=True, timeout=600)
    adapter = Adapter(executable)
    try:
        adapter.request("initialize", {"adapterID": "cppdbg", "linesStartAt1": True,
                        "columnsStartAt1": True, "pathFormat": "path"})
        launching = adapter.send("launch", launch)
        adapter.event("initialized")
        function = "sitl_input_init" if backend == "SITL" else "main"
        result = adapter.request("setFunctionBreakpoints", {
            "breakpoints": [{"name": function}]})
        assert result["breakpoints"][0]["verified"], result
        adapter.request("configurationDone")
        adapter.response(launching)
        stopped = adapter.event("stopped")
        adapter.request("continue", {"threadId": stopped["threadId"]})
        stopped = adapter.event("stopped")
        stack = adapter.request("stackTrace", {"threadId": stopped["threadId"]})
        frame = stack["stackFrames"][0]
        assert function in frame["name"], frame
        assert frame.get("source", {}).get("path") and frame["line"] > 0, frame
        assert Path(frame["source"]["path"]).is_file(), frame
        # Function breakpoints do not exercise VS Code's reverse source-path
        # mapping. Verify a gutter breakpoint at a known executable line too.
        source_breakpoint = adapter.request("setBreakpoints", {
            "source": {"path": frame["source"]["path"]},
            "breakpoints": [{"line": frame["line"]}]})
        assert source_breakpoint["breakpoints"][0]["verified"], source_breakpoint
        adapter.request("evaluate", {"expression": "$pc", "frameId": frame["id"]})
        memory = adapter.request("readMemory", {
            "memoryReference": frame["instructionPointerReference"], "count": 16})
        assert memory.get("data"), memory
        scopes = adapter.request("scopes", {"frameId": frame["id"]})
        for scope in scopes["scopes"]:
            adapter.request("variables", {"variablesReference": scope["variablesReference"]})
        adapter.request("next", {"threadId": stopped["threadId"]})
        stepped = adapter.event("stopped")
        adapter.request("stackTrace", {"threadId": stepped["threadId"]})
        adapter.request("disconnect", {"terminateDebuggee": True})
        # Check normal adapter shutdown before the outer safety net stops it.
        adapter.tree.process.wait(timeout=30)
        print(f"PASS {backend}: function/source breakpoints, source, scopes, variables, registers, memory, step, disconnect", flush=True)
    finally:
        adapter.tree.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--backend", choices=("SITL", "Renode"))
    parser.add_argument("--build", action="store_true", help="run each workspace build task first")
    args = parser.parse_args()
    for backend in ([args.backend] if args.backend else ["SITL", "Renode"]):
        validate(args.adapter, args.workspace, backend, args.build)


if __name__ == "__main__":
    main()
