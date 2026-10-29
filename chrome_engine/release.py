"""The fingerprint-chromium builds a profile can run (Profile.chrome_version).

Builds come from https://github.com/adryfish/fingerprint-chromium (BSD-3,
based on ungoogled-chromium). Each one is pinned by SHA-256 because the
browser is downloaded at run time and then trusted with cookies and proxies.
Upstream only lists a digest from 136 on; older ones were hashed from the
release files when they were pinned. Every macOS build is arm64.

150.0.7871.186 is deliberately not the default: drawing an emoji onto a 2D
canvas crashes its renderer in about half of all launches while canvas
spoofing is on, and fingerprinting scripts draw emoji all the time. 148 does
not. tests/test_browser.py::test_emoji_canvas_does_not_crash guards this when
the pin moves.
"""

from __future__ import annotations

import platform
import subprocess
import sys
from dataclasses import dataclass

_BASE = "https://github.com/adryfish/fingerprint-chromium/releases/download"


@dataclass(frozen=True)
class Asset:
    url: str
    sha256: str
    size: int
    # "zip", "tar.xz" or "dmg"
    kind: str
    # Path of the browser executable inside the extracted archive.
    executable: str


@dataclass(frozen=True)
class Release:
    version: str
    assets: dict[str, Asset]

    def asset_for(self, host: str) -> Asset:
        try:
            return self.assets[host]
        except KeyError:
            raise UnsupportedPlatformError(
                f"fingerprint-chromium {self.version} has no build for {host}; "
                f"available: {', '.join(sorted(self.assets))}"
            ) from None


class UnsupportedPlatformError(RuntimeError):
    pass


def _asset(version: str, name: str, sha256: str, size: int) -> Asset:
    """A release file; the archive's own name is the folder it unpacks to."""
    kind = next(k for k in ("zip", "tar.xz", "dmg") if name.endswith("." + k))
    folder = name.removesuffix("." + kind)
    executable = {
        "zip": f"{folder}/chrome.exe",
        "tar.xz": f"{folder}/chrome",
        "dmg": "Chromium.app/Contents/MacOS/Chromium",
    }[kind]
    return Asset(f"{_BASE}/{version}/{name}", sha256, size, kind, executable)


# (file name, sha256, size); None where upstream has no usable build.
_File = tuple[str, str, int] | None


def _release(version: str, windows: _File, linux: _File, macos: _File) -> Release:
    files = {"windows-x64": windows, "linux-x64": linux, "macos": macos}
    return Release(
        version=version,
        assets={host: _asset(version, *f) for host, f in files.items() if f},
    )


# Upstream's file names changed over time (<=138: "..._<v>-1_linux.tar.xz"), so
# they are spelled out. fetch._locate_executable finds the browser if the folder
# inside an archive differs.
RELEASES: dict[str, Release] = {
    r.version: r
    for r in (
        _release(
            "150.0.7871.186",
            windows=(
                "ungoogled-chromium_150.0.7871.186-1.1_windows_x64.zip",
                "4d549c326e51ebbabf562fd365eb5380d9d4a81200da2c60f075c688d9a77e03",
                193743241,
            ),
            linux=(
                "ungoogled-chromium-150.0.7871.186-1-x86_64_linux.tar.xz",
                "48eff860053efd9ad498d51370f0a1f59f983df39c02a72ad2a8682cdead4055",
                145806232,
            ),
            macos=None,
        ),
        _release(
            "148.0.7778.215",
            windows=(
                "ungoogled-chromium_148.0.7778.215-1.1_windows_x64.zip",
                "9ef3f471b7a6641b4224532522b29141ce3746e27d55788d88e2fd951f362579",
                189767686,
            ),
            linux=(
                "ungoogled-chromium-148.0.7778.215-1-x86_64_linux.tar.xz",
                "70d239830332e5820aa34dfcb284161cac0429eee25da642830afe04bda717f4",
                141269020,
            ),
            macos=(
                "ungoogled-chromium_148.0.7778.215-1.1_macos.dmg",
                "b72f091e2e1a7583eed389c4b8e3534ed355e568af8c8bbf8fc30a25e23ca679",
                140187500,
            ),
        ),
        _release(
            "144.0.7559.132",
            windows=(
                "ungoogled-chromium_144.0.7559.132-1.1_windows_x64.zip",
                "ab409667ca7ec3d67a060f1ffea8bd3a450b71ed54d17b361024986e7edd0996",
                176796270,
            ),
            linux=(
                "ungoogled-chromium-144.0.7559.132-1-x86_64_linux.tar.xz",
                "bb4c44840bae7e881f6258ed5b33f972f284384d898b6289e1f0c0bf47555ede",
                133493680,
            ),
            macos=None,
        ),
        _release(
            "142.0.7444.175",
            windows=(
                "ungoogled-chromium_142.0.7444.175-1.1_windows_x64.zip",
                "c99406fe2822cd6b7281b8eb54469bb59b2f1ec13e8d4d0f1f16e871fa25e7ab",
                410369977,
            ),
            linux=(
                "ungoogled-chromium-142.0.7444.175-1-x86_64_linux.tar.xz",
                "d8cabb277a557a13db4166a320beb044cdea2eca51cde82d497f09c15dc0a25e",
                132048760,
            ),
            macos=(
                "ungoogled-chromium_142.0.7444.175-1.1_macos.dmg",
                "ae3349a2d6368406543a84e43c783d019ac0743bf4fe08f6391933d207a65696",
                130929159,
            ),
        ),
        _release(
            "139.0.7258.154",
            windows=(
                "ungoogled-chromium_139.0.7258.154-1.1_windows_x64.zip",
                "d3991ba80f165936c3a01f2bfbfcbe5d6134fc0c5f2ef317ff3aedfa6b20d7c2",
                403763462,
            ),
            linux=(
                "ungoogled-chromium-139.0.7258.154-1-x86_64_linux.tar.xz",
                "de96cd5a692053556e2d3828da7591be3cc98b18dedee7ff5bcef5cc8dc3a6f9",
                131364556,
            ),
            macos=(
                "ungoogled-chromium_139.0.7258.154-1.1_macos.dmg",
                "0cc679d6cf83fef5cc0e413719dae58a4afdf75e9ba976bad66cd37aa0236603",
                129973813,
            ),
        ),
        # Its linux archive upstream is a 32-byte stub.
        _release(
            "138.0.7204.183",
            windows=(
                "ungoogled-chromium_138.0.7204.183-1.1_windows_x64.zip",
                "4979a5fad4bbf936096eb7b192486c55de85bd248410fc84f12b18fe82d54ce3",
                400935413,
            ),
            linux=None,
            macos=None,
        ),
        _release(
            "136.0.7103.113",
            windows=(
                "ungoogled-chromium_136.0.7103.113-1.1_windows_x64.zip",
                "2313c38e1e2d0bca4ed2fc9e4f78c2299601e6f38f9c223cf15bfbeb86f57145",
                395128530,
            ),
            linux=(
                "ungoogled-chromium_136.0.7103.113-1_linux.tar.xz",
                "56c1a5deb37d845593fff31ae8b072f22fcf0ecb9f76f8a48a8a8de295d30834",
                132023928,
            ),
            macos=(
                "ungoogled-chromium_136.0.7103.113-1.1_macos.dmg",
                "1caa5c9f02272ab67daa160469116c07554f47fff20016d451a67390f6ceb7f6",
                126173095,
            ),
        ),
        _release(
            "135.0.7049.95",
            windows=(
                "ungoogled-chromium_135.0.7049.95-1.1_windows_x64.zip",
                "7bdb7184e4166337f020fc85e856155b35b8792d7d56d7b704455f18e43ecca1",
                391511588,
            ),
            linux=(
                "ungoogled-chromium_135.0.7049.95-1_linux.tar.xz",
                "1c2294609f2afbce4cf9ffcaf7d3046d0c79511f678596d25f831067a0446a5f",
                130230216,
            ),
            macos=(
                "ungoogled-chromium_135.0.7049.95-1.1_macos.dmg",
                "9704f916c88378318c190d4407f45d237bf85fbc42bff98fd71f881e7f10eba0",
                125111910,
            ),
        ),
        _release(
            "134.0.6998.165",
            windows=(
                "ungoogled-chromium_134.0.6998.165-1.1_windows_x64.zip",
                "4326fa47fb98d21022558789f19966718d805261a40217da88c18a2cb3246b02",
                388984370,
            ),
            linux=(
                "ungoogled-chromium_134.0.6998.165-1_linux.tar.xz",
                "bd05dcd306fc61aa2861958d9cd5df14eca9147e82283bed8de8ddaf16fa68a7",
                129858564,
            ),
            macos=None,
        ),
        _release(
            "133.0.6943.126",
            windows=(
                "ungoogled-chromium_133.0.6943.126-1.1_windows_x64.zip",
                "d7fa957b8797e40b651a56afff532e03e30ddd100e8e23ee2ac46dca1350f0b1",
                388934271,
            ),
            linux=(
                "ungoogled-chromium_133.0.6943.126-1_linux.tar.xz",
                "83c7c777cefdb09b2176db3b181d019b4f36177967c3dffe818fb9e8ffe1eee4",
                130276784,
            ),
            macos=None,
        ),
        # Its macOS build is 132.0.6834.110: upstream never published a .159 one.
        _release(
            "132.0.6834.159",
            windows=(
                "ungoogled-chromium_132.0.6834.159-1.1_windows_x64.zip",
                "99cdcfab8320154efb392dab07cb6eefd3bd995a04e604861fe90a527753732e",
                384277805,
            ),
            linux=(
                "ungoogled-chromium_132.0.6834.159-1_linux.tar.xz",
                "3260a2e43b870190f7f7568bc1b799f56d8f6e2963dff860e716dbf0a67f5aa6",
                128724600,
            ),
            macos=(
                "ungoogled-chromium_132.0.6834.110-1.1_macos.dmg",
                "0f2a2772ce154b57fe4a718e918fb086aa65a6e6cdc4945ee00a348d62e024f4",
                123261209,
            ),
        ),
        _release(
            "131.0.6778.264",
            windows=None,
            linux=(
                "ungoogled-chromium_131.0.6778.264-1_linux.tar.xz",
                "aa29f8e3a0a65d7f3f8d29a46ccbbfaedbcc1adc533ba6f78f4beafd4297997d",
                129011216,
            ),
            macos=None,
        ),
        _release(
            "130.0.6723.116",
            windows=(
                "ungoogled-chromium_130.0.6723.116-1.1_windows_x64.zip",
                "2f685ecb9c1b92f649e1cd7194f0adaa69512f6d14c7eb1be51f5cb91014f680",
                381254088,
            ),
            linux=(
                "ungoogled-chromium_130.0.6723.116-1_linux.tar.xz",
                "63ce4cb9b99832ad9ed00a699d4083c76b12580efc168d493cb879a5083ac31f",
                133066404,
            ),
            macos=None,
        ),
        _release(
            "129.0.6668.100",
            windows=(
                "ungoogled-chromium_129.0.6668.100-1.1_windows_x64.zip",
                "5f905fee43ef517c562638a7f24cf0b2348d6056d1f73fc42edb2b08ef71d1a0",
                373557422,
            ),
            linux=(
                "ungoogled-chromium_129.0.6668.100-1_linux.tar.xz",
                "1dc8e1d302eb0bad7946a6574829bdcb802fd3b6953043083f2379df0763207e",
                129051104,
            ),
            macos=None,
        ),
    )
}

# A profile can only move to a newer build (Chromium cannot open data a newer
# version wrote), and the panels list every pin: keep it at most this many.
MAX_BUILDS = 20
if len(RELEASES) > MAX_BUILDS:
    raise RuntimeError(f"At most {MAX_BUILDS} fingerprint-chromium builds can be pinned")

DEFAULT_VERSION = "148.0.7778.215"
# Chrome profiles made before builds were selectable ran this one.
LEGACY_VERSION = "148.0.7778.215"


def key(version: str) -> tuple[int, ...]:
    """Sortable form of a version: key("139.0.7258.154") < key("148.0.7778.215")."""
    return tuple(int(part) for part in version.split("."))


def available(host: str | None) -> list[str]:
    """The pinned versions with a build for `host`, newest first."""
    found = [v for v, r in RELEASES.items() if host in r.assets]
    return sorted(found, key=key, reverse=True)


def get_release(version: str | None = None) -> Release:
    version = version or DEFAULT_VERSION
    try:
        return RELEASES[version]
    except KeyError:
        raise ValueError(
            f"Unknown fingerprint-chromium version {version}; pinned: {', '.join(RELEASES)}"
        ) from None


def _under_rosetta() -> bool:
    """True for an x86_64 process translated by Rosetta on an Apple Silicon Mac."""
    try:
        out = subprocess.run(
            ["sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.stdout.strip() == "1"


def _host_machine() -> str:
    machine = platform.machine().lower()
    # An x86_64 Python under Rosetta still runs on an arm64 Mac.
    if sys.platform == "darwin" and machine == "x86_64" and _under_rosetta():
        return "arm64"
    return machine


def host_platform(system: str | None = None, machine: str | None = None) -> str:
    """The asset key for this machine: windows-x64, linux-x64 or macos."""
    system = (system or sys.platform).lower()
    machine = (machine or _host_machine()).lower()
    if system.startswith("win"):
        if machine in ("amd64", "x86_64"):
            return "windows-x64"
    elif system.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux-x64"
    # The macOS build is arm64 only (Mach-O thin); it cannot run on Intel Macs.
    elif system == "darwin" and machine in ("arm64", "aarch64"):
        return "macos"
    raise UnsupportedPlatformError(f"No fingerprint-chromium build for {system}/{machine}")


def host_os(system: str | None = None) -> str:
    """The persona platform name of this machine: windows, macos or linux."""
    system = (system or sys.platform).lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "macos"
    return "linux"
