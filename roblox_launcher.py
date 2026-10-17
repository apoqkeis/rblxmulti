#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================================
 Roblox Headless Multi-Instance Launcher
============================================================================
 Launches your MAIN account in a normal (visible) Roblox client and one or
 more ALT accounts in HIDDEN ("headless") Roblox clients, all at the same
 time on Windows.

 How it works (all local + verified patterns used by popular tools):
   1. For every account we fetch a short-lived auth ticket from
      https://auth.roblox.com/v1/authentication-ticket/ using the account's
      .ROBLOSECURITY cookie (no stored-cookie surgery needed).
   2. We launch each client with the standard roblox-player: protocol URL
      passing that ticket as gameinfo - this logs the client in as that
      specific account.
   3. We hold the "ROBLOX_singletonMutex" named mutex and keep the Roblox
      player's RobloxCookies.dat locked (share=none) for the lifetime of
      the program - the community mitigation against Roblox closing extra
      instances / error 773-style resets.
   4. Windows of ALT accounts are hidden with ShowWindow(SW_HIDE); a
      watcher re-hides them if Roblox recreates the window. (Optional,
      off-by-default `noRender` also minimizes them so they stop drawing.)
   5. Anti-AFK: every alt jumps (Space) each jumpIntervalSec while the main
      is in a game. Roblox needs the window focused (raw input), so the jump
      shows the alt only as a fully transparent layered window (alpha 0) —
      nothing ever appears on screen, and focus returns to your previous
      window immediately after. Jumps are held off while the main client is
      your active window (Windows always gives focus to whoever gets the key,
      so jumping then would blur the main). Alt clients are also marked as
      background work (EcoQoS + low priority) to keep their hidden rendering
      cheap.

 IMPORTANT (read me):
   * Requires Roblox installed from https://www.roblox.com/download
     (the web installer) - NOT the Microsoft Store version.
   * Roblox actively detects and closes multiple instances (anti-farm).
     This tool is BEST-EFFORT: instances may still be closed by Roblox.
   * Automating accounts may violate Roblox's ToS. Use only for your own
     accounts and legitimate purposes. You are responsible for your use.
   * "Headless" = hidden window. The client still renders and uses GPU/CPU.

 Run (GUI):      python roblox_launcher.py
 Run (CLI):      python roblox_launcher.py --cli --launch-all
 Help:           python roblox_launcher.py --help
============================================================================
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import datetime
import json
import os
import random
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

import bootstrapper

# When compiled with PyInstaller --noconsole the bootloader sets stdout/stderr
# to None; our print() calls (status/log lines) would then raise. Redirect them
# to NUL so the GUI exe runs without a console. No-op when run as a script.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

APP_NAME = "Roblox Headless Multi-Instance Launcher"
DEFAULT_CONFIG = "config.json"
CONFIG_TEMPLATE = "config.example.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

REFERER = "https://www.roblox.com/games/4924922222/Brookhaven-RP"
AUTH_URL = "https://auth.roblox.com/v1/authentication-ticket/"
USER_URL = "https://users.roblox.com/v1/users/authenticated"
PLACE_DETAILS_URL = "https://www.roblox.com/places/api-get-details?assetId={}"

LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
ROBLOX_DIR = Path(LOCALAPPDATA) / "Roblox"
COOKIES_FILE = ROBLOX_DIR / "LocalStorage" / "RobloxCookies.dat"

MUTEX_NAME = "ROBLOX_singletonMutex"

# ShowWindow commands
SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6                     # <- what actually stops rendering
SW_RESTORE = 9
# The only ShowWindow call that brings a minimized+hidden window back WITHOUT
# activating it. SW_SHOW and SW_RESTORE both activate, and measured, SW_SHOW does
# not even un-minimize. See post_jump_alt_instance.
SW_SHOWNOACTIVATE = 4
# WM_SYSCOMMAND / SC_MINIMIZE: the exact call the rbxsilent project uses to
# make a client stop rendering (D3D stops presenting to a minimized swapchain,
# physics + networking keep running).
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE = 0xF020
WS_EX_TOOLWINDOW = 0x00000080

# Window-long offset + layered-window styles (flicker-free anti-AFK jumps)
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
LWA_ALPHA = 0x00000002

# --- status overlay (a click-through, always-on-top readout) -----------------
# WS_EX_TRANSPARENT makes the window click-through; WS_EX_NOACTIVATE stops it
# ever taking focus (which matters here: the anti-AFK jump and the jump's
# focus-restore both reason about the foreground window, so an overlay that
# could activate would interfere with them).
WS_POPUP = 0x80000000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_NOACTIVATE = 0x08000000
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
HTTRANSPARENT = -1
WM_NCHITTEST = 0x0084
WM_PAINT = 0x000F
WM_TIMER = 0x0113
WM_DESTROY = 0x0002
TRANSPARENT_BK = 1
DT_CALCRECT = 0x0400
WM_ERASEBKGND = 0x0014

# CreateFileW / OpenProcess constants
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
PROCESS_TERMINATE = 0x0001
PROCESS_SET_INFORMATION = 0x0200
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

# SetProcessInformation(ProcessPowerThrottling) -> EcoQoS for alt clients.
_PROC_POWER_THROTTLING = 8          # PROCESS_INFORMATION_CLASS member


class _POWER_THROTTLING_STATE(ctypes.Structure):
    _fields_ = [("Version", wt.DWORD), ("ControlMask", wt.DWORD), ("StateMask", wt.DWORD)]

ERROR_ALREADY_EXISTS = 183
ERROR_SHARING_VIOLATION = 32


# ---------------------------------------------------------------------------
# Win32 helpers (ctypes, stdlib only)
# ---------------------------------------------------------------------------

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


def enum_windows():
    """Return a list of all top-level window handles."""
    result = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL

    @WNDENUMPROC
    def _cb(hwnd, lparam):
        result.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return result


def window_pid(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
    return pid.value


def window_title(hwnd):
    length = user32.GetWindowTextLengthW(wt.HWND(hwnd))
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(wt.HWND(hwnd), buf, length + 1)
    return buf.value


def window_visible(hwnd):
    return bool(user32.IsWindowVisible(wt.HWND(hwnd)))


def window_minimized(hwnd):
    """True while the window is iconic.

    A minimized client stops presenting frames (that is the "no render" trick),
    so this is the state a hidden alt is supposed to sit in.
    """
    return bool(user32.IsIconic(wt.HWND(hwnd)))


def hide_window(hwnd, no_render=True):
    """Make an alt invisible AND stop it rendering (the rbxsilent trick).

    SW_HIDE alone only makes the window invisible — the client keeps drawing
    frames into a window nobody sees. Minimizing is what actually disables
    rendering: the GPU stops presenting to a minimized swapchain while physics
    and networking carry on.

    Order matters, and was measured on real windows: minimizing an already-HIDDEN
    window leaves WS_VISIBLE set (iconic but still "visible" to the OS, which
    would make the watcher re-minimize it forever). So minimize FIRST, send the
    WM_SYSCOMMAND/SC_MINIMIZE message the client expects, and hide LAST — that
    lands on hidden + minimized, which is the state we want.
    Also drops the window from the taskbar so it behaves like background work.
    """
    h = wt.HWND(hwnd)
    exstyle = user32.GetWindowLongW(h, GWL_EXSTYLE)
    user32.SetWindowLongW(h, GWL_EXSTYLE, exstyle | WS_EX_TOOLWINDOW)
    if no_render:
        user32.ShowWindow(h, SW_MINIMIZE)                  # stop rendering
        user32.PostMessageW(h, WM_SYSCOMMAND, SC_MINIMIZE, 0)
    user32.ShowWindow(h, SW_HIDE)                          # invisible (last)


def show_window(hwnd):
    """Undo hide_window: un-minimize, un-hide, and become a normal window again
    (taskbar entry restored) so the user can actually see the alt."""
    if not hwnd:
        return
    h = wt.HWND(hwnd)
    user32.ShowWindow(h, SW_RESTORE)          # leave the no-render state
    user32.ShowWindow(h, SW_SHOW)
    exstyle = user32.GetWindowLongW(h, GWL_EXSTYLE)
    if exstyle & WS_EX_TOOLWINDOW:
        user32.SetWindowLongW(h, GWL_EXSTYLE, exstyle & ~WS_EX_TOOLWINDOW)


VK_SPACE = 0x20
VK_LEFT = 0x25
VK_RIGHT = 0x27
KEYEVENTF_KEYUP = 0x0002

# Robust HWND/thread types so 64-bit handles survive the ctypes boundary.
user32.GetForegroundWindow.restype = wt.HWND
user32.GetForegroundWindow.argtypes = []
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.BringWindowToTop.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
kernel32.GetCurrentThreadId.restype = wt.DWORD

# Layered-window API: lets us SHOW a window while it renders nothing on screen
# (alpha 0), so a hidden alt can take foreground for a jump with zero flicker.
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD]
user32.SetLayeredWindowAttributes.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
# SendMessageW must be declared explicitly. Without argtypes ctypes defaults
# every argument to c_int, which TRUNCATES the 64-bit HWND - so the message
# would be delivered to a bogus window (or crash) rather than to the alt.
#
# The return type is SIGNED and it matters: LRESULT is LONG_PTR, and window
# procedures return negative sentinels. Declaring it c_size_t makes
# HTTRANSPARENT (-1) come back as 18446744073709551615, which silently breaks
# every hit test that relies on it. (LRESULT does not exist in this Python's
# wintypes, hence c_ssize_t.)
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
# IsWindowVisible is what `window_visible` uses to decide an alt's window is
# really on screen. It has no argtypes, so the HWND would be truncated on
# 64-bit and it would answer about the wrong window entirely.
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL

# Process power throttling (EcoQoS) + priority, for alt clients only.
kernel32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.SetPriorityClass.restype = wt.BOOL
try:
    kernel32.SetProcessInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    kernel32.SetProcessInformation.restype = wt.BOOL
    _HAS_POWER_THROTTLE = True
except AttributeError:                     # ancient Windows: skip EcoQoS
    _HAS_POWER_THROTTLE = False


def force_foreground(hwnd):
    """Move foreground focus to `hwnd`, bypassing the foreground-lock rules.

    Roblox reads raw input, so injected keys fall on whatever window has focus —
    the target must be foreground for a jump to land. We attach our input queue
    to the target (and the current foreground) thread, which is the documented
    way to allow SetForegroundWindow from a background process.
    """
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    cur = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    tgt_tid = user32.GetWindowThreadProcessId(wt.HWND(hwnd), None)
    user32.AttachThreadInput(cur, fg_tid, True)
    attached = user32.AttachThreadInput(cur, tgt_tid, True)
    try:
        user32.BringWindowToTop(wt.HWND(hwnd))
        user32.SetForegroundWindow(wt.HWND(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur, tgt_tid, False)
        user32.AttachThreadInput(cur, fg_tid, False)
    # Give the window manager a moment to actually transfer focus.
    for _ in range(5):
        if user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return False


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

# How hard to try to get an alt's window presenting again before giving up on a
# posted jump. Each attempt re-asserts SW_SHOWNOACTIVATE and waits `settle`, so
# this is the only thing standing between a transient restore failure and a
# silently ignored key press.
_JUMP_RESTORE_ATTEMPTS = 3


def post_jump_alt_instance(hwnd, no_render=True, settle=0.18, report=None):
    """Jump an alt by POSTING Space to it, without ever taking focus.

    Two variants are tried, cheapest first, because the earlier finding was not
    "posting does not work" but "posting to a HIDDEN or TRANSPARENT window is
    ignored":

      1. transparent  - WS_EX_LAYERED + alpha 0. Still "visible" to Windows, but
         Roblox does not present frames for it and ignores the input.
      2. off-screen   - left fully OPAQUE, but moved to a coordinate no user can
         see. Windows still treats it as a normal visible window, so whatever
         made variant 1 fail may not apply here, and the user still never sees
         anything. This is the one variant that has never actually been tested.

    Both encode lParam per the documented layout - repeat count (bits 0-15),
    scan code (16-23), extended (24), context (25-29), previous key state (30),
    transition state (31). The old code sent 0x00390001 for the key-down, which
    omitted the previous-key-state bit, so even a window that DID read the
    message had no reason to believe a key was involved.

    PostMessage and SendMessage are both tried, because an app that pumps its
    own queue only sees the synchronous form.

    IMPORTANT - this cannot be verified quickly. There is no observable that
    says "the character jumped": the client log records no input at all, and the
    only ground truth is whether Roblox eventually kicks the alt for idling,
    which takes many minutes. A harness proved the underlying limitation
    (see keyboard_path_test): a posted WM_KEYDOWN IS delivered to the window's
    WndProc, yet a system-wide WH_KEYBOARD_LL hook never sees it, and Roblox
    reads raw input, which only ever comes from a real key event. So this can
    only work if Roblox also happens to read ordinary window messages.
    """
    if not hwnd:
        return False
    # observe the real state: a minimized alt must be restored even when
    # no_render is off, or SW_SHOW on a minimized window does nothing
    was_minimized = window_minimized(hwnd)
    orig_exstyle = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    h = wt.HWND(hwnd)

    # lParam per the documented WM_KEYDOWN layout. The key-down sets bit 30
    # (the key was already down) and leaves bit 31 clear (this is the
    # transition); the key-up sets bit 31 as well.
    lp_down = 1 | (0x39 << 16) | (1 << 30)          # 0x40390001
    lp_up = 1 | (0x39 << 16) | (1 << 30) | (1 << 31)   # 0xC0390001

    def send_both():
        """Post, then Send. Either alone can miss an app that reads one form.

        SendMessage's return value is whatever the window procedure returned,
        which is legitimately 0 for WM_KEYDOWN - so it says NOTHING about whether
        the message was delivered. Gating the key-up on it silently swallowed
        every key-up, leaving the key logically stuck down. Only PostMessage
        returns a meaningful success BOOL.
        """
        ok = False
        if user32.PostMessageW(h, WM_KEYDOWN, VK_SPACE, lp_down):
            user32.PostMessageW(h, WM_KEYUP, VK_SPACE, lp_up)
            ok = True
        user32.SendMessageW(h, WM_KEYDOWN, VK_SPACE, lp_down)
        user32.SendMessageW(h, WM_KEYUP, VK_SPACE, lp_up)
        return ok

    delivered = False
    if report is None:
        report = {}
    try:
        # --- variant 1: transparent, shown, focus never touched -------------
        user32.SetWindowLongW(h, GWL_EXSTYLE, orig_exstyle | WS_EX_LAYERED)
        user32.SetLayeredWindowAttributes(h, 0, 0, LWA_ALPHA)
        # Bring the window back WITHOUT ACTIVATING IT. This is the entire point
        # of the "message" method, and the call matters enormously. Measured on
        # real windows that are genuinely minimized+hidden, with another window
        # genuinely in the foreground:
        #
        #     call                       came back   kept the user's focus
        #     SW_SHOW          (5)          NO           NO   <- stole it
        #     SW_RESTORE       (9)          yes          NO   <- stole it
        #     SW_SHOWNOACTIVATE(4)          yes          yes  <- the one
        #     WM_SYSCOMMAND SC_RESTORE      yes          NO   <- stole it
        #
        # So SW_SHOW is doubly wrong: it ACTIVATES the window, and on a
        # minimized+hidden one it does not even un-minimize it. Using it here
        # made this method steal focus exactly like the "focus" method while
        # also failing to jump - which is what was reported. SW_RESTORE is no
        # better. Only SW_SHOWNOACTIVATE does both jobs.
        user32.ShowWindow(h, SW_SHOWNOACTIVATE)

        # --- verify, and keep trying until it really is presented -----------
        # "Delivered" is not the question; "was the window actually able to
        # accept it" is. Retrying here is what turns a silent no-op into an
        # observable one, and the observations go back in `report` so a
        # stop-after-a-few-jumps failure can be diagnosed instead of guessed at.
        attempts = 0
        while attempts < _JUMP_RESTORE_ATTEMPTS:
            if not user32.IsIconic(h) and user32.IsWindowVisible(h):
                break
            user32.ShowWindow(h, SW_SHOWNOACTIVATE)
            time.sleep(settle)
            attempts += 1
        report["restore_attempts"] = attempts
        report["iconic"] = bool(user32.IsIconic(h))
        report["visible"] = bool(user32.IsWindowVisible(h))
        if report["iconic"]:
            # Cannot be delivered: the client is not presenting a window, and
            # this is the single most likely reason a jump "works a few times
            # then stops".
            report["reason"] = "window is still minimized after restoring"
            return False
        delivered = send_both()

        # --- variant 2: opaque but off-screen -------------------------------
        # Restores the original exstyle first, so the window really is opaque
        # again, then parks it where no desktop can show it. A window at
        # -32000 is still "visible" in every sense Windows uses for input.
        # SWP_NOACTIVATE keeps the focus promise even if the window somehow
        # wants activating.
        user32.SetWindowLongW(h, GWL_EXSTYLE, orig_exstyle)
        user32.SetWindowPos(h, None, -32000, -32000, 800, 600,
                            0x0001 | 0x0010)      # SWP_NOSIZE | SWP_NOACTIVATE
        time.sleep(settle)
        report["iconic_after_move"] = bool(user32.IsIconic(h))
        delivered = send_both() or delivered
        report["reason"] = "delivered"
        return delivered
    finally:
        # straight back to hidden + no-render; focus is never touched
        if was_minimized:
            hide_window(hwnd, no_render=True)
        else:
            user32.ShowWindow(h, SW_HIDE)
        user32.SetWindowLongW(h, GWL_EXSTYLE, orig_exstyle)


def jump_alt_instance(hwnd, no_render=True, settle=0.35, action="jump"):
    """Make a hidden alt client jump (Space) for anti-AFK, without the window
    ever appearing on screen.

    Roblox reads raw input, so injected keys only land on whichever window is
    foreground — the alt must briefly hold focus. To keep that completely
    invisible we temporarily set WS_EX_LAYERED + alpha 0 (fully transparent):
    the window can be "shown" and focused, but nothing is drawn, so the user
    never sees a window change. A minimized (no-render) alt is restored first,
    because a minimized window cannot take focus; afterwards it goes straight
    back to hidden+minimized so rendering stays off. Focus is handed back to
    whatever window the user had in front.

    Two ideas taken from JunkBeat/AntiAFK-Roblox, a working tray tool that does
    the same thing (briefly focus, real key press, optional invisible window):

      * `settle` — "Open for ... sec: set a duration long enough for the window
        contents to load". A client that has been minimized and hidden has to
        resume rendering and its own loop before it will act on input, and 50ms
        is not enough for that. Their troubleshooting says it outright: "if
        Roblox windows don't focus in time or key presses fail to register, try
        increasing the delay". So this is now configurable rather than a guess.
      * `action="camera"` — their CameraShift mode, which "involves more
        interactions with the Roblox window" and is reported as more reliable
        than a lone jump. Here: jump, then nudge the camera both ways.

    Keys are only injected if the focus actually moved, so a failed focus grab
    can never fire a stray Space into the user's active window.
    Returns True on success, False if the window could not be focused in time.
    """
    if not hwnd:
        return False
    prev = user32.GetForegroundWindow()
    was_minimized = no_render and window_minimized(hwnd)
    orig_exstyle = user32.GetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE)
    try:
        # Transparent but still "visible" -> focusable with zero flicker.
        user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE,
                              orig_exstyle | WS_EX_LAYERED)
        user32.SetLayeredWindowAttributes(wt.HWND(hwnd), 0, 0, LWA_ALPHA)
        if was_minimized:
            user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)   # leave no-render state
        user32.ShowWindow(wt.HWND(hwnd), SW_SHOW)         # draws nothing (alpha 0)
        if not force_foreground(hwnd):
            return False
        # Let the client actually come back before pressing anything. A window
        # that has just been restored is visible but its game loop has not
        # resumed, and a key sent into that gap is simply lost.
        time.sleep(max(0.05, float(settle or 0.0)))

        def tap(vk, scan, hold=0.03):
            user32.keybd_event(vk, scan, 0, 0)             # key down
            time.sleep(hold)
            user32.keybd_event(vk, scan, KEYEVENTF_KEYUP, 0)

        tap(VK_SPACE, 0x39)                               # scancodes: Space 0x39
        if (action or "jump").strip().lower() == "camera":
            time.sleep(0.05)
            tap(VK_LEFT, 0x4B)                            # Left  0x4B
            time.sleep(0.05)
            tap(VK_RIGHT, 0x4D)                           # Right 0x4D
        time.sleep(0.02)                                  # let the key register
        return True
    finally:
        if was_minimized:
            hide_window(hwnd, no_render=True)             # hidden + no-render again
        else:
            user32.ShowWindow(wt.HWND(hwnd), SW_HIDE)     # back to headless
        user32.SetWindowLongW(wt.HWND(hwnd), GWL_EXSTYLE, orig_exstyle)
        if prev and int(prev) != int(hwnd):
            force_foreground(prev)                        # give focus back


def list_processes():
    """Return [(pid, exe_name, parent_pid), ...] using a toolhelp snapshot."""
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wt.DWORD),
            ("cntUsage", wt.DWORD),
            ("th32ProcessID", wt.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wt.DWORD),
            ("cntThreads", wt.DWORD),
            ("th32ParentProcessID", wt.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wt.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return []

    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        procs = []
        while True:
            procs.append((entry.th32ProcessID, entry.szExeFile, entry.th32ParentProcessID))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
        return procs
    finally:
        kernel32.CloseHandle(snapshot)


def roblox_player_pids():
    return {pid for pid, exe, _ in list_processes() if exe.lower() == "robloxplayerbeta.exe"}


def process_alive(pid):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wt.DWORD()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return ok and code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def kill_process(pid):
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def throttle_process(pid):
    """Mark an alt client as background work: EcoQoS + below-normal priority.

    Windows then schedules it on efficient cores / a lower frequency and lets it
    yield to whatever the user is doing. This is the supported way to cut the
    CPU/GPU cost of a client that renders into a hidden window. Best effort:
    returns True if at least one call took, False if the OS refused.
    """
    if not pid:
        return False
    handle = kernel32.OpenProcess(PROCESS_SET_INFORMATION | 0x1000, False, pid)
    if not handle:
        return False
    ok = False
    try:
        if _HAS_POWER_THROTTLE:
            state = _POWER_THROTTLING_STATE()
            state.Version = 1                      # CURRENT_VERSION
            # ControlMask enables the mechanism, StateMask turns it on:
            # execution-speed throttling == EcoQoS (background work).
            state.ControlMask = 0x1
            state.StateMask = 0x1
            ok = bool(kernel32.SetProcessInformation(
                handle, _PROC_POWER_THROTTLING, ctypes.byref(state),
                ctypes.sizeof(state)))
        if kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS):
            ok = True
    finally:
        kernel32.CloseHandle(handle)
    return ok


def instance_alive(rec):
    """True while ANY process of an instance is running.

    Roblox spawns a short-lived bootstrap process plus the real client, so an
    instance is tracked by a SET of pids. Fall back to the single 'pid' entry.
    Returns True when no pid was recorded at all (unknown = keep monitoring).
    """
    pids = rec.get("pids") or ([rec["pid"]] if rec.get("pid") else [])
    if not pids:
        return True
    return any(process_alive(p) for p in pids)


def windows_for_pid(pid):
    return [h for h in enum_windows() if window_pid(h) == pid]


def acquire_mutex(name):
    """Create (and own) a named mutex. Returns (handle, owned) or (None, False)."""
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return None, False
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        # Someone else holds it (e.g. another launcher / tool). Don't touch it.
        kernel32.CloseHandle(handle)
        return None, False
    return handle, True


def lock_cookies_file():
    """Lock RobloxCookies.dat with FileShare.None. Returns handle or None."""
    if not COOKIES_FILE.exists():
        return None
    handle = kernel32.CreateFileW(str(COOKIES_FILE), GENERIC_READ, 0,
                                  None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)
    if handle and handle != -1 and ctypes.get_last_error() != ERROR_SHARING_VIOLATION:
        return handle
    return None


def find_player_exe():
    """Locate RobloxPlayerBeta.exe (web install) in Versions folders.

    Covers the stock layout plus third-party launchers that keep their own
    version folders (e.g. Froststrap), which is where plenty of people actually
    have the client installed.
    """
    candidates = []
    bases = [ROBLOX_DIR / "Versions",
             Path(LOCALAPPDATA) / "Froststrap" / "Versions",
             Path(r"C:\Program Files (x86)\Roblox\Versions"),
             Path(r"C:\Program Files\Roblox\Versions")]
    for base in bases:
        if base.exists():
            candidates.extend(sorted(base.glob("version-*/RobloxPlayerBeta.exe"), reverse=True))
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Per-instance client settings (e.g. an alt-only frame-rate cap)
# ---------------------------------------------------------------------------

# RobloxPlayerBeta accepts -g / --globalBasicSettingsPath: "path to
# GlobalBasicSettings_(n).xml". Pointing an alt at its OWN settings file caps
# only the alts, leaving the main client's settings (and frame cap) untouched.
SETTINGS_XML = ROBLOX_DIR / "GlobalBasicSettings_13.xml"
ALT_SETTINGS_DIR = Path(os.environ.get("TEMP") or str(Path.home() / "AppData/Local")) \
                  / "RobloxMultiInstance"

_SETTINGS_TEMPLATE = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xsi:noNamespaceSchemaLocation="http://www.roblox.com/roblox.xsd" version="4">\n'
    '  <External>null</External>\n'
    '  <External>nil</External>\n'
    '  <Item class="UserGameSettings" referent="RBXFBA8C1564CC84602BC8E5C46AB2BE0F8">\n'
    '    <Properties>\n'
    '      <token name="SavedQualityLevel">{quality}</token>\n'
    '      <float name="MouseSensitivity">0.180000007</float>\n'
    '      <int name="FramerateCap">{fps}</int>\n'
    '    </Properties>\n'
    '  </Item>\n'
    '</roblox>\n'
)


def build_alt_settings_xml(tag, fps=1, quality_level=1):
    """Write a per-alt GlobalBasicSettings file with a low frame-rate cap.

    KEPT FOR REFERENCE ONLY - this is not wired into the launch path, because it
    was measured not to work. The client accepts -g/--globalBasicSettingsPath,
    but when launched with a capped file it used the SAME CPU as an uncapped
    control run (22% vs 20% of a core) and never even rewrote the file, i.e. it
    does not adopt it. Reaching the switch also requires spawning the exe
    directly, which loses Roblox's singleton-mutex race against the clients that
    are already running (they sit in waitForNewPlayerProcess until timeout and
    never join).

    The measured way to make a hidden client cheaper is to keep it MINIMIZED
    (see hide_window / no_render), which cuts CPU by ~1.7x on this machine.
    """
    import re
    try:
        fps = max(0, int(float(fps)))
        quality_level = int(quality_level)
    except (TypeError, ValueError):
        return None
    text = None
    try:
        if SETTINGS_XML.exists():
            text = SETTINGS_XML.read_text(encoding="utf-8", errors="replace")
    except Exception:
        text = None
    if text and "FramerateCap" in text:
        text = re.sub(r'(<int name="FramerateCap">)\s*\d+\s*(</int>)',
                      rf'\g<1>{fps}\g<2>', text)
        text = re.sub(r'(<token name="SavedQualityLevel">)\s*\d+\s*(</token>)',
                      rf'\g<1>{quality_level}\g<2>', text)
    else:
        text = _SETTINGS_TEMPLATE.format(fps=fps, quality=quality_level)
    try:
        # keep the canonical file name inside a per-alt folder: the client is
        # documented as taking "GlobalBasicSettings_(n).xml"
        folder = ALT_SETTINGS_DIR / re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tag))
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / SETTINGS_XML.name
        path.write_text(text, encoding="utf-8")
        return path
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Roblox web API helpers
# ---------------------------------------------------------------------------

def http_request(method, url, cookie=None, csrf=None, referer=REFERER, data=None,
                 json_body=None, timeout=20):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": "https://www.roblox.com",
    }
    if cookie:
        headers["Cookie"] = ".ROBLOSECURITY=" + cookie
    if csrf:
        headers["X-CSRF-TOKEN"] = csrf

    body = None
    if json_body is not None:
        body = json.dumps(json_body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def get_auth_ticket(cookie):
    """Return a short-lived authentication ticket for the given .ROBLOSECURITY cookie."""
    # Step 1: POST without CSRF -> 403 + x-csrf-token header.
    code, headers, _ = http_request("POST", AUTH_URL, cookie=cookie, json_body={})
    csrf = headers.get("x-csrf-token")
    ticket = headers.get("rbx-authentication-ticket")
    if not csrf and ticket:
        return ticket

    if not csrf:
        raise RuntimeError(
            f"Could not obtain a CSRF token (HTTP {code}). "
            "Check that the account cookie is valid and not expired.")

    # Step 2: POST with CSRF -> rbx-authentication-ticket header.
    # Roblox requires Content-Type: application/json on this call (else HTTP 415).
    code, headers, body = http_request("POST", AUTH_URL, cookie=cookie, csrf=csrf, json_body={})
    if code != 200:
        snippet = body[:200].decode("utf-8", "ignore")
        raise RuntimeError(f"Auth ticket request failed (HTTP {code}): {snippet}")

    ticket = headers.get("rbx-authentication-ticket")
    if not ticket:
        raise RuntimeError("Roblox did not return an authentication ticket.")
    return ticket


def get_authenticated_user(cookie):
    try:
        code, _, body = http_request("GET", USER_URL, cookie=cookie)
        if code != 200:
            return None
        data = json.loads(body.decode("utf-8", "ignore"))
        return data.get("name"), data.get("displayName"), data.get("id")
    except Exception:
        return None


def get_place_name(place_id):
    try:
        code, _, body = http_request("GET", PLACE_DETAILS_URL.format(place_id))
        if code != 200:
            return None
        data = json.loads(body.decode("utf-8", "ignore"))
        return data.get("Name") or data.get("name")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reading the client's OWN idea of which server it is in
# ---------------------------------------------------------------------------
# Presence was the only source of "which server is this account in", and it is
# unreliable in exactly the way that matters:
#
#   * it is rate limited to the point of uselessness (233 HTTP 429s in one log),
#   * it returns phantom readings (type=2 with gameId/placeId/lastLocation all
#     null), and
#   * worst of all it is STALE across a client restart. In the live log the
#     main's client was closed and restarted, presence kept reporting the
#     PREVIOUS session's job for another ~30s, and the launcher duly aimed six
#     alts at a server the main had already left. The alts were pinned
#     perfectly - to the wrong server.
#
# The client itself writes the answer to its own log, and it is first-hand:
#
#   ! Joining game 'fdba3109-2da7-4922-8464-6baa72bcc06e' place 107778070777162 at 161.193.12.10
#
# No API, no rate limit, no cache, and it cannot be stale - it is written at the
# moment of the join. Every line below exists to read that one line.

# "place <id>" is sometimes a universe id or absent, so only the job is trusted.
_CLIENT_JOIN_RE = re.compile(r"! Joining game '([0-9a-fA-F-]{36})' place (\d+)")
_CLIENT_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{3,6})Z")
# The client's session state machine, logged as it changes:
#     Session history: IASP    Initialized, App, Sent-play, Play   -> in a game
#     Session history: IASPSA  ... back to App                      -> LEFT the game
#     Session history: IASPSAT ... Tearing down                     -> shutting down
# This is what makes "still in the game" answerable locally. The
# "! Joining game" line alone cannot: it PERSISTS in the log after the client
# leaves, so "this log mentions a join" read as "joined" forever and an alt
# that got kicked never went back to disconnected.
_CLIENT_SESSION_RE = re.compile(r"Session history:\s*([A-Z]{1,24})")
SESSION_IN_GAME = "P"          # last state: play session
SESSION_LEFT_GAME = "A"        # last state: back at the app/home screen
SESSION_TEARDOWN = "T"         # last state: shutting down
# A log is only ever read from its end: these files run to megabytes and we
# want the LAST join, not the first, and we do not want to read a whole file on
# every one-second health tick.
_CLIENT_TAIL_BYTES = 256 * 1024


def client_log_dirs():
    """Every directory Roblox writes client logs to, most likely first."""
    dirs = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Roblox", "logs"))
    roaming = os.environ.get("APPDATA")
    if roaming:
        dirs.append(os.path.join(roaming, "Roblox", "logs"))
    tmp = os.environ.get("TEMP")
    if tmp:
        dirs.append(os.path.join(tmp, "Roblox", "logs"))
    return [d for d in dirs if os.path.isdir(d)]


def _client_log_start_epoch(path):
    """Epoch seconds of a client log's first timestamped line, or None.

    The first timestamped line is written as the client boots, so it is the
    process's own start time. That is how a log is matched to a process, since
    Roblox logs contain neither the pid nor the account name.
    """
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(4096)
    except OSError:
        return None
    for raw in chunk.split(b"\n"):
        m = _CLIENT_TS_RE.match(raw.decode("utf-8", "ignore").strip())
        if not m:
            continue
        try:
            dt = datetime.datetime.strptime(
                m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc)
            return dt.timestamp() + int(m.group(2).ljust(6, "0")) / 1e6
        except ValueError:
            return None
    return None


def read_client_state(path):
    """Everything the log says about this client, in ONE backward scan.

    Returns (job_id, place_id, session_history) where any element may be None:

      * `job_id`/`place_id` - the server it most recently joined. NOTE this line
        PERSISTS after the client leaves the game, so it answers "which server
        was this?" and not "am I still in it?".
      * `session_history` - the last state of the client's session machine
        (e.g. "IASP"). Its FINAL letter is the current state:
            P  in a play session, i.e. in a game right now
            A  back at the app / home screen  -> it LEFT the game
            T  tearing down                   -> the client is shutting down
        A history that never reached P means it has not been in a game yet.

    Scanning backwards and keeping the first match of each kind is what makes
    this cheap: a fresh client is read in one small read, and an 8 MB log still
    resolves in milliseconds instead of being read whole every health tick.
    """
    job = place = session = None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            pos = size
            carry = b""
            while pos > 0:
                step = min(_CLIENT_TAIL_BYTES, pos)
                pos -= step
                fh.seek(pos)
                chunk = fh.read(step) + carry
                lines = chunk.split(b"\n")
                carry = lines[0] if pos > 0 else b""
                for raw in reversed(lines[1:] if pos > 0 else lines):
                    text = raw.decode("utf-8", "ignore")
                    if session is None:
                        ms = _CLIENT_SESSION_RE.search(text)
                        if ms:
                            session = ms.group(1)
                    if job is None:
                        mj = _CLIENT_JOIN_RE.search(text)
                        if mj:
                            job, place = mj.group(1), mj.group(2)
                    if job is not None and session is not None:
                        return job, place, session
    except OSError:
        return None, None, None
    return job, place, session


def read_client_join(path):
    """The server this client most recently joined -> (job_id, place_id) or None."""
    job, place, _session = read_client_state(path)
    return (job, place) if job else None


def find_client_log(started_at, tolerance=180.0, skip=(), not_before=None):
    """The client log whose boot time is nearest `started_at` -> path or None.

    Roblox logs carry no pid and no account name, so a process is matched to
    its log by start time. `skip` holds paths already claimed by another
    process, which keeps two clients started within the same second apart.

    `not_before` is the stronger, necessary guard. Nearest-to is not enough on
    its own: a freshly launched client may not have written its log yet, so the
    nearest log is then the PREVIOUS client's - and reading it would report a
    server that has nothing to do with the running one, which looks exactly
    like "this alt joined the wrong server" and gets a healthy client closed.
    A client's log must therefore have STARTED at or after the moment we
    launched it; if none does yet, the answer is None and the caller waits.
    """
    floor = (not_before - 5.0) if not_before else None    # 5s: clock granularity
    best, best_delta = None, None
    for folder in client_log_dirs():
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not name.lower().endswith(".log"):
                continue
            path = os.path.join(folder, name)
            if path in skip:
                continue
            started = _client_log_start_epoch(path)
            if started is None:
                continue
            if floor is not None and started < floor:
                continue                     # belongs to an earlier client
            delta = abs(started - started_at)
            if delta <= tolerance and (best_delta is None or delta < best_delta):
                best, best_delta = path, delta
    return best


# ---------------------------------------------------------------------------
# Launch logic
# ---------------------------------------------------------------------------

def server_page_url(place_id, job_id):
    """Roblox's own link to one specific server of a place.

    This is the URL behind the in-game "Copy Server Link" / share button, and
    what you get when you "join a friend": /games/<placeId>/<jobId>. Joining
    through it is the friend-join path rather than a plain "give me any server"
    request, which is the whole point - see build_join_url.
    """
    return f"https://www.roblox.com/games/{place_id}/{job_id}"


def build_join_url(place_id, tracker, job_id=None, method="api"):
    """The URL the client is told to use to pick a server.

    Two ways, both real Roblox mechanics:

    "api" (default) - the launcher's own endpoint:
        https://assetgame.roblox.com/game/PlaceLauncher.ashx
            ?request=RequestGameJob&placeId=..&gameId=<jobId>
      It asks for one specific job, but Roblox is free to answer with a
      DIFFERENT server when that one is full. That is why the health monitor
      also has to check which server each alt actually landed in
      (serverQueueRetrySec).

    "pageurl" - hand the client the server's own page URL instead:
        https://www.roblox.com/games/<placeId>/<jobId>
      This is the friend-join route ("join this friend in this server"), which
      is a stronger request than asking for any server. The client is already
      launched with the alt's own auth ticket, so it can resolve and join that
      server itself - no browser and no per-account browser profile involved.

    Falls back to the "api" form when there is no job to target (we only know
    which server the MAIN is in once the main is in a game).
    """
    if not job_id:
        params = [
            ("request", "RequestGame"),
            ("browserTrackerId", tracker),
        ]
        if place_id:
            params.append(("placeId", place_id))
        params.append(("isPlayTogetherGame", "false"))
        query = urllib.parse.urlencode(params)
        return f"https://assetgame.roblox.com/game/PlaceLauncher.ashx?{query}"
    if (method or "api").strip().lower() == "pageurl":
        return server_page_url(place_id, job_id)
    params = [
        ("request", "RequestGameJob"),
        ("browserTrackerId", tracker),
    ]
    if place_id:
        params.append(("placeId", place_id))
    params.append(("isPlayTogetherGame", "false"))
    params.append(("gameId", job_id))
    query = urllib.parse.urlencode(params)
    return f"https://assetgame.roblox.com/game/PlaceLauncher.ashx?{query}"


def build_protocol_url(place_id, ticket, tracker, job_id=None, method="api"):
    launchtime = int(time.time() * 1000)
    join_url = build_join_url(place_id, tracker, job_id, method)
    encoded = urllib.parse.quote(join_url, safe="")
    return (
        f"roblox-player:1+launchmode:play+gameinfo:{ticket}"
        f"+launchtime:{launchtime}"
        f"+browsertrackerid:{tracker}"
        f"+placelauncherurl:{encoded}"
        f"+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp"
    )


def build_app_url(ticket, tracker):
    """roblox-player URL that opens the client to the Roblox home screen
    (used for the main account in follow mode when no placeId is set —
    the user joins any game manually and alts follow)."""
    launchtime = int(time.time() * 1000)
    return (
        f"roblox-player:1+launchmode:app+gameinfo:{ticket}"
        f"+launchtime:{launchtime}"
        f"+browsertrackerid:{tracker}"
        f"+robloxLocale:en_us+gameLocale:en_us+channel:+LaunchExp:InApp"
    )


def launch_protocol(url):
    """Launch via the roblox-player: protocol handler (ShellExecute).

    Raises RuntimeError with a clear message if the protocol handler isn't
    registered (i.e. Roblox isn't installed on this machine).
    """
    try:
        os.startfile(url)  # ShellExecute 'open'
    except OSError as exc:
        raise RuntimeError(
            "Roblox is not installed or the roblox-player: protocol handler "
            f"is not registered ({exc}). Install Roblox from "
            "https://www.roblox.com/download, or use the bootstrapper to "
            "download its own client.") from exc


def account_has_settings(account) -> bool:
    """True when the account has a customized FPS or graphics level set."""
    return ("fps" in account and int(account.get("fps", 60)) != 60) or \
        ("quality_level" in account and account.get("quality_level") != "lowest")


def write_alt_settings(account, alt_name) -> Optional[str]:
    """Write a per-alt GlobalBasicSettings XML from the account's fps/quality.

    Returns the settings file path, or None when no customization is configured.
    Uses bootstrapper.write_settings_xml so the XML matches what the bootstrapper
    produces (valid GlobalBasicSettings_13.xml schema).
    """
    fps = int(account.get("fps", 60) or 60)
    quality = account.get("quality_level", "lowest") or "lowest"
    if fps == 60 and quality == "lowest":
        return None
    tag = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(alt_name))
    settings_dir = ALT_SETTINGS_DIR / tag
    try:
        path = bootstrapper.write_settings_xml(settings_dir,
                                               framerate_cap=fps,
                                               graphics_level=quality)
        return str(path)
    except Exception as exc:
        print(f"[{alt_name}] could not write settings XML: {exc}")
        return None


def launch_direct(place_id, ticket, tracker, job_id=None, method="api",
                  settings_path=None):
    """Direct RobloxPlayerBeta.exe launch.

    When `settings_path` is given (a per-client GlobalBasicSettings XML with a
    custom FramerateCap / SavedQualityLevel), it is passed with `-g` so each
    alt can run at its own FPS cap and graphics level.

    If Roblox is not installed locally, falls back to the bootstrapper which
    downloads its own client from setup.rbxcdn.com.
    """
    exe = find_player_exe()
    if not exe:
        try:
            exe_dir = bootstrapper.ensure_roblox()
            exe = exe_dir / "RobloxPlayerBeta.exe"
        except Exception as exc:
            raise RuntimeError(
                "Could not locate RobloxPlayerBeta.exe on this machine and the "
                f"bootstrapper download also failed: {exc}. Install Roblox from "
                "https://www.roblox.com/download.") from exc
    join = build_join_url(place_id, tracker, job_id, method)
    cmd = [str(exe), "--app", "-t", ticket, "-j", join]
    if settings_path:
        cmd += ["-g", str(settings_path)]
    return subprocess.Popen(cmd, cwd=str(exe.parent))


# ---------------------------------------------------------------------------
# Headless-CLI backend (headless/roblox_headless: join via the official client)
# ---------------------------------------------------------------------------
# The headless CLI launches the SAME official exe with the SAME arguments as
# launch_direct above (`--app -t <ticket> -j <joinScriptUrl>`), so joining
# behaviour is identical — same server pin, same client. What it adds is a
# stable, independently testable boundary (JSON in/out, dry-run mode, pid
# tracking) instead of an inline Popen. Everything downstream (hidden windows,
# EcoQoS, health monitor, overlay, client-log reading) is untouched: this only
# replaces HOW the process is started.
def headless_cli_dir():
    """Directory containing the roblox_headless package, or None."""
    for base in (Path(__file__).parent / "headless",
                 Path.cwd() / "headless"):
        try:
            if (base / "roblox_headless" / "cli.py").is_file():
                return base
        except OSError:
            continue
    return None


def use_cli_backend(role, app_mode, account, default="protocol"):
    """Should this launch go through the headless CLI? Pure decision helper.

    Rules, in order:
      * the main opening the home screen (app_mode) always uses the protocol
        handler — the CLI join needs a place to join, and there isn't one.
      * a per-account "backend" key overrides the global default.
      * anything else follows the global `altBackend` default.
    """
    if app_mode:
        return False
    want = (account.get("backend") or default or "protocol").strip().lower()
    return want == "cli"


def launch_via_cli(place_id, ticket, tracker, job_id=None, method="api",
                   python_exe=None, exe_path="", timeout=60, hide=False,
                   _run=None):
    """Start one client through `python -m roblox_headless join`.

    The joinScriptUrl is built HERE with build_join_url, so the server pin
    (RequestGameJob for the main's exact job) is byte-identical to the
    protocol/direct paths — the CLI is only ever handed an explicit URL, never
    asked to build one (its own builder uses RequestGame, which does not pin).

    Returns (pid_or_None, entry_dict). pid is None when the CLI fell back to a
    dry-run (e.g. exe not found) — the caller then proceeds to its normal
    wait-for-process path, same as today.
    """
    cli_dir = headless_cli_dir()
    if cli_dir is None:
        raise RuntimeError(
            "altBackend=cli needs the headless/ folder next to the launcher "
            "(roblox_headless/cli.py not found).")
    join_url = build_join_url(place_id, tracker, job_id, method)
    cmd = [python_exe or sys.executable, "-m", "roblox_headless", "join",
           "--place-id", str(place_id),
           "--auth-ticket", ticket,
           "--join-script-url", join_url]
    if exe_path:
        cmd += ["--exe-path", exe_path]
    if hide:
        # Minimize + hide the client window once it opens, so it stops
        # rendering and disappears. The launcher's watcher keeps it that way
        # afterwards regardless of backend.
        cmd += ["--hide"]
    run = _run or subprocess.run
    try:
        proc = run(cmd, cwd=str(cli_dir), capture_output=True, text=True,
                   timeout=timeout, creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception as exc:
        raise RuntimeError(f"headless CLI did not run: {exc}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"headless CLI join failed (exit {proc.returncode}): {err}")
    try:
        entry = json.loads((proc.stdout or "").strip())
    except Exception:
        raise RuntimeError(
            f"headless CLI printed no JSON (stdout={proc.stdout[:200]!r}).")
    return entry.get("pid"), entry


def wait_for_roblox_clients(existing_pids, process_timeout=90, window_timeout=60,
                            visible=True):
    """Collect every RobloxPlayerBeta pid spawned after `existing_pids` was
    snapshotted, and find a window owned by any of them.

    Roblox launches a short-lived bootstrap RobloxPlayerBeta.exe, then the REAL
    client process (usually a *different* PID) which owns the window. Latching
    onto only the first new pid is fragile: when the bootstrap exits, the main
    looks like it "closed its client" even though the real client is open. So we
    gather ALL new pids and accept the first window owned by any of them.

    Returns (pid_set, hwnd_or_None, window_pid_or_None).
    """
    new_pids = set()
    start = time.time()
    process_deadline = start + process_timeout
    window_deadline = start + process_timeout + window_timeout
    while True:
        now = time.time()
        for pid in roblox_player_pids():
            if pid not in existing_pids:
                new_pids.add(pid)
        hwnd, wpid = find_window_for_pids(new_pids, visible=visible)
        if hwnd:
            return new_pids, hwnd, wpid
        if now > window_deadline:
            return new_pids, None, None
        if not new_pids and now > process_deadline:
            return set(), None, None
        time.sleep(0.5)


def find_window_for_pids(pids, visible=None):
    """First window owned by any pid in `pids`. `visible` filters on visibility."""
    for pid in pids:
        for hwnd in windows_for_pid(pid):
            if visible is None or window_visible(hwnd) == visible:
                return hwnd, pid
    return None, None


def visible_windows_for_pids(pids):
    """Every visible top-level window owned by any pid in `pids`.

    Deliberately *visible only*: a client process also owns helper windows
    (IME, TtkMonitor, ...), and treating one of those as "the" client window
    breaks jumping and status reporting.
    """
    wins = []
    for pid in pids:
        for hwnd in windows_for_pid(pid):
            if window_visible(hwnd):
                wins.append(hwnd)
    return wins


# A Roblox client process owns several top-level windows: the real client
# window plus hidden helpers ("Default IME", "MSCTFIME UI", unnamed ones).
# EnumWindows returns them in z-order, so for a HIDDEN alt - whose real window
# is hidden - a helper can come first. Posting to a helper silently does
# nothing: PostMessage succeeds, so no error is ever reported, the character
# just never jumps. So pick the client window by identity, not by position.
_HELPER_TITLES = ("default ime", "msctftime ui")


def is_helper_window(hwnd):
    """True for the IME/Ttk helper windows a Roblox process also owns."""
    title = (window_title(hwnd) or "").strip().lower()
    return (not title) or title in _HELPER_TITLES


def client_window_for_pids(pids, prefer=None):
    """The real client window for these pids, or None.

    `prefer` is the handle we already trust (recorded at launch, when the
    window was still visible and therefore identifiable). Keeping it while it
    is still valid and still owned by these pids is what stops the handle from
    flip-flopping between helpers on every tick. Only when it has genuinely
    died - the client recreated its window - do we go looking, and then we
    insist on a real, titled client window.
    """
    if prefer:
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(wt.HWND(prefer), ctypes.byref(pid))
        if pid.value in pids and not is_helper_window(prefer):
            return prefer
    for pid in pids:
        for hwnd in windows_for_pid(pid):
            title = (window_title(hwnd) or "").strip().lower()
            if "roblox" in title:
                return hwnd
    # last resort: any non-helper window (an untitled custom-title client)
    for pid in pids:
        for hwnd in windows_for_pid(pid):
            if not is_helper_window(hwnd):
                return hwnd
    return None


# ---------------------------------------------------------------------------
# Status overlay: a click-through, always-on-top readout of every account
# ---------------------------------------------------------------------------

_gdi32 = ctypes.windll.gdi32
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, wt.BYTE, wt.DWORD]
user32.SetLayeredWindowAttributes.restype = wt.BOOL
user32.RegisterClassW.argtypes = [ctypes.c_void_p]
user32.RegisterClassW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE,
                                   ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.BeginPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.BeginPaint.restype = wt.HDC
user32.EndPaint.argtypes = [wt.HWND, ctypes.c_void_p]
user32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = wt.LPARAM
_gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
_gdi32.CreateSolidBrush.restype = wt.HBRUSH
# all 14 parameters, in order - a truncated argtypes makes ctypes reject the
# call outright ("this function takes 9 arguments (14 given)")
_gdi32.CreateFontW.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, wt.DWORD, wt.DWORD,
                              wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD, wt.DWORD,
                              wt.DWORD, wt.LPCWSTR]
_gdi32.CreateFontW.restype = wt.HGDIOBJ
_gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
_gdi32.SelectObject.restype = wt.HGDIOBJ
_gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wt.HDC]
_gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]
_gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
_gdi32.Rectangle.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int]
# FillRect / DrawTextW live in user32, not gdi32
user32.FillRect.argtypes = [wt.HDC, ctypes.c_void_p, wt.HBRUSH]
user32.FillRect.restype = ctypes.c_int
_gdi32.PatBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, wt.HDC, ctypes.c_int, ctypes.c_int,
                          wt.DWORD]

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


# DrawTextW(HDC, LPCWSTR, int, LPRECT, UINT) - all five, and the rect MUST be
# passed by reference. Declaring this as c_void_p and handing it a bare struct
# raised a TypeError, the window procedure swallowed it, and the panel painted
# with no text on it.
user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int,
                             ctypes.POINTER(_RECT), ctypes.c_uint]
user32.DrawTextW.restype = ctypes.c_int


# status -> (label, dot colour)
_OVERLAY_STATES = {
    "joined":     ("joined",       0x39D98A),
    "joining":    ("joining",      0xF2C94C),
    "queued":     ("queued",       0xEB5757),
    "paused":     ("paused",       0x9B51E0),
    "disconnect": ("disconnected", 0xEB5757),
    "closed":     ("closed",       0x6B7280),
    # presence gave us nothing usable (rate-limited, or a phantom "InGame"
    # reading with no server id). This is deliberately NOT "joining": the
    # difference is whether we asked and got an answer.
    "noreport":   ("no signal",    0x8B93A8),
    # the main account is user-driven, so it has its own states
    "ingame":     ("in game",      0x39D98A),
    "idle":       ("idle",         0xF2C94C),
}


class StatusOverlay:
    """A small always-on-top, click-through panel showing every account.

    Click-through comes from WS_EX_TRANSPARENT plus answering WM_NCHITTEST with
    HTTRANSPARENT, so it never swallows a click meant for the game.
    WS_EX_NOACTIVATE means it can never become the foreground window - which
    matters because the anti-AFK jump decides where to send focus.

    It draws itself with plain GDI and is composited by Windows at a fixed
    alpha (SetLayeredWindowAttributes), so the text stays properly
    anti-aliased and no per-pixel UpdateLayeredWindow work is needed.
    """

    def __init__(self, provider, alpha=210, margin=14, refresh_ms=400,
                 afk_provider=None, on_error=None):
        self.provider = provider          # callable -> list of row dicts
        # callable -> {"text", "colour"} for the anti-AFK line under the states
        self.afk_provider = afk_provider
        self.alpha = max(20, min(255, int(alpha)))
        self.margin = margin
        self.refresh_ms = refresh_ms
        self.on_error = on_error or (lambda m: None)
        self.hwnd = None
        self._proc = None
        self._thread = None
        self._fonts = []
        self._class_ok = False
        self._paint_error_logged = False
        self._last_signature = None
        self.force_paint = True       # first paint always happens
        self._stopped = False
        self._watcher = None
        self._cur_size = None

    # -- window procedure -------------------------------------------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT          # never take the mouse
            if msg == WM_ERASEBKGND:
                # Claim the background as handled. With a class background
                # brush, every WM_PAINT first erases the whole window and then we
                # draw over it - and a layered window re-composites on each pass,
                # so that erase shows up as flicker. We fill the whole panel
                # ourselves anyway, so there is nothing to erase.
                return 1
            if msg == WM_TIMER:
                # Only ask for a repaint when the content actually changed.
                # Repainting identical pixels still costs a composite on a
                # layered window, which is precisely the flicker we are avoiding.
                sig = self._signature()
                if sig != self._last_signature:
                    self._last_signature = sig
                    self.force_paint = True
                user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_PAINT:
                if not self.force_paint and self._last_signature:
                    # nothing changed since the last paint: validate the region
                    # (BeginPaint/EndPaint is what tells Windows we're done with
                    # it) without redrawing anything
                    dc = user32.BeginPaint(hwnd, None)
                    if dc:
                        user32.EndPaint(hwnd, None)
                    return 0
                self.force_paint = False
                self._paint(hwnd)
                return 0
            if msg == WM_DESTROY:
                return 0
        except Exception as exc:
            # Swallowing this silently is how an empty panel shipped once: the
            # paint aborted after the background fill and nothing said so. Report
            # it once, then stay quiet so a repaint loop cannot flood the log.
            if not self._paint_error_logged:
                self._paint_error_logged = True
                self.on_error(f"status overlay paint failed: {exc!r}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _signature(self):
        """A cheap fingerprint of the panel's content.

        The anti-AFK countdown is part of it, and deliberately so: the text
        changes every second, so that is what drives its repaints. Without it in
        the fingerprint the countdown would freeze on whatever second it was
        drawn at, which is worse than no timer - it would look authoritative
        while being wrong. One repaint a second is not the repaint storm the
        BeginPaint/EndPaint work was about; that was Windows re-sending WM_PAINT
        continuously, not a deliberate 1 Hz update.
        """
        try:
            rows = self.provider() or []
        except Exception:
            rows = []
        extra = self._afk_line() or {}
        return (tuple((r.get("name"), r.get("state"), r.get("job"))
                      for r in rows), extra.get("text"))

    def _paint(self, hwnd):
        """Draw one frame.

        Two DCs, for two different jobs, and both are needed:

        - BeginPaint/EndPaint VALIDATES the window's update region. Without it
          Windows re-sends WM_PAINT immediately and forever - a repaint storm
          that looks exactly like flicker. Its DC, however, does not render on
          this layered window.
        - GetDC is what actually paints. The original implementation used it and
          the panel appeared; switching the drawing to BeginPaint made the window
          render nothing at all while still reporting successful paints.

        So: validate with BeginPaint, draw with GetDC.
        """
        rows = []
        try:
            rows = self.provider() or []
        except Exception:
            rows = []
        extra = self._afk_line() or {}
        # validate the update region first, whatever we end up drawing
        vdc = user32.BeginPaint(hwnd, None)
        if vdc:
            user32.EndPaint(hwnd, None)
        if not rows and not extra.get("text"):
            return

        # ANTIALIASED_QUALITY, NOT CLEARTYPE: ClearType sub-pixel rendering is
        # dropped on a layered (WS_EX_LAYERED) surface, which is exactly what
        # this overlay is - on ClearType the text came out invisible.
        font = _gdi32.CreateFontW(-13, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 4, 0, "Segoe UI")
        small = _gdi32.CreateFontW(-11, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 4, 0, "Consolas")
        hdc = user32.GetDC(hwnd)          # this is the DC that actually paints
        if not hdc:
            return
        try:
            gdi32_old_font = _gdi32.SelectObject(hdc, font)
            _gdi32.SetBkMode(hdc, TRANSPARENT_BK)

            # measure, but never wider than the window actually is: drawing
            # outside it is silently clipped, which is how the job id went missing
            r = _RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            win_w = max(1, r.right - r.left)
            win_h = max(1, r.bottom - r.top)
            widest = 0
            for r0 in rows:
                head = f"{r0['name']}  ·  {r0['label']}"
                box = _RECT(0, 0, 0, 0)
                _gdi32.SelectObject(hdc, font)
                user32.DrawTextW(hdc, head, -1, ctypes.byref(box), DT_CALCRECT)
                widest = max(widest, (box.right - box.left) + 60)
                # the job id line too - it is the longest thing on the row and
                # was being clipped off the right of the panel
                _gdi32.SelectObject(hdc, small)
                user32.DrawTextW(hdc, r0.get("job") or "-", -1,
                                 ctypes.byref(box), DT_CALCRECT)
                widest = max(widest, (box.right - box.left) + 44)
            _gdi32.SelectObject(hdc, font)
            width = max(190, min(win_w, widest))
            row_h = 34
            foot = 18 if extra.get("text") else 0
            height = 10 + row_h * len(rows) + 8 + foot

            panel = _gdi32.CreateSolidBrush(0x00000000 | 0x101010)
            border = _gdi32.CreateSolidBrush(0x00000000 | 0x3A3A3A)
            # Fill the WHOLE window, not just the content width. On a layered
            # window any pixel we never paint keeps whatever was in the surface
            # and gets composited at the constant alpha - which showed up as a
            # black box beside the panel.
            user32.FillRect(hdc, ctypes.byref(_RECT(0, 0, win_w, win_h)), panel)
            _gdi32.SelectObject(hdc, border)
            _gdi32.Rectangle(hdc, 0, 0, win_w - 1, win_h - 1)

            y = 8
            for r in rows:
                label, colour = _OVERLAY_STATES.get(
                    r.get("state", "closed"), _OVERLAY_STATES["closed"])
                dot = _gdi32.CreateSolidBrush(0x00000000 | colour)
                user32.FillRect(hdc, ctypes.byref(_RECT(10, y + 5, 16, y + 11)), dot)
                _gdi32.DeleteObject(dot)

                _gdi32.SetTextColor(hdc, 0x00000000 | 0xF2F2F2)
                _gdi32.SelectObject(hdc, font)
                head = f"{r['name']}  ·  {label}"
                box = _RECT(24, y, width - 8, y + 15)
                user32.DrawTextW(hdc, head, -1, ctypes.byref(box), 0x0000)

                job = r.get("job") or "-"
                if r.get("mismatch"):
                    # not the main's server - show it in a warning colour
                    _gdi32.SetTextColor(hdc, 0x00000000 | 0xFF6B6B)
                else:
                    _gdi32.SetTextColor(hdc, 0x00000000 | 0x9AA0A6)
                _gdi32.SelectObject(hdc, small)
                box = _RECT(24, y + 15, width - 8, y + 28)
                user32.DrawTextW(hdc, job, -1, ctypes.byref(box), 0x0000)
                y += row_h

            if extra.get("text"):
                # The anti-AFK countdown, directly under the state list.
                #
                # It is ONE line for every alt on purpose, not one per row: a
                # jump pass visits all the alts within the same second (they are
                # staggered by a fraction of a second), so repeating the same
                # countdown on seven rows would be noise pretending to be data.
                # What it says is the truth about the whole set - including the
                # cases where no jump is coming at all, which a bare countdown
                # would hide.
                _gdi32.SetTextColor(hdc, 0x00000000 | extra.get("colour", 0x9AA0A6))
                _gdi32.SelectObject(hdc, small)
                box = _RECT(10, y + 2, width - 8, y + 16)
                user32.DrawTextW(hdc, extra["text"], -1, ctypes.byref(box), 0x0000)
            _gdi32.DeleteObject(panel)
            _gdi32.DeleteObject(border)
            _gdi32.SelectObject(hdc, gdi32_old_font)
        finally:
            user32.ReleaseDC(hwnd, hdc)
            for f in (font, small):
                if f:
                    _gdi32.DeleteObject(f)

    # -- lifecycle -------------------------------------------------------
    def start(self):
        """Create the overlay window on its own thread. Never raises."""
        def run():
            try:
                self._run()
            except Exception:
                pass
        self._thread = threading.Thread(target=run, daemon=True,
                                        name="status-overlay")
        self._thread.start()
        self._watcher = threading.Thread(target=self._watch, daemon=True,
                                         name="status-overlay-watch")
        self._watcher.start()
        return self

    def _watch(self):
        """Poll the data source and ask for a repaint only when it changed.

        A plain thread rather than a window timer: a WM_TIMER-based tick proved
        unreliable here (it simply never arrived), and silently not repainting
        would leave a stale panel - the one thing a status readout must not do.
        """
        while self.hwnd is None:
            if self._stopped:
                return
            time.sleep(0.05)
        while not self._stopped:
            try:
                sig = self._signature()
                if sig != self._last_signature:
                    self._last_signature = sig
                    self.force_paint = True
                    rows = self.provider() or []
                    self._maybe_resize(rows)
                    if self.hwnd:
                        user32.InvalidateRect(self.hwnd, None, False)
            except Exception:
                pass
            time.sleep(self.refresh_ms / 1000.0)

    def _run(self):
        self._proc = WNDPROC(self._wndproc)     # must outlive the class
        inst = kernel32.GetModuleHandleW(None)
        wc = _WNDCLASS(0, self._proc, 0, 0, inst, None, None,
                       None,                       # NO background brush
                       None, "RobloxStatusOverlay")
        user32.RegisterClassW(ctypes.byref(wc))
        self._class_ok = True
        hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TRANSPARENT |
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
            "RobloxStatusOverlay", "Roblox Status", WS_POPUP,
            0, 0, 240, 60, None, None, inst, None)
        if not hwnd:
            return
        self.hwnd = hwnd
        user32.SetLayeredWindowAttributes(hwnd, 0, self.alpha, LWA_ALPHA)
        self._reposition()          # initial size from the current row set
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _reposition(self):
        if not self.hwnd:
            return
        SW, SH = user32.GetSystemMetrics(0) or 1920, user32.GetSystemMetrics(1) or 1080
        width, height = self._desired_size(self._safe_rows())
        self._cur_size = (width, height)
        user32.SetWindowPos(self.hwnd, wt.HWND(HWND_TOPMOST),
                            max(0, SW - width - self.margin), self.margin,
                            width, height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def _afk_line(self):
        """The anti-AFK status line, or None if there is nothing to say."""
        try:
            getter = getattr(self, "afk_provider", None)
            return getter() if getter else None
        except Exception:
            return None

    def _desired_size(self, rows):
        """Panel size for a given row set - the same geometry _paint lays out
        with.

        Both lines have to be measured. Sizing on the name/status line alone made
        the panel too narrow for a 36-character job id, which then got clipped to
        a similar-looking stub on every row - so it looked like every account was
        showing the main's server.
        """
        extra = self._afk_line()
        foot = 0 if (not extra or not extra.get("text")) else 18
        if not rows:
            return 240, 30 + foot             # nothing to show: a small empty panel
        widest = 0
        for r in rows:
            head = f"{r.get('name', '?')}  ·  {r.get('label', '')}"
            job = r.get("job") or "-"
            widest = max(widest, len(head) * 7 + 44, len(job) * 6.6 + 44)
        if extra and extra.get("text"):
            widest = max(widest, len(extra["text"]) * 6.2 + 28)
        return max(210, min(520, int(widest))), 10 + 34 * len(rows) + 8 + foot

    def _maybe_resize(self, rows):
        """Resize to fit the current rows, but only when it actually changed.

        Without this the panel kept the size it was created with, so switching
        accounts off left a tall empty box behind.
        """
        if not self.hwnd:
            return
        want = self._desired_size(rows)
        cur = getattr(self, "_cur_size", None)
        if cur and abs(cur[0] - want[0]) < 12 and cur[1] == want[1]:
            return
        self._reposition()

    def _safe_rows(self):
        try:
            return self.provider() or []
        except Exception:
            return []

    def stop(self):
        self._stopped = True
        try:
            if self.hwnd:
                user32.DestroyWindow(self.hwnd)
        except Exception:
            pass
        self.hwnd = None


# ---------------------------------------------------------------------------
# RobloxManager: guard, launching, hiding, watching
# ---------------------------------------------------------------------------

class PresenceThrottled(RuntimeError):
    """The presence API rate-limited us (HTTP 429) — retry later, decide nothing."""


class RobloxManager:
    """Owns the multi-instance guard and all launched instances."""

    def __init__(self, accounts, place_id, headless_override=None,
                 guard=True, restore_on_exit=True, close_existing=False,
                 launch_mode="follow", join_timeout=900, poll_interval=1.0,
                 jump_interval=None, jump_method="focus",
                 jump_settle=0.35, jump_action="jump",
                 jump_skip_when_main_focused=True, jump_max_gap=180.0,
                 alt_throttle=True, no_render=True,
                 server_queue_retry=15.0, alt_presence_interval=6.0,
                 alt_presence_idle_interval=25.0,
                 join_method="api",
                 alt_backend="protocol", cli_python="", cli_exe_path="",
                 drop_confirm_count=2, drop_grace_sec=30.0,
                 join_confirm_sec=90.0,
                 rejoin_pause_drops=6, rejoin_pause_window=120.0,
                 rejoin_pause_sec=60.0, relaunch_kill_wait=2.0,
                 quick_fail_sec=90.0, quick_fail_backoff=30.0,
                 on_log=None, on_status=None, on_follow=None):
        self.accounts = accounts                     # list of dicts
        self.place_id = str(place_id)
        self.headless_override = headless_override   # None | bool
        self.guard_enabled = guard
        self.restore_on_exit = restore_on_exit
        self.close_existing = close_existing
        self.launch_mode = launch_mode               # "follow" | "simultaneous"
        self.join_timeout = int(join_timeout or 900)
        self.poll_interval = float(poll_interval or 1.0)   # presence/health poll seconds
        # seconds between anti-AFK jumps for each alt; None -> 10, 0 disables
        self.jump_interval = float(jump_interval if jump_interval is not None else 10.0)
        # "focus"  = transparent focus dance (verified against Roblox)
        # "message"= post WM_KEYDOWN straight to the hidden window (no focus,
        #           but MEASURED not to produce a jump: a posted message never
        #           becomes a keyboard event, so gameplay input never sees it)
        # "none"   = anti-AFK off entirely
        self.jump_method = (jump_method or "focus").strip().lower()
        # How long the alt is left focused and presented before the key goes
        # in. A client that has been minimized and hidden has to resume
        # rendering and its own loop first, and a key sent into that gap is
        # lost. Taken from JunkBeat/AntiAFK-Roblox's "Open for ... sec" and its
        # troubleshooting note about key presses failing to register.
        self.jump_settle = float(jump_settle if jump_settle is not None else 0.35)
        # "jump"  = one Space
        # "camera"= jump plus a camera nudge each way, which the same tool
        #           reports as more reliable because it makes more interactions
        #           with the window
        self.jump_action = (jump_action or "jump").strip().lower()
        # Focus is a single owner on Windows: whichever window gets the key
        # becomes foreground, so the MAIN blurs for the duration of the jump.
        # These keep that from happening while the user is actually playing.
        self.jump_skip_when_main_focused = bool(jump_skip_when_main_focused)
        # Safety net: never let alts go longer than this without a jump
        # (0 = never jump while the main is focused).
        self.jump_max_gap = float(jump_max_gap or 0.0)
        self._last_jump_at = time.time()
        self._jump_skip_logged = 0.0
        self._jump_warned = 0.0       # last time we warned about an upcoming focus steal
        self._jump_notices = {}      # alt -> last time we reported a jump problem
        # Mark alt clients as background work (EcoQoS / low priority) so their
        # hidden rendering costs less CPU/GPU.
        self.alt_throttle = bool(alt_throttle)
        self._throttled_pids = set()
        # pids seen recently, so the orphan sweep can't kill a client that has
        # just launched but is not registered yet
        self._recent_pids = {}
        self._swept = False            # orphan sweep runs once, on first use
        # Alts sit hidden AND minimized: minimizing is the only thing measured
        # to actually cut a client's work (20% -> 12% of a core on this
        # machine). A per-client FPS cap is NOT possible from outside the
        # process - see the notes in build_alt_settings_xml.
        self.no_render = bool(no_render)
        # When the main's server is full Roblox drops the alt into a different
        # server. With this > 0 the alt is closed and re-queued for the main's
        # exact server every N seconds (with backoff); 0 = accept any server.
        self.server_queue_retry = float(server_queue_retry or 0)
        self._wrong_server_tries = {}
        self.on_log = on_log or (lambda msg: None)
        self.on_status = on_status or (lambda name, state, detail: None)
        self.on_follow = on_follow or (lambda msg: None)

        self.running = {}                            # name -> {"pid", "hwnd", "headless"}
        self._lock = threading.Lock()
        self._launch_lock = threading.Lock()          # one client launch at a time
        self._stop = threading.Event()
        self._guard_mutex = None
        self._guard_file = None
        self._watcher = None
        self._follow_active = False              # True while follow_main_loop runs

        # Alt health-monitor state (checks alts every poll_interval for drops):
        self._joined = set()         # alt names currently joined (follow mode)
        self._reconnecting = set()   # alt names being relaunched right now
        self._join_since = {}        # alt name -> time it joined (presence grace)
        self._target = None          # (place, job) alts should be in, or None
        self._alt_uid = {}           # cached alt user IDs
        self._drop_history = {}      # alt name -> [drop timestamps]
        self._seen_in_game = {}      # alt name -> proven in-game by presence
        self._launch_since = {}      # alt name -> when its client was started
        # account name -> its Roblox client log file. The log is first-hand
        # ("! Joining game '<job>' place <id>"), so it - not presence - is the
        # authority on which server an account is actually in.
        self._client_logs = {}
        # accounts whose window is deliberately presented right now, so the
        # watcher leaves it alone for the duration of a jump.
        self._jump_busy = set()
        # alt name -> the server its own log says it joined, or None when the
        # log exists but records no join yet. Once an entry exists here the log
        # is the authority for that alt and presence is not allowed to
        # overwrite it: the log is written at the instant of the join, while
        # presence is cached, throttled and capable of reporting a previous
        # session entirely.
        self._log_state = {}
        # alt name -> "in" / "out" / "unknown": what the last usable presence
        # reading actually said. "unknown" is a real outcome, not an error - it
        # is what the overlay must show instead of pretending an alt is still
        # "joining" when the truth is that we were rate-limited and cannot tell.
        self._presence_verdict = {}
        self._pause_until = {}       # alt name -> time; auto-reconnect paused until
        self._last_alt_presence = {} # alt name -> last presence-check time
        self._next_presence = {}     # alt name -> next allowed check (staggered)
        self._bad_readings = {}      # alt name -> (reason, consecutive count)
        self._presence_backoff = 0.0 # don't call presence before this time
        self._presence_penalty = 0.0 # current backoff length
        # tripped when the main leaves/closes: pending alt launches must abort
        # instead of starting a client nobody will ever clean up
        self._alts_closed = threading.Event()
        self.alt_presence_interval = float(alt_presence_interval or 3.0)
        # used when nothing is happening: a few seconds of freshness is worth
        # nothing, and the API rate limit is a shared, finite resource
        self.alt_presence_idle_interval = float(alt_presence_idle_interval or 20.0)
        self._last_presence_at = 0.0     # when we last actually called the API
        self._presence_429_logged = 0.0
        # How an alt is put into the MAIN's exact server.
        #   "api" (default) - the launcher's own RequestGameJob endpoint. This is
        #                    the only route that actually works: handing the
        #                    client the server's HTML page URL
        #                    (/games/<placeId>/<jobId>) makes it fail to join
        #                    entirely, because that is a web page, not something
        #                    the client can use as a launch target.
        #   "pageurl"       - kept selectable for reference. Roblox's own server
        #                    page carries no launch URL at all (verified: 0
        #                    roblox-player:/assetgame URLs, the jobId is not even
        #                    in the served HTML - the join is client-side JS), so
        #                    it cannot be driven per-account either.
        # RequestGameJob may still be answered with a DIFFERENT server when the
        # target is full, so the health monitor checks which server each alt
        # really landed in and re-queues it (see _verify_join_outcome).
        self.join_method = (join_method or "api").strip().lower()
        # "protocol" = roblox-player: URL via the OS handler (default, current
        # behaviour). "cli" = start the client through headless/roblox_headless
        # (same official exe, same --app/-t/-j arguments, JSON result). Only
        # ever a different way to START the process; hiding, health, overlay
        # and log-reading are identical downstream.
        self.alt_backend = (alt_backend or "protocol").strip().lower()
        self.cli_python = (cli_python or "").strip()
        self.cli_exe_path = (cli_exe_path or "").strip()
        # --- how quickly a drop is noticed and acted on -----------------------
        # consecutive bad presence readings required before closing anything.
        # 2 protects against a single stale/blipped reading, which previously
        # caused whole cycles of close-and-relaunch for no reason.
        self.drop_confirm_count = max(1, int(drop_confirm_count or 2))
        # Post-join grace. Presence LAGS reality: right after a client enters a
        # server the API can still report "not in game" for tens of seconds.
        # Judging a drop on a timer alone meant a short grace let us close
        # perfectly healthy alts. The primary guard is now `_seen_in_game` (we
        # must have observed the alt in-game before absence means anything);
        # this is only a secondary floor.
        self.drop_grace_sec = float(drop_grace_sec if drop_grace_sec is not None else 30.0)
        # How long a launched alt gets to prove it is actually in the game before
        # a still-not-in-game reading is treated as a FAILED JOIN rather than
        # presence lag. Without it an alt refused by a full server is stranded:
        # never seen in-game, so absence never counts, so nothing ever retries it.
        self.join_confirm_sec = float(
            join_confirm_sec if join_confirm_sec is not None else 90.0)
        # per-join outcome tracking: did the requested server actually get
        # honoured, or did Roblox hand back a different one?
        self._join_verified = {}   # alt name -> bool (reported already)
        self._join_stats = {}      # "pinned" / "wrong" -> count
        self._alt_job = {}         # alt name -> the server it is actually in
        self._overlay = None       # StatusOverlay, when enabled
        # anti-thrash: too many drops inside the window -> pause that long.
        # Was 3 drops / 5 min / 5 min pause, which parked an alt for five
        # minutes during ordinary server churn.
        self.rejoin_pause_drops = max(2, int(rejoin_pause_drops or 6))
        self.rejoin_pause_window = float(rejoin_pause_window or 120.0)
        self.rejoin_pause_sec = float(rejoin_pause_sec or 60.0)
        # how long to wait for a dropped client's processes to actually die
        # before relaunching (the mutex must be released by the old process)
        self.relaunch_kill_wait = float(relaunch_kill_wait if relaunch_kill_wait is not None else 2.0)
        # "died within quick_fail_sec of joining" = the server/anti-abuse is
        # rejecting the alt, not a random drop. Retry with a growing backoff
        # instead of immediately (which just re-enters the kick loop).
        self.quick_fail_sec = float(quick_fail_sec if quick_fail_sec is not None else 90.0)
        self.quick_fail_backoff = float(quick_fail_backoff or 30.0)
        self._quick_fails = {}   # alt name -> consecutive quick fails
        self._health_thread = None   # the 1s monitor thread
        self._jump_thread = None     # the anti-AFK jump thread (jump_interval > 0)

    # -- status overlay -----------------------------------------------------
    def overlay_rows(self):
        """One row per enabled account for the on-screen readout.

        Deliberately cheap and defensive: it is called several times a second
        from the overlay's paint timer, so it must never raise and never block
        on anything (no API calls - the server id comes from what presence last
        told us, not from a fresh lookup).
        """
        now = time.time()
        rows = []
        try:
            with self._lock:
                running = dict(self.running)
                joined = set(self._joined)
                reconnecting = set(self._reconnecting)
                pause = dict(self._pause_until)
                wrong = dict(self._wrong_server_tries)
                jobs = dict(self._alt_job)
                seen = dict(self._seen_in_game)
                verdicts = dict(self._presence_verdict)
                target = self._target
        except Exception:
            return []
        for acc in self.accounts:
            if not acc.get("enabled", True):
                continue
            name = acc.get("name", "?")
            rec = running.get(name)
            is_main = acc.get("role") == "main"

            if is_main:
                # The main is never in `_joined` (that set only holds alts) and
                # is driven by presence polling, not by launches - so it needs
                # its own logic or it reads "joining" forever.
                if rec is None:
                    state = "closed"
                elif target and target[0]:
                    state = "ingame"          # main is actually in a game
                else:
                    state = "idle"            # client open, not in a game
            elif name in reconnecting:
                state = "joining"
            elif pause.get(name, 0) > now:
                state = "paused"
            elif wrong.get(name):
                state = "queued"
            elif rec is None:
                # No client at all. This has to be tested BEFORE the "joined"
                # branch: `_seen_in_game` is historical proof and outlives the
                # client, and closing an alt (with the main, or by hand) pops
                # its record but leaves that flag set. Checking "joined" first
                # therefore showed alts stuck on "joined" with nothing running,
                # which is exactly what was reported. A client that does not
                # exist cannot be in a game.
                state = "disconnect"
            elif seen.get(name) and verdicts.get(name) != "out":
                # Proof it is really in a game, AND it has not since been
                # observed out of one. `_seen_in_game` is the historical proof
                # and never goes stale on its own, so on its own it made
                # "joined" a one-way street: an alt that left the game kept
                # reading "joined" until something tore it down. The live
                # verdict is what says whether it is in a game RIGHT NOW, so
                # the state flips the moment the log says otherwise - while the
                # teardown itself stays debounced.
                state = "joined"
            elif verdicts.get(name) == "in":
                # Presence says in-game but the client log hasn't re-confirmed
                # yet (e.g. right after a reconnect, before _alt_log_check has
                # read the new log). Trust presence - it's first-hand from
                # Roblox's presence service.
                state = "joined"
            elif verdicts.get(name) == "out":
                state = "joining"        # presence says: not in a game yet
            elif verdicts.get(name) == "unknown":
                # We asked and got nothing usable - almost always HTTP 429.
                # Calling that "joining" is a guess, and the guess is what made
                # this panel useless: an alt frozen on "joining" for minutes was
                # really a launcher that had stopped being able to see.
                state = "noreport"
            else:
                state = "joining"        # not asked yet

            if is_main:
                job = target[1] if (target and target[0]) else acc.get("jobId")
            else:
                job = jobs.get(name) or acc.get("jobId") or (target[1] if target else None)
            # Is this the server the MAIN is in? Comparing the two job ids is
            # the whole point of showing them, so mark a mismatch loudly rather
            # than leaving six rows that look alike.
            main_job = target[1] if target else None
            mismatch = bool(not is_main and job and main_job
                            and str(job).lower() != str(main_job).lower())
            rows.append({"name": name, "state": state,
                         "label": _OVERLAY_STATES[state][0], "job": job,
                         "mismatch": mismatch})
        return rows

    def overlay_afk(self):
        """The anti-AFK status line for the overlay -> {"text", "colour"}.

        Asked "when will the next jump happen", but a bare countdown would be
        dishonest in three of its five states, so each is spelled out:

          * off            - jumpMethod "none" / jumpIntervalSec 0
          * idle           - the main is not in a game, so no alts are either
          * held off       - the jump is being skipped because the main is the
                             window being used. Note `_last_jump_at` is NOT
                             advanced while held off, so a naive countdown would
                             sit at 0:00 forever and look like it was about to
                             fire. This is the case that matters most to the user
                             and the one a timer would misreport.
          * next in M:SS   - a real countdown to the next pass

        When it is within a few seconds of firing it switches to amber, so the
        focus steal is visible before it happens rather than after.
        """
        now = time.time()
        method = (self.jump_method or "focus").strip().lower()
        if method == "none" or self.jump_interval <= 0:
            return {"text": "anti-AFK: off", "colour": 0x6B7280}
        if not (self._target and self._target[0]):
            return {"text": "anti-AFK: idle - main not in a game", "colour": 0x6B7280}
        if method == "focus" and self._main_is_foreground():
            since = now - self._last_jump_at
            if self.jump_skip_when_main_focused and (
                    self.jump_max_gap <= 0 or since < self.jump_max_gap):
                return {"text": "anti-AFK: held off - main is active",
                        "colour": 0xF2C94C}
        left = self._last_jump_at + self.jump_interval - now
        if left <= 0:
            return {"text": "anti-AFK: jumping now", "colour": 0xF2C94C}
        left = int(left)
        return {"text": f"anti-AFK: next in {left // 60}:{left % 60:02d}",
                "colour": 0xF2C94C if left <= 5 else 0x9AA0A6}

    def start_overlay(self, alpha=210):
        """Bring up the click-through readout. Safe to call twice."""
        if self._overlay is not None:
            return self._overlay
        try:
            self._overlay = StatusOverlay(self.overlay_rows, alpha=alpha,
                                          afk_provider=self.overlay_afk,
                                          on_error=self.log).start()
        except Exception as exc:
            self._overlay = None
            self.log(f"status overlay could not start: {exc}")
        return self._overlay

    def stop_overlay(self):
        ov, self._overlay = self._overlay, None
        if ov is not None:
            try:
                ov.stop()
            except Exception:
                pass

    # -- utilities ----------------------------------------------------------
    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        self.on_log(line)
        print(line)

    def _status(self, name, state, detail=""):
        self.on_status(name, state, detail)

    # -- guard ---------------------------------------------------------------
    def start_guard(self):
        if not self.guard_enabled:
            self.log("Multi-instance guard disabled.")
            return
        mutex, owned = acquire_mutex(MUTEX_NAME)
        if owned:
            self._guard_mutex = mutex
            self.log(f"Acquired '{MUTEX_NAME}' (multi-instance guard active).")
        else:
            self.log("Another program already owns the Roblox singleton mutex "
                     "(guard mediated elsewhere).")
        file_handle = lock_cookies_file()
        if file_handle:
            self._guard_file = file_handle
            self.log(f"Locking {COOKIES_FILE.name} (error-773 mitigation active).")
        else:
            self.log("Could not lock RobloxCookies.dat "
                     "(file missing or already locked by another program).")

    def stop_guard(self):
        if self._guard_file:
            kernel32.CloseHandle(self._guard_file)
            self._guard_file = None
        if self._guard_mutex:
            kernel32.ReleaseMutex(self._guard_mutex)
            kernel32.CloseHandle(self._guard_mutex)
            self._guard_mutex = None

    # -- launching -----------------------------------------------------------
    def is_headless(self, account):
        if self.headless_override is not None:
            return self.headless_override
        return bool(account.get("headless", account.get("role") != "main"))

    def verify_account(self, account):
        info = get_authenticated_user(account.get("cookie", ""))
        if info:
            account["username"], account["displayName"], account["userId"] = info
            return True
        return False

    def launch_account(self, account, wait_for_process=True, wait_for_window_s=60, follow=None):
        """Launch one account. `follow` = (place_id, job_id) to join main's server."""
        name = account.get("name", "?")
        cookie = account.get("cookie", "").strip()
        role = account.get("role", "alt")
        headless = self.is_headless(account)
        account_job = account.get("jobId") or None

        if follow:
            place_id = str(follow[0]) if follow[0] else (self.place_id or None)
            job_id = follow[1] or account_job
        else:
            place_id = self.place_id or None
            job_id = account_job

        # In follow mode the main doesn't need a placeId: open the client to
        # the home screen and let the user join anything — alts follow via
        # presence. Only the main (role "main") in follow mode may do this.
        app_mode = False
        if not place_id:
            if self.launch_mode == "follow" and role == "main":
                app_mode = True
            else:
                raise RuntimeError("No placeId configured.")

        self._status(name, "launching", "getting ticket...")

        if not cookie:
            raise RuntimeError(f"Account '{name}' has no cookie set.")

        # Serialize launches: a "new RobloxPlayerBeta pid" is only attributable
        # to this account if no other launch is in flight — otherwise two
        # relaunch threads can both adopt the same client process and then kill
        # each other's alt.
        with self._launch_lock:
            # The main may have closed while we waited for the lock; a queued alt
            # must not start a client that nothing would then close.
            if self._alts_closed.is_set() and role != "main":
                self.log(f"[{name}] main is gone — not launching this alt.")
                return None
            return self._launch_locked(name, account, cookie, place_id, job_id,
                                      role, headless, follow, app_mode,
                                      wait_for_process, wait_for_window_s)

    def _launch_locked(self, name, account, cookie, place_id, job_id, role,
                       headless, follow, app_mode, wait_for_process,
                       wait_for_window_s):
        existing = roblox_player_pids()
        ticket = get_auth_ticket(cookie)
        self._status(name, "launching", "got ticket, starting client...")

        tracker = str(random.randint(10**10, 10**11 - 1))
        direct = bool(account.get("directLaunch"))
        if app_mode:
            url = build_app_url(ticket, tracker)
            launch_protocol(url)
            self.log(f"[{name}] opened Roblox home screen (follow mode, no placeId).")
        elif account_has_settings(account):
            # Per-account FPS/graphics settings need -g, which requires a direct
            # exe launch (the roblox-player: URL scheme and the headless CLI
            # can't pass it).
            settings_path = write_alt_settings(account, name)
            proc = launch_direct(place_id, ticket, tracker, job_id,
                                 self.join_method,
                                 settings_path=settings_path)
            self.log(f"[{name}] launched RobloxPlayerBeta.exe directly "
                     f"(PID {proc.pid})" + (f" with -g {settings_path}" if settings_path else "."))
        elif use_cli_backend(role, app_mode, account, self.alt_backend):
            cli_pid, _entry = launch_via_cli(
                place_id, ticket, tracker, job_id, self.join_method,
                python_exe=self.cli_python or None,
                exe_path=self.cli_exe_path, hide=headless)
            self.log(f"[{name}] launched via headless CLI"
                     + (f" (PID {cli_pid})." if cli_pid
                        else " (dry-run fallback — waiting for a client)."))
        elif direct:
            proc = launch_direct(place_id, ticket, tracker, job_id,
                                 self.join_method)
            self.log(f"[{name}] launched RobloxPlayerBeta.exe directly (PID {proc.pid}).")
        else:
            url = build_protocol_url(place_id, ticket, tracker, job_id,
                                     self.join_method)
            launch_protocol(url)
            if follow and job_id:
                how = ("friend-join via the server page URL"
                       if self.join_method == "pageurl"
                       else "RequestGameJob for that exact server")
                self.log(f"[{name}] joining main's server (job {job_id}, {how}).")
            else:
                self.log(f"[{name}] sent roblox-player: launch request.")

        pid = None
        pids = set()
        hwnd = None
        if wait_for_process:
            pids, hwnd, wpid = wait_for_roblox_clients(
                existing, process_timeout=90, window_timeout=wait_for_window_s)
            # remember these pids for a while: the orphan sweep must not treat
            # a just-launched client as a stray while we register it
            seen = time.time()
            with self._lock:
                for p in pids:
                    self._recent_pids[p] = seen
            if not pids:
                self.log(f"[{name}] warning: no new RobloxPlayerBeta process detected in 90s.")
            else:
                pid = wpid or min(pids)
                self._status(name, "loading", f"pid {pid} (clients {sorted(pids)})")
                if hwnd is None:
                    self.log(f"[{name}] warning: no window detected for pids {sorted(pids)}.")

        with self._lock:
            self.running[name] = {
                "pid": pid,
                "pids": set(pids),
                "hwnd": (hwnd if hwnd else None),
                "headless": headless,
                "role": role,
                "username": account.get("username", ""),
            }
        # when the client was started. Presence confirming it is in a game is a
        # separate event (`_seen_in_game`); the gap between the two is how long
        # we are willing to wait before calling the join a failure.
        self._launch_since[name] = time.time()
        # a relaunched client writes a NEW log, and Roblox logs carry no pid or
        # account name, so the old mapping would point this account at its
        # predecessor's server - the exact stale-target bug this log reading
        # exists to eliminate.
        self._client_logs.pop(name, None)
        self._log_state.pop(name, None)

        if pid and hwnd:
            if headless:
                hide_window(hwnd, self.no_render)
                self._apply_alt_throttle(self.running[name])
                self._status(name, "running (hidden)", f"pid {pid}")
                self.log(f"[{name}] hidden ({'no-render' if self.no_render else 'hidden'}). "
                         f"pid={pid}")
            else:
                self._status(name, "running (visible)", f"pid {pid}")
                self.log(f"[{name}] visible (main/GUI). pid={pid}")
        elif headless and pid:
            # no window yet: throttle the processes anyway (cheaper while booting)
            self._apply_alt_throttle(self.running[name])
        return self.running[name]

    # -- presence ------------------------------------------------------------
    # -- presence API etiquette ------------------------------------------------
    def _presence_ready(self):
        """False while we're deliberately backing off the presence API."""
        return time.time() >= self._presence_backoff

    def _note_presence_error(self, throttled):
        """Back off after a failed presence call so a 429 storm can't turn into
        a reconnect storm. Doubles up to a cap, reset on the next good reply.

        The cap used to be 60s, which is nowhere near enough: Roblox's presence
        API throttles hard, and a session that was 429ing every minute could
        stay throttled indefinitely because we kept probing. The floor is now
        30s and the ceiling 5 minutes, so a throttled run actually settles
        instead of feeding the limit.
        """
        if throttled:
            self._presence_penalty = min(max(self._presence_penalty * 2, 30.0), 300.0)
        else:
            self._presence_penalty = min(max(self._presence_penalty, 2.0), 300.0)
        self._presence_backoff = time.time() + self._presence_penalty
        return self._presence_penalty

    def _note_presence_ok(self):
        self._presence_penalty = 0.0
        self._presence_backoff = 0.0
        self._presence_429_logged = 0.0

    def _presence_gap(self, now):
        """Minimum seconds between presence requests.

        One request per health tick (1s) looked fast but is ~60 requests a
        minute, which Roblox's presence API refuses: the log filled with HTTP
        429 and, because a throttled check decides nothing, the whole health
        net (kicks AND wrong-server correction) went silently offline.

        The old "settling" test was `any join in the last 60s`, and `_join_since`
        is never pruned - so with six alts launching over a minute and a half
        it was true essentially all the time, and the launcher ran at the fast
        rate for the whole session. That is what earned the 429 in the log
        ("presence rate-limited ... pausing 30s") that left every alt unable to
        be confirmed.

        So the fast rate now applies only while an alt is genuinely AWAITING
        confirmation: launched, and presence has not yet proven it is in a
        game. It is bounded by `join_confirm_sec` so an alt that cannot join at
        all (a full server) does not hold the launcher at the fast rate forever.
        Everything else runs at the slow rate.
        """
        fast = self.alt_presence_interval
        awaiting = False
        for name, launched_at in self._launch_since.items():
            if name in self._seen_in_game:
                continue
            if now - launched_at < self.join_confirm_sec:
                awaiting = True
                break
        if awaiting or self._wrong_server_tries:
            return max(1.0, fast)
        return max(fast, self.alt_presence_idle_interval)

    def _bad_reading(self, name, reason):
        """Debounce teardown decisions: require 2 consecutive bad readings for
        the SAME reason before anything gets closed. One odd reading (a blip, a
        stale cache) must never start a close/relaunch cycle."""
        prev, count = self._bad_readings.get(name, (None, 0))
        count = count + 1 if prev == reason else 1
        self._bad_readings[name] = (reason, count)
        return count >= self.drop_confirm_count

    def _good_reading(self, name):
        self._bad_readings.pop(name, None)

    def _presence_batch(self, uids, cookie=""):
        """Query presence for MANY users in ONE request.

        The endpoint takes a list, so checking every alt together is both far
        fresher (they are all judged on the same fresh reading) and far cheaper
        (one request instead of N), which is what keeps us clear of the 429
        limit. Returns {uid: presence_dict}; an empty dict means "no usable
        readings", and the caller must then decide nothing at all.
        """
        ids = []
        for u in uids:
            try:
                ids.append(int(u))
            except (TypeError, ValueError):
                continue
        if not ids:
            return {}
        url = "https://presence.roblox.com/v1/presence/users"
        body = json.dumps({"userIds": ids}).encode("utf-8")
        headers = {"User-Agent": UA, "Accept": "application/json",
                   "Content-Type": "application/json", "Referer": REFERER}
        if cookie:
            headers["Cookie"] = ".ROBLOXSECURITY=" + cookie
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and cookie:       # cookie rejected - retry anonymous
                return self._presence_batch(ids, "")
            if exc.code == 429:                 # rate limited - caller backs off
                raise PresenceThrottled("presence API HTTP 429 (rate limited)")
            raise RuntimeError(f"presence API HTTP {exc.code}")
        if data.get("requestStatus") == "Throttled":
            return {}                            # no readings; decide nothing
        out = {}
        for pres in data.get("userPresences") or []:
            try:
                out[int(pres.get("userId"))] = pres
            except (TypeError, ValueError):
                continue
        return out

    def _presence(self, uid, cookie=""):
        """Query Roblox presence for a single user (one-element batch)."""
        try:
            key = int(uid)
        except (TypeError, ValueError):
            return {}
        return self._presence_batch([key], cookie).get(key, {})

    def _client_log_path(self, name):
        """The Roblox client log belonging to `name`, resolved once and cached.

        Roblox logs carry neither the pid nor the account name, so a client is
        matched to its log by boot time, using the moment we launched it. That
        is unambiguous because alt launches are sequential, but the mapping is
        still cached: re-resolving it would rescan the log directory on a
        one-second loop, and a wrong answer would be worse than a stale one.
        """
        cached = self._client_logs.get(name)
        if cached:
            return cached if os.path.isfile(cached) else None
        started = self._launch_since.get(name)
        if not started:
            return None
        claimed = set(p for p in self._client_logs.values() if p)
        path = find_client_log(started, skip=claimed, not_before=started)
        if path:
            self._client_logs[name] = path
            return path
        return None

    def _client_log_target(self, name):
        """Which server this client's own log says it joined -> (job, place).

        Returns (None, None) whenever the log is missing, unreadable, or has no
        join line yet, so every caller must treat this as evidence that may
        simply be absent - it is a strictly better source than presence where
        available, not a replacement for it.
        """
        path = self._client_log_path(name)
        if not path:
            return None, None
        try:
            got = read_client_join(path)
        except Exception:
            return None, None
        return got if got else (None, None)

    def _main_log_state(self, name):
        """The MAIN client's own log -> (job, place, session_history).

        Same reader the alts use. The session history is the part that matters
        for the main: it is the only first-hand answer to "is the main still in
        a game", which presence cannot give reliably.
        """
        path = self._client_log_path(name)
        if not path:
            return None, None, None
        try:
            return read_client_state(path)
        except Exception:
            return None, None, None

    def _alt_log_check(self, name, acc, now):
        """Ask this alt's OWN client log what it is actually doing.

        This is the reliable half of the health check, and it is free: a local
        file read, no API call, no rate limit, and it cannot be stale or
        throttled. Presence could confirm almost nothing here - phantom
        readings, 429s, and an alt that never got a usable verdict - so every
        alt sat on "no signal" while its own log had the answer in plain text.

        The log answers TWO questions, and they need different evidence:

          * which server  -> the "! Joining game '<job>'" line, and
          * still in it?  -> the LAST letter of "Session history:", because the
            join line PERSISTS after the client leaves. Reading only the join
            line made "joined" a one-way street: an alt kicked from the game
            kept the line, kept reading "joined" forever, and the state never
            went back to disconnected. Presence was supposed to catch that,
            and in practice never once did (0 kicks detected in the whole log
            against 157 log-confirmed arrivals).

        So the session state machine is now the authority for kicks too, which
        leaves presence with nothing essential to do.

        Returns True when the log had something to say.
        """
        path = self._client_log_path(name)
        if not path:
            # No client log AT ALL, and no window either. This client never
            # came up properly: hhhhhhohkk6 sat in this state for hours,
            # logging "anti-AFK jump not delivered: its window is gone" every
            # jump interval. The process is alive, so the process check passes;
            # there is no log, so the log check has nothing to say; and presence
            # is blind, so nothing could ever confirm or reject it. It was
            # unreachable by the entire health net. All the evidence is local,
            # so act on it.
            launched = self._launch_since.get(name)
            waited = (now - launched) if launched else 0
            if waited >= self.join_confirm_sec:
                with self._lock:
                    rec = self.running.get(name) or {}
                if (not rec.get("hwnd")
                        and self._bad_reading(name, "no-client")):
                    self._handle_drop(
                        name, f"client never started properly (no window "
                              f"and no client log after {waited:.0f}s)")
            return False

        try:
            job, _place, session = read_client_state(path)
        except Exception:
            return False

        history = session or ""
        if session:
            in_game = history.endswith(SESSION_IN_GAME)
            # Reached a play session at some point, but the last state is not P:
            # it was in a game and is not any more. This is a real, local kick.
            was_in_game = (not in_game) and (SESSION_IN_GAME in history[:-1])
        else:
            # No session history in the log. Roblox writes "! Joining game" a
            # moment BEFORE "Session history: IASP", so there is a real
            # sub-second window with a join and no session state.
            #
            # With a join line, fall back to it: absence of session evidence is
            # NOT evidence of absence, and treating it as "left" would close a
            # healthy, joined client. With no join line either, the log is
            # positive evidence the client is running but has not entered a
            # session - which is "not in a game yet", not "we cannot tell".
            in_game, was_in_game = bool(job), False

        if not in_game:
            self._log_state[name] = None
            self._presence_verdict[name] = "out"
            if was_in_game:
                # A genuine leave, from the client's own state machine rather
                # than from a throttled API. Debounced like every other
                # teardown decision, and the drop reopens the relaunch.
                if (now - self._join_since.get(name, 0) >= self.drop_grace_sec
                        and self._bad_reading(name, "left")):
                    self._handle_drop(
                        name, "left the server (kicked from game) - its own "
                              "client log shows it left the play session")
                return True
            # Never reached a play session: still joining, or the join failed.
            # Positive local evidence, so the overlay can say so instead of
            # falling back to "no signal" while presence is blind.
            launched = self._launch_since.get(name)
            waited = (now - launched) if launched else 0
            if (waited >= self.join_confirm_sec
                    and self._bad_reading(name, "never-joined")):
                self._handle_drop(
                    name, f"failed to join (its own client log shows it never "
                          f"reached a game after {waited:.0f}s)")
            return True

        # --- in a play session: it is in a game right now --------------------
        self._log_state[name] = job
        if not self._seen_in_game.get(name):
            self._seen_in_game[name] = True
            launched = self._launch_since.get(name)
            self.log(f"[{name}] its own client log confirms it joined server "
                     f"{job}"
                     + (f" after {now - launched:.0f}s."
                        if launched else "."))
        self._alt_job[name] = job
        self._presence_verdict[name] = "in"
        target = self._target
        if (job and self.server_queue_retry > 0 and target and target[1]
                and str(job).lower() != str(target[1]).lower()):
            # Debounced exactly like the presence path. This runs every second
            # and `_queue_for_main_server` CLOSES the client, so acting on a
            # single reading risks killing a healthy client over one transient
            # value. Same `dropConfirmCount` consecutive agreement as kicks.
            #
            # `_good_reading` must NOT be called on this branch: it pops the
            # debounce counter, so calling it here would reset the count to
            # zero every single second and the two-reading rule could never
            # once be satisfied.
            if (now - self._join_since.get(name, 0) >= self.drop_grace_sec
                    and self._bad_reading(name, "wrong-server")):
                self._queue_for_main_server(name, acc, target, job)
        else:
            self._good_reading(name)         # a good reading clears the counter
            # This alt is in the MAIN's server, so any outstanding
            # wrong-server flag is stale and MUST be cleared here.
            #
            # It used to be cleared in exactly one place - the presence sweep's
            # all-good branch - and this log-authoritative path made that branch
            # unreachable for log-backed alts. So once an alt was queued it
            # stayed queued FOREVER, and the overlay showed "queued" over
            # accounts that were demonstrably in the right server, one more
            # every few minutes. Clearing has to live wherever the verdict is
            # reached, and the log is now where that happens.
            self._wrong_server_tries.pop(name, None)
        return True

    def follow_main_loop(self, main_account):
        """Persistent follower (follow mode).

        - Launches the main account (visible).
        - While the main's client is open, polls its presence every ~1s.
        - Main enters a game  -> alts launch (hidden) into that exact server.
        - Main leaves a game  -> alts are closed.
        - Main joins again    -> alts join again.
        - Main's client window closes -> all alts closed, follow stops.
        Retries failed alt joins every 15s (e.g. server full) until they land.
        A health monitor (separate thread, 1s) detects alt disconnects and
        auto-reconnects them into the main's current server.
        """
        results = []
        name = main_account.get("name", "?")
        with self._lock:
            if self._follow_active:
                self.log("A follow loop is already running — ignoring duplicate click.")
                return results
            self._follow_active = True
        try:
            try:
                info = self.launch_account(main_account)
                results.append((name, "ok", info))
            except Exception as exc:
                self.log(f"[{name}] FAILED: {exc}")
                self._status(name, "error", str(exc))
                results.append((name, "error", str(exc)))
                self.on_follow(f"{name} failed to launch — follow stopped")
                return results

            uid = main_account.get("userId")
            if not uid:
                info2 = get_authenticated_user(main_account.get("cookie", ""))
                if info2:
                    uid = info2[2]
                    main_account["userId"] = uid
            if not uid:
                self.log("Cannot resolve the main account's user ID — follow disabled.")
                self.on_follow("No user ID — follow disabled")
                return results

            with self._lock:
                main_pid = (self.running.get(name) or {}).get("pid")

            alt_accounts = [a for a in self.accounts if a.get("role") != "main"]
            self._joined.clear()   # alt names currently joined
            self._join_since.clear()
            self._target = None    # (place, job) alts should be in
            # alts may launch again: clear the "main is gone" guard tripped by a
            # previous _close_alts (main re-joining the same session)
            self._alts_closed.clear()
            cooldown = {}          # alt name -> next allowed join attempt (retry after failure)
            poll = self.poll_interval

            self.log(f"[follow] monitoring '{name}' (pid {main_pid}) — "
                     "1s health checks on every alt (auto-reconnect on drops).")
            self.on_follow(f"Waiting for {name} to join a game…")
            self._start_health_monitor()
            self._start_jump_loop()

            while not self._stop.is_set():
                # --- did the main's client close? ---
                # Check the whole pid set: the bootstrap process may exit early,
                # so the main is only "closed" when NONE of its pids remain.
                with self._lock:
                    rec = self.running.get(name)
                if rec is None or not instance_alive(rec):
                    self.log(f"[follow] main '{name}' closed its client — closing alts.")
                    # always call it: _close_alts also kills alts that are
                    # running but not yet in `_joined`, and blocks pending
                    # relaunches. Skipping it when `_joined` looked empty was
                    # how alt clients survived the main closing.
                    self._close_alts(alt_accounts, self._joined)
                    self.on_follow(f"{name} closed — alts closed, follow stopped")
                    return results

                # --- presence ---
                if not self._presence_ready():
                    self._stop.wait(poll)
                    continue
                try:
                    p = self._presence(uid, main_account.get("cookie", ""))
                    self._note_presence_ok()
                except PresenceThrottled as exc:
                    wait = self._note_presence_error(True)
                    self.log(f"presence rate-limited (HTTP 429) — pausing presence "
                             f"checks {wait:.0f}s; alts are left alone.")
                    self._stop.wait(min(wait, 30))
                    continue
                except Exception as exc:
                    wait = self._note_presence_error(False)
                    # NEVER treat a failed check as "the main left" — that used to
                    # close every alt on a single rate-limit error.
                    self.log(f"presence check failed: {exc} — retrying in {wait:.0f}s "
                             f"(state UNKNOWN, alts untouched).")
                    self._stop.wait(min(wait, 30))
                    continue

                if p.get("userPresenceType") == -1:
                    # Throttled / unknown — make NO decision, just wait. The
                    # presence API self-backoffs for a few seconds on throttle.
                    self._stop.wait(poll)
                    continue

                in_game = p.get("userPresenceType") == 2    # 2 = in game
                place = p.get("placeId") or self.place_id
                job = p.get("gameId")

                # The client's OWN log outranks presence for WHICH server this
                # is. Presence is stale across a client restart: in the live log
                # the main's client was closed and relaunched, presence went on
                # reporting the previous session's job for ~30s, and six alts
                # were aimed at a server the main had already left. They were
                # pinned perfectly - to the wrong server.
                #
                # The log is written at the instant of the join, cannot be
                # cached, and needs no API call, so it is also immune to the
                # 429s that leave presence deciding nothing at all.
                #
                # It also answers WHETHER the main is still in that game. The
                # join line alone cannot: it PERSISTS after the client leaves,
                # so deriving `in_game` from it made the main read "in game"
                # forever once it had ever joined, and only the window closing
                # could change it. That is the same stale-line trap the alts
                # had, and the fix is the same - the session history's last
                # letter is the live state:
                #     P in a game   A back at the app   T tearing down
                log_job, log_place, log_session = self._main_log_state(name)
                if log_session:
                    log_in_game = log_session.endswith(SESSION_IN_GAME)
                    if log_in_game != in_game:
                        self.log(f"[follow] {name} {'entered' if log_in_game else 'left'}"
                                 f" a game (its own client log: session "
                                 f"{log_session}).")
                    # The log is first-hand, so it wins BOTH ways: presence can
                    # be stale forward (the old session's job) and stale
                    # backward (a game already left).
                    in_game = log_in_game
                    # Use log job/place whenever available - the log is
                    # authoritative for WHICH server, even when presence
                    # returns no placeId (which happens on some clients).
                    if log_job:
                        if job and log_job != job:
                            self.log(f"[follow] {name}'s server corrected: presence "
                                     f"said {job}, the client's own log says "
                                     f"{log_job} (trusting the log).")
                        job, place = log_job, (log_place or place)
                    elif log_place and not place:
                        place = log_place
                elif log_job:
                    # No session state yet, but we have a join line. Trust it
                    # for WHICH server even though it says nothing about whether
                    # the main is still in it.
                    if job and log_job != job:
                        self.log(f"[follow] {name}'s server corrected: presence "
                                 f"said {job}, the client's own log says "
                                 f"{log_job} (trusting the log).")
                    job, place = log_job, (log_place or place)
                elif log_place and not place:
                    place = log_place

                if in_game:
                    # the main is back in a game, so alt launches are allowed again
                    self._alts_closed.clear()
                    self._target = (place, job)
                    # Launch alts if we know the place OR at least the job
                    # (gameId). PlaceLauncher can resolve a jobId even without
                    # a placeId - some clients don't report placeId in presence.
                    if (place or job) and not self._joined:
                        self.log(f"[follow] {name} joined a game — launching alts "
                                 f"(place {place or 'n/a'}, job {job or 'n/a'}).")
                        self._status(name, "in game", f"place {place or 'n/a'} job {job or 'n/a'}")
                        self.on_follow(f"{name} in-game — launching alts…")
                    if not place and not job:
                        self.log("[follow] main is in-game but presence and "
                                 "client log gave no place or job — retrying.")
                    for acc in alt_accounts:
                        aname = acc.get("name", "?")
                        # Re-checked every pass, not just from the one-time
                        # snapshot: `alt_accounts` was built once when the loop
                        # started, so an account switched off mid-session would
                        # still be launched and relaunched.
                        if not acc.get("enabled", True):
                            # Switched off. Drop any leftover bookkeeping, but
                            # do NOT relaunch it - reviving an account the user
                            # turned off would be worse than the stale flag.
                            if aname in self._joined:
                                self._joined.discard(aname)
                            continue
                        if not (place or job) or aname in self._reconnecting:
                            continue                       # nothing to target yet
                        with self._lock:
                            has_client = aname in self.running
                        if aname in self._joined:
                            if has_client:
                                continue                   # genuinely running
                            # Stale bookkeeping: `_joined` claims it is in, but
                            # there is no client at all. Trusting that set made
                            # the alt unreachable FOREVER - the follow loop
                            # skipped it as "already joined", and the health
                            # monitor only ever walks `running`, so nothing
                            # could relaunch it.
                            # A live client is the truth; the set is a hint.
                            self._joined.discard(aname)
                            self.log(f"[{aname}] was marked joined but has no "
                                     f"running client — relaunching it into the "
                                     f"main's server.")
                        if cooldown.get(aname, 0) > time.time():
                            continue                       # retry backoff after a failed join
                        if self._pause_until.get(aname, 0) > time.time():
                            continue                       # anti-thrash pause
                        if self._alts_closed.is_set():
                            continue                       # main gone: no new alts
                        try:
                            self.on_follow(f"Launching {aname} into main's server…")
                            self.launch_account(acc, follow=(place, job))
                            self._joined.add(aname)
                            self._join_since[aname] = time.time()
                            with self._lock:
                                apid = (self.running.get(aname) or {}).get("pid")
                            # NOT "joined". All that is proven here is that the
                            # client process started; Roblox still has to put it
                            # in the server, which can fail (a full server
                            # refuses the join). Claiming a join here is what
                            # made the log contradict the status overlay - the
                            # real arrival is logged by the presence sweep.
                            self.log(f"[{aname}] client started (pid {apid}) — "
                                     f"waiting for presence to confirm it is in "
                                     f"the game.")
                        except Exception as exc:
                            self.log(f"[{aname}] join attempt failed: {exc} — retrying in 15s.")
                            cooldown[aname] = time.time() + 15
                else:
                    self._target = None
                    # leaving the game closes alts too, and the same "running but
                    # not joined" window applies — so don't gate on _joined
                    with self._lock:
                        any_running = any(
                            r.get("role") != "main" for r in self.running.values())
                    if self._joined or any_running:
                        self.log(f"[follow] {name} left the game — closing alts.")
                        self._status(name, "online", "left game")
                        self.on_follow(f"{name} left the game — closing alts…")
                        self._close_alts(alt_accounts, self._joined)
                        cooldown.clear()
                        self._join_since.clear()
                        self.on_follow(f"Waiting for {name} to join a game again…")

                self._stop.wait(poll)
            self.log("[follow] follow loop stopped.")
            return results
        finally:
            with self._lock:
                self._follow_active = False

    def _close_alts(self, alt_accounts, joined):
        """Close EVERY alt: kill the clients and stop any pending relaunch.

        Iterating `joined` alone was not enough: an alt can be on screen but
        not yet in `joined` (a relaunch in flight, a queue retry pending), and
        such a client would survive the main closing with nothing left to
        clean it up. So this sweeps every alt in `self.running`, kills the
        clients, and trips `_alts_closed` so in-flight workers abort instead of
        resurrecting an alt after the main is gone.
        """
        # Trip the guard FIRST: a worker may be sitting on the launch lock right
        # now and would otherwise start a brand-new client after we return.
        self._alts_closed.set()
        with self._lock:
            names = {a.get("name", "?") for a in self.accounts
                     if a.get("role") != "main"}
            pending = list(self.running.items())
        for aname, rec in pending:
            if aname not in names:
                continue                      # never touch the main
            pids = rec.get("pids") or ([rec["pid"]] if rec.get("pid") else [])
            for p in pids:
                kill_process(p)
            self.log(f"[{aname}] closed (pids {sorted(p for p in pids if p)}).")
            self._status(aname, "stopped", "left with main")
        with self._lock:
            for aname in names:
                self.running.pop(aname, None)
        # The "was ever in a game" proof describes a client that no longer
        # exists. Clearing it matters beyond tidiness: an alt switched off and
        # back on would otherwise still look in-game to anything that consults
        # the history rather than the live record.
        for aname in names:
            self._seen_in_game.pop(aname, None)
            self._log_state.pop(aname, None)
            self._alt_job.pop(aname, None)
            self._presence_verdict.pop(aname, None)
        joined.clear()
        # forget queued/retry bookkeeping so a later session starts clean
        self._reconnecting.clear()
        self._wrong_server_tries.clear()

    # -- alt health monitor (checks every poll_interval = 1s) -----------------
    def _alt_account(self, name):
        return next((a for a in self.accounts if a.get("name") == name), None)

    def _resolve_alt_uid(self, acc):
        name = acc.get("name", "?")
        uid = self._alt_uid.get(name) or acc.get("userId")
        if not uid:
            info = get_authenticated_user(acc.get("cookie", ""))
            if info:
                uid = info[2]
                acc["userId"] = uid
        if uid:
            self._alt_uid[name] = uid
        return uid

    def _start_health_monitor(self):
        """Start the single 1-second health-monitor thread if it isn't running."""
        with self._lock:
            if self._health_thread and self._health_thread.is_alive():
                return
            th = threading.Thread(target=self._health_loop, daemon=True)
            self._health_thread = th
            th.start()

    def _health_loop(self):
        # one-time cleanup of clients left over from an earlier session
        if not self._swept:
            self._swept = True
            try:
                self._sweep_orphan_clients()
            except Exception as exc:
                self.log(f"orphan sweep error: {exc}")
        while not self._stop.is_set():
            try:
                self._health_tick()
            except Exception as exc:   # keep the monitor alive
                self.log(f"health monitor error: {exc}")
            # forget stale pid sightings so _recent_pids can't grow forever
            cutoff = time.time() - 300
            with self._lock:
                for p in [p for p, seen in self._recent_pids.items() if seen < cutoff]:
                    self._recent_pids.pop(p, None)
            self._stop.wait(self.poll_interval)

    def _sweep_orphan_clients(self):
        """Close Roblox clients that no instance owns.

        A drop that lost its record (or a client launched just before the
        launcher started) leaves a live process nothing can ever close; the
        symptom is clients piling up that never go away. Anything running that
        is not in `self.running` gets terminated once, at startup.

        Recently-launched pids are exempt: between "old client killed" and
        "new client registered" an alt is briefly untracked, and sweeping that
        gap would kill the very client a reconnect is waiting for.
        """
        if not self.guard_enabled:
            return                    # without the guard we can't tell ours apart
        now = time.time()
        with self._lock:
            tracked = {p for rec in self.running.values()
                       for p in (rec.get("pids") or ([rec["pid"]] if rec.get("pid") else []))
                       if p}
            recent = {p for p, seen in self._recent_pids.items() if now - seen < 30}
        stray = [p for p in sorted(roblox_player_pids()) if p not in (tracked | recent)]
        if not stray:
            return
        for p in stray:
            kill_process(p)
        self.log(f"Closed {len(stray)} untracked Roblox client process(es) "
                 f"(orphans from an earlier session): pids {stray}.")

    def _health_tick(self):
        """One pass over every tracked alt (called every second).

        Detects two kinds of disconnection:
          1. The alt's client process is gone (Roblox closed/killed/crashed it).
             Local and immediate - no API call, decided on the first tick.
          2. The client is alive but no longer in-game (kicked / server dropped
             us), or it is on a DIFFERENT server than the main (Roblox does that
             when the main's server is full).

        (2) needs the presence API, so every alt that is due is checked in a
        SINGLE batched request rather than one request per alt. That is what
        makes rejoin fast *and* cheap: one call covers everyone, so the interval
        can be short without earning a 429.
        """
        now = time.time()
        with self._lock:
            snapshot = dict(self.running)
        due = []                      # (name, acc, uid) waiting on a reading
        for name, rec in snapshot.items():
            if rec.get("role") == "main":
                continue                       # the main is user-controlled
            acc = self._alt_account(name)
            if not acc:
                continue
            if name in self._reconnecting:
                continue                       # a relaunch is already in flight
            if self._pause_until.get(name, 0) > now:
                continue                       # anti-thrash pause

            # the main is gone -> alts are being closed; never relaunch here
            if self._alts_closed.is_set():
                continue

            # 1) process-level disconnect (fast, local, no API call)
            if not instance_alive(rec):
                self._handle_drop(name, "client closed (process gone)")
                continue

            # 1b) the client's own log: which server is it ACTUALLY in?
            # Local, free, instant, and immune to the 429s that leave presence
            # unable to confirm anything. This is the only dependable arrival
            # signal we have had.
            self._alt_log_check(name, acc, now)

            # 2) needs presence: main in a game, not backing off, and this alt
            # is due for a reading
            target = self._target
            if not (target and target[0]):
                continue                       # main not in a game right now
            if not self._presence_ready():
                continue                       # deliberately backing off
            if now < self._next_presence.get(name, 0):
                continue
            uid = self._resolve_alt_uid(acc)
            if not uid:
                continue
            self._next_presence[name] = now + self.alt_presence_interval + \
                random.uniform(0, self.alt_presence_interval * 0.3)
            self._last_alt_presence[name] = now
            due.append((name, acc, uid))

        if due:
            self._presence_sweep(due, now)

    def _mark_blind(self, due):
        """Record that presence gave us NOTHING for these alts.

        Every early return below means the same thing - we asked and were told
        nothing (HTTP 429, a network error, or an empty reply). The launcher
        correctly decides nothing in that case, but the overlay used to fall
        back to "joining", which is a guess. And it is the worst possible guess:
        being rate-limited looks exactly like an alt that is still loading, so
        a launcher that had stopped being able to see looked like one that was
        merely slow. Recording "unknown" lets the panel say "no signal" and
        distinguish the two.
        """
        for name, _acc, _uid in due:
            if name in self._log_state:
                continue        # the log already spoke for this alt; asking
                                # again and learning nothing proves nothing
            self._presence_verdict[name] = "unknown"

    def _presence_sweep(self, due, now):
        """One batched presence request for every due alt, then act per alt."""
        # A hard floor between calls. The per-alt due times above decide WHO is
        # in the batch; this decides HOW OFTEN we are allowed to ask at all.
        if now - self._last_presence_at < self._presence_gap(now):
            return
        self._last_presence_at = now
        try:
            readings = self._presence_batch([uid for _n, _a, uid in due])
        except PresenceThrottled:
            self._note_presence_error(True)
            if time.time() - self._presence_429_logged > 120:
                self._presence_429_logged = time.time()
                self.log("presence rate-limited (HTTP 429) — backing off "
                         f"{self._presence_penalty:.0f}s; no alts will be touched "
                         "until the limit clears.")
            self._mark_blind(due)
            return
        except Exception as exc:
            self._note_presence_error(False)
            if time.time() - self._presence_429_logged > 120:
                self._presence_429_logged = time.time()
                self.log(f"health check failed ({exc}) — retrying later "
                         "(no alts touched).")
            self._mark_blind(due)
            return
        if not readings:
            self._mark_blind(due)        # throttled/unknown: decide nothing
            return
        self._note_presence_ok()

        target = self._target
        for name, acc, uid in due:
            if name in self._log_state:
                # This alt's own log is authoritative for arrival and identity,
                # so presence is not allowed to overwrite it here. Presence
                # still has the job it is actually good at: noticing a KICK,
                # which leaves no trace in the log.
                if readings.get(int(uid), {}).get("userPresenceType") not in (2, -1):
                    joined_at = self._join_since.get(name, 0)
                    if (self._seen_in_game.get(name)
                            and now - joined_at >= self.drop_grace_sec
                            and self._bad_reading(name, "left")):
                        self._handle_drop(
                            name, "left the server (kicked from game)")
                continue
            p = readings.get(int(uid))
            # Classify the reading. `userPresenceType` alone is NOT a reliable
            # discriminator: Roblox returns type=2 ("InGame") with gameId,
            # placeId AND lastLocation all null. That is a phantom left over
            # from an earlier session, and accepting it is how an alt got logged
            # as "confirmed in a game by presence after 1s" - a Roblox client
            # cannot be in a game one second after starting. A genuine in-game
            # reading always carries a gameId (the main's does, alongside
            # lastLocation "Steal An Egg"), and the payload carries no timestamp
            # at all, so freshness cannot be proven any other way.
            #
            # So the gameId is the evidence, and there are three verdicts:
            #   in      - in a game, and we know WHICH server
            #   out     - definitively not in a game (presence says so)
            #   unknown - Roblox said something we cannot act on
            if not p:
                if self._presence_verdict.get(name) != "in":
                    self._presence_verdict[name] = "unknown"
                continue
            ptype = p.get("userPresenceType")
            if ptype == -1:
                if self._presence_verdict.get(name) != "in":
                    self._presence_verdict[name] = "unknown"
                continue                   # throttled/unknown — no decision
            if ptype == 2 and not p.get("gameId"):
                # Claims InGame but names no server. Ambiguous, so it decides
                # nothing: it is not proof of arrival, and it is not an absence
                # either. Counting it as arrival is what made the overlay claim
                # a join that had not happened.
                if self._presence_verdict.get(name) != "in":
                    self._presence_verdict[name] = "unknown"
                continue
            if ptype != 2:
                # Absence only means "left the server" if we actually saw this
                # alt in a game since it joined. Without that check, presence
                # lag right after a join reads as a kick and we close a healthy
                # client - which is exactly what happened.
                self._presence_verdict[name] = "out"   # definitive absence
                if not self._seen_in_game.get(name):
                    # ...but that rule alone strands an alt whose join NEVER
                    # succeeds: a full server refuses it, so it is never seen
                    # in-game, so absence never counts, so it sits here forever
                    # as a live client that is not in the game. The launcher
                    # logged "joined main's server" because that message means
                    # "the process started", not "it joined".
                    #
                    # So the other branch needs an escape too: once the confirm
                    # window has passed and `drop_confirm_count` consecutive
                    # definitive readings all agree it is not in a game, the join
                    # itself failed and retrying is the only way out.
                    launched_at = self._launch_since.get(name)
                    waited = (now - launched_at) if launched_at else 0
                    if (waited >= self.join_confirm_sec
                            and self._bad_reading(name, "never-joined")):
                        self._handle_drop(
                            name, f"failed to join (no presence confirmation "
                                  f"after {waited:.0f}s)")
                    continue
                joined_at = self._join_since.get(name, 0)
                if (now - joined_at >= self.drop_grace_sec
                        and self._bad_reading(name, "left")):
                    self._handle_drop(name, "left the server (kicked from game)")
                continue
            self._presence_verdict[name] = "in"    # in a game, server known
            if not self._seen_in_game.get(name):
                # The real arrival, proven rather than assumed. This is the line
                # that pairs with "client started ..." above: between them the
                # log shows how long the join actually took, and if it never
                # appears, the join failed and `joinConfirmSec` will retry it.
                self._seen_in_game[name] = True
                self.log(f"[{name}] confirmed in a game by presence (server "
                         f"{p.get('gameId')})"
                         + (f" after {now - self._launch_since[name]:.0f}s."
                            if self._launch_since.get(name) else "."))
            self._alt_job[name] = p["gameId"]

            # in a game, but is it the MAIN's server? When the main's server is
            # full Roblox turns the alt away and drops it into any OTHER server
            # of the same place - which looks healthy but defeats the point.
            target_job = target[1] if target else None
            alt_job = p.get("gameId")
            if (self.server_queue_retry > 0 and target_job and alt_job
                    and str(alt_job).lower() != str(target_job).lower()):
                if (now - self._join_since.get(name, 0) >= self.drop_grace_sec
                        and self._bad_reading(name, "wrong-server")):
                    self._queue_for_main_server(name, acc, target, alt_job)
                continue
            self._good_reading(name)               # all good
            self._wrong_server_tries.pop(name, None)
            self._verify_join_outcome(name, acc, target_job, alt_job, now)

    def _verify_join_outcome(self, name, acc, target_job, alt_job, now):
        """Report, once per join, whether the alt really landed in the MAIN's
        server.

        This is the measurement that cannot be made from outside: PlaceLauncher
        refuses external callers ({"isValid":false} with or without auth), so
        there is no way to ask Roblox "would you have honoured this jobId?".
        But we can see the answer a few seconds after the join, because the
        alt's presence reports the server it is actually in.

        So the launcher reports the outcome directly, which is what tells you
        whether joinMethod is doing its job:
            landed in main's server (pinned)
            landed in a different server (… ) - correcting
        A tallies how often the pin held, so a session's worth of joins gives
        you a real rate instead of a guess.
        """
        if not (target_job and alt_job):
            return                          # nothing to compare yet
        landed = str(alt_job).lower() == str(target_job).lower()
        if self._join_verified.get(name):
            if not landed:
                self._join_verified[name] = False   # allow one more report
            else:
                return                              # already reported
        # only speak up once the alt has been up a moment, so a join still
        # settling is not misreported as a wrong server
        if now - self._join_since.get(name, 0) < self.drop_grace_sec:
            return
        self._join_verified[name] = landed
        if landed:
            self._join_stats["pinned"] = self._join_stats.get("pinned", 0) + 1
            self.log(f"[{name}] landed in the main's server — the join was pinned "
                     f"({self._join_stats['pinned']} pinned / "
                     f"{self._join_stats.get('wrong', 0)} corrected).")
        else:
            self._join_stats["wrong"] = self._join_stats.get("wrong", 0) + 1
            self.log(f"[{name}] landed in a DIFFERENT server ({alt_job}) than the "
                     f"main's ({target_job}) — correcting "
                     f"({self._join_stats.get('pinned', 0)} pinned / "
                     f"{self._join_stats['wrong']} corrected).")

    def _close_rec_pids(self, rec, why="", wait=0.0):
        """Terminate every process of a dropped/closed alt client.

        Only pids that are still alive are killed: a pid that has already died
        may since have been REUSED by an unrelated process, and terminating
        that blindly would take out something innocent.

        `wait` optionally blocks until the processes exit, so a relaunch that
        follows isn't confused with the old client by wait_for_roblox_clients
        (which matches by *new* pid). The health monitor passes 0 and does the
        waiting on a worker thread, so one slow client can't stall the checks
        for every other alt.
        """
        if not rec:
            return
        pids = rec.get("pids") or ([rec["pid"]] if rec.get("pid") else [])
        pids = [p for p in pids if p]
        alive = [p for p in pids if process_alive(p)]
        if not alive:
            return
        for p in alive:
            try:
                kill_process(p)
            except Exception:
                pass
        deadline = time.time() + max(0.0, wait)
        while time.time() < deadline:
            if not any(process_alive(p) for p in alive):
                break
            time.sleep(0.15)
        still = [p for p in alive if process_alive(p)]
        if still:
            self.log(f"warning: client pid(s) {still} still alive {why}.")
        elif alive:
            self.log(f"closed client pid(s) {sorted(alive)} {why}.")

    def _handle_drop(self, name, reason):
        """An alt disconnected: close its client, unregister, decide on reconnect.

        Killing the client is essential, not just tidiness: a "kicked from game"
        drop leaves the Roblox process ALIVE, so forgetting it would leak a
        client that nothing can ever close, while the relaunch starts yet
        another one. (Process-death drops are already gone; killing is a no-op.)

        The wait for the process to actually exit happens on a worker thread,
        so a slow-dying client doesn't block the health checks for other alts.
        """
        now = time.time()
        hist = [t for t in self._drop_history.get(name, [])
                if now - t < self.rejoin_pause_window]
        hist.append(now)
        self._drop_history[name] = hist
        with self._lock:
            rec = self.running.pop(name, None)
            self._joined.discard(name)
            # a new launch must prove it is in-game before absence means
            # anything, otherwise stale presence closes it again straight away
            self._seen_in_game.pop(name, None)
            self._presence_verdict.pop(name, None)   # stale verdict must not outlive the drop
            self._join_verified.pop(name, None)
            # the dropped client is gone; its log verdict described THAT client
            self._log_state.pop(name, None)
            # claim the relaunch slot NOW, synchronously: the follow loop
            # launches any alt that isn't in `_joined`, so if we left a gap
            # between here and the worker thread it would launch a duplicate.
            claimed = name not in self._reconnecting
            if claimed:
                self._reconnecting.add(name)
        self._status(name, "disconnected", reason)
        self.log(f"[{name}] DISCONNECTED ({reason}).")
        acc = self._alt_account(name)
        if not claimed:
            return
        try:
            threading.Thread(target=self._drop_cleanup_and_reconnect,
                             args=(name, acc, rec, hist, now, reason),
                             daemon=True).start()
        except Exception as exc:
            with self._lock:      # the worker never started; release the claim
                self._reconnecting.discard(name)
            self.log(f"[{name}] could not start reconnect cleanup: {exc}")

    def _drop_cleanup_and_reconnect(self, name, acc, rec, hist, now, reason):
        """Kill the dropped client, wait for it to go, then relaunch if we should.

        `_handle_drop` claimed the relaunch slot for `name` so the follow loop
        can't launch a duplicate while we work. EVERY exit path that doesn't
        hand the claim to `_reconnect_worker` must release it here — otherwise
        the alt stays in `_reconnecting` forever and is skipped by every later
        relaunch attempt (it would never come back after, say, an anti-thrash
        pause).
        """
        handed_off = False
        try:
            self._close_rec_pids(rec, f"after '{reason}'", wait=self.relaunch_kill_wait)
            if self._stop.is_set() or self._alts_closed.is_set():
                return
            if len(hist) >= self.rejoin_pause_drops:   # anti-thrash: Roblox keeps killing it
                self._pause_until[name] = now + self.rejoin_pause_sec
                self.log(f"[{name}] dropped {len(hist)}+ times in "
                         f"{self.rejoin_pause_window:.0f}s — auto-reconnect paused "
                         f"for {self.rejoin_pause_sec:.0f}s (relaunch it manually or wait).")
                self.on_follow(f"{name} keeps dropping — auto-reconnect paused "
                               f"{self.rejoin_pause_sec:.0f}s")
                return
            if not acc:
                return
            target = self._target
            place, job = target if target else (None, None)
            if not place:
                self.log(f"[{name}] no game to reconnect to yet — it will rejoin the next "
                         "time the main is in a game.")
                return
            threading.Thread(target=self._reconnect_worker,
                             args=(acc, place, job, reason),
                             daemon=True).start()
            handed_off = True       # the worker owns the claim and releases it
        except Exception as exc:       # never strand the claim on an error
            self.log(f"[{name}] reconnect handoff failed: {exc}")
        finally:
            if not handed_off:
                with self._lock:
                    self._reconnecting.discard(name)

    def _queue_for_main_server(self, name, acc, target, alt_job=None):
        """Alt is in a different server than the main: close it and queue back
        up for the main's server.

        Roblox gives no real "wait in line" for a full server, so the queue is
        implemented here: close the alt (it must not squat in the wrong server),
        wait a backoff interval, then re-request the main's exact server ID.
        The backoff grows with each attempt so a permanently full server isn't
        hammered, and it is NOT counted as a "drop" so the anti-thrash pause
        can't park the alt for 5 minutes just because the server is busy.
        """
        now = time.time()
        tries = self._wrong_server_tries.get(name, 0) + 1
        self._wrong_server_tries[name] = tries
        # 15s, 30s, 60s, 120s… capped at server_queue_retry * 8
        wait = min(self.server_queue_retry * (2 ** min(tries - 1, 3)),
                   self.server_queue_retry * 8)
        place, job = target if target else (None, None)
        if not place or not job:
            return
        with self._lock:
            rec = self.running.get(name)
        if not rec or name in self._reconnecting:
            return
        # free up the wrong server: the client must be closed, not just
        # forgotten, or it stays in there eating a slot
        self._close_rec_pids(rec, "in the wrong server")
        with self._lock:
            self.running.pop(name, None)
            self._joined.discard(name)
            self._seen_in_game.pop(name, None)   # the re-queued alt must re-prove it
            self._join_verified.pop(name, None)
            self._reconnecting.add(name)
        self._status(name, "queued", "server full — waiting for main's server")
        self.log(f"[{name}] main's server {job} was full — this alt was put in "
                 f"server {alt_job or '?'} instead; closing it and retrying the "
                 f"main's server in {wait:.0f}s (attempt {tries}).")
        try:
            threading.Thread(target=self._queue_worker,
                             args=(name, acc, place, job, wait), daemon=True).start()
        except Exception as exc:
            with self._lock:      # the worker never ran, so release its claim
                self._reconnecting.discard(name)
            self.log(f"[{name}] could not start the server queue: {exc}")

    def _queue_worker(self, aname, acc, place, job, wait):
        """Sleep out the backoff, then re-request the main's exact server."""
        try:
            if self._stop.wait(wait):
                return                          # shutting down
            if self._alts_closed.is_set():
                return                          # main left/closed meanwhile
            self.on_follow(f"{aname} waiting in line for main's server…")
            self._status(aname, "reconnecting", f"place {place}")
            self.launch_account(acc, follow=(place, job))
            if self._alts_closed.is_set():
                # the main closed while we were launching — clean up our client
                self._close_one_alt(aname)
                return
            self._joined.add(aname)
            self._join_since[aname] = time.time()
            self._presence_verdict.pop(aname, None)   # stale verdict must not outlive the re-queue
            self.log(f"[{aname}] rejoined the queue for main's server "
                     f"(job {job}).")
        except Exception as exc:
            self.log(f"[{aname}] re-queue failed: {exc} — will retry.")
            self._status(aname, "error", str(exc))
        finally:
            with self._lock:
                self._reconnecting.discard(aname)

    def _close_one_alt(self, aname):
        """Kill just one alt's client and forget it (used when the main closes
        while that alt was mid-launch)."""
        with self._lock:
            rec = self.running.get(aname)
        self._close_rec_pids(rec, "after the main closed")
        with self._lock:
            self.running.pop(aname, None)
            self._joined.discard(aname)

    def _reconnect_worker(self, acc, place, job, reason=""):
        """Relaunch a dropped alt into the main's current server (async).

        A relaunch is NOT always instant, and the difference matters:

        - The client PROCESS died (crashed, closed, killed). Nothing is
          refusing us, so relaunch immediately - that is the whole point of
          auto-reconnect.
        - The client was kicked / removed from the game. If that happens very
          soon after joining, the server is rejecting the alt, and retrying
          straight away just re-enters the loop (observed: alts surviving only
          16-20s each pass, a relaunch every ~15s, which also drove the
          presence API into 429s). So that case waits a growing backoff.
        """
        aname = acc.get("name", "?")
        try:
            if self._alts_closed.is_set():
                return                          # main left/closed meanwhile
            process_death = "process gone" in reason
            joined_at = self._join_since.get(aname, 0)
            lived = time.time() - joined_at if joined_at else 1e9
            if not process_death and joined_at and lived < self.quick_fail_sec:
                fails = self._quick_fails.get(aname, 0) + 1
                self._quick_fails[aname] = fails
                wait = min(self.quick_fail_backoff * (2 ** min(fails - 1, 3)),
                           self.quick_fail_backoff * 8)
                self.log(f"[{aname}] dropped just {lived:.0f}s after joining "
                         f"(quick-fail #{fails}) — that is a rejection, not a random "
                         f"drop. Waiting {wait:.0f}s before trying again.")
                self.on_follow(f"{aname} rejected again — retrying in {wait:.0f}s")
                self._status(aname, "queued", f"retrying in {wait:.0f}s")
                if self._stop.wait(wait):
                    return
                if self._alts_closed.is_set():
                    return
            else:
                # either a genuine long-lived drop, or the client simply died:
                # in both cases retrying now is right
                self._quick_fails.pop(aname, None)
            self.on_follow(f"{aname} dropped — reconnecting…")
            self._status(aname, "reconnecting", f"place {place}")
            self.log(f"[{aname}] reconnecting (place {place}, job {job or 'n/a'})…")
            self.launch_account(acc, follow=(place, job))
            if self._alts_closed.is_set():
                self._close_one_alt(aname)      # main closed mid-launch
                return
            with self._lock:
                rec = self.running.get(aname) or {}
                apid = rec.get("pid")
                pids = set(rec.get("pids") or ([] if not apid else [apid]))
            # launch_account only warns when no client process appears; it still
            # returns a record. Treating that as a success produced
            # "reconnected (pid None)" and marked a dead alt as joined, so it
            # sat there doing nothing until Roblox kicked it.
            if not apid or not any(process_alive(p) for p in pids):
                with self._lock:
                    self.running.pop(aname, None)
                    self._joined.discard(aname)   # never leave a dead alt "joined"
                self.log(f"[{aname}] relaunch produced no live client "
                         "— not counted as rejoined; retrying on the next drop check.")
                self._status(aname, "error", "relaunch produced no client")
                return
            self._joined.add(aname)                    # back in the "joined" set
            self._join_since[aname] = time.time()
            self._seen_in_game.pop(aname, None)        # must re-prove it's in-game
            self._presence_verdict.pop(aname, None)    # stale verdict must not outlive the reconnect
            self._join_verified.pop(aname, None)  # measure this join fresh
            self._status(aname, "running (hidden)", f"reconnected pid {apid}")
            self.log(f"[{aname}] reconnected (pid {apid}).")
        except Exception as exc:
            self.log(f"[{aname}] reconnect failed: {exc} — will retry automatically.")
            self._status(aname, "error", str(exc))
        finally:
            with self._lock:
                self._reconnecting.discard(aname)

    # -- background throttling -------------------------------------------------
    def _apply_alt_throttle(self, rec):
        """Mark every PID of a hidden alt as EcoQoS / low priority (once each).

        Only alts: the main is the one the user is actually playing, so it keeps
        normal scheduling. The role/headless check lives here too, so no caller
        can accidentally throttle the main.
        """
        if not self.alt_throttle:
            return
        if rec.get("role") == "main" or not rec.get("headless"):
            return                            # never throttle what the user plays
        pids = rec.get("pids") or ([rec["pid"]] if rec.get("pid") else [])
        for pid in pids:
            if not pid:
                continue
            with self._lock:
                if pid in self._throttled_pids:
                    continue
            try:
                if throttle_process(pid):
                    with self._lock:
                        self._throttled_pids.add(pid)
            except Exception:
                pass                       # never let throttling break a launch

    def _main_is_foreground(self):
        """True when the user's active window belongs to the MAIN client.

        Only then would a jump blur the window they're using, so that's exactly
        when the jump is held off.
        """
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        try:
            fg_pid = window_pid(fg)
        except Exception:
            return False
        if not fg_pid:
            return False
        with self._lock:
            for rec in self.running.values():
                if rec.get("role") != "main":
                    continue
                pids = rec.get("pids") or ([rec["pid"]] if rec.get("pid") else [])
                if fg_pid in {p for p in pids if p}:
                    return True
        return False

    # -- anti-AFK jump loop ---------------------------------------------------
    def _start_jump_loop(self):
        """Start the jump thread (every `jump_interval` s each alt jumps)."""
        if self.jump_interval <= 0:
            return                      # disabled (jumpIntervalSec: 0)
        if (self.jump_method or "focus").strip().lower() == "none":
            # Nothing to run. Starting the thread anyway would just spin and log.
            self.log("Anti-AFK: DISABLED (jumpMethod=none) — no jumps, and your "
                     "active window is never touched.")
            return
        with self._lock:
            if self._jump_thread and self._jump_thread.is_alive():
                return
            th = threading.Thread(target=self._jump_loop, daemon=True)
            self._jump_thread = th
            th.start()
        if self.jump_method == "none":
            self.log("Anti-AFK: DISABLED (jumpMethod=none) — no jumps, and your "
                     "active window is never touched.")
            return
        how = ("a key posted straight to the hidden window — no focus taken, "
               "your main is never disturbed"
               if self.jump_method == "message"
               else "transparently focused jump (invisible, but the main loses "
                    "focus for a few ms)")
        self.log(f"Anti-AFK: alts will jump (Space) every {self.jump_interval:g}s "
                 f"via {how}.")
        if self.alt_throttle:
            self.log("Alts run as background work (EcoQoS + low priority) so their "
                     "hidden rendering costs less CPU/GPU.")
        if self.jump_method == "focus" and self.jump_skip_when_main_focused:
            self.log("Jumps are held off while your main client is the active window, "
                     f"so it never loses focus"
                     + (f" (forced at least every {self.jump_max_gap:g}s)."
                        if self.jump_max_gap > 0 else " (no forced jump)."))

    def _jump_loop(self):
        try:
            self._stop.wait(3.0)        # let clients boot/join before first jump
        except Exception:
            pass
        while not self._stop.is_set():
            if self.jump_interval <= 0:
                return                      # jumps were switched off while running
            try:
                self._jump_tick()
            except Exception as exc:    # keep the loop alive
                self.log(f"jump loop error: {exc}")
            self._stop.wait(max(0.25, self.jump_interval))

    def _jump_tick(self):
        """One round: make every running, joined alt jump once (staggered).

        Jumps only happen while the main is in a game (self._target set) and
        only for non-main clients with a window handle. Staggering keeps the
        focus handling from fighting across windows.
        """
        if not (self._target and self._target[0]):
            self._last_jump_at = time.time()   # gap clock only runs while in a game
            return                      # main not in a game -> alts aren't either
        if self.jump_method == "none":
            # Anti-AFK explicitly disabled.
            #
            # Worth having as a first-class option rather than a huge interval,
            # because the "focus" method costs the user something real - their
            # active window is briefly activated - and across an entire recorded
            # session there was not one AFK kick to prevent. Every disconnect
            # was a failed join (a full server) or a closed client. Paying a
            # focus steal for a kick that never happens is a bad trade, and this
            # makes saying so explicit.
            self._last_jump_at = time.time()
            return
        jump_fn = post_jump_alt_instance if self.jump_method == "message" else jump_alt_instance
        # The CLI jump restores + focuses the target window just like the
        # focus method (a real key only lands on the foreground window), so it
        # shares the hold-off and the advance warning.
        takes_focus = jump_fn is jump_alt_instance or self.jump_method == "cli"

        # A real key press can only be delivered to the foreground window, and
        # Windows promotes whatever takes focus to foreground. So jumping while
        # the user is in the main client is what makes the main blink/blur.
        # Hold off in that case, unless the alts have gone too long without one.
        if (takes_focus
                and self.jump_skip_when_main_focused
                and self._main_is_foreground()):
            since = time.time() - self._last_jump_at
            if self.jump_max_gap <= 0 or since < self.jump_max_gap:
                if time.time() - self._jump_skip_logged > 30:
                    self._jump_skip_logged = time.time()
                    self.log("Anti-AFK: main client is in use — holding off the jump so "
                             "your main never loses focus. Alts jump as soon as you're "
                             "not using it"
                             + (f" (or after {self.jump_max_gap:g}s, whichever comes "
                                f"first)." if self.jump_max_gap > 0 else "."))
                return
            self.log(f"Anti-AFK: no jump for {since:.0f}s — doing one even though the "
                     "main is in use, so alts can't idle out.")
        elif (takes_focus
                and not self.jump_skip_when_main_focused
                and self._main_is_foreground()
                and time.time() - self._jump_warned > 30):
            # Say it BEFORE it happens. Focus is not avoidable here, so the
            # honest thing is to warn rather than let the user discover it as a
            # flicker. JunkBeat/AntiAFK-Roblox does the same: "you'll get a
            # notification ~3 seconds before a window comes into focus, so you
            # can pause your work if needed."
            self._jump_warned = time.time()
            self.log("Anti-AFK: about to take focus for a moment — your main "
                     "client will blur briefly. Tick 'hold off while main active' "
                     "if you'd rather it never did that.")
            try:
                self.on_follow("Taking focus briefly for an anti-AFK jump…")
            except Exception:
                pass

        with self._lock:
            snapshot = dict(self.running)
        for name, rec in snapshot.items():
            if rec.get("role") == "main":
                continue                # the user controls the main as usual
            if name not in {a.get("name") for a in self.accounts}:
                # No longer a managed account - the user switched it off. Its
                # record can outlive the switch-off (the client is killed, but
                # the bookkeeping is not always cleared), and this loop used to
                # walk `running` and nothing else, so it kept poking a DISABLED
                # alt forever: "anti-AFK jump not delivered: its window is
                # gone" every ten minutes, indefinitely, for an account the
                # user had deliberately turned off.
                self._jump_notices.pop(name, None)
                continue
            if name in self._reconnecting:
                continue                # window may still be spawning
            # Re-resolve the window every tick, but by IDENTITY not by
            # position: a stale HWND dies when Roblox recreates its window,
            # yet blindly taking "the first window this pid owns" lands on a
            # hidden IME helper, which swallows the key silently.
            pids = set(rec.get("pids") or ([rec["pid"]] if rec.get("pid") else []))
            hwnd = client_window_for_pids(pids, prefer=rec.get("hwnd")) if pids else None
            if not hwnd:
                self._log_jump_problem(name, "its window is gone (client restarting?)")
                continue
            if hwnd != rec.get("hwnd"):
                with self._lock:
                    live = self.running.get(name)
                    if live:
                        old = live.get("hwnd")
                        live["hwnd"] = hwnd
                self._jump_notices.pop(name, None)
                self.log(f"[{name}] client window changed (hwnd {old} -> {hwnd}); "
                         "anti-AFK now targets the new one.")
            try:
                # The watcher re-minimizes every visible alt window every 2s.
                # A posted jump needs the window PRESENTED for a moment, so
                # without this the watcher can re-minimize the alt mid-send and
                # the key press is delivered to an iconic window and dropped -
                # which looks exactly like the jump "working a few times and
                # then stopping".
                report = {}
                self._jump_busy.add(name)
                try:
                    if self.jump_method == "cli":
                        # One anti-AFK jump through the headless CLI: it
                        # resolves the pid to a window, restores + focuses it,
                        # sends Space, and hides it again - reporting each step.
                        # Same focus cost as the focus method, same honesty.
                        pid = rec.get("pid") or next(iter(pids), None)
                        ok, report = self._jump_via_cli(name, pid)
                    elif jump_fn is jump_alt_instance:
                        ok = jump_fn(hwnd, self.no_render,
                                     settle=self.jump_settle,
                                     action=self.jump_action)
                    elif jump_fn is post_jump_alt_instance:
                        ok = jump_fn(hwnd, report=report)
                    else:
                        ok = jump_fn(hwnd)
                finally:
                    self._jump_busy.discard(name)
                if not ok:
                    # a silent failure here means the alt silently stops
                    # anti-AFK, which only shows up much later as a kick
                    self._log_jump_problem(
                        name, "could not take focus" if jump_fn is jump_alt_instance
                        else (report.get("reason") or "window rejected the message"))
            except Exception as exc:
                self._log_jump_problem(name, f"error: {exc}")
                continue
            self._jump_notices.pop(name, None)
            time.sleep(0.25)            # stagger: let focus settle / move on
        self._last_jump_at = time.time()

    def _log_jump_problem(self, name, why):
        """Report a jump problem at most once a minute per alt.

        With a 1s interval an unthrottled warning buries the log in hundreds of
        identical lines, which hides the one line that matters.
        """
        now = time.time()
        last = self._jump_notices.get(name, 0)
        if now - last < 60:
            return
        self._jump_notices[name] = now
        self.log(f"[{name}] anti-AFK jump not delivered: {why} "
                 f"(repeats are suppressed for 60s).")

    # -- launch-all ----------------------------------------------------------
    def launch_all(self, wait_each=True):
        main_accounts = [a for a in self.accounts if a.get("role") == "main"]
        alt_accounts = [a for a in self.accounts if a.get("role") != "main"]
        if not (main_accounts or alt_accounts):
            self.log("No accounts configured.")
            return

        if self.close_existing:
            pids = roblox_player_pids()
            if pids:
                for pid in pids:
                    kill_process(pid)
                self.log(f"Closed {len(pids)} existing RobloxPlayerBeta process(es).")
                time.sleep(2)

        if self.launch_mode == "simultaneous" and not self.place_id:
            self.log("simultaneous mode needs a Place ID set in config.json. "
                     "Switch Mode to 'follow' to open the client and pick a game manually.")
            return

        if self.launch_mode == "follow" and main_accounts:
            return self.follow_main_loop(main_accounts[0])
        return self._launch_simultaneous(main_accounts + alt_accounts)

    def _launch_simultaneous(self, ordered):
        self._target = (self.place_id, None) if self.place_id else None
        self._start_health_monitor()
        self._start_jump_loop()
        results = []
        for account in ordered:
            name = account.get("name", "?")
            try:
                results.append((name, "ok", self.launch_account(account)))
            except Exception as exc:
                self.log(f"[{name}] FAILED: {exc}")
                self._status(name, "error", str(exc))
                results.append((name, "error", str(exc)))
            time.sleep(1.5)  # let each client settle before the next launch
        return results

    # -- per-instance controls ------------------------------------------------
    def hide_instance(self, name):
        with self._lock:
            rec = self.running.get(name)
        if not rec:
            return False
        pid = rec.get("pid")
        hwnd = rec.get("hwnd")
        if not hwnd:
            hwnd, _ = find_window_for_pids(
                rec.get("pids") or ([pid] if pid else []))
        if hwnd:
            hide_window(hwnd, self.no_render)
            rec["hwnd"], rec["headless"] = hwnd, True
            self.log(f"[{name}] window hidden"
                     + (" (no-render)." if self.no_render else "."))
            return True
        self.log(f"[{name}] no window found to hide.")
        return False

    def show_instance(self, name):
        with self._lock:
            rec = self.running.get(name)
        if not rec:
            return False
        hwnd = rec.get("hwnd")
        if hwnd:
            show_window(hwnd)
            rec["headless"] = False
            self.log(f"[{name}] window shown.")
            return True
        self.log(f"[{name}] no window handle stored.")
        return False

    def forget_account(self, name):
        """Kill an account's client AND unregister it, for when it is switched
        off or deleted.

        `kill_instance` deliberately only kills, because "Kill Selected" is
        there to test auto-reconnect - the record has to survive so the health
        monitor notices the dead process and brings it back. That is the wrong
        behaviour when the user has switched an account OFF or deleted it: the
        record and the `_joined` marker both stayed behind, and because
        `_alt_account` no longer returns a disabled account, the health monitor
        skips it and nothing ever cleaned them up. Two consequences, both real:

          * the anti-AFK loop kept poking a dead window forever, logging
            "its window is gone" every jump interval for hours; and
          * re-enabling the account later would never launch it, because the
            stale `_joined` entry made the follow loop skip it and the health
            monitor only ever walks `running`.
        """
        killed = self.kill_instance(name)
        with self._lock:
            rec = self.running.pop(name, None)
        self._joined.discard(name)
        self._seen_in_game.pop(name, None)
        self._log_state.pop(name, None)
        self._client_logs.pop(name, None)
        self._alt_job.pop(name, None)
        self._launch_since.pop(name, None)
        self._join_since.pop(name, None)
        self._presence_verdict.pop(name, None)
        self._bad_readings.pop(name, None)
        self._jump_notices.pop(name, None)
        self._wrong_server_tries.pop(name, None)
        return killed or rec is not None

    def kill_instance(self, name):
        with self._lock:
            rec = self.running.get(name)
        if not rec:
            return False
        pids = rec.get("pids") or ([rec["pid"]] if rec.get("pid") else [])
        if pids:
            for p in pids:
                kill_process(p)
            self.log(f"[{name}] killed (pids {sorted(pids)}).")
            return True
        self.log(f"[{name}] no pid stored.")
        return False

    # -- watcher ---------------------------------------------------------------
    def _watch_pass(self):
        """One watcher pass: prune dead instances and re-assert the hidden /
        no-render state of every alt window. Split out of the loop so it can be
        exercised directly."""
        with self._lock:
            snapshot = dict(self.running)
        monitor_alive = self._health_thread is not None and self._health_thread.is_alive()
        for name, rec in snapshot.items():
            if name in self._jump_busy:
                # A jump has this window presented right now; re-minimizing it
                # here would swallow the key press. See _jump_tick.
                continue
            if not instance_alive(rec):
                if rec.get("role") != "main" and monitor_alive:
                    continue           # the health monitor owns alt drops/reconnects
                with self._lock:
                    self.running.pop(name, None)
                self._status(name, "closed", "")
                self.log(f"[{name}] process exited.")
                continue
            if rec.get("headless"):
                # background scheduling: keep alts cheap even if Roblox
                # spawns extra client processes behind our back
                self._apply_alt_throttle(rec)
                # only REAL client windows (visible ones) — enumerating every
                # top-level window would latch onto helper windows
                wins = visible_windows_for_pids(
                    rec.get("pids") or ([rec["pid"]] if rec.get("pid") else []))
                for hw in wins:
                    hide_window(hw, self.no_render)
                if wins:
                    rec["hwnd"] = wins[0]

    def _watcher_loop(self):
        """Re-hide alt windows if Roblox recreates them; prune dead PIDs."""
        while not self._stop.is_set():
            try:
                self._watch_pass()
            except Exception as exc:  # keep watcher alive
                self.log(f"watcher error: {exc}")
            self._stop.wait(2.0)

    def start_watcher(self):
        self._watcher = threading.Thread(target=self._watcher_loop, daemon=True)
        self._watcher.start()

    def restore_windows(self):
        for name, rec in list(self.running.items()):
            hwnd = rec.get("hwnd")
            if hwnd:
                show_window(hwnd)
                self.log(f"[{name}] window restored (visible).")

    def shutdown(self):
        """Cleanly shut down all threads and release all resources.

        Called on GUI close. Waits for background threads to exit so that
        PyInstaller's onefile temp-directory cleanup can succeed (no open
        file handles or running threads holding the temp dir locked).
        """
        self._stop.set()
        # Stop the overlay window first so its message-loop thread exits.
        try:
            self.stop_overlay()
        except Exception:
            pass
        # Restore windows before releasing the guard.
        if self.restore_on_exit:
            self.restore_windows()
        # Release the singleton mutex and cookie lock.
        self.stop_guard()
        # Give background threads a moment to observe _stop and exit.
        for _ in range(50):  # up to ~5 seconds
            alive = (
                (self._health_thread is not None and self._health_thread.is_alive())
                or (self._jump_thread is not None and self._jump_thread.is_alive())
                or (self._watcher is not None and self._watcher.is_alive())
            )
            if not alive:
                break
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class FileLog:
    """Mirror every log line into a file next to config.json.

    The GUI log pane disappears when the app closes, which makes "why did my
    alt reconnect?" unanswerable. This writes the same lines to
    `launcher.log` so a session can be reviewed afterwards.
    """

    def __init__(self, path, max_bytes=2_000_000):
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._fh = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > max_bytes:
                self.path.unlink()                    # keep it small
            self._fh = open(self.path, "a", encoding="utf-8", errors="replace")
            self.write(f"--- session started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
        except Exception:
            self._fh = None

    def write(self, line):
        if not self._fh:
            return
        with self._lock:
            try:
                self._fh.write(str(line) + "\n")
                self._fh.flush()
            except Exception:
                pass

    def close(self):
        with self._lock:
            try:
                if self._fh:
                    self._fh.close()
            except Exception:
                pass
            self._fh = None


def load_config(path):
    if not Path(path).exists():
        raise FileNotFoundError(f"Config file not found: {path}\n"
                                f"Copy config.example.json to {path} and fill in your cookies.")
    # utf-8-sig tolerates a UTF-8 BOM (Notepad saves JSON with one).
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def run_cli(args):
    config = load_config(args.config)
    accounts = config.get("accounts", [])
    place_id = args.place_id or config.get("placeId")
    mode = args.mode or config.get("launchMode", "follow")
    if mode == "simultaneous" and not place_id:
        print("ERROR: simultaneous mode requires a placeId "
              "(set 'placeId' in config or pass --place-id).")
        return 2

    file_log = FileLog(Path(args.config).resolve().parent / "launcher.log")
    file_log.write("=== CLI session ===")

    mgr = RobloxManager(
        accounts=accounts,
        place_id=place_id,
        headless_override=args.headless,
        guard=not args.no_guard,
        restore_on_exit=not args.no_restore,
        close_existing=config.get("closeExistingRoblox", False),
        launch_mode=args.mode or config.get("launchMode", "follow"),
        join_timeout=config.get("joinTimeoutSec", 900),
        jump_interval=config.get("jumpIntervalSec", 10),
        jump_method=config.get("jumpMethod", "focus"),
        jump_settle=config.get("jumpSettleSec", 0.35),
        jump_action=config.get("jumpAction", "jump"),
        jump_skip_when_main_focused=config.get("jumpSkipWhenMainFocused", True),
        jump_max_gap=config.get("jumpMaxGapSec", 180),
        alt_throttle=config.get("altThrottle", True),
        no_render=config.get("noRender", True),
        server_queue_retry=config.get("serverQueueRetrySec", 15),
        alt_presence_interval=config.get("altPresenceCheckSec", 6),
        alt_presence_idle_interval=config.get("altPresenceIdleSec", 25),
        join_method=config.get("joinMethod", "api"),
        alt_backend=config.get("altBackend", "protocol"),
        cli_python=config.get("cliPython", ""),
        cli_exe_path=config.get("cliExePath", ""),
        drop_confirm_count=config.get("dropConfirmCount", 2),
        drop_grace_sec=config.get("dropGraceSec", 30),
        join_confirm_sec=config.get("joinConfirmSec", 90),
        rejoin_pause_drops=config.get("rejoinPauseDrops", 6),
        rejoin_pause_window=config.get("rejoinPauseWindowSec", 120),
        rejoin_pause_sec=config.get("rejoinPauseSec", 60),
        relaunch_kill_wait=config.get("relaunchKillWaitSec", 2),
        quick_fail_sec=config.get("quickFailSec", 90),
        quick_fail_backoff=config.get("quickFailBackoffSec", 30),
        on_log=lambda msg: (file_log.write(msg),
                            print(msg) if msg else None),
        on_follow=lambda msg: print(f"[follow] {msg}") if msg else None,
    )

    if args.verify:
        print("Verifying accounts...")
        ok = True
        for acc in accounts:
            name = acc.get("name", "?")
            info = get_authenticated_user(acc.get("cookie", ""))
            if info:
                print(f"  OK  {name}: {info[0]} ({info[1]})")
            else:
                print(f"  BAD {name}: cookie invalid or expired")
                ok = False
        return 0 if ok else 1

    if args.ticket:
        matches = [a for a in accounts if a.get("name") == args.ticket] or accounts
        for acc in matches:
            try:
                print(f"{acc.get('name')}: {get_auth_ticket(acc['cookie'])}")
            except Exception as exc:
                print(f"{acc.get('name')}: ERROR {exc}")
        return 0

    mgr.start_guard()
    mgr.start_watcher()

    try:
        if args.launch_all:
            if mode == "follow":
                print("Follow mode: launching main and watching it.\n"
                      "Alts join when you join a game, close when you leave, "
                      "and stop when the main's window closes. Ctrl+C quits.\n")
            mgr.launch_all()
        elif args.launch_main:
            for acc in [a for a in accounts if a.get("role") == "main"] or accounts[:1]:
                mgr.launch_account(acc)
        elif args.launch:
            for want in args.launch:
                acc = next((a for a in accounts if a.get("name") == want), None)
                if not acc:
                    print(f"Unknown account: {want}")
                    continue
                mgr.launch_account(acc)
        else:
            print("Nothing to do. Use --launch-all / --launch-main / --launch NAME.")
            return 2

        if mode != "follow":
            print("\nInstances running. The guard is active (hold + watcher).")
            print("Press Ctrl+C to stop the program and restore hidden windows.\n")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        mgr.shutdown()
    return 0


# ---------------------------------------------------------------------------
# GUI (tkinter)
# ---------------------------------------------------------------------------

def run_gui(args):
    try:
        import tkinter as tk
        from tkinter import ttk, messagebox, simpledialog
    except ImportError:
        print("tkinter is not available. Use the CLI: python roblox_launcher.py --cli --launch-all")
        return 3

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("820x560")

    try:
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass

    # ---- state -------------------------------------------------------------
    config_path = str(Path(args.config).expanduser().resolve())  # absolute, stable target
    config = {"accounts": []}
    if Path(config_path).exists():
        try:
            config = load_config(config_path)
        except Exception as exc:
            messagebox.showwarning("Config", str(exc))
    accounts = config.get("accounts", [])
    # mirror everything to launcher.log next to config.json so a session can be
    # reviewed after the GUI is closed
    file_log = FileLog(Path(config_path).parent / "launcher.log")

    def save_config():
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2, ensure_ascii=False)

    # Row keys -> account dict
    row_map = {}

    def log(msg):
        file_log.write(msg)
        log_text.insert("end", msg + "\n")
        log_text.see("end")

    def set_status(name, state, detail=""):
        for row_id, acc in list(row_map.items()):
            if acc.get("name") == name:
                tree.set(row_id, "status", state)
        root.update_idletasks()

    def sync_enabled(mgr_accounts):
        """Keep the manager's list in step with the on/off checkboxes."""
        mgr_accounts[:] = [a for a in mgr_accounts if is_enabled(a)]

    def set_follow_status(msg):
        follow_status_var.set(msg or "")

    def sync_enabled():
        """Keep the manager's account list in step with the on/off checkboxes, so
        the follow loop never launches (or relaunches) a disabled alt."""
        mgr.accounts = [a for a in accounts if a.get("enabled", True)]

    mgr = RobloxManager(
        accounts=[a for a in accounts if a.get("enabled", True)],
        place_id=config.get("placeId", ""),
        close_existing=config.get("closeExistingRoblox", False),
        launch_mode=config.get("launchMode", "follow"),
        join_timeout=config.get("joinTimeoutSec", 900),
        jump_interval=config.get("jumpIntervalSec", 10),
        jump_method=config.get("jumpMethod", "focus"),
        jump_settle=config.get("jumpSettleSec", 0.35),
        jump_action=config.get("jumpAction", "jump"),
        jump_skip_when_main_focused=config.get("jumpSkipWhenMainFocused", True),
        jump_max_gap=config.get("jumpMaxGapSec", 180),
        alt_throttle=config.get("altThrottle", True),
        no_render=config.get("noRender", True),
        server_queue_retry=config.get("serverQueueRetrySec", 15),
        alt_presence_interval=config.get("altPresenceCheckSec", 6),
        alt_presence_idle_interval=config.get("altPresenceIdleSec", 25),
        join_method=config.get("joinMethod", "api"),
        alt_backend=config.get("altBackend", "protocol"),
        cli_python=config.get("cliPython", ""),
        cli_exe_path=config.get("cliExePath", ""),
        drop_confirm_count=config.get("dropConfirmCount", 2),
        drop_grace_sec=config.get("dropGraceSec", 30),
        join_confirm_sec=config.get("joinConfirmSec", 90),
        rejoin_pause_drops=config.get("rejoinPauseDrops", 6),
        rejoin_pause_window=config.get("rejoinPauseWindowSec", 120),
        rejoin_pause_sec=config.get("rejoinPauseSec", 60),
        relaunch_kill_wait=config.get("relaunchKillWaitSec", 2),
        quick_fail_sec=config.get("quickFailSec", 90),
        quick_fail_backoff=config.get("quickFailBackoffSec", 30),
        on_log=log,
        on_status=set_status,
        on_follow=set_follow_status,
    )

    # ---- top bar -------------------------------------------------------------
    top = ttk.Frame(root, padding=(8, 8, 8, 4))
    top.pack(fill="x")
    ttk.Label(top, text="Mode:").pack(side="left")
    mode_var = tk.StringVar(value=config.get("launchMode", "follow"))
    mode_combo = ttk.Combobox(top, textvariable=mode_var, state="readonly", width=14,
                              values=("follow", "simultaneous"))
    mode_combo.pack(side="left", padx=(4, 8))
    mode_combo.bind("<<ComboboxSelected>>",
                    lambda e: (config.__setitem__("launchMode", mode_var.get()), save_config()))

    ttk.Button(top, text="Launch Main", command=lambda: launch_thread("main")).pack(side="left", padx=2)
    ttk.Button(top, text="Launch All", command=lambda: launch_thread("all")).pack(side="left", padx=2)

    # ---- anti-AFK row --------------------------------------------------------
    def apply_jump_settings(*_):
        """Save the anti-AFK controls and push them into the live manager."""
        try:
            interval = max(0, int(float(jump_every_var.get() or 0)))
        except ValueError:
            interval = 10
        try:
            max_gap = max(0, int(float(jump_gap_var.get() or 0)))
        except ValueError:
            max_gap = 180
        jump_every_var.set(str(interval))
        jump_gap_var.set(str(max_gap))
        method = jump_method_var.get()
        skip_main = bool(skip_main_var.get())
        throttle = bool(throttle_var.get())
        config["jumpIntervalSec"] = interval
        config["jumpMethod"] = method
        config["jumpSkipWhenMainFocused"] = skip_main
        config["jumpMaxGapSec"] = max_gap
        config["altThrottle"] = throttle
        config["noRender"] = bool(no_render_var.get())
        mgr.jump_interval = float(interval)
        mgr.jump_method = method
        mgr.jump_skip_when_main_focused = skip_main
        mgr.jump_max_gap = float(max_gap)
        mgr.alt_throttle = throttle
        mgr.no_render = bool(no_render_var.get())
        if interval > 0 and not (mgr._jump_thread and mgr._jump_thread.is_alive()):
            mgr._start_jump_loop()            # jumps were off -> start them now
        save_config()
        log(f"Anti-AFK: every {interval}s | method={method} | "
            f"hold off while main active={'yes' if skip_main else 'no'} | "
            f"max gap={max_gap}s | alt EcoQoS={'on' if throttle else 'off'} | "
            f"no-render alts={'on' if mgr.no_render else 'off'}.")

    afk = ttk.Frame(root, padding=(8, 0, 8, 2))
    afk.pack(fill="x")
    ttk.Label(afk, text="Anti-AFK:").pack(side="left")
    ttk.Label(afk, text="every").pack(side="left", padx=(4, 2))
    jump_every_var = tk.StringVar(value=str(config.get("jumpIntervalSec", 10)))
    every_spin = ttk.Spinbox(afk, from_=0, to=600, increment=1, width=4,
                             textvariable=jump_every_var, command=apply_jump_settings)
    every_spin.pack(side="left")
    every_spin.bind("<FocusOut>", apply_jump_settings)
    every_spin.bind("<Return>", apply_jump_settings)
    ttk.Label(afk, text="s").pack(side="left", padx=(2, 8))
    jump_method_var = tk.StringVar(value=config.get("jumpMethod", "focus"))
    method_combo = ttk.Combobox(afk, textvariable=jump_method_var, state="readonly",
                                width=9, values=("focus", "message", "none"))
    method_combo.pack(side="left")
    method_combo.bind("<<ComboboxSelected>>", apply_jump_settings)
    skip_main_var = tk.BooleanVar(value=bool(config.get("jumpSkipWhenMainFocused", True)))
    ttk.Checkbutton(afk, text="hold off while main active", variable=skip_main_var,
                    command=apply_jump_settings).pack(side="left", padx=6)
    ttk.Label(afk, text="max gap").pack(side="left")
    jump_gap_var = tk.StringVar(value=str(config.get("jumpMaxGapSec", 180)))
    gap_spin = ttk.Spinbox(afk, from_=0, to=3600, increment=30, width=5,
                           textvariable=jump_gap_var, command=apply_jump_settings)
    gap_spin.pack(side="left")
    gap_spin.bind("<FocusOut>", apply_jump_settings)
    gap_spin.bind("<Return>", apply_jump_settings)
    ttk.Label(afk, text="s").pack(side="left", padx=(2, 8))
    throttle_var = tk.BooleanVar(value=bool(config.get("altThrottle", True)))
    ttk.Checkbutton(afk, text="alts EcoQoS", variable=throttle_var,
                    command=apply_jump_settings).pack(side="left", padx=(6, 0))
    no_render_var = tk.BooleanVar(value=bool(config.get("noRender", True)))
    ttk.Checkbutton(afk, text="no-render alts (minimize)", variable=no_render_var,
                    command=apply_jump_settings).pack(side="left", padx=(6, 0))
    ttk.Label(afk, text="(max gap 0 = never jump while the main is active)",
              foreground="#555").pack(side="left", padx=8)

    # usage hint
    ttk.Label(root, text="Click the ☑/☐ column (or select rows + Space) to switch an "
                         "account on/off — off accounts are skipped by Launch All. "
                         "Launch All opens the main client — pick any game inside it: "
                         "alts join you, close when you leave, close all when you quit.",
              foreground="#555", padding=(8, 0)).pack(fill="x")

    # follow-mode status line
    follow_status_var = tk.StringVar(value="")
    ttk.Label(root, textvariable=follow_status_var, foreground="#0a7", padding=(8, 0)).pack(fill="x")

    # ---- accounts table ------------------------------------------------------
    mid = ttk.Frame(root, padding=(8, 4, 8, 4))
    mid.pack(fill="both", expand=True)

    cols = ("on", "name", "user", "role", "headless", "status", "pid")
    tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
    headers = {"on": "On", "name": "Name", "user": "Account", "role": "Role",
               "headless": "Headless", "status": "Status", "pid": "PID"}
    widths = {"on": 34, "name": 105, "user": 120, "role": 55, "headless": 68,
              "status": 140, "pid": 62}
    for col in cols:
        tree.heading(col, text=headers[col])
        tree.column(col, width=widths[col], anchor="w")
    tree.column("on", anchor="center")
    tree.tag_configure("off", foreground="#888888")

    tree_scroll = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=tree_scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    tree_scroll.pack(side="right", fill="y")

    def is_enabled(acc):
        """An account is 'on' unless it was explicitly switched off.

        Stored as "enabled" in config.json; accounts saved before this existed
        have no key, so they default to on (nothing silently disappears).
        """
        return bool(acc.get("enabled", True))

    def refresh_table():
        for row_id in list(row_map):
            tree.delete(row_id)
        row_map.clear()
        for acc in accounts:
            row = tree.insert("", "end", values=(
                "☑" if is_enabled(acc) else "☐",     # On
                acc.get("name", "?"),
                acc.get("username", acc.get("displayName", "")),
                acc.get("role", "alt"),
                "yes" if acc.get("headless", acc.get("role") != "main") else "no",
                acc.get("_status", ""),
                "",
            ), tags=() if is_enabled(acc) else ("off",))
            row_map[row] = acc

    def set_enabled(names, enabled):
        """Turn accounts on/off, persist it, and stop the ones just switched off."""
        for acc in accounts:
            if acc.get("name") in names and is_enabled(acc) != enabled:
                acc["enabled"] = enabled
                if not enabled:
                    # don't leave a running client behind for an "off" account,
                    # and don't leave its bookkeeping behind either
                    try:
                        mgr.forget_account(acc.get("name"))
                    except Exception:
                        pass
        save_config()
        # the follow loop iterates mgr.accounts, so it must not resurrect alts
        # the user just switched off
        sync_enabled()
        refresh_table()
        on = sum(1 for a in accounts if is_enabled(a))
        log(f"{'Enabled' if enabled else 'Disabled'} {len(names)} account(s) — "
            f"{on}/{len(accounts)} on.")

    def toggle_selected():
        """Space/Return flips the checkbox on the highlighted rows."""
        rows = tree.selection()
        if not rows:
            return
        accs = [row_map[r] for r in rows if r in row_map]
        # if any are off, turn them all on; otherwise turn them all off
        target = not all(is_enabled(a) for a in accs)
        set_enabled({a.get("name") for a in accs}, target)

    def on_row_click(event):
        if tree.identify_region(event.x, event.y) == "heading":
            return
        row_id = tree.identify_row(event.y)
        if not row_id:
            return
        on_col = tree.identify_column(event.x)          # "#1" == first column
        tree.selection_set(row_id)
        if on_col == "#1":
            acc = row_map.get(row_id)
            if acc:
                set_enabled({acc.get("name")}, not is_enabled(acc))

    tree.bind("<Button-1>", on_row_click)
    tree.bind("<space>", lambda e: (toggle_selected(), "break")[1])
    tree.bind("<Return>", lambda e: (toggle_selected(), "break")[1])

    def verify_thread():
        threading.Thread(target=_do_verify, daemon=True).start()

    def _do_verify():
        log("Verifying account cookies...")
        for acc in accounts:
            name = acc.get("name", "?")
            info = get_authenticated_user(acc.get("cookie", ""))
            if info:
                acc["username"], acc["displayName"], acc["userId"] = info
                log(f"  OK  {name}: {info[0]} ({info[1]})")
            else:
                acc["username"] = ""
                log(f"  BAD {name}: cookie invalid or expired.")
            root.after(0, refresh_table)
        pid = str(config.get("placeId", "") or "").strip()
        if pid:
            pname = get_place_name(pid)
            if pname:
                log(f"Place {pid}: {pname}")
        log("Verification done.")

    # ---- buttons ---------------------------------------------------------------
    btns = ttk.Frame(root, padding=(8, 0, 8, 4))
    btns.pack(fill="x")

    def add_account():
        dialog = tk.Toplevel(root)
        dialog.title("Add Account")
        dialog.geometry("470x350")
        dialog.transient(root)
        dialog.grab_set()

        ttk.Label(dialog, text="Display name:").grid(row=0, column=0, sticky="w", padx=8, pady=(10, 2))
        name_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=name_var, width=45).grid(row=0, column=1, padx=8, pady=(10, 2))

        ttk.Label(dialog, text="Role:").grid(row=1, column=0, sticky="w", padx=8, pady=2)
        role_var = tk.StringVar(value="main")
        ttk.Radiobutton(dialog, text="Main (visible GUI)", variable=role_var, value="main").grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(dialog, text="Alt (hidden / headless)", variable=role_var, value="alt").grid(row=2, column=1, sticky="w")
        headless_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(dialog, text="Force headless (hide window)", variable=headless_var).grid(row=3, column=1, sticky="w")

        ttk.Label(dialog, text=".ROBLOSECURITY cookie:").grid(row=4, column=0, sticky="nw", padx=8, pady=(10, 2))
        cookie_text = tk.Text(dialog, width=45, height=6)
        cookie_text.grid(row=4, column=1, padx=8, pady=(10, 2))

        def browser_login(cookie_widget=cookie_text):
            # Sub-dialog showing instructions + live status while we log in.
            login_win = tk.Toplevel(dialog)
            login_win.title("Login with browser")
            login_win.geometry("440x190")
            login_win.transient(dialog)
            status_var = tk.StringVar(value="Opening browser…")
            ttk.Label(login_win, text=(
                "A browser window will open at roblox.com/login.\n\n"
                "Log in (complete 2-factor verification if asked),\n"
                "then wait — the cookie is captured automatically.\n"
                "The browser closes by itself when it's done.\n\n")).pack(padx=12, pady=(12, 0), anchor="w")
            ttk.Label(login_win, textvariable=status_var, foreground="#0a7").pack(padx=12, anchor="w")
            btn_row = ttk.Frame(login_win)
            btn_row.pack(fill="x", padx=12, pady=12)

            cancel_evt = threading.Event()

            def cancel():
                cancel_evt.set()
                status_var.set("Cancelling…")

            ttk.Button(btn_row, text="Cancel", command=cancel).pack(side="left")
            ttk.Button(btn_row, text="Close", command=login_win.destroy).pack(side="right")

            def worker():
                try:
                    import cookie_login
                except ImportError as exc:
                    status_var.set(f"cookie_login.py missing: {exc}")
                    return
                try:
                    cookie = cookie_login.login_and_get_cookie(
                        on_status=lambda m: root.after(0, status_var.set, m),
                        cancel=cancel_evt, timeout=420)
                    if cookie:
                        root.after(0, lambda: (
                            cookie_widget.delete("1.0", "end"),
                            cookie_widget.insert("1.0", cookie)))
                        root.after(0, lambda: status_var.set("Cookie captured ✔ — click Close"))
                    else:
                        root.after(0, lambda: status_var.set("Login cancelled / timed out."))
                except Exception as exc:
                    root.after(0, lambda: status_var.set(f"Error: {exc}"))

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(dialog, text="Login with browser (capture cookie)…",
                   command=browser_login).grid(row=4, column=1, sticky="w", padx=8)

        ttk.Label(dialog, text="Optional job ID (server):").grid(row=5, column=0, sticky="w", padx=8, pady=2)
        job_var = tk.StringVar()
        ttk.Entry(dialog, textvariable=job_var, width=45).grid(row=5, column=1, padx=8, pady=2)

        ttk.Label(dialog, text="FPS cap (0 = uncapped):").grid(row=6, column=0, sticky="w", padx=8, pady=2)
        fps_var = tk.IntVar(value=60)
        ttk.Spinbox(dialog, from_=0, to=1000, increment=1, width=42,
                    textvariable=fps_var).grid(row=6, column=1, padx=8, pady=2, sticky="w")

        ttk.Label(dialog, text="Graphics level:").grid(row=7, column=0, sticky="w", padx=8, pady=2)
        quality_var = tk.StringVar(value="lowest")
        quality_combo = ttk.Combobox(dialog, textvariable=quality_var, state="readonly",
                                     width=42,
                                     values=["automatic", "lowest", "low", "medium", "high", "ultra"])
        quality_combo.grid(row=7, column=1, padx=8, pady=2, sticky="w")

        def on_add():
            cookie = cookie_text.get("1.0", "end").strip()
            if not name_var.get().strip() or not cookie:
                messagebox.showerror("Add Account", "Name and cookie are required.")
                return
            accounts.append({
                "name": name_var.get().strip(),
                "role": role_var.get(),
                "headless": bool(headless_var.get()) or role_var.get() != "main",
                "cookie": cookie,
                "jobId": job_var.get().strip() or None,
                "fps": int(fps_var.get()),
                "quality_level": quality_var.get(),
                "enabled": True,
            })
            save_config()
            refresh_table()
            dialog.destroy()
            verify_thread()

        ttk.Button(dialog, text="Add & Verify", command=on_add).grid(row=6, column=1, sticky="e", padx=8, pady=10)

    def remove_selected():
        for row_id in tree.selection():
            acc = row_map.get(row_id)
            if acc:
                try:
                    mgr.forget_account(acc.get("name"))
                except Exception:
                    pass
                accounts.remove(acc)
        save_config()
        refresh_table()

    def edit_selected():
        """Edit the selected account(s): rename, change role, headless, or job."""
        accs = selected_accounts()
        if not accs:
            messagebox.showinfo("Edit Account", "Select an account first (click a row).")
            return
        acc = accs[0]
        if len(accs) > 1:
            extra = ", ".join(a.get("name", "?") for a in accs[1:])
            if not messagebox.askyesno(
                    "Edit Account",
                    f"{len(accs)} accounts selected.\n\nEdit '{acc.get('name')}' only?\n\n"
                    f"({extra})\n\nChoose 'No' to rename all selected accounts at once."):
                # batch rename
                base = acc.get("name", "?")
                new = simpledialog.askstring("Rename", "New name for all selected:",
                                              initialvalue=base, parent=root)
                if new:
                    new = new.strip()
                    if not new:
                        return
                    for i, a in enumerate(accs, start=1):
                        a["name"] = new if len(accs) == 1 else f"{new}{i}"
                    save_config()
                    refresh_table()
                    log(f"Renamed {len(accs)} accounts to '{new}'.")
                return

        dialog = tk.Toplevel(root)
        dialog.title("Edit Account")
        dialog.geometry("470x330")
        dialog.transient(root)
        dialog.grab_set()

        ttk.Label(dialog, text="Display name:").grid(row=0, column=0, sticky="w", padx=8, pady=(10, 2))
        name_var = tk.StringVar(value=acc.get("name", ""))
        ttk.Entry(dialog, textvariable=name_var, width=45).grid(row=0, column=1, padx=8, pady=(10, 2))

        ttk.Label(dialog, text="Role:").grid(row=1, column=0, sticky="w", padx=8, pady=2)
        role_var = tk.StringVar(value=acc.get("role", "alt"))
        ttk.Radiobutton(dialog, text="Main (visible GUI)", variable=role_var, value="main").grid(row=1, column=1, sticky="w")
        ttk.Radiobutton(dialog, text="Alt (hidden / headless)", variable=role_var, value="alt").grid(row=2, column=1, sticky="w")
        headless_var = tk.BooleanVar(value=bool(acc.get("headless", acc.get("role") != "main")))
        ttk.Checkbutton(dialog, text="Force headless (hide window)", variable=headless_var).grid(row=3, column=1, sticky="w")

        ttk.Label(dialog, text=".ROBLOSECURITY cookie:").grid(row=4, column=0, sticky="nw", padx=8, pady=(10, 2))
        cookie_text = tk.Text(dialog, width=45, height=5)
        cookie_text.grid(row=4, column=1, padx=8, pady=(10, 2))
        cookie_text.insert("1.0", acc.get("cookie", ""))

        ttk.Label(dialog, text="Optional job ID (server):").grid(row=5, column=0, sticky="w", padx=8, pady=2)
        job_var = tk.StringVar(value=acc.get("jobId") or "")
        ttk.Entry(dialog, textvariable=job_var, width=45).grid(row=5, column=1, padx=8, pady=2)

        ttk.Label(dialog, text="FPS cap (0 = uncapped):").grid(row=6, column=0, sticky="w", padx=8, pady=2)
        fps_var = tk.IntVar(value=int(acc.get("fps", 60)))
        ttk.Spinbox(dialog, from_=0, to=1000, increment=1, width=42,
                    textvariable=fps_var).grid(row=6, column=1, padx=8, pady=2, sticky="w")

        ttk.Label(dialog, text="Graphics level:").grid(row=7, column=0, sticky="w", padx=8, pady=2)
        quality_var = tk.StringVar(value=acc.get("quality_level", "lowest"))
        quality_combo = ttk.Combobox(dialog, textvariable=quality_var, state="readonly",
                                     width=42,
                                     values=["automatic", "lowest", "low", "medium", "high", "ultra"])
        quality_combo.grid(row=7, column=1, padx=8, pady=2, sticky="w")

        on_var = tk.BooleanVar(value=is_enabled(acc))
        ttk.Checkbutton(dialog, text="Enabled (launch with the others)",
                        variable=on_var).grid(row=8, column=1, sticky="w", padx=8)

        def on_save():
            new_name = name_var.get().strip()
            cookie = cookie_text.get("1.0", "end").strip()
            if not new_name:
                messagebox.showerror("Edit Account", "Name cannot be empty.")
                return
            if not cookie:
                messagebox.showerror("Edit Account", "Cookie cannot be empty.")
                return
            old_name = acc.get("name")
            acc["name"] = new_name
            acc["role"] = role_var.get()
            acc["headless"] = bool(headless_var.get()) or role_var.get() != "main"
            acc["cookie"] = cookie
            acc["jobId"] = job_var.get().strip() or None
            acc["fps"] = int(fps_var.get())
            acc["quality_level"] = quality_var.get()
            acc["enabled"] = bool(on_var.get())
            if old_name != new_name:
                # keep the running instance and the manager in sync
                with mgr._lock:
                    rec = mgr.running.pop(old_name, None)
                    if rec:
                        mgr.running[new_name] = rec
                mgr._joined.discard(old_name)
                mgr._joined.add(new_name)
                mgr._join_since[new_name] = mgr._join_since.pop(old_name, time.time())
                mgr._alt_uid[new_name] = mgr._alt_uid.pop(old_name, 0) or \
                    mgr._alt_uid.get(new_name, 0)
            save_config()
            refresh_table()
            log(f"[{new_name}] updated.")
            dialog.destroy()

        ttk.Button(dialog, text="Save", command=on_save).grid(row=7, column=1, sticky="e", padx=8, pady=10)

    def selected_accounts():
        seen = []
        for row_id in tree.selection():
            acc = row_map.get(row_id)
            if acc and acc not in seen:
                seen.append(acc)
        return seen

    def launch_thread(mode):
        threading.Thread(target=lambda: _do_launch(mode), daemon=True).start()

    def _do_launch(mode):
        mgr.place_id = str(config.get("placeId", "") or "").strip()
        mgr.launch_mode = mode_var.get()
        if not mgr.place_id and mgr.launch_mode == "simultaneous":
            log("simultaneous mode needs a Place ID — add \"placeId\" to config.json "
                "(or switch Mode to 'follow' and pick a game in the client).")
            return
        save_config()
        if mode == "all":
            # only launch accounts that are switched on
            mgr.accounts = [a for a in accounts if is_enabled(a)]
            disabled = [a.get("name", "?") for a in accounts if not is_enabled(a)]
            if disabled:
                log(f"Skipping {len(disabled)} disabled account(s): {', '.join(disabled)}")
            mgr.launch_all()
        elif mode == "main":
            mains = [a for a in accounts
                     if a.get("role") == "main" and is_enabled(a)] or \
                    [a for a in accounts if a.get("role") == "main"][:1]
            for acc in mains:
                try:
                    mgr.launch_account(acc)
                except Exception as exc:
                    log(f"[{acc.get('name')}] FAILED: {exc}")
        else:
            for acc in selected_accounts():
                if not is_enabled(acc):
                    log(f"[{acc.get('name')}] is switched off — not launching.")
                    continue
                try:
                    mgr.launch_account(acc)
                except Exception as exc:
                    log(f"[{acc.get('name')}] FAILED: {exc}")

    ttk.Button(btns, text="Add Account", command=add_account).pack(side="left", padx=2)
    ttk.Button(btns, text="Edit Selected", command=edit_selected).pack(side="left", padx=2)
    ttk.Button(btns, text="Remove Selected", command=remove_selected).pack(side="left", padx=2)
    ttk.Button(btns, text="Launch Selected",
               command=lambda: launch_thread("selected")).pack(side="left", padx=2)

    def hide_show_selected():
        accs = selected_accounts()
        for acc in accs:
            name = acc.get("name")
            rec = mgr.running.get(name)
            if rec and rec.get("headless"):
                mgr.show_instance(name)
            else:
                mgr.hide_instance(name)

    ttk.Button(btns, text="Hide/Show Selected", command=hide_show_selected).pack(side="left", padx=2)
    ttk.Button(btns, text="Kill Selected", command=lambda: [mgr.kill_instance(a.get("name")) for a in selected_accounts()]).pack(side="left", padx=2)
    ttk.Button(btns, text="On/Off Selected", command=toggle_selected).pack(side="left", padx=2)
    ttk.Button(btns, text="All On", command=lambda: set_enabled({a.get("name") for a in accounts}, True)).pack(side="left", padx=2)
    ttk.Button(btns, text="All Off", command=lambda: set_enabled({a.get("name") for a in accounts}, False)).pack(side="left", padx=2)
    ttk.Button(btns, text="Verify Cookies", command=verify_thread).pack(side="right", padx=2)

    # ---- on-screen status overlay ---------------------------------------------
    # A click-through top-right readout of every account: name, the server it is
    # actually in, and its state. It must never take focus, so it cannot
    # interfere with the anti-AFK jump.
    overlay_row = ttk.Frame(root)
    overlay_row.pack(fill="x", padx=8, pady=(0, 4))
    overlay_var = tk.BooleanVar(value=bool(config.get("statusOverlay", True)))
    overlay_alpha_var = tk.IntVar(value=int(config.get("statusOverlayAlpha", 210)))

    def apply_overlay():
        try:
            config["statusOverlay"] = bool(overlay_var.get())
            save_config()
        except Exception as exc:
            log(f"could not save the overlay setting: {exc}")
        if overlay_var.get():
            mgr.start_overlay(alpha=overlay_alpha_var.get())
        else:
            mgr.stop_overlay()

    ttk.Checkbutton(overlay_row, text="status overlay (top-right, click-through)",
                    variable=overlay_var, command=apply_overlay).pack(side="left")
    ttk.Label(overlay_row, text="opacity").pack(side="left", padx=(10, 2))

    def apply_overlay_alpha(*_a):
        try:
            v = max(20, min(255, int(overlay_alpha_var.get())))
        except Exception:
            return
        config["statusOverlayAlpha"] = v
        try:
            save_config()
        except Exception:
            pass
        ov = getattr(mgr, "_overlay", None)
        if ov is not None and ov.hwnd:
            try:
                user32.SetLayeredWindowAttributes(ov.hwnd, 0, v, LWA_ALPHA)
            except Exception:
                pass

    alpha_spin = ttk.Spinbox(overlay_row, from_=20, to=255, increment=10, width=5,
                             textvariable=overlay_alpha_var,
                             command=apply_overlay_alpha)
    alpha_spin.pack(side="left")
    alpha_spin.bind("<FocusOut>", apply_overlay_alpha)
    alpha_spin.bind("<Return>", apply_overlay_alpha)

    # ---- log --------------------------------------------------------------------
    log_frame = ttk.LabelFrame(root, text="Log", padding=(6, 4))
    log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
    log_text = tk.Text(log_frame, height=10, wrap="word", state="normal")
    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_text.pack(side="left", fill="both", expand=True)
    log_scroll.pack(side="right", fill="y")

    # Startup diagnostics: warn if Roblox isn't installed or config is missing.
    _exe = find_player_exe()
    if _exe:
        log(f"Roblox client found: {_exe}")
    else:
        log("WARNING: RobloxPlayerBeta.exe not found on this machine. "
            "Launches will fall back to the bootstrapper (downloads its own "
            "client from setup.rbxcdn.com) or fail with a clear error.")

    if not Path(config_path).exists():
        log(f"WARNING: {config_path} not found. Create config.json with your "
            "accounts before launching. See config.example.json.")

    # ---- finalize ----------------------------------------------------------------
    refresh_table()
    mgr.start_guard()
    mgr.start_watcher()
    log(f"Guard active. Mode: {mgr.launch_mode}"
        + (f", Place ID: {mgr.place_id}" if mgr.place_id else " (no Place ID — pick a game in the client)"))
    if overlay_var.get():
        mgr.start_overlay(alpha=overlay_alpha_var.get())
    root.after(300, verify_thread)

    def on_close():
        # never let a hiccup in shutdown keep the window alive
        try:
            mgr.stop_overlay()
        except Exception:
            pass
        try:
            mgr.shutdown()
        except Exception:
            pass
        file_log.write("=== GUI closed ===")
        file_log.close()
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--cli", action="store_true", help="run in CLI mode (no GUI)")
    # When frozen (PyInstaller exe), default the config to the exe's own folder
    # so double-clicking the exe finds config.json next to it, no matter what
    # working directory it is launched from.
    frozen_default = (str(Path(sys.executable).resolve().parent / DEFAULT_CONFIG)
                      if getattr(sys, "frozen", False) else DEFAULT_CONFIG)
    parser.add_argument("--config", default=frozen_default, help="path to config JSON")
    parser.add_argument("--place-id", help="override place ID")
    parser.add_argument("--verify", action="store_true", help="check cookies against Roblox")
    parser.add_argument("--ticket", metavar="NAME", help="print an auth ticket for an account")
    parser.add_argument("--launch-all", action="store_true", help="launch main + all alts")
    parser.add_argument("--launch-main", action="store_true", help="launch the main account only")
    parser.add_argument("--launch", nargs="+", metavar="NAME", help="launch specific accounts")
    parser.add_argument("--headless", type=int, choices=[0, 1], help="override headless (CLI)")
    parser.add_argument("--mode", choices=["follow", "simultaneous"],
                        help="follow = alts join main's server; simultaneous = all join place")
    parser.add_argument("--no-guard", action="store_true", help="disable multi-instance guard")
    parser.add_argument("--no-restore", action="store_true", help="do not show windows on exit")
    args = parser.parse_args()

    if args.cli:
        sys.exit(run_cli(args))
    sys.exit(run_gui(args))


if __name__ == "__main__":
    main()