#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
============================================================================
 Browser cookie capture for Roblox (stdlib-only CDP client)
============================================================================
 Opens Microsoft Edge / Google Chrome pointed at the Roblox login page and
 captures the .ROBLOSECURITY session cookie straight out of that browser
 session, using the Chrome DevTools Protocol over a raw WebSocket.

 * No external packages.  Pure stdlib (socket, urllib, subprocess).
 * The browser and its temp profile are disposed of afterwards.
 * The cookie never leaves this machine.

 Usage:
     import cookie_login
     cookie = cookie_login.login_and_get_cookie(on_status=print)
============================================================================
"""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

LOGIN_URL = "https://www.roblox.com/login"
COOKIE_NAME = ".ROBLOSECURITY"


class LoginCancelled(Exception):
    pass


def find_chromium_browser():
    env = os.environ
    candidates = [
        env.get("ProgramFiles(x86)", "") + r"\Microsoft\Edge\Application\msedge.exe",
        env.get("ProgramFiles", "") + r"\Microsoft\Edge\Application\msedge.exe",
        env.get("ProgramFiles", "") + r"\Google\Chrome\Application\chrome.exe",
        env.get("ProgramFiles(x86)", "") + r"\Google\Chrome\Application\chrome.exe",
        env.get("LocalAppData", "") + r"\Google\Chrome\Application\chrome.exe",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# ---------------------------------------------------------------------------
# Minimal WebSocket client (RFC 6455) + CDP
# ---------------------------------------------------------------------------

def _recv_exact(sock, n):
    chunks = []
    while n > 0:
        data = sock.recv(n)
        if not data:
            raise RuntimeError("connection closed while reading frame")
        chunks.append(data)
        n -= len(data)
    return b"".join(chunks)


def _ws_handshake(sock, host, port, path):
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: http://localhost\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("connection closed during handshake")
        response += chunk
    status_line = response.split(b"\r\n", 1)[0]
    if b"101" not in status_line:
        raise RuntimeError(f"WebSocket handshake failed: {status_line.decode('ascii', 'ignore')}")


def _ws_send(sock, payload):
    sock.sendall(b"\x81")  # FIN + text frame
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        sock.sendall(bytes([0x80 | n]))
    elif n < 65536:
        sock.sendall(bytes([0x80 | 126]) + struct.pack(">H", n))
    else:
        sock.sendall(bytes([0x80 | 127]) + struct.pack(">Q", n))
    sock.sendall(mask)
    sock.sendall(bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_recv(sock):
    h1, h2 = _recv_exact(sock, 2)
    if h1 & 0x80 == 0:
        raise RuntimeError("fragmented messages are not supported")
    length = h2 & 0x7F
    masked = bool(h2 & 0x80)
    if length == 126:
        length = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mask = _recv_exact(sock, 4) if masked else None
    payload = _recv_exact(sock, length)
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    opcode = h1 & 0x0F
    if opcode == 0x8:  # close
        raise RuntimeError("WebSocket closed by peer")
    if opcode == 0x9:  # ping - ignore (server will treat silence fine)
        return None
    if opcode != 0x1 and opcode != 0x2:
        return None
    return payload


class CDP:
    def __init__(self, ws_url, timeout=10):
        parsed = urllib.parse.urlsplit(ws_url)
        self._host = parsed.hostname
        self._port = parsed.port or 80
        self._path = parsed.path or "/"
        self._id = 0
        self._sock = socket.create_connection((self._host, self._port), timeout=timeout)
        self._sock.settimeout(timeout)
        _ws_handshake(self._sock, self._host, self._port, self._path)

    def send(self, method, params=None, timeout=30):
        self._id += 1
        msg_id = self._id
        message = json.dumps({"id": msg_id, "method": method, "params": params or {}})
        _ws_send(self._sock, message.encode("utf-8"))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                frame = _ws_recv(self._sock)
            except socket.timeout:
                continue
            if frame is None:
                continue
            try:
                data = json.loads(frame.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error: {data['error']}")
                return data.get("result", {})
        raise TimeoutError("DevTools command timed out")

    def close(self):
        try:
            self._sock.close()
        except Exception:
            pass


def _get_page_ws_url(port, base_url="http://127.0.0.1"):
    """Wait for the DevTools endpoint, return the first page target's WS URL."""
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}:{port}/json", timeout=2) as resp:
                targets = json.load(resp)
            pages = [t for t in targets if t.get("type") == "page"]
            if pages:
                ws = pages[0].get("webSocketDebuggerUrl")
                if ws:
                    return ws
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Could not reach the browser debugging endpoint.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def login_and_get_cookie(on_status=None, cancel=None, timeout=420, login_url=LOGIN_URL):
    """Open the browser at the Roblox login page and return the
    .ROBLOSECURITY cookie the user obtains by logging in.

    Returns the cookie string, or None if cancelled/timed out.
    """
    on_status = on_status or (lambda msg: None)

    browser = find_chromium_browser()
    if not browser:
        raise RuntimeError("Neither Microsoft Edge nor Google Chrome was found.")

    port = _free_port()
    profile_dir = tempfile.mkdtemp(prefix="rbx_login_")
    proc = None
    try:
        on_status("Opening browser…")
        args = [
            browser,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-popup-blocking",
            "about:blank",
        ]
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws_url = _get_page_ws_url(port)
        cdp = CDP(ws_url)
        on_status("Loading Roblox sign-in page… (log in, then wait a moment)")

        cdp.send("Network.enable")
        cdp.send("Page.enable")
        cdp.send("Page.navigate", {"url": login_url})

        deadline = time.time() + timeout
        while time.time() < deadline:
            if cancel is not None and cancel.is_set():
                raise LoginCancelled()
            try:
                result = cdp.send("Network.getAllCookies")
            except Exception as exc:
                raise RuntimeError(f"Could not read cookies from the browser ({exc})")
            for cookie in result.get("cookies", []):
                if cookie.get("name") == COOKIE_NAME and "roblox.com" in cookie.get("domain", ""):
                    on_status("Session cookie captured ✔")
                    return cookie["value"]
            time.sleep(1.0)

        on_status("Login timed out.")
        return None
    finally:
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:
                pass
        try:
            shutil.rmtree(profile_dir, ignore_errors=True)
        except Exception:
            pass
        try:
            cdp.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Quick manual test: python cookie_login.py
    def _print(msg):
        print(msg)

    try:
        cookie = login_and_get_cookie(on_status=_print, timeout=300)
        print("COOKIE:", (cookie[:24] + "…") if cookie else None)
    except LoginCancelled:
        print("Cancelled.")