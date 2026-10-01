ESCSim for Windows: Renode and SITL
=================================

Run ESCSim-installer.exe. It installs both applications for the current user
under %LOCALAPPDATA%\Programs\ESCSim by default. The installer always shows
a folder chooser; choose any suitable installation folder. Upgrades remember
the previously chosen folder, which you can change on that page.
The Start menu and optional desktop shortcuts provide two launchers:
  ESCSim Renode - runs hardware firmware on an emulated microcontroller.
  ESCSim SITL   - runs firmware compiled for the Windows host.
Python and a compiler are not needed to run the bundled applications.
Renode and published firmware are downloaded by the Renode launcher on first
use. USB configurator access optionally needs the bundled usbip-win2 driver.

Developing your own firmware with Visual Studio Code
---------------------------------------------------
1. Install VS Code and its Microsoft C/C++ extension (ms-vscode.cpptools).
2. Check out current AM32 firmware sources from:
   https://github.com/am32-firmware/AM32
3. In that checkout, run env_setup_scripts\gcc_windows_env_setup.cmd for
   the AM32 ARM compiler/GDB and env_setup_scripts\sitl_setup_windows.cmd
   for the Cygwin host compiler/GDB. Cygwin also needs python3 for the AM32
   hardware-image signature step; select it in Cygwin Setup if absent.
4. From Start, choose "Configure ESCSim for VS Code". Select the firmware
   checkout, enter the hardware target name and your Cygwin installation.
   This creates ESCSim.code-workspace without changing your .vscode files.
5. Open that workspace. Choose "ESCSim SITL" or "ESCSim Renode" in Run and
   Debug and press F5. Each configuration builds its own firmware first.

SITL uses the Cygwin gdb.exe matched to the compiler. It starts at main;
set a breakpoint in am32_main or tenKhzRoutine, then Continue. SIGUSR1
is passed through so the emulated interrupts can run. Reset ends a debug
session; press F5 again to restart it. Builds go in build/escsim-sitl.

Renode uses arm-none-eabi-gdb.exe and a GDB server owned by VS Code.
It stops at the reset vector. Set a breakpoint in main and Continue;
source breakpoints, stepping, locals, registers and memory are available.
F5 loads the freshly built ELF directly, without a hardware bootloader.
Builds go in build/escsim-renode. The supplied workspace supports ARM
targets; RISC-V targets need a matching RISC-V toolchain/configuration.

To send throttle and view the motor while debugging, use Terminal > Run
Task > "ESCSim: SITL motor controls" or "ESCSim: Renode motor controls".
Start only one backend on these default ports at a time. Leave the ordinary
launchers' simulator Start buttons alone during a VS Code debug session.
The motor control task connects to the debugger-owned firmware process.
Disconnecting/stopping the Renode debugger also stops its emulator.

Custom tool locations / setup from a terminal
--------------------------------------------
In PowerShell (change the source and tool paths to match your machine):

$installDir = "$env:LOCALAPPDATA\Programs\ESCSim" # Or your chosen install folder.
& "$installDir\ESCSim-cli.exe" debug workspace `
  --repo C:\src\AM32 --target VIMDRONES_L431 --cygwin C:\cygwin64 `
  --arm-gdb C:\tools\arm\bin\arm-none-eabi-gdb.exe

Use --force only to replace an existing ESCSim.code-workspace. The installed
application directory can be changed in the installer; run the helper from
that installation so the workspace points to the right executables.

References:
https://code.visualstudio.com/docs/cpp/launch-json-reference
https://renode.readthedocs.io/en/latest/debugging/gdb.html
