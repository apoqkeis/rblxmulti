#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
  Roblox lightweight bootstrapper (voidstrap / froststrap style, stdlib only)
===============================================================================

Downloads and maintains its OWN RobloxPlayerBeta installation instead of
relying on the already-installed copy in %LOCALAPPDATA%\\Roblox\\Versions.
Each launched client gets its own GlobalBasicSettings XML so the FramerateCap
(FPS) can be set per instance via the official `-g /globalBasicSettingsPath`
switch (REA-verified: RobloxPlayerBeta.exe accepts `-g <path>`).

Design goals (voidstrap / froststrap inspiration, implemented in Python):

  * Own installation: downloads `version-<hash>-RobloxApp.zip` from
    https://setup.rbxcdn.com/ and extracts it to a private folder.
  * Per-client FPS: writes a per-instance GlobalBasicSettings_<n>.xml with a
    custom FramerateCap and passes it with `-g`.
  * Per-client graphics level: sets SavedQualityLevel in the same XML via
    `--graphics-level` (presets: automatic, lowest, low, medium, high, ultra)
    or `--quality` (int 0-10).
  * Multi-instance safe: holds the ROBLOX_singletonMutex and locks
    RobloxCookies.dat (share=none) for the lifetime of the client, the same
    guard the main launcher uses to stop Roblox closing extra instances.
  * Stdlib only: no PyInstaller, no .NET, no extra dependencies.
  * Read-only analysis: no patching, no injection, no integrity bypass.

Usage (CLI):

    python bootstrapper.py download <version-hash>     # download + extract
    python bootstrapper.py launch <version-hash> <auth-ticket> <join-script-url>
                            [--fps 60]
                            [--graphics-level lowest|low|medium|high|ultra]
                            [--quality 1]              # int 0-10, overrides preset
                            [--hide] [--minimize]
                            [--boot-dir ./bootstrapper]
                            [--wait]                   # wait for client to exit

    python bootstrapper.py versions                     # query latest version

Example:

    python bootstrapper.py download 11f155f3450f4c8e
    python bootstrapper.py launch 11f155f3450f4c8e TICKET JOIN_URL --fps 30 \
        --graphics-level lowest --hide --wait

Integration with roblox_launcher.py:
  import bootstrapper
  exe = bootstrapper.ensure_roblox("11f155f3450f4c8e", "./bootstrapper")
  settings = bootstrapper.write_settings_xml(
      "./bootstrapper/settings/alt1",
      framerate_cap=30,
      graphics_level="lowest")   # or quality_level=1
  proc = bootstrapper.launch_client(exe, ticket, join_url,
                                    settings_path=settings,
                                    hide=True, minimize=True)
  # keep bootstrapper.guard held for the lifetime of the client
===============================================================================
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import argparse
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MUTEX_NAME = "ROBLOX_singletonMutex"
COOKIES_REL = Path("LocalStorage") / "RobloxCookies.dat"

ROBLOX_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Roblox"
COOKIES_FILE = ROBLOX_DIR / COOKIES_REL

# RobloxApp.zip naming, verified against setup.rbxcdn.com
ROBLOX_APP_ZIP = "version-{version}-RobloxApp.zip"
ROBLOX_MANIFEST = "version-{version}-rbxPkgManifest.txt"
ROBLOX_CDN = "https://setup.rbxcdn.com/"
CLIENT_VERSION_URL = "https://clientsettings.roblox.com/v2/client-version/WindowsPlayer"

# Minimal GlobalBasicSettings_13.xml schema (Item class="UserGameSettings").
# Only FramerateCap and SavedQualityLevel are set; everything else is left to
# Roblox's defaults. The referent GUID is arbitrary but must be unique-ish.
_SETTINGS_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">\n'
    '  <External>null</External>\n'
    '  <External>nil</External>\n'
    '  <Item class="UserGameSettings" referent="{referent}">\n'
    '    <Properties>\n'
    '      <token name="SavedQualityLevel">{quality}</token>\n'
    '      <float name="MouseSensitivity">0.180000007</float>\n'
    '      <int name="FramerateCap">{fps}</int>\n'
    '    </Properties>\n'
    '  </Item>\n'
    '</roblox>\n'
)

# Roblox SavedQualityLevel values (from GlobalBasicSettings_13.xml).
# 0 = Automatic (Roblox picks), 1 = Lowest, 2-10 = rising quality.
# These are the same values your main launcher's altQualityLevel uses.
GRAPHICS_LEVELS = {
    "automatic": 0,
    "lowest": 1,
    "low": 2,
    "medium": 5,
    "high": 8,
    "ultra": 10,
}


def resolve_graphics_level(value) -> int:
    """Resolve a graphics level to a SavedQualityLevel integer.

    Accepts an int (0-10) or a case-insensitive name:
      automatic, lowest, low, medium, high, ultra
    Returns the int. Raises ValueError on unknown input.
    """
    if isinstance(value, int):
        level = value
    else:
        key = str(value).strip().lower()
        if key not in GRAPHICS_LEVELS:
            raise ValueError(
                f"Unknown graphics level {value!r}. "
                f"Options: {', '.join(sorted(GRAPHICS_LEVELS))} or an int 0-10.")
        level = GRAPHICS_LEVELS[key]
    if not 0 <= level <= 10:
        raise ValueError(
            f"Graphics level {level} out of range (use 0-10, or a preset name).")
    return level

# ShowWindow commands
SW_HIDE = 0
SW_MINIMIZE = 6
SW_RESTORE = 9
SW_SHOWNOACTIVATE = 4
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020

ERROR_ALREADY_EXISTS = 183
ERROR_SHARING_VIOLATION = 32


# ---------------------------------------------------------------------------
# Version discovery
# ---------------------------------------------------------------------------

def discover_latest_version(timeout=20) -> str:
    """Query Roblox's client-version endpoint and return the upload hash.

    Returns e.g. "11f155f3450f4c8e" (the part after "version-" in the zip name).
    Raises RuntimeError on failure.
    """
    req = urllib.request.Request(
        CLIENT_VERSION_URL,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as exc:
        raise RuntimeError(f"Could not query Roblox client version: {exc}") from exc
    upload = data.get("clientVersionUpload") or ""
    if not upload:
        raise RuntimeError(f"Roblox version endpoint returned no upload hash: {data!r}")
    m = re.search(r"version-([0-9a-f]+)", upload, re.IGNORECASE)
    if not m:
        raise RuntimeError(f"Could not parse version hash from {upload!r}")
    return m.group(1)


# ---------------------------------------------------------------------------
# Download + extraction (own installation)
# ---------------------------------------------------------------------------

class BootstrapperError(RuntimeError):
    pass


def _download(url: str, dest: Path, chunk: int = 1 << 20,
              progress=None) -> int:
    """Download `url` to `dest`. Returns bytes written. `progress(bytes_done)`."""
    req = urllib.request.Request(url,
                                 headers={"User-Agent": "RobloxBootstrapper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = resp.headers.get("Content-Length")
            if total:
                total = int(total)
            done = 0
            with open(dest, "wb") as out:
                while True:
                    data = resp.read(chunk)
                    if not data:
                        break
                    out.write(data)
                    done += len(data)
                    if progress:
                        progress(done, total)
    except Exception as exc:
        raise BootstrapperError(f"Download failed ({url}): {exc}") from exc
    return done


def download_roblox(version: str, dest_dir: str | Path,
                    progress=None) -> Path:
    """Download + extract `version-<version>-RobloxApp.zip` into `dest_dir`.

    Returns the path to the extracted version folder
    (dest_dir / version-<version> /). Skips re-download if the exe already
    exists there. `dest_dir` defaults to ./bootstrapper relative to this file.
    """
    dest = Path(dest_dir)
    version_dir = dest / f"version-{version}"
    exe = version_dir / "RobloxPlayerBeta.exe"
    if exe.exists():
        return version_dir

    dest.mkdir(parents=True, exist_ok=True)
    zip_name = ROBLOX_APP_ZIP.format(version=version)
    zip_url = ROBLOX_CDN + zip_name
    zip_path = dest / zip_name

    def _prog(done, total):
        if progress:
            progress(done, total)
        elif total:
            pct = done * 100 // total
            print(f"\r  {zip_name}: {pct:3d}% ({done // 1024 // 1024} MB / "
                  f"{total // 1024 // 1024} MB)", end="", flush=True)

    print(f"Downloading {zip_name} ...")
    _download(zip_url, zip_path, progress=_prog)
    if not progress:
        print()

    print(f"Extracting to {version_dir} ...")
    version_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(version_dir)
    except Exception as exc:
        raise BootstrapperError(f"Failed to extract {zip_path}: {exc}") from exc
    finally:
        # keep the zip as a local cache; it's small enough and useful offline
        pass

    if not exe.exists():
        # Some zips wrap everything in a top-level folder; look one level down.
        nested = next(version_dir.iterdir(), None)
        if nested and nested.is_dir() and (nested / "RobloxPlayerBeta.exe").exists():
            # move contents up one level
            for item in nested.iterdir():
                shutil.move(str(item), str(version_dir / item.name))
            shutil.rmtree(nested, ignore_errors=True)
    if not exe.exists():
        raise BootstrapperError(
            f"Extracted {zip_name} but RobloxPlayerBeta.exe not found in "
            f"{version_dir}")
    print(f"Ready: {exe}")
    return version_dir


def ensure_roblox(version: Optional[str] = None,
                  boot_dir: str | Path = "bootstrapper",
                  progress=None) -> Path:
    """Ensure a Roblox installation exists for `version`.

    Downloads it if missing. Returns the version folder containing
    RobloxPlayerBeta.exe. If `version` is None, queries Roblox for the latest.
    """
    if version is None:
        version = discover_latest_version()
        print(f"Latest Roblox version: {version}")
    return download_roblox(version, boot_dir, progress=progress)


def roblox_exe(boot_dir: str | Path = "bootstrapper",
               version: Optional[str] = None) -> Optional[Path]:
    """Return the path to RobloxPlayerBeta.exe for `version`, or None."""
    dest = Path(boot_dir)
    if version is None:
        # pick the newest version folder
        folders = sorted(dest.glob("version-*"), key=lambda p: p.stat().st_mtime,
                         reverse=True)
        for f in folders:
            exe = f / "RobloxPlayerBeta.exe"
            if exe.exists():
                return exe
        return None
    exe = dest / f"version-{version}" / "RobloxPlayerBeta.exe"
    return exe if exe.exists() else None


# ---------------------------------------------------------------------------
# Per-client settings XML (FPS control)
# ---------------------------------------------------------------------------

def write_settings_xml(settings_dir: str | Path,
                       framerate_cap: int = 60,
                       quality_level: int = 1,
                       graphics_level=None,
                       mouse_sensitivity: float = 0.18) -> Path:
    """Write a per-client GlobalBasicSettings XML with a custom FramerateCap.

    Each client instance should get its own file so alts can have different FPS
    caps and graphics levels. Returns the path to the written XML. The caller
    passes this path with `-g /globalBasicSettingsPath <path>` when launching
    the client.

    `graphics_level` overrides `quality_level` when given; it accepts an int
    (0-10) or a preset name: automatic, lowest, low, medium, high, ultra.
    """
    if graphics_level is not None:
        quality_level = resolve_graphics_level(graphics_level)
    else:
        quality_level = resolve_graphics_level(quality_level)
    sdir = Path(settings_dir)
    sdir.mkdir(parents=True, exist_ok=True)
    referent = "RBX" + "".join(random.choice("0123456789ABCDEF") for _ in range(32))
    text = _SETTINGS_TEMPLATE.format(
        referent=referent,
        quality=quality_level,
        fps=max(0, int(framerate_cap)),
    )
    path = sdir / "GlobalBasicSettings.xml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Multi-instance guard (mutex + cookie lock)
# ---------------------------------------------------------------------------

_kernel32 = ctypes.windll.kernel32

_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
_kernel32.CreateMutexW.restype = wt.HANDLE
_kernel32.GetLastError.argtypes = []
_kernel32.GetLastError.restype = wt.DWORD
_kernel32.ReleaseMutex.argtypes = [wt.HANDLE]
_kernel32.ReleaseMutex.restype = wt.BOOL
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.CloseHandle.restype = wt.BOOL

_kernel32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD,
                                  ctypes.c_void_p, wt.DWORD, wt.DWORD,
                                  wt.HANDLE]
_kernel32.CreateFileW.restype = wt.HANDLE

GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80


@dataclass
class InstanceGuard:
    """Holds the Roblox singleton mutex + cookie lock for one client lifetime.

    Acquire before launching, release after the client exits. Without this,
    Roblox may refuse to start a second instance (error 773 / singleton race).
    """
    mutex_handle: Optional[wt.HANDLE] = None
    cookie_handle: Optional[wt.HANDLE] = None
    owned_mutex: bool = False

    def acquire(self) -> bool:
        self.mutex_handle = _kernel32.CreateMutexW(None, True, MUTEX_NAME)
        if self.mutex_handle:
            if _kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
                _kernel32.CloseHandle(self.mutex_handle)
                self.mutex_handle = None
            else:
                self.owned_mutex = True
        if COOKIES_FILE.exists():
            h = _kernel32.CreateFileW(str(COOKIES_FILE), GENERIC_READ, 0,
                                      None, OPEN_EXISTING,
                                      FILE_ATTRIBUTE_NORMAL, None)
            # CreateFileW returns INVALID_HANDLE_VALUE (-1) on failure.
            if int(h) != -1 and _kernel32.GetLastError() != ERROR_SHARING_VIOLATION:
                self.cookie_handle = h
        return self.owned_mutex or self.cookie_handle is not None

    def release(self) -> None:
        if self.cookie_handle:
            try:
                _kernel32.CloseHandle(self.cookie_handle)
            except Exception:
                pass
            self.cookie_handle = None
        if self.mutex_handle:
            try:
                _kernel32.ReleaseMutex(self.mutex_handle)
            except Exception:
                pass
            try:
                _kernel32.CloseHandle(self.mutex_handle)
            except Exception:
                pass
            self.mutex_handle = None
            self.owned_mutex = False


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def launch_client(exe: str | Path,
                  ticket: str,
                  join_url: str,
                  settings_path: Optional[str | Path] = None,
                  hide: bool = False,
                  minimize: bool = False,
                  cwd: Optional[str | Path] = None,
                  creation_flags: int = 0) -> subprocess.Popen:
    """Launch RobloxPlayerBeta.exe with `--app -t <ticket> -j <join_url>`.

    If `settings_path` is given, adds `-g <settings_path>` so the client uses a
    per-instance GlobalBasicSettings (FramerateCap etc.). `hide`/`minimize` are
    accepted for API symmetry but the actual window hiding is done by the
    caller (or by roblox_launcher.hide_window) once the window appears - this
    function only starts the process.

    Returns the Popen handle. The caller is responsible for waiting / cleanup.
    """
    exe = Path(exe)
    if not exe.exists():
        raise BootstrapperError(f"RobloxPlayerBeta.exe not found: {exe}")

    cmd = [str(exe), "--app"]
    if ticket:
        cmd += ["-t", ticket]
    if join_url:
        cmd += ["-j", join_url]
    if settings_path:
        cmd += ["-g", str(settings_path)]

    printable = " ".join(f'"{c}"' if " " in c else c for c in cmd)
    print(f"[bootstrapper] {printable}")
    if cwd is None:
        cwd = exe.parent
    proc = subprocess.Popen(cmd, cwd=str(cwd),
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            creationflags=creation_flags)
    return proc


def wait_for_exit(proc: subprocess.Popen,
                  poll: float = 1.0) -> int:
    """Block until `proc` exits. Returns its return code."""
    while proc.poll() is None:
        time.sleep(poll)
    return proc.returncode or 0


# ---------------------------------------------------------------------------
# Orchestration: download + launch in one call
# ---------------------------------------------------------------------------

@dataclass
class LaunchResult:
    proc: subprocess.Popen
    exe: Path
    settings: Path
    guard: InstanceGuard
    version: str
    exit_code: Optional[int] = None


def run(version: Optional[str],
        ticket: str,
        join_url: str,
        *,
        boot_dir: str | Path = "bootstrapper",
        fps: int = 60,
        quality: int = 1,
        graphics_level: Optional[int | str] = None,
        hide: bool = False,
        minimize: bool = False,
        wait: bool = False,
        guard: bool = True,
        settings_tag: Optional[str] = None) -> LaunchResult:
    """Ensure Roblox is downloaded, write per-client settings, acquire the
    multi-instance guard, and launch the client.

    This is the single entry point for "my own bootstrapper with FPS control".

    `graphics_level` overrides `quality` when given; accepts an int (0-10) or
    a preset name: automatic, lowest, low, medium, high, ultra.
    """
    if not ticket:
        raise BootstrapperError("auth ticket is required (-t / --ticket)")
    if not join_url:
        raise BootstrapperError("join script url is required (-j / --join-url)")

    exe_dir = ensure_roblox(version, boot_dir)
    exe = exe_dir / "RobloxPlayerBeta.exe"

    tag = settings_tag or f"client-{os.getpid()}-{int(time.time())}"
    settings_dir = Path(boot_dir) / "settings" / tag
    settings = write_settings_xml(settings_dir,
                                  framerate_cap=fps,
                                  quality_level=quality,
                                  graphics_level=graphics_level)

    g = InstanceGuard() if guard else InstanceGuard()
    if guard:
        if not g.acquire():
            print("[bootstrapper] warning: could not acquire full guard "
                  "(mutex or cookie lock); launching anyway.", file=sys.stderr)

    proc = launch_client(exe, ticket, join_url,
                         settings_path=settings,
                         hide=hide, minimize=minimize,
                         cwd=exe_dir)

    if wait:
        rc = wait_for_exit(proc)
        g.release()
        print(f"[bootstrapper] client exited with code {rc}")
        return LaunchResult(proc=proc, exe=exe, settings=settings,
                            guard=g, version=str(exe_dir.name), exit_code=rc)

    return LaunchResult(proc=proc, exe=exe, settings=settings,
                        guard=g, version=str(exe_dir.name))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser():
    import argparse
    ap = argparse.ArgumentParser(
        prog="bootstrapper",
        description="Lightweight Roblox bootstrapper: download its own client, "
                    "launch with per-client FPS cap (voidstrap/froststrap style).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    dl = sub.add_parser("download", help="download + extract a Roblox version")
    dl.add_argument("version", help="version hash (e.g. 11f155f3450f4c8e)")
    dl.add_argument("--boot-dir", default="bootstrapper")
    dl.set_defaults(func=_cmd_download)

    ver = sub.add_parser("versions", help="query the latest Roblox version")
    ver.set_defaults(func=_cmd_versions)

    ln = sub.add_parser("launch", help="launch a Roblox client with custom FPS")
    ln.add_argument("version", nargs="?", default=None,
                    help="version hash (queries latest if omitted)")
    ln.add_argument("ticket", nargs="?", default="",
                    help="authentication ticket (-t)")
    ln.add_argument("join_url", nargs="?", default="",
                    help="joinScriptUrl (-j)")
    ln.add_argument("--fps", type=int, default=60, help="frame rate cap")
    ln.add_argument("--quality", type=int, default=1,
                    help="SavedQualityLevel int (0=automatic, 1=lowest, 2-10)")
    ln.add_argument("--graphics-level", default=None,
                    help="Graphics level preset: automatic, lowest, low, "
                         "medium, high, ultra (overrides --quality)")
    ln.add_argument("--hide", action="store_true",
                    help="flag to hide window after it opens (caller handles it)")
    ln.add_argument("--minimize", action="store_true",
                    help="minimize (stops rendering swapchain presentation)")
    ln.add_argument("--boot-dir", default="bootstrapper")
    ln.add_argument("--no-guard", action="store_true",
                    help="do not hold the singleton mutex / cookie lock")
    ln.add_argument("--wait", action="store_true",
                    help="wait for the client to exit before returning")
    ln.add_argument("--settings-tag", default=None,
                    help="tag for the per-client settings folder")
    ln.set_defaults(func=_cmd_launch)

    return ap


def _cmd_download(args) -> int:
    vdir = download_roblox(args.version, args.boot_dir)
    print(f"Installed: {vdir}")
    print(f"Exe: {vdir / 'RobloxPlayerBeta.exe'}")
    return 0


def _cmd_versions(args) -> int:
    try:
        v = discover_latest_version()
        print(f"Latest Roblox WindowsPlayer version: {v}")
        print(f"Zip: {ROBLOX_CDN}{ROBLOX_APP_ZIP.format(version=v)}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_launch(args) -> int:
    try:
        result = run(args.version, args.ticket, args.join_url,
                     boot_dir=args.boot_dir,
                     fps=args.fps,
                     quality=args.quality,
                     graphics_level=args.graphics_level,
                     hide=args.hide,
                     minimize=args.minimize,
                     wait=args.wait,
                     guard=not args.no_guard,
                     settings_tag=args.settings_tag)
    except BootstrapperError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # keep the CLI honest
        print(f"Error: {exc!r}", file=sys.stderr)
        return 1

    gl = args.graphics_level if args.graphics_level else f"{args.quality}"
    print(f"[bootstrapper] client started (pid {result.proc.pid})")
    print(f"[bootstrapper] exe: {result.exe}")
    print(f"[bootstrapper] settings: {result.settings} "
          f"(FPS cap {args.fps}, graphics level {gl})")
    print(f"[bootstrapper] guard held: mutex={result.guard.owned_mutex}, "
          f"cookie={result.guard.cookie_handle is not None}")
    if args.wait:
        print(f"[bootstrapper] client exited with code {result.proc.returncode}")
        result.guard.release()
    else:
        print("[bootstrapper] client is running. Hold this process (or its guard) "
              "for the client's lifetime, then call guard.release().")
    return 0


def main(argv=None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
