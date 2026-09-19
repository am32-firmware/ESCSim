#!/usr/bin/env python3
"""Archive the built ESCSim application before CI uploads it."""

from pathlib import Path
import platform
import shutil
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    system = platform.system()
    arch = {"x86_64": "X64", "AMD64": "X64", "arm64": "ARM64", "aarch64": "ARM64"}[
        platform.machine()
    ]
    label = "macOS" if system == "Darwin" else system
    name = f"ESCSim-{label}-{arch}"
    dist = ROOT / "dist"
    output = dist / "release"
    output.mkdir(parents=True, exist_ok=True)
    if system == "Darwin":
        # Preserve framework symlinks and code signatures inside the artifact.
        archive = output / f"{name}.zip"
        subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(dist / "ESCSim.app")],
            check=True,
        )
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--sequesterRsrc",
                "--keepParent",
                str(dist / "ESCSim.app"),
                str(archive),
            ],
            check=True,
        )
    elif system == "Linux":
        archive = output / f"{name}.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(dist / "ESCSim", arcname="ESCSim")
    elif system == "Windows":
        archive = Path(shutil.make_archive(str(output / name), "zip", dist, "ESCSim"))
        shutil.copy2(
            dist / "installer/ESCSim-installer.exe", output / f"{name}-installer.exe"
        )
    else:
        raise RuntimeError(f"unsupported package platform: {system}")
    print(archive)


if __name__ == "__main__":
    main()
