# Windows test status

The native Windows 11 lab sweep uses firmware release 2.21, the verified
Renode 1.16.1 build from firmware.ardupilot.org, native Python 3.12, and a
64-bit MinGW-built `am32sim.dll`. Each cell was run with both the published ELF
and Intel HEX image; the two formats produced the same result.

| MCU family | Representative target | PWM | DShot600 | BDShot |
|---|---|---:|---:|---:|
| A153 | FRDM_A153 | pass | pass | pass |
| E230 | GD32DEV_A_E230 | pass | pass | pass |
| F031 | CRAWLMASTER_F031 | pass | pass | pass |
| F051 | AGFRC_V2_F051 | pass | pass | pass |
| F415 | AT32DEV_F415 | pass | pass | pass |
| F421 | AIKON_55A_F421 | pass | pass | pass |
| G031 | GEN_G031 | pass | pass | pass |
| G071 | AIKON_04_G071 | pass | pass | pass |
| G431 | AS_G431 | pass | pass | pass |
| L431 | NEUTRON_L431 | pass | pass | pass |
| V203 | AIRBOT_V203 | pass | pass | pass |

This is 66 passing cells out of 66. Every cell starts a fresh Renode process,
waits for the firmware's reported armed state, advances the throttle, and for
BDShot also requires decoded telemetry replies.

Windows-specific validation also covers:

- 240 native Python/Qt tests passing and 10 skipped in the DHO804 installer
  validation at `c058fe0`; this is an earlier broad run, not a claim that every
  test was rerun for subsequent documentation/debugger changes;
- native DLL build and smoke executable;
- usbip-win2 0.9.7.7 attach, exact serial identity, COM-port enumeration,
  28-byte serial echo, and exact owned-port detach;
- PyInstaller application build, packaged CLI and offscreen GUI startup;
- a per-user Inno Setup installer with the verified usbip-win2 prerequisite,
  silent non-driver installation, artifact and Renode discovery, and target
  generation from the installed application.

Run the comprehensive matrix and USB/IP check with the Makefile commands in
[packaging.md](packaging.md). The scheduled Windows workflow fails if any
matrix cell fails and still uploads the full JSON report for diagnosis.

## Fresh-checkout walkthrough validation

The [SITL guide](windows-vscode-sitl.md) and [Renode guide](windows-vscode-renode.md)
were exercised on Windows 11 on 2026-10-02 using a separate VS Code profile,
Microsoft C/C++ 1.34.4, and a clean upstream AM32 checkout at
`2738df3240baa5bd4295b460cf0c5cfe0bd49d97`. The setup scripts installed the ARM
toolchain and detected the existing Cygwin installation (GCC 14.4.0, GDB 17.2,
Make 4.4.1, Python 3.12.12). A completely new Cygwin installation was not tested.

Actual RDP screenshots cover the welcome screen, extension installation,
installer directory/shortcuts, source checkout, toolchain verification,
workspace generation, both debugger entries, source breakpoints, control
tasks, running motors, and DHO804 captures. These are application captures,
not mockups. See [capture details](images/windows-vscode/README.md).

- Renode: built SEQURE_G431, stopped at reset and a source gutter breakpoint,
  inspected registers, continued to arming, drove DShot300 from motor controls,
  and captured phase voltage/current, bus voltage and comparator signals.
  CSV and setup export succeeded. The real C/C++ debug-adapter check passed
  function and source breakpoints, variables, registers, memory, stepping and
  disconnect after correcting the native Windows source-path mapping.
- SITL: built the host firmware, stopped at host `main` and firmware
  `am32_main`, controlled the motor with DShot300 (about 1,559 RPM), and saved a
  four-channel DHO804 single capture with 0.5 µs samples. The shared controls
  now accept the extended scope telemetry. Enabling the existing `--nosleep`
  timing mode restored approximately real-time operation under Cygwin GDB;
  without it the measured stream was only about 44 samples/s at a requested
  20,000 samples/s. Generated workspaces now set this option and log firmware
  diagnostics to a file.
- The final generated SITL launch also passed the real C/C++ debug-adapter
  checks for function/source breakpoints, variables, registers, memory, stepping
  and disconnect, using the restored executable without rebuilding.
- The focused workspace/stream regression suite passed all 7 tests on both
  Linux and Windows. Packaged Renode controls also opened and closed the
  DHO804 scope successfully.
  The full hardware-family matrix above predates these debugger/UI changes.

**Release blocker:** Defender detected the freshly built demonstration SITL
`firmware.exe` as `Trojan:Win32/Bearfoos.A!ml` (signature 1.459.506.0). This has
not been established as a false positive. The debug/scope test used an
explicitly approved temporary exclusion for that exact file only, and the
already-built executable was restored from quarantine. The exclusion was
removed after testing, the normal workspace build task was restored, and no
test firmware or Renode processes were left running. Its intermediate ELF
remained blocked, so the debugger test skipped the pre-launch rebuild; this
was not an uninterrupted clean F5 validation. Do not treat those screenshots
as evidence that a fresh Windows installation will pass Defender. Resolve
and recheck this detection before publishing the release or declaring the
fresh-checkout SITL workflow fully validated.
