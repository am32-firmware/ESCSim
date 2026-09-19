#!/usr/bin/env python3
"""Create an ESCSim GitHub release from successful CI application packages.

Requires Python 3.12+ and an authenticated GitHub CLI (gh auth login).
Use --prepare-only to download and check assets without changing GitHub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from urllib.parse import quote, urlencode
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "am32-firmware/ESCSim"
WORKFLOWS = ("sitl-gui.yml", "package.yml")
SITL = {
    "am32-sitl-gui-windows": "windows",
    "am32-sitl-gui-linux": "linux",
    "am32-sitl-gui-macos-arm64": "macos",
    "am32-sitl-gui-macos-x86_64": "macos",
}
CAPTURE = {"am32-capture-linux", "am32-capture-windows"}
RENODE = re.compile(r"ESCSim-(Linux|Windows|macOS)-(X64|ARM64)\Z")


class ReleaseError(RuntimeError):
    pass


class GitHub:
    def __init__(self, repo):
        self.repo = repo

    def command(self, *args, **kwargs):
        result = subprocess.run(
            ["gh", *map(str, args)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            **kwargs,
        )
        if result.returncode:
            raise ReleaseError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    def api(self, endpoint, *, method="GET", data=None, missing_ok=False):
        args = ["api", f"repos/{self.repo}/{endpoint}", "--method", method]
        if data is not None:
            args += ["--input", "-"]
        try:
            text = self.command(
                *args, input=json.dumps(data) if data is not None else None
            )
        except ReleaseError as exc:
            if missing_ok and "HTTP 404" in str(exc):
                return None
            raise
        return json.loads(text) if text.strip() else None

    def pages(self, endpoint, key=None):
        page = 1
        while True:
            separator = "&" if "?" in endpoint else "?"
            data = self.api(f"{endpoint}{separator}per_page=100&page={page}")
            items = data[key] if key else data
            yield from items
            if len(items) < 100:
                return
            page += 1

    def download(self, artifact, cache):
        cache.mkdir(parents=True, exist_ok=True)
        path = cache / f"{artifact['id']}.zip"
        expected = artifact.get("digest")
        if expected and not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
            raise ReleaseError(f"unsupported artifact digest: {expected}")
        if not path.exists() or (expected and sha256(path) != expected[7:]):
            partial = path.with_suffix(".part")
            print(
                f"Downloading {artifact['name']} ({artifact['size_in_bytes'] / 1e6:.1f} MB)",
                flush=True,
            )
            try:
                with partial.open("wb") as output:
                    result = subprocess.run(
                        [
                            "gh",
                            "api",
                            f"repos/{self.repo}/actions/artifacts/{artifact['id']}/zip",
                        ],
                        stdout=output,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                    )
                if result.returncode:
                    raise ReleaseError(result.stderr.strip())
                if expected and sha256(partial) != expected[7:]:
                    raise ReleaseError(
                        f"SHA256 mismatch downloading {artifact['name']}"
                    )
                partial.replace(path)
            finally:
                partial.unlink(missing_ok=True)
        return path


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def select_run(gh, workflow, sha):
    query = urlencode({"head_sha": sha, "status": "success"})
    runs = gh.pages(f"actions/workflows/{workflow}/runs?{query}", "workflow_runs")
    eligible = [
        run
        for run in runs
        if run["head_sha"] == sha
        and run["conclusion"] == "success"
        and run["status"] == "completed"
        and run["event"] in ("push", "workflow_dispatch")
        and run["head_repository"]["full_name"].lower() == gh.repo.lower()
    ]
    return max(eligible, key=lambda run: run["id"], default=None)


def plan_release(gh, sha):
    selected = []
    for workflow in WORKFLOWS:
        run = select_run(gh, workflow, sha)
        if run is None:
            if workflow == "sitl-gui.yml":
                raise ReleaseError(
                    f"no successful {workflow} run for {sha}; run that workflow on the release commit"
                )
            print(
                f"No Renode application package run for {sha}; including SITL packages only."
            )
            continue
        artifacts = list(gh.pages(f"actions/runs/{run['id']}/artifacts", "artifacts"))
        names = set()
        for artifact in artifacts:
            name = artifact["name"]
            wanted = (
                (name in SITL or name in CAPTURE)
                if workflow == "sitl-gui.yml"
                else bool(RENODE.fullmatch(name))
            )
            if not wanted:
                continue
            if name in names:
                raise ReleaseError(f"duplicate artifact {name} in run {run['id']}")
            names.add(name)
            if artifact["expired"]:
                raise ReleaseError(f"artifact {name} has expired; rebuild commit {sha}")
            selected.append(
                {
                    "artifact": artifact,
                    "run_id": run["id"],
                    "run_url": run["html_url"],
                    "workflow": workflow,
                }
            )
    platforms = {
        SITL[item["artifact"]["name"]]
        for item in selected
        if item["artifact"]["name"] in SITL
    }
    missing = {"windows", "macos", "linux"} - platforms
    if missing:
        raise ReleaseError(
            "missing required SITL packages: " + ", ".join(sorted(missing))
        )
    return sorted(selected, key=lambda item: item["artifact"]["name"])


def zip_members(archive):
    """Validate names without extracting untrusted archive paths."""
    members = archive.infolist()
    seen = set()
    for info in members:
        # ZipInfo normalizes separators on Windows and truncates at NUL.
        # Validate the original name so malformed entries cannot be hidden.
        name = info.orig_filename
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or name != info.filename
            or not path.parts
            or ":" in path.parts[0]
            or name in seen
        ):
            raise ReleaseError(f"unsafe or duplicate archive member: {name!r}")
        seen.add(name)
    return members


def unix_tar(archive, members, target, root=None):
    """Turn a loose Unix artifact into a tarball, preserving its symlinks."""
    with tarfile.open(target, "w:gz") as output:
        for info in members:
            name = f"{root}/{info.filename}" if root else info.filename
            entry = tarfile.TarInfo(name)
            entry.mtime = 0
            mode = info.external_attr >> 16
            entry.mode = (mode & 0o777) or (0o755 if info.is_dir() else 0o644)
            if info.is_dir():
                entry.type = tarfile.DIRTYPE
                output.addfile(entry)
            elif stat.S_ISLNK(mode):
                link = archive.read(info).decode("utf-8")
                parts = list(PurePosixPath(name).parent.parts)
                for part in PurePosixPath(link).parts:
                    if part == "..":
                        if not parts:
                            raise ReleaseError(f"escaping symlink: {name}")
                        parts.pop()
                    elif part != ".":
                        parts.append(part)
                if link.startswith("/") or "\\" in link:
                    raise ReleaseError(f"unsafe symlink: {name}")
                entry.type, entry.linkname = tarfile.SYMTYPE, link
                output.addfile(entry)
            else:
                entry.size = info.file_size
                # Older Actions uploads may lose executable bits.
                with archive.open(info) as stream:
                    magic = stream.read(4)
                if magic == b"\x7fELF" or PurePosixPath(name).name in (
                    "am32-sitl-gui",
                    "am32-capture",
                    "ESCSim",
                ):
                    entry.mode |= 0o111
                with archive.open(info) as stream:
                    output.addfile(entry, stream)


def materialize(artifact, downloaded, output):
    """Unwrap existing packages; turn legacy loose Linux uploads into tarballs."""
    name = artifact["name"]
    with zipfile.ZipFile(downloaded) as archive:
        members = zip_members(archive)
        files = [info for info in members if not info.is_dir()]
        if not files:
            raise ReleaseError(f"empty artifact: {name}")
        # Keep prebuilt archives byte-for-byte, especially signed Mac apps.
        allowed = {name + ".zip", name + ".tar.gz", name + "-installer.exe"}
        if all(info.filename in allowed for info in files) and any(
            info.filename in (name + ".zip", name + ".tar.gz") for info in files
        ):
            assets = []
            for info in files:
                target = output / info.filename
                with archive.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                if target.suffix == ".zip" and not zipfile.is_zipfile(target):
                    raise ReleaseError(f"invalid package ZIP: {target.name}")
                if target.name.endswith(".tar.gz") and not tarfile.is_tarfile(target):
                    raise ReleaseError(f"invalid package tarball: {target.name}")
                assets.append(target)
            return assets
        if name.startswith("am32-sitl-gui-macos-") or name.startswith("ESCSim-macOS-"):
            raise ReleaseError(
                f"{name} lacks a prebuilt app archive; rebuild CI with archive preservation"
            )
        if name == "am32-sitl-gui-linux":
            raise ReleaseError(
                "legacy Linux SITL artifact lacks the bundled bootloader; rebuild with SITL/package_linux.py"
            )
        if name.endswith("-linux") or name.startswith("ESCSim-Linux-"):
            target = output / (name + ".tar.gz")
            unix_tar(
                archive, members, target, root=name if name.endswith("-linux") else None
            )
            return [target]
        target = output / (name + ".zip")
        shutil.copy2(downloaded, target)
        assets = [target]
        if name.startswith("ESCSim-Windows-"):
            for info in files:
                if PurePosixPath(info.filename).name == "ESCSim-installer.exe":
                    installer = output / (name + "-installer.exe")
                    with archive.open(info) as src, installer.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    assets.append(installer)
        return assets


def prepare(gh, tag, sha, selected, output):
    output.mkdir(parents=True, exist_ok=True)
    records, assets = [], []
    for item in selected:
        artifact = item["artifact"]
        downloaded = gh.download(artifact, output / ".artifacts")
        paths = materialize(artifact, downloaded, output)
        for path in paths:
            if path.name in {asset.name for asset in assets}:
                raise ReleaseError(f"duplicate release asset: {path.name}")
            assets.append(path)
            records.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                    "artifact_id": artifact["id"],
                    "artifact_name": artifact["name"],
                    "artifact_digest": artifact.get("digest"),
                    "run_id": item["run_id"],
                    "run_url": item["run_url"],
                    "workflow": item["workflow"],
                }
            )
    manifest = output / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {"repository": gh.repo, "tag": tag, "commit": sha, "assets": records},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    assets.append(manifest)
    sums = output / "SHA256SUMS"
    sums.write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in assets), encoding="utf-8"
    )
    assets.append(sums)
    return assets, records


def release_notes(tag, sha, records, extra=""):
    lines = [
        f"ESCSim {tag}",
        "",
        extra.strip(),
        "",
        "Choose `am32-sitl-gui-*` for the native AM32 SITL simulator, or "
        "`ESCSim-*` for the Renode-based simulator. `am32-capture-*` is the calibration capture tool.",
        "",
        "SITL macOS packages are provided separately for Apple Silicon (arm64) and Intel (x86_64). "
        "macOS apps are not notarized; see the package instructions for first launch. "
        "The Renode application downloads its emulator and firmware through its setup tools.",
        "",
        f"Source commit: `{sha}`. Package checksums are in `SHA256SUMS`; "
        "`release-manifest.json` records the CI source of each asset.",
        "",
        "CI builds:",
    ]
    for url in sorted({record["run_url"] for record in records}):
        lines.append(f"- {url}")
    return "\n".join(lines).strip() + "\n"


def publish(
    gh,
    tag,
    sha,
    assets,
    notes,
    *,
    title=None,
    draft=False,
    prerelease=False,
    resume=False,
):
    tag_ref = gh.api(f"git/ref/tags/{quote(tag, safe='')}", missing_ok=True)
    if tag_ref and gh.api(f"commits/{quote(tag, safe='')}")["sha"] != sha:
        raise ReleaseError(f"tag {tag} changed while preparing the release")
    existing = next((r for r in gh.pages("releases") if r["tag_name"] == tag), None)
    if existing:
        if not existing["draft"] or not resume:
            raise ReleaseError(
                f"release {tag} already exists; only drafts can be resumed with --resume"
            )
        if existing["target_commitish"] != sha:
            raise ReleaseError("existing draft targets a different commit")
        release = existing
    else:
        release = gh.api(
            "releases",
            method="POST",
            data={
                "tag_name": tag,
                "target_commitish": sha,
                "name": title or f"ESCSim {tag}",
                "body": notes,
                "draft": True,
                "prerelease": prerelease,
            },
        )
    # Keep the release a draft until every upload is present and verified.
    # A failure leaves the draft intact for a later --resume invocation.
    for path in assets:
        print(f"Uploading {path.name}", flush=True)
        gh.command("release", "upload", tag, path, "--repo", gh.repo, "--clobber")
    uploaded = list(gh.pages(f"releases/{release['id']}/assets"))
    expected = {path.name: path for path in assets}
    if {asset["name"] for asset in uploaded} != set(expected):
        raise ReleaseError(
            "draft asset inventory differs from the prepared release; left as a draft"
        )
    for asset in uploaded:
        path = expected[asset["name"]]
        if asset["size"] != path.stat().st_size:
            raise ReleaseError(f"upload size mismatch: {path.name}; left as a draft")
        if asset.get("digest") and asset["digest"] != "sha256:" + sha256(path):
            raise ReleaseError(
                f"upload checksum mismatch: {path.name}; left as a draft"
            )
    result = gh.api(
        f"releases/{release['id']}",
        method="PATCH",
        data={
            "name": title or f"ESCSim {tag}",
            "body": notes,
            "draft": draft,
            "prerelease": prerelease,
        },
    )
    print(result["html_url"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", help="release tag, e.g. v1.0.0")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--ref", help="commit/branch to release (default: existing tag, otherwise main)"
    )
    parser.add_argument(
        "--output", type=Path, help="download directory (default: dist/releases/TAG)"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--prepare-only",
        action="store_true",
        help="download and validate without creating a release",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="show CI selection without downloading or changing GitHub",
    )
    parser.add_argument(
        "--draft", action="store_true", help="leave the uploaded release as a draft"
    )
    parser.add_argument("--prerelease", action="store_true")
    parser.add_argument(
        "--resume", action="store_true", help="resume uploads to an existing draft"
    )
    parser.add_argument("--title")
    parser.add_argument("--notes-file", type=Path, help="additional release notes")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", args.tag):
        parser.error(
            "use a simple release tag containing letters, digits, '.', '_' or '-'"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        parser.error("--repo must be OWNER/REPO")
    gh = GitHub(args.repo)
    tag_ref = gh.api(f"git/ref/tags/{quote(args.tag, safe='')}", missing_ok=True)
    ref = args.ref or (args.tag if tag_ref else "main")
    sha = gh.api(f"commits/{quote(ref, safe='')}")["sha"]
    if tag_ref and gh.api(f"commits/{quote(args.tag, safe='')}")["sha"] != sha:
        raise ReleaseError(f"tag {args.tag} already points at a different commit")
    selected = plan_release(gh, sha)
    print(f"Release {args.repo} {args.tag} from {sha}", flush=True)
    for item in selected:
        print(f"  {item['artifact']['name']} <- {item['run_url']}")
    if args.dry_run:
        return 0
    output = (args.output or ROOT / "dist" / "releases" / args.tag).resolve()
    assets, records = prepare(gh, args.tag, sha, selected, output)
    extra = args.notes_file.read_text(encoding="utf-8") if args.notes_file else ""
    notes = release_notes(args.tag, sha, records, extra)
    (output / "release-notes.md").write_text(notes, encoding="utf-8")
    print(f"Prepared {len(assets)} assets in {output}", flush=True)
    if not args.prepare_only:
        publish(
            gh,
            args.tag,
            sha,
            assets,
            notes,
            title=args.title,
            draft=args.draft,
            prerelease=args.prerelease,
            resume=args.resume,
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, OSError, zipfile.BadZipFile) as exc:
        print(f"release: {exc}", file=sys.stderr)
        raise SystemExit(1)
