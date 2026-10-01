"""Detach owned USB/IP imports around Linux system sleep.

The exporters and simulations stay alive. Only the host USB connections are
recreated; serial clients must reopen their ports after resume.
"""

from __future__ import annotations

import threading
import time


class UsbAttachment:
    """Stable identity for an import whose host port can change on resume.

    Read port under UsbSleep.lock when it must remain valid across an operation.
    """

    def __init__(self, port, kwargs):
        self.port = port
        self.kwargs = kwargs
        self.attached = True


class UsbSleep:
    def __init__(self, usbip, reconnected, log=print):
        self.usbip = usbip
        self.reconnected = reconnected
        self.log = log
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.sleeping = threading.Event()
        self.closed = False
        self.imports = set()

    def attach(self, *, cancel=None, **kwargs):
        with self.condition:
            while self.sleeping.is_set() and not self.closed:
                if cancel is not None and cancel.is_set():
                    raise RuntimeError("USB attach cancelled")
                # Stop can run on the Qt thread which also receives Resume.
                # Cancellation must not depend on that thread processing events.
                self.condition.wait(timeout=0.05 if cancel is not None else None)
            if self.closed:
                raise RuntimeError("USB sleep handler is closed")
            if cancel is not None and cancel.is_set():
                raise RuntimeError("USB attach cancelled")
            port = self.usbip.attach(**kwargs)
            if port is None or isinstance(port, bool):
                raise RuntimeError(
                    "USB attach refused or did not report its owned port"
                )
            attachment = UsbAttachment(port, kwargs)
            self.imports.add(attachment)
            return attachment

    def port_attached(self, attachment):
        with self.lock:
            if attachment not in self.imports:
                return False
            # A suspended lease still belongs to us, so Stop must forget it.
            return not attachment.attached or self.usbip.port_attached(attachment.port)

    def detach(self, attachment):
        if not isinstance(attachment, UsbAttachment):
            raise RuntimeError("USB detach requires an owned attachment")
        with self.lock:
            if attachment not in self.imports:
                return True
            if not attachment.attached or not self.usbip.port_attached(attachment.port):
                self.forget(attachment)
                return True
            result = self.usbip.detach(attachment.port)
            if result:
                self.forget(attachment)
            return result

    def forget(self, attachment):
        """Drop a lease after a firmware-initiated disconnect."""
        with self.lock:
            if attachment in self.imports:
                self.imports.remove(attachment)
                attachment.port = None
                attachment.attached = False

    def prepare(self):
        # Set this before waiting for any in-flight attach or reconnect.
        self.sleeping.set()
        with self.lock:
            for attachment in list(self.imports):
                if not attachment.attached:
                    continue
                port = attachment.port
                if not self.usbip.port_attached(port):
                    self.forget(attachment)
                    continue
                if not self.usbip.detach(port):
                    raise RuntimeError(f"USB detach refused on port {port}")
                deadline = time.monotonic() + 2
                while self.usbip.port_attached(port):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"USB port {port} did not disconnect")
                    time.sleep(0.02)
                attachment.attached = False
                attachment.port = None
            self.log("USB detached for system sleep")

    def resume(self):
        with self.condition:
            try:
                if self.closed:
                    return
                pending = [a for a in self.imports if not a.attached]
                failures = 0
                for attachment in pending:
                    try:
                        port = self.usbip.attach(**attachment.kwargs)
                        if port is None or isinstance(port, bool):
                            raise RuntimeError("USB attach refused")
                    except Exception as error:
                        failures += 1
                        self.forget(attachment)
                        self.log(f"USB reconnect after sleep failed: {error}")
                    else:
                        attachment.port = port
                        attachment.attached = True
                    self.reconnected(attachment)
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
        usb.log(
            f"USB sleep protection unavailable: {error}. "
            "Virtual USB may prevent suspend. On Debian/Ubuntu install "
            "python3-pyside6.qtdbus and restart ESCSim, or use the GUI venv."
        )
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
