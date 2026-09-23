"""Detach owned USB/IP imports around Linux system sleep.

The exporters and simulations stay alive. Only the host USB connections are
recreated; serial clients must reopen their ports after resume.
"""

from __future__ import annotations

import threading
import time


class UsbSleep:
    def __init__(self, usbip, reconnected, log=print):
        self.usbip = usbip
        self.reconnected = reconnected
        self.log = log
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.sleeping = threading.Event()
        self.closed = False
        self.imports = {}

    def attach(self, **kwargs):
        with self.condition:
            while self.sleeping.is_set() and not self.closed:
                self.condition.wait()
            if self.closed:
                raise RuntimeError("USB sleep handler is closed")
            port = self.usbip.attach(**kwargs)
            if port is not None and port is not False:
                self.imports[port] = (kwargs, True)
            return port

    def port_attached(self, port):
        with self.lock:
            # A suspended lease still belongs to us, so Stop must forget it.
            if port in self.imports and not self.imports[port][1]:
                return True
            return self.usbip.port_attached(port)

    def detach(self, port):
        if port is None or isinstance(port, bool):
            raise RuntimeError("USB detach requires an owned port")
        with self.lock:
            entry = self.imports.get(port)
            if entry is not None and not entry[1]:
                del self.imports[port]
                return True
            result = self.usbip.detach(port)
            if result:
                self.imports.pop(port, None)
            return result

    def forget(self, port):
        """Drop a lease after a firmware-initiated disconnect."""
        with self.lock:
            self.imports.pop(port, None)

    def prepare(self):
        # Set this before waiting for any in-flight attach or reconnect.
        self.sleeping.set()
        with self.lock:
            for port, (kwargs, attached) in list(self.imports.items()):
                if not attached:
                    continue
                if not self.usbip.port_attached(port):
                    self.imports.pop(port)
                    continue
                if not self.usbip.detach(port):
                    raise RuntimeError(f"USB detach refused on port {port}")
                deadline = time.monotonic() + 2
                while self.usbip.port_attached(port):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"USB port {port} did not disconnect")
                    time.sleep(0.02)
                self.imports[port] = (kwargs, False)
            self.log("USB detached for system sleep")

    def resume(self):
        with self.condition:
            try:
                if self.closed:
                    return
                # Remove all old keys first: another process can occupy our
                # former ports, and new imports may reuse another old key.
                pending = [(p, k) for p, (k, a) in self.imports.items() if not a]
                for port, _ in pending:
                    del self.imports[port]
                failures = 0
                for old_port, kwargs in pending:
                    try:
                        port = self.usbip.attach(**kwargs)
                        if port is None or port is False:
                            raise RuntimeError("USB attach refused")
                        self.imports[port] = (kwargs, True)
                        self.reconnected(old_port, port)
                    except Exception as error:
                        failures += 1
                        self.reconnected(old_port, None)
                        self.log(f"USB reconnect after sleep failed: {error}")
                if pending and not failures:
                    self.log("USB resume complete; reopen the configurator connection")
            finally:
                self.sleeping.clear()
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


def watch_system_sleep(owner, usb):
    """Create the Qt/logind watcher on the GUI thread (Linux only)."""
    import sys

    if not sys.platform.startswith("linux"):
        return None
    from PySide6.QtCore import QObject, Signal, Slot, SLOT

    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage
    except ImportError as error:
        usb.log(f"USB sleep protection unavailable: {error}")
        return None

    class Watcher(QObject):
        finished = Signal(bool, str)

        def __init__(self):
            super().__init__(owner)
            self.fd = None
            self.worker = None
            self.closed = False
            self.pending_sleep = None
            self.bus = QDBusConnection.systemBus()
            self.service = "org.freedesktop.login1"
            self.path = "/org/freedesktop/login1"
            self.interface = self.service + ".Manager"
            self.manager = QDBusInterface(
                self.service, self.path, self.interface, self.bus
            )
            self.manager.setTimeout(2000)
            self.finished.connect(self.complete)
            if not self.bus.connect(
                self.service,
                self.path,
                self.interface,
                "PrepareForSleep",
                self,
                SLOT("prepare(bool)"),
            ):
                usb.log(
                    "USB sleep notifications unavailable: "
                    + self.bus.lastError().message()
                )
                return
            self.inhibit()

        def inhibit(self):
            reply = self.manager.call(
                "Inhibit", "sleep", "ESCSim", "Detach simulated USB devices", "delay"
            )
            if reply.type() == QDBusMessage.ErrorMessage:
                usb.log("USB sleep protection unavailable: " + reply.errorMessage())
                return
            self.fd = reply.arguments()[0]

        def release(self):
            # QDBusUnixFileDescriptor owns the received descriptor.
            self.fd = None

        @Slot(bool)
        def prepare(self, sleeping):
            if self.closed:
                return
            if sleeping:
                usb.sleeping.set()
            if self.worker is not None:
                # A cancelled sleep or another sleep can arrive during I/O.
                self.pending_sleep = sleeping
                return
            if not sleeping:
                self.inhibit()

            def run():
                error = ""
                try:
                    (usb.prepare if sleeping else usb.resume)()
                except Exception as exc:
                    error = str(exc)
                self.finished.emit(sleeping, error)

            self.worker = threading.Thread(target=run, daemon=True)
            self.worker.start()

        @Slot(bool, str)
        def complete(self, sleeping, error):
            self.worker = None
            if error:
                usb.log("USB sleep handling failed: " + error)
            if sleeping:
                self.release()
            if self.pending_sleep is not None and not self.closed:
                pending = self.pending_sleep
                self.pending_sleep = None
                self.prepare(pending)

        def close(self):
            self.closed = True
            usb.close()
            self.bus.disconnect(
                self.service,
                self.path,
                self.interface,
                "PrepareForSleep",
                self,
                SLOT("prepare(bool)"),
            )
            if self.worker is not None:
                self.worker.join()
            self.release()

    return Watcher()
