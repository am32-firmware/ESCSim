AM32 SITL for Linux x86_64
=========================

Extract the tar.gz archive and run ./am32-sitl-gui in the extracted folder.
Python and a compiler are not needed. A graphical Linux desktop and its
OpenGL/X11 runtime libraries are required (libGL, libEGL, libxkbcommon,
libfontconfig, libdbus and libxcb-cursor on Debian/Ubuntu).

The GUI bundles precompiled AM32 firmware and bootloader simulators. Leave
both selected, choose DShot input, and click Start simulator. Enable DShot
at zero throttle and wait for the startup/arming tones before increasing
throttle. Use Browse to select alternative Linux SITL binaries. Hardware
firmware images cannot run as host executables.

The virtual scope displays phase voltage/current and other simulated signals.
Stop all simulators before changing the ESC count (one to eight).

Browser USB serial support requires the kernel vhci_hcd module and the
usbip tools provided by your Linux distribution. The GUI's built-in DShot,
DroneCAN and scope controls work without USB/IP attachment.

Sources and licences:
https://github.com/am32-firmware/ESCSim
https://github.com/am32-firmware/AM32
https://github.com/am32-firmware/AM32-bootloader
Qt/PySide6: https://www.qt.io/qt-for-python (LGPLv3)
See LICENSE.txt and LGPL-3.0.txt. Other bundled Python components retain
their upstream licences; see the source repository's SITL/requirements.txt.
