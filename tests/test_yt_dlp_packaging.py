"""Offline pin/fetch evidence; PowerShell checks are explicitly static only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("scripts/build-release.sh", "scripts/build-windows-wine.sh")
ORACLE = {"schema": 1, "tag": "2026.08.19", "assets": {
    "windows-x86_64": {"name": "yt-dlp.exe", "sha256": "66674953fe251b89f4d08c5f0e35e0728679bd67ab3d7d05c0562af101dd3e7a"},
    "linux-x86_64": {"name": "yt-dlp_linux", "sha256": "58162f9bfdc27458ea47bfcb311cf47028f17d8154a8bf7d689861d46399230a"}}}


def test_literal_pin_oracle():
    pin = ROOT / "packaging/yt-dlp-pins.json"
    assert pin.is_file(), "builds must share a checked-in pin manifest"
    assert json.loads(pin.read_text()) == ORACLE


@pytest.mark.parametrize("script", (*SCRIPTS, "build.ps1"))
def test_scripts_use_pins_never_latest(script):
    source = (ROOT / script).read_text()
    assert "releases/latest" not in source, "moving release URL cannot provide deterministic packaging"
    assert "packaging/yt-dlp-pins.json" in source


def test_identical_bash_fetch_blocks():
    assert block(SCRIPTS[0]) == block(SCRIPTS[1])


def block(script):
    source = (ROOT / script).read_text()
    assert "# BEGIN yt-dlp pin" in source, "missing testable verified-fetch behavior"
    return source.split("# BEGIN yt-dlp pin", 1)[1].split("# END yt-dlp pin", 1)[0]


@pytest.mark.parametrize("script", SCRIPTS)
def test_bash_syntax(script):
    if not shutil.which("bash"):
        pytest.skip("bash unavailable")
    result = subprocess.run(["bash", "-n", str(ROOT / script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_powershell_static_structure_only():
    source = (ROOT / "build.ps1").read_text()
    assert "ConvertFrom-Json" in source
    assert "Get-FileHash" in source and "-Algorithm SHA256" in source
    assert "-ine" in source and 'throw "yt-dlp checksum mismatch' in source
    assert "finally" in source and "Remove-Item" in source
    assert "--retry 3" in source and "--max-time" in source
    assert "Move-Item" in source
    assert source.count("($ytDlpExe   + [IO.Path]::PathSeparator + '.')") == 2


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("platform", ("linux-x86_64", "windows-x86_64"))
@pytest.mark.parametrize("case", ("match", "wrong", "truncated", "curl-failure", "missing-asset",
    "missing-key", "missing-file", "invalid-json", "schema", "tag", "sha", "name",
    "bool-schema", "uppercase-sha", "tag-control",
    "cache-valid", "cache-invalid", "cache-invalid-bad"))
def test_verified_fetch(tmp_path, script, platform, case, umask="022"):
    if not shutil.which("bash"):
        pytest.skip("bash unavailable")
    fetch = block(script)
    payload = b"complete synthetic executable\n"
    expected = hashlib.sha256(payload).hexdigest()
    pin = json.loads(json.dumps(ORACLE))
    pin["assets"][platform]["sha256"] = expected
    if case == "missing-key":
        del pin["assets"][platform]
    for field, value in {"schema": 2, "tag": "latest", "sha": "xyz", "name": "../bad"}.items():
        if case == field:
            if field in ("schema", "tag"):
                pin[field] = value
            else:
                pin["assets"][platform]["sha256" if field == "sha" else "name"] = value
    manifest = tmp_path / "pins.json"
    if case == "bool-schema":
        pin["schema"] = True
    elif case == "uppercase-sha":
        pin["assets"][platform]["sha256"] = expected.upper()
    elif case == "tag-control":
        pin["tag"] += "\n"
    if case != "missing-file":
        manifest.write_text("{" if case == "invalid-json" else json.dumps(pin))
    dest = tmp_path / ("yt-dlp" if platform.startswith("linux") else "yt-dlp.exe")
    if case.startswith("cache"):
        dest.write_bytes(payload if case == "cache-valid" else b"stale")
        dest.chmod(0o600)
    downloaded = tmp_path / "payload"
    downloaded.write_bytes(payload[:4] if case == "truncated" else b"wrong" if case in ("wrong", "cache-invalid-bad") else payload)
    calls = tmp_path / "curl.json"
    curl = tmp_path / "curl"
    curl.write_text(f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
pathlib.Path(os.environ['CALLS']).write_text(json.dumps(args))
target = pathlib.Path(args[args.index('-o') + 1])
dest = pathlib.Path(os.environ['DEST'])
assert target != dest and target.parent == dest.parent
assert not dest.exists(), 'unverified destination during fetch'
if os.environ['CASE'] != 'missing-asset':
    target.write_bytes(pathlib.Path(os.environ['PAYLOAD']).read_bytes())
sys.exit(22 if os.environ['CASE'] in ('curl-failure', 'missing-asset') else 0)
''')
    curl.chmod(0o755)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
               CALLS=str(calls), DEST=str(dest), PAYLOAD=str(downloaded), CASE=case)
    result = subprocess.run(["bash", "-c", 'set -euo pipefail\numask "$4"\nYTDLP_PINS="$1"\n' + fetch +
        '\nfetch_yt_dlp "$2" "$3" || exit 1', "test", str(manifest), platform, str(dest), umask],
        env=env, capture_output=True, text=True, timeout=15)
    success = case in ("match", "cache-valid", "cache-invalid")
    assert (result.returncode == 0) == success, result.stdout + result.stderr
    if success:
        assert dest.read_bytes() == payload
        assert stat.S_IMODE(dest.stat().st_mode) == (0o755 if platform.startswith("linux") else 0o644)
        assert f"yt-dlp 2026.08.19 {ORACLE['assets'][platform]['name']} sha256 {expected} verified" in result.stdout
        if platform.startswith("linux"):
            assert os.access(dest, os.X_OK)
    else:
        assert not dest.exists(), "never leave an unverified packaged executable"
        assert "yt-dlp" in result.stderr
        if case in ("wrong", "truncated", "cache-invalid-bad", "curl-failure", "missing-asset"):
            assert expected in result.stderr and "actual" in result.stderr
    if case == "cache-valid":
        assert not calls.exists() and "cached yt-dlp verified" in result.stdout
    elif success or case in ("wrong", "truncated", "curl-failure", "missing-asset", "cache-invalid-bad"):
        args = json.loads(calls.read_text())
        assert args[-1] == f"https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/{ORACLE['assets'][platform]['name']}"
        assert "latest" not in args[-1]
        for flag in ("--fail", "--location", "--silent", "--show-error"):
            assert flag in args
        assert args[args.index("--retry") + 1] == "3"
        assert 0 < int(args[args.index("--max-time") + 1]) <= 300
    else:
        assert not calls.exists(), "invalid manifest must fail before any download"
    assert not list(tmp_path.glob(dest.name + ".tmp.*")), "partial downloads must be removed"


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("platform", ("linux-x86_64", "windows-x86_64"))
@pytest.mark.parametrize("case", ("match", "cache-valid"))
@pytest.mark.parametrize("umask", ("077", "022"))
def test_verified_modes_independent_of_umask(tmp_path, script, platform, case, umask):
    test_verified_fetch(tmp_path, script, platform, case, umask)


@pytest.mark.parametrize("script", SCRIPTS)
@pytest.mark.parametrize("platform", ("linux-x86_64", "windows-x86_64"))
@pytest.mark.parametrize("case", ("match", "cache-valid", "cache-invalid", "cache-invalid-bad"))
def test_verified_fetch_backslash_directory(tmp_path, script, platform, case):
    directory = tmp_path / "cove\\build"
    directory.mkdir()
    test_verified_fetch(directory, script, platform, case)
