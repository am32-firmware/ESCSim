#!/usr/bin/env python3
"""Build a macOS app ZIP containing the GUI, SITL firmware and bootloader.

Run on the target Mac with PyInstaller and SITL/requirements.txt installed.
The firmware and bootloader must already be built for this architecture.
"""
import argparse
from pathlib import Path
import platform
import shutil
import subprocess
import sys

import am32_paths

HERE = Path(__file__).resolve().parent
ROOT = Path(am32_paths.ESCSIM_ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sitl', help='host firmware binary (default: firmware checkout)')
    parser.add_argument('--bootloader', help='host bootloader binary (default: bootloader checkout)')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('run this packager on macOS')
    firmware = am32_paths.sitl_binary(args.sitl)
    bootloader = am32_paths.bootloader_binary(args.bootloader)
    subprocess.run([sys.executable, str(HERE / 'build_sitl_gui.py'),
                    '--sitl', firmware, '--bootloader', bootloader], check=True)
    stage = ROOT / 'dist' / ('am32-sitl-gui-macos-' + platform.machine())
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(ROOT / 'dist/am32-sitl-gui.app', stage / 'am32-sitl-gui.app',
                    symlinks=True)
    shutil.copy2(HERE / 'macos/README.txt', stage / 'README.txt')
    shutil.copy2(ROOT / 'LICENSE', stage / 'LICENSE.txt')
    shutil.copy2(HERE / 'macos/THIRD-PARTY.txt', stage / 'THIRD-PARTY.txt')
    shutil.copy2(HERE / 'windows/LGPL-3.0.txt', stage / 'LGPL-3.0.txt')
    archive = stage.with_suffix('.zip')
    archive.unlink(missing_ok=True)
    # ditto preserves executable modes, framework symlinks and the .app
    # structure when the user extracts it with Finder's Archive Utility.
    subprocess.run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                    str(stage), str(archive)], check=True)
    print('Built %s (%.1f MB)' % (archive, archive.stat().st_size / 1e6))


if __name__ == '__main__':
    main()
