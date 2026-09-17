AM32 SITL for macOS
==================

This package contains the ESCSim control GUI and prebuilt AM32 firmware
and AM32 bootloader simulators. Python, Xcode and a compiler are not needed.
Use the arm64 package on Apple Silicon or x86_64 package on an Intel Mac.

Launch
------
1. Extract the ZIP. Move am32-sitl-gui.app to Applications if desired.
2. Open am32-sitl-gui.app. This build is ad-hoc signed, not notarized by
   Apple. macOS may require approval in System Settings > Privacy & Security
   before opening a downloaded app.
3. Choose Allow when macOS asks for Local Network access. The GUI, firmware
   and bootloader exchange local UDP messages; DroneCAN uses multicast.
   If access was denied, enable it in System Settings > Privacy & Security
   > Local Network, then restart the app.

Spin a motor
------------
1. In the SITL process tab, leave the bundled firmware and bootloader selected
   and choose DShot input. Browse can select different macOS SITL executables.
   Hardware firmware images cannot run as host executables.
2. Click Start simulator.
3. In PWM / DShot, select dshot600, enable bidir (BDShot), leave throttle at
   zero and tick Enable. Wait for bootloader handoff and armed=yes in the
   simulator status. Then increase throttle. Enable EDT for voltage/current/
   temperature telemetry. Return throttle to zero before stopping.
4. Tick Virtual scope (DHO804) for phase-voltage/current traces. Tile with
   controls arranges both windows on the display. Each ESC has its own tab;
   stop all simulators before changing the ESC count (1 to 8).

Settings are stored outside the app, so replacing it preserves EEPROM data.
The Simulation tab selects motor models and optional benchmark recipes.
The benchmark defaults to None; Start benchmark applies the selected recipe.

DroneCAN
--------
Select DroneCAN input in SITL process before starting. In the DroneCAN tab,
enable the CAN sender at zero throttle, wait for arming, then raise throttle.
Use one input source at a time. For a local-only bench, if macOS does not
loop multicast traffic back, run this in Terminal:

    sudo route -n add -net 239.65.82.0/24 -interface lo0

This route lasts until reboot. Remove it with:

    sudo route -n delete -net 239.65.82.0/24

USB
---
USB/IP attachment is currently supported on Linux and Windows only. This
macOS package supports the built-in controls, scope and UDP/DroneCAN tools;
it does not create a browser-visible USB serial port for am32.ca or Betaflight.

Sources and licences
--------------------
https://github.com/am32-firmware/ESCSim
https://github.com/am32-firmware/AM32
https://github.com/am32-firmware/AM32-bootloader

See LICENSE.txt, THIRD-PARTY.txt and LGPL-3.0.txt.
