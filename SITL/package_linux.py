#!/usr/bin/env python3
"""Build a Linux SITL GUI tarball with firmware and bootloader bundled."""
import argparse
from pathlib import Path
import subprocess
import sys
import tarfile

import am32_paths

HERE = Path(__file__).resolve().parent
ROOT = Path(am32_paths.ESCSIM_ROOT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sitl')
    parser.add_argument('--bootloader')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('run this packager on Linux')
    firmware = am32_paths.sitl_binary(args.sitl)
    bootloader = am32_paths.bootloader_binary(args.bootloader)
    subprocess.run([sys.executable, str(HERE / 'build_sitl_gui.py'),
                    '--sitl', firmware, '--bootloader', bootloader], check=True)
    output = ROOT / 'dist/am32-sitl-gui-linux.tar.gz'
    with tarfile.open(output, 'w:gz') as archive:
        for source, name in [(ROOT / 'dist/am32-sitl-gui', 'am32-sitl-gui'),
                             (HERE / 'linux/README.txt', 'README.txt'),
                             (ROOT / 'LICENSE', 'LICENSE.txt'),
                             (HERE / 'windows/LGPL-3.0.txt', 'LGPL-3.0.txt')]:
            archive.add(source, arcname='am32-sitl-gui-linux/' + name)
    print(output)


if __name__ == '__main__':
    main()
