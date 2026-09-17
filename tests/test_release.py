from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import stat
import tarfile
from unittest.mock import Mock
import zipfile

import pytest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location(
    "release_script", ROOT / "scripts/release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
SHA = "a" * 40


def run(number=1, **changes):
    value = {
        "id": number,
        "head_sha": SHA,
        "conclusion": "success",
        "status": "completed",
        "event": "push",
        "head_repository": {"full_name": release.DEFAULT_REPO},
        "html_url": f"https://github.com/{release.DEFAULT_REPO}/actions/runs/{number}",
    }
    return value | changes


def artifact(name, number=1, **changes):
    return {"id": number, "name": name, "expired": False, "size_in_bytes": 10} | changes


def test_run_selection_rejects_foreign_pr_failed_and_other_commits():
    gh = Mock(repo=release.DEFAULT_REPO)
    gh.pages.return_value = [
        run(),
        run(2),
        run(3, event="pull_request"),
        run(4, head_sha="b" * 40),
        run(5, conclusion="failure"),
        run(6, head_repository={"full_name": "other/fork"}),
    ]
    assert release.select_run(gh, "sitl-gui.yml", SHA)["id"] == 2


def plan_client(names, package_names=()):
    gh = Mock(repo=release.DEFAULT_REPO)

    def pages(endpoint, key):
        if "sitl-gui.yml/runs" in endpoint:
            return [run(1)]
        if "package.yml/runs" in endpoint:
            return [run(2)] if package_names else []
        names_here = package_names if "/runs/2/" in endpoint else names
        return [artifact(name, i + 1) for i, name in enumerate(names_here)]

    gh.pages.side_effect = pages
    return gh


def test_plan_requires_all_three_sitl_platforms():
    gh = plan_client(["am32-sitl-gui-windows", "am32-sitl-gui-macos-arm64"])
    with pytest.raises(release.ReleaseError, match="linux"):
        release.plan_release(gh, SHA)


def test_plan_collects_optional_apps_and_ignores_logs():
    gh = plan_client(
        list(release.SITL) + ["am32-capture-linux", "test-log"],
        [
            "ESCSim-Linux-X64",
            "ESCSim-Windows-X64",
            "ESCSim-macOS-ARM64",
            "native-library",
        ],
    )
    names = {item["artifact"]["name"] for item in release.plan_release(gh, SHA)}
    assert len(names) == 8
    assert "ESCSim-macOS-ARM64" in names
    assert "test-log" not in names


def test_expired_required_artifact_fails():
    gh = plan_client(list(release.SITL))
    original = gh.pages.side_effect

    def pages(endpoint, key):
        items = original(endpoint, key)
        if "/artifacts" in endpoint:
            items[0]["expired"] = True
        return items

    gh.pages.side_effect = pages
    with pytest.raises(release.ReleaseError, match="expired"):
        release.plan_release(gh, SHA)


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            archive.writestr(info, data)
    return path


def test_prebuilt_mac_zip_is_preserved_byte_for_byte(tmp_path):
    name = "am32-sitl-gui-macos-arm64"
    inner = make_zip(
        tmp_path / "inner.zip", [("app/link", b"target", stat.S_IFLNK | 0o777)]
    )
    original = inner.read_bytes()
    outer = make_zip(tmp_path / "outer.zip", [(name + ".zip", original, 0o100644)])
    result = release.materialize(artifact(name), outer, tmp_path)
    assert result[0].read_bytes() == original
    with zipfile.ZipFile(result[0]) as archive:
        assert stat.S_ISLNK(archive.getinfo("app/link").external_attr >> 16)


def test_prebuilt_windows_app_and_installer_are_both_published(tmp_path):
    name = "ESCSim-Windows-X64"
    inner = make_zip(tmp_path / "inner.zip", [("ESCSim/ESCSim.exe", b"MZ", 0o100644)])
    outer = make_zip(
        tmp_path / "outer.zip",
        [
            (name + ".zip", inner.read_bytes(), 0o100644),
            (name + "-installer.exe", b"MZ installer", 0o100644),
        ],
    )
    results = release.materialize(artifact(name), outer, tmp_path)
    assert {p.name for p in results} == {name + ".zip", name + "-installer.exe"}


def test_legacy_capture_gets_executable_tarball(tmp_path):
    outer = make_zip(
        tmp_path / "artifact.zip", [("am32-capture", b"\x7fELFbinary", 0o100644)]
    )
    result = release.materialize(artifact("am32-capture-linux"), outer, tmp_path)[0]
    with tarfile.open(result) as archive:
        member = archive.getmember("am32-capture-linux/am32-capture")
        assert member.mode & 0o111 == 0o111
        assert archive.extractfile(member).read() == b"\x7fELFbinary"


@pytest.mark.parametrize("name", ["ESCSim-macOS-ARM64", "am32-sitl-gui-linux"])
def test_rejects_legacy_incomplete_packages(tmp_path, name):
    outer = make_zip(tmp_path / "artifact.zip", [("app", b"binary", 0o100644)])
    with pytest.raises(release.ReleaseError, match="rebuild"):
        release.materialize(artifact(name), outer, tmp_path)


@pytest.mark.parametrize(
    "member", ["../escape", "/absolute", "C:/drive", "back\\slash"]
)
def test_unsafe_archive_paths_rejected(tmp_path, member):
    outer = make_zip(tmp_path / "artifact.zip", [(member, b"data", 0o100644)])
    with pytest.raises(release.ReleaseError, match="unsafe"):
        release.materialize(artifact("am32-capture-linux"), outer, tmp_path)


def test_escaping_symlink_rejected(tmp_path):
    outer = make_zip(
        tmp_path / "artifact.zip", [("link", b"../../outside", stat.S_IFLNK | 0o777)]
    )
    with pytest.raises(release.ReleaseError, match="escaping symlink"):
        release.materialize(artifact("am32-capture-linux"), outer, tmp_path)


def publishing_client(assets, fail_upload=False, existing=()):
    gh = Mock(repo=release.DEFAULT_REPO)

    def api(endpoint, **kwargs):
        if endpoint.startswith("git/ref/"):
            return {"object": {"sha": SHA}}
        if endpoint.startswith("commits/"):
            return {"sha": SHA}
        if kwargs.get("method") == "POST":
            assert kwargs["data"]["draft"] is True
            return {"id": 5}
        if kwargs.get("method") == "PATCH":
            return {"html_url": "https://github.com/release"}
        raise AssertionError(endpoint)

    gh.api.side_effect = api
    gh.pages.side_effect = lambda endpoint: (
        list(existing)
        if endpoint == "releases"
        else [
            {
                "name": p.name,
                "size": p.stat().st_size,
                "digest": "sha256:" + release.sha256(p),
            }
            for p in assets
        ]
    )
    if fail_upload:
        gh.command.side_effect = release.ReleaseError("upload failed")
    return gh


def test_upload_failure_leaves_draft_unpublished(tmp_path):
    asset = tmp_path / "asset.zip"
    asset.write_bytes(b"package")
    gh = publishing_client([asset], fail_upload=True)
    with pytest.raises(release.ReleaseError, match="upload failed"):
        release.publish(gh, "v1.0", SHA, [asset], "notes")
    assert not any(
        call.kwargs.get("method") == "PATCH" for call in gh.api.call_args_list
    )


def test_publishes_only_after_assets_are_verified(tmp_path):
    asset = tmp_path / "asset.zip"
    asset.write_bytes(b"package")
    gh = publishing_client([asset])
    release.publish(gh, "v1.0", SHA, [asset], "notes")
    assert gh.api.call_args.kwargs["data"]["draft"] is False


def test_upload_checksum_mismatch_prevents_publication(tmp_path):
    asset = tmp_path / "asset.zip"
    asset.write_bytes(b"package")
    gh = publishing_client([asset])
    gh.pages.side_effect = lambda endpoint: (
        []
        if endpoint == "releases"
        else [{"name": asset.name, "size": 7, "digest": "sha256:wrong"}]
    )
    with pytest.raises(release.ReleaseError, match="checksum mismatch"):
        release.publish(gh, "v1.0", SHA, [asset], "notes")
    assert not any(
        call.kwargs.get("method") == "PATCH" for call in gh.api.call_args_list
    )


def test_existing_published_release_is_never_overwritten(tmp_path):
    gh = publishing_client([], existing=[{"tag_name": "v1.0", "draft": False}])
    with pytest.raises(release.ReleaseError, match="already exists"):
        release.publish(gh, "v1.0", SHA, [], "notes", resume=True)
    gh.command.assert_not_called()


def test_prepare_writes_matching_manifest_and_checksums(tmp_path):
    downloaded = make_zip(
        tmp_path / "source.zip", [("am32-capture", b"\x7fELF", 0o100755)]
    )
    gh = Mock(repo=release.DEFAULT_REPO)
    gh.download.return_value = downloaded
    selected = [
        {
            "artifact": artifact("am32-capture-linux"),
            "run_id": 1,
            "run_url": "https://github.com/run",
            "workflow": "sitl-gui.yml",
        }
    ]
    assets, records = release.prepare(gh, "v1.0", SHA, selected, tmp_path / "output")
    manifest = json.loads((tmp_path / "output/release-manifest.json").read_text())
    assert manifest["commit"] == SHA
    assert manifest["assets"] == records
    for line in (tmp_path / "output/SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ")
        assert release.sha256(tmp_path / "output" / name) == digest
    assert len(assets) == 3


def test_download_rejects_corrupt_artifact_and_removes_partial(tmp_path, monkeypatch):
    gh = release.GitHub(release.DEFAULT_REPO)

    def subprocess_run(command, **kwargs):
        kwargs["stdout"].write(b"bad download")
        return Mock(returncode=0, stderr="")

    monkeypatch.setattr(release.subprocess, "run", subprocess_run)
    with pytest.raises(release.ReleaseError, match="SHA256 mismatch"):
        gh.download(artifact("example", digest="sha256:" + "0" * 64), tmp_path)
    assert not list(tmp_path.iterdir())


def test_verified_cache_avoids_download(tmp_path, monkeypatch):
    path = tmp_path / "1.zip"
    path.write_bytes(b"cached bytes")
    gh = release.GitHub(release.DEFAULT_REPO)
    command = Mock(side_effect=AssertionError("unexpected download"))
    monkeypatch.setattr(release.subprocess, "run", command)
    assert (
        gh.download(
            artifact("example", digest="sha256:" + release.sha256(path)), tmp_path
        )
        == path
    )


def test_tag_change_prevents_any_release_writes():
    gh = publishing_client([])
    gh.api.side_effect = [{"object": {}}, {"sha": "b" * 40}]
    with pytest.raises(release.ReleaseError, match="tag .* changed"):
        release.publish(gh, "v1.0", SHA, [], "notes")
    gh.command.assert_not_called()


def test_resume_requires_matching_draft_commit():
    gh = publishing_client(
        [], existing=[{"tag_name": "v1.0", "draft": True, "target_commitish": "b" * 40}]
    )
    with pytest.raises(release.ReleaseError, match="different commit"):
        release.publish(gh, "v1.0", SHA, [], "notes", resume=True)
    gh.command.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes and symlinks")
def test_preserved_renode_linux_package_keeps_symlinks(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "archive_script", ROOT / "scripts/archive-package.py"
    )
    archive_script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive_script)
    monkeypatch.setattr(archive_script, "ROOT", tmp_path)
    monkeypatch.setattr(archive_script.platform, "system", lambda: "Linux")
    monkeypatch.setattr(archive_script.platform, "machine", lambda: "x86_64")
    app = tmp_path / "dist/ESCSim"
    app.mkdir(parents=True)
    executable = app / "ESCSim"
    executable.write_bytes(b"\x7fELF")
    executable.chmod(0o755)
    (app / "alias").symlink_to("ESCSim")
    archive_script.main()
    with tarfile.open(tmp_path / "dist/release/ESCSim-Linux-X64.tar.gz") as archive:
        assert archive.getmember("ESCSim/ESCSim").mode == 0o755
        assert archive.getmember("ESCSim/alias").issym()
        assert archive.getmember("ESCSim/alias").linkname == "ESCSim"
