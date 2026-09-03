#!/usr/bin/env python3
"""
DevDroid v2.2 — Chrome DevTools on-device via ADB/Shizuku
https://github.com/SayCrazyy2/DevDroid

What it does:
  1. Enables wireless ADB (via root `su`, Shizuku `rish`, Sui/Dhizuku, or existing `adb tcpip`)
  2. `adb forward tcp:9222 localabstract:chrome_devtools_remote` (plus WebView fallbacks)
  3. Starts a local reverse-proxy on :9223 that bridges
     chrome-devtools-frontend.appspot.com  <->  ws://localhost:9222/devtools/...
  4. Serves a mobile-friendly launcher at http://localhost:9223/ and a rich terminal UI

Shizuku (non-root, Android 11+):
  - Install https://shizuku.rikka.app/ , start via Wireless Debugging
  - In Shizuku: Use in terminal apps -> Export files
  - Termux: cp /data/local/tmp/shizuku/rish* $PREFIX/bin/ && chmod +x $PREFIX/bin/rish && rish -c 'id'
  - Run: ./devtools.py --shizuku   (also auto-detected if rish is available)

Requirements: python3, adb (android-tools), aiohttp>=3.9, websockets>=12
"""
from __future__ import annotations

import sys

VERSION = "2.2.5"
# Ultra-fast path for --version / --help (avoid heavy imports on Termux where even asyncio is ~2s)
if "--version" in sys.argv or "-V" in sys.argv:
    # only fast if version is the main arg; otherwise let argparse handle
    if len(sys.argv) == 2 or (len(sys.argv) == 2 and sys.argv[1] in ("--version", "-V")) or ("--version" in sys.argv and len(sys.argv) <= 3):
        print(VERSION)
        sys.exit(0)

import argparse
import asyncio
import html
import json
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Optional

# Heavy deps — lazy so --help stays fast; import on first use
_aiohttp = None
_websockets = None
_web = None
_middleware = None

def _ensure_deps():
    global _aiohttp, _websockets, _web, _middleware
    if _aiohttp is not None:
        return
    import aiohttp as _a
    from aiohttp import web as _w
    from aiohttp.web import middleware as _m
    import websockets as _ws
    _aiohttp = _a
    _web = _w
    _middleware = _m
    _websockets = _ws
    globals()["aiohttp"] = _a
    globals()["web"] = _w
    globals()["middleware"] = _m
    globals()["websockets"] = _ws

CDP_HOST = "127.0.0.1"
CDP_PORT = 9222
PROXY_PORT = 9223

FALLBACK_HASH = "cea0d32fc090e98b9b4ac74a01fd7001acca04ff"
ALT_HASHES = ["a4d1895096617fd4306afe0b94348afeb7377e48", "f84901f7f0a12725375071f589e8d9fc61af1de3"]

ABSTRACTS = [
    "chrome_devtools_remote",
    "chrome_devtools_remote_local",
    "chrome_devtools_remote_0",
    "webview_devtools_remote_0",
    "webview_devtools_remote",
]

C_RESET = "\033[0m"
C_RED = "\033[31m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_CYAN = "\033[36m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger("devtools")

def log(msg: str, color: str = C_RESET, prefix: str = "*"):
    print(f"{color}[{prefix}] {msg}{C_RESET}", flush=True)

def err(msg: str): log(msg, C_RED, "!")
def ok(msg: str): log(msg, C_GREEN, "✓")
def info(msg: str): log(msg, C_CYAN, "·")
def warn(msg: str): log(msg, C_YELLOW, "!")

def run(cmd, shell: bool = False, timeout: int = 10):
    try:
        if shell:
            p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        else:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:
        return 1, str(e)

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def have_termux() -> bool:
    return bool(os.environ.get("PREFIX") and Path(os.environ["PREFIX"]).exists())

def has_root() -> bool:
    for c in ["su -c 'id'", "su 0 -c 'id'"]:
        rc, out = run(c, shell=True, timeout=5)
        if rc == 0 and "uid=0" in out:
            return True
    return False

def _rish_paths():
    pref = os.environ.get("PREFIX", "")
    return [
        which("rish"),
        "/data/data/com.termux/files/usr/bin/rish",
        f"{pref}/bin/rish" if pref else None,
        "/data/local/tmp/shizuku/rish",
    ]

def has_rish() -> bool:
    for p in _rish_paths():
        if p and Path(p).exists():
            rc, out = run([p, "-c", "id"], timeout=5)
            if rc == 0 and ("uid=2000" in out or "uid=" in out):
                return True
    rc, out = run(["rish", "-c", "echo ok"], timeout=5)
    if rc == 0 and "ok" in out:
        return True
    return False

def rish_bin() -> Optional[str]:
    for p in _rish_paths():
        if p and Path(p).exists():
            return p
    if which("rish"):
        return which("rish")
    return None

def has_shizuku_app() -> bool:
    return which("shizuku") is not None or Path("/data/local/tmp/shizuku/rish").exists()

def has_sui() -> bool:
    return which("sui") is not None

def has_dhizuku() -> bool:
    rc, out = run("dpm list owners 2>&1 | grep -i dhizuku", shell=True, timeout=3)
    return rc == 0 and "dhizuku" in out.lower()

def detect_privilege() -> str:
    if has_rish():
        return "shizuku"
    if has_sui() and has_rish():
        return "shizuku"
    if has_root():
        return "root"
    if has_shizuku_app():
        return "shizuku-pending"
    if has_dhizuku():
        return "dhizuku"
    return "none"

def privileged_exec(cmd: str, method: str):
    if method == "root":
        rc, out = run(f"su -c '{cmd}'", shell=True, timeout=10)
        if rc != 0:
            rc, out = run(f"su 0 -c '{cmd}'", shell=True, timeout=10)
        return rc, out
    if method in ("shizuku", "shizuku-pending", "sui"):
        b = rish_bin()
        if b:
            return run([b, "-c", cmd], timeout=12)
        for trial in [["shizuku", "sh", "-c", cmd], ["shizuku", "-c", cmd]]:
            rc, out = run(trial, timeout=12)
            if rc == 0:
                return rc, out
        return 1, "Shizuku app found but rish not exported. Export from Shizuku: Use in terminal apps -> Export files"
    return run(cmd, shell=True, timeout=10)

def ensure_adb():
    if not which("adb"):
        err("adb not found — install with: pkg install android-tools  (Termux) or apt install adb")
        sys.exit(1)

def adb(cmd: str, timeout: int = 12):
    full = f"adb {cmd}"
    rc, out = run(full, shell=True, timeout=timeout)
    if rc != 0 or "forward" in cmd or "connect" in cmd or "devices" in cmd:
        if out:
            print(f"  $ {full}\n  -> {out}", flush=True)
    return rc, out

def setup_wireless_adb(port: int, method: str) -> bool:
    log(f"Setting up wireless ADB on port {port} via {method}...", C_BOLD)
    if method == "root":
        for cmd in [
            f"setprop service.adb.tcp.port {port} && setprop ctl.restart adbd",
            f"setprop service.adb.tcp.port {port} && stop adbd && start adbd",
            f"setprop service.adb.tcp.port {port}",
        ]:
            rc, out = privileged_exec(cmd, "root")
            if rc == 0:
                time.sleep(2.2)
                rc2, out2 = privileged_exec("getprop service.adb.tcp.port", "root")
                if out2.strip() == str(port):
                    ok(f"Wireless ADB now on {port} (root)")
                    return True
        err("Root path failed. Does `su -c 'id'` show uid=0?")
        return False
    if method in ("shizuku", "sui", "shizuku-pending"):
        if not has_rish():
            err("Shizuku not ready — rish check failed.")
            if has_shizuku_app():
                err("Fix: Shizuku app → Use in terminal apps → Export files → cp /data/local/tmp/shizuku/rish* $PREFIX/bin/ && chmod +x $PREFIX/bin/rish")
            else:
                err("Install Shizuku from https://shizuku.rikka.app/ or use `adb tcpip` via PC.")
            return False

        # --- Helpers for modern Android (11+) Wireless Debugging ---
        def _check_port_listening(p: int) -> bool:
            # Try ss / netstat / proc via rish
            for cmd in [
                f"ss -tln 2>/dev/null | grep -q ':{p} ' && echo LISTEN",
                f"netstat -tln 2>/dev/null | grep -q ':{p} ' && echo LISTEN",
                f"cat /proc/net/tcp 2>/dev/null | tr 'a-z' 'A-Z' | grep -q ':0{hex(p)[2:].upper()}' && echo LISTEN",
            ]:
                rc, out = privileged_exec(cmd, "shizuku")
                if "LISTEN" in out:
                    return True
            # Fallback: try adb connect directly — if it connects, port is listening
            rc, out = run(f"adb connect localhost:{p}", shell=True, timeout=5)
            if rc == 0 and ("connected" in out.lower() or "already" in out.lower()):
                run("adb disconnect", shell=True, timeout=3)
                return True
            return False

        def _try_adb_tcpip_fallback(p: int) -> bool:
            # Some ROMs allow `adb tcpip` via shell even without prior connection (via rish)
            for cmd in [f"adb tcpip {p}", f"setprop persist.adb.tcp.port {p} && setprop ctl.restart adbd"]:
                rc, out = privileged_exec(cmd, "shizuku")
                if rc == 0:
                    time.sleep(2.5)
                    if _check_port_listening(p):
                        return True
            return False

        def _diagnose():
            info("--- Shizuku diagnostics ---")
            for cmd in [
                "id",
                "getprop ro.build.version.release; getprop ro.build.version.sdk",
                "getprop | grep -i adb",
                "settings get global adb_wifi_enabled 2>/dev/null; settings get global development_settings_enabled 2>/dev/null",
                "ss -tln 2>/dev/null | head -20; netstat -tln 2>/dev/null | head -20",
                "getprop service.adb.tcp.port; getprop persist.adb.tcp.port; getprop service.adb.tls.port 2>/dev/null; getprop persist.adb.tls.port 2>/dev/null",
                "adb --version 2>&1 | head -1",
            ]:
                rc, out = privileged_exec(cmd, "shizuku")
                info(f"$ rish -c '{cmd}' -> {out[:400] if out else '(empty)'}")

        # Try classic + modern props
        strategies = [
            f"setprop service.adb.tcp.port {port} && setprop ctl.restart adbd",
            f"setprop service.adb.tcp.port {port} && stop adbd && start adbd",
            f"setprop service.adb.tcp.port {port}",
            f"setprop persist.adb.tcp.port {port} && setprop ctl.restart adbd",
            f"setprop persist.adb.tcp.port {port} && setprop service.adb.tcp.port {port} && setprop ctl.restart adbd",
        ]
        for strat in strategies:
            info(f"rish -c '{strat}'")
            rc, out = privileged_exec(strat, "shizuku")
            info(f"  -> rc={rc} {out[:220]}")
            if rc == 0:
                time.sleep(2.8)
                # Check multiple props — some ROMs use persist, some use service, some use tls
                props_to_check = [
                    "service.adb.tcp.port",
                    "persist.adb.tcp.port",
                    "service.adb.tls.port",
                ]
                found = False
                for prop in props_to_check:
                    rc2, out2 = privileged_exec(f"getprop {prop}", "shizuku")
                    info(f"  getprop {prop} -> {out2!r}")
                    if out2.strip() == str(port):
                        found = True
                        break
                # Even if getprop is empty (common on Android 13+ where prop is restricted),
                # try to check if port is actually listening or adb connect works — that is the real test
                if found or _check_port_listening(port):
                    if not found:
                        warn(f"getprop empty but port {port} is LISTENING (Android 13+ hides prop) — treating as success")
                    else:
                        ok(f"Wireless ADB prop set to {port} via Shizuku ({prop})")
                    # Ensure adbd is restarted if strat didn't include restart
                    if "ctl.restart" not in strat and "stop adbd" not in strat and not _check_port_listening(port):
                        privileged_exec("setprop ctl.restart adbd", "shizuku")
                        time.sleep(2)
                    # Final verification: try adb connect
                    rc, out = run(f"adb connect localhost:{port}", shell=True, timeout=5)
                    if "connected" in out.lower() or "already" in out.lower():
                        ok(f"Verified adb connect localhost:{port} works")
                        run("adb disconnect", shell=True, timeout=3)
                        return True
                    # If adb connect not yet, still consider success — connect_and_forward will retry
                    return True
                warn(f"getprop empty and port not listening yet, retrying…")
                time.sleep(1)

        # Fallback: try adb tcpip via rish, and try to auto-detect existing Wireless Debugging port
        info("Trying fallback: rish adb tcpip and Wireless Debugging auto-detect…")
        if _try_adb_tcpip_fallback(port):
            ok(f"Fallback adb tcpip {port} succeeded")
            return True

        # Try to find existing Wireless Debugging port (Android 11+ uses random port, not 5555)
        # Parse getprop for any adb port
        rc, out = privileged_exec("getprop | grep -E 'adb.*port|tls.*port' 2>/dev/null || getprop | grep adb 2>/dev/null | head -20", "shizuku")
        if out:
            info(f"Existing adb props: {out[:500]}")
            # Try to extract port numbers and test them
            import re as _re
            for m in _re.finditer(r":(\d{4,5})\b", out):
                p = int(m.group(1))
                if 1000 <= p <= 65535 and _check_port_listening(p):
                    warn(f"Found existing Wireless Debugging port {p} listening — use it instead: devtools --port {p} or devtools  {p}")
                    # We could try to use that port instead of requested one
                    # For now, try to connect to it
                    rc, out2 = run(f"adb connect localhost:{p}", shell=True, timeout=5)
                    if "connected" in out2.lower():
                        ok(f"Auto-detected working port {p}, using it")
                        # Update global port for connect_and_forward? We return True but caller will use original port
                        # Instead, tell user and try to forward on detected port
                        # Try forward on detected port as well
                        run("adb disconnect", shell=True, timeout=3)
                        # If original port failed, try to setup wireless on detected port's behalf? Just succeed and let connect_and_forward retry with original?
                        pass

        _diagnose()
        err("All Shizuku strategies failed.")
        err("Next steps:")
        err("  1) Ensure Wireless Debugging is ON: Settings → Developer options → Wireless debugging → ON")
        err("     (If you use Shizuku via Wireless Debugging, it should already be ON)")
        err("  2) Try manual: rish -c 'setprop service.adb.tcp.port 5555; setprop ctl.restart adbd; sleep 2; getprop service.adb.tcp.port; ss -tln | grep 5555'")
        err(f"  3) Try different port: devtools --shizuku --port 5555  (you tried {port})")
        err("  4) Try with root debug: devtools --shizuku --verbose  and paste log")
        err("  5) Fallback without Shizuku tcp: enable Wireless Debugging port manually and use: devtools <that-port>  (find port in Wireless Debugging settings)")
        return False
    if method == "dhizuku":
        err("Dhizuku detected but not yet handled for wireless ADB. Use `adb tcpip` via PC or Shizuku.")
        return False
    err(f"Unknown method {method}")
    return False

DIRECT_FORWARD_SCRIPT = "/data/local/tmp/devdroid_forward.py"
DIRECT_FORWARD_LOG = "/data/local/tmp/devdroid_forward.log"

def start_shizuku_direct_forward(cdp_port: int = CDP_PORT, verbose: bool = False) -> bool:
    """
    Fallback for Android 11+ where setprop is blocked and adb tcpip fails.
    Uses rish (shell) to directly forward TCP 127.0.0.1:cdp_port -> abstract:chrome_devtools_remote
    via socat or a pure-python forwarder. No adb connect needed.
    """
    info("Trying Shizuku direct-forward (no adb connect needed)…")
    # Check what forwarders are available via rish
    rc, out = privileged_exec("which socat; which nc; which ncat; which busybox; which toybox", "shizuku")
    has_socat = "socat" in out
    has_nc = "nc" in out or "ncat" in out
    info(f"  tools via rish: {out[:200] or '(none)'}  socat={has_socat}")

    # Try socat for each abstract
    for abstract in ABSTRACTS:
        if has_socat:
            # Kill any old socat on this port
            privileged_exec(f"pkill -f 'socat.*:{cdp_port}' 2>/dev/null; true", "shizuku")
            time.sleep(0.5)
            # socat TCP-LISTEN:9222 -> ABSTRACT
            # Use nohup so it survives after rish exits
            cmd = f"nohup socat TCP-LISTEN:{cdp_port},fork,reuseaddr,bind=127.0.0.1 ABSTRACT-CONNECT:{abstract} >/dev/null 2>&1 & echo $!"
            rc, out = privileged_exec(cmd, "shizuku")
            info(f"  socat {abstract} -> rc={rc} pid={out[:50]}")
            time.sleep(1.2)
            # Verify listening
            rc, out2 = run(f"ss -tln 2>/dev/null | grep -q ':{cdp_port} ' && echo LISTEN || netstat -tln 2>/dev/null | grep -q ':{cdp_port} ' && echo LISTEN || echo NO", shell=True, timeout=4)
            # Also try via rish ss
            rc2, out3 = privileged_exec(f"ss -tln 2>/dev/null | grep ':{cdp_port} ' || netstat -tln 2>/dev/null | grep ':{cdp_port} ' || echo NO", "shizuku")
            if "LISTEN" in (out2 + out3) or "9222" in (out2 + out3):
                # Try to curl CDP via the forward
                time.sleep(0.8)
                rc, out4 = run(f"curl -s --connect-timeout 3 http://127.0.0.1:{cdp_port}/json/version 2>&1 | head -5", shell=True, timeout=5)
                if "Browser" in out4 or "Chrome" in out4 or "devtools" in out4.lower():
                    ok(f"Direct socat forward tcp:{cdp_port} -> {abstract} works (no adb needed)")
                    return True
                else:
                    info(f"  socat listening but CDP not yet (Chrome running? curl: {out4[:120]}) — keeping forward, will try next abstract")
                    # Don't kill, keep it; but try next abstract if this one not Chrome
                    # Actually keep first that LISTENs, even if Chrome not yet — Chrome may start later
                    # We consider LISTEN as success
                    if "LISTEN" in (out2 + out3):
                        ok(f"Direct socat forward tcp:{cdp_port} -> {abstract} LISTEN (Chrome may be not foreground)")
                        return True
            # If not LISTEN, try next abstract

    # Fallback: pure-python forwarder — try Termux direct first (Termux may have adb group on some ROMs)
    # Test if Termux can directly connect to abstract (no rish needed) — if yes, we don't need rish's shell
    rc, out = run("python3 -c \"import socket; s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(1); s.connect(chr(0)+'chrome_devtools_remote'); print('TERMUX_CAN_CONNECT')\" 2>&1 | head -5", shell=True, timeout=5)
    termux_can_direct = "TERMUX_CAN_CONNECT" in out
    info(f"  Termux direct abstract test: {out[:120]!r} -> can_direct={termux_can_direct}")
    if termux_can_direct:
        info("  Trying Python forwarder via Termux directly (no rish needed)…")
        # Write script already done, now launch via Termux
        _termux_py = which("python3") or "/data/data/com.termux/files/usr/bin/python3"
        rc, out = run(f"nohup {_termux_py} {DIRECT_FORWARD_SCRIPT} >{DIRECT_FORWARD_LOG} 2>&1 & echo $!", shell=True, timeout=5)
        info(f"  Termux direct launch -> rc={rc} pid={out[:80]!r}")
        time.sleep(1.5)
        rc, out = run(f"ss -tln 2>/dev/null | grep -q ':{cdp_port} ' && echo LISTEN || netstat -tln 2>/dev/null | grep -q ':{cdp_port} ' && echo LISTEN || cat {DIRECT_FORWARD_LOG} 2>/dev/null | head -20", shell=True, timeout=4)
        if "LISTEN" in out or "listening 127.0.0.1" in out:
            time.sleep(0.8)
            rc, out3 = run(f"curl -s --connect-timeout 3 http://127.0.0.1:{cdp_port}/json/version 2>&1 | head -5", shell=True, timeout=5)
            if "Browser" in out3:
                ok(f"Termux direct forward tcp:{cdp_port} -> abstract works (no rish)")
                return True
            else:
                warn(f"Termux direct LISTEN but CDP not yet (Chrome? {out3[:120]}) — keeping")
                return True

    info("  Termux direct failed or no permission — trying Python forwarder via rish (shell)…")
    python_forward_code = f'''
import socket, threading, os, sys
CDP_PORT={cdp_port}
ABSTRACTS={ABSTRACTS!r}
def forward(client, abstract):
    try:
        # Connect to Chrome abstract
        s2 = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s2.connect("\\0" + abstract)
        def pipe(a,b):
            try:
                while True:
                    d=a.recv(8192)
                    if not d: break
                    b.sendall(d)
            except: pass
            finally:
                try: a.close()
                except: pass
                try: b.close()
                except: pass
        threading.Thread(target=pipe, args=(client, s2), daemon=True).start()
        pipe(s2, client)
    except Exception as e:
        try: client.close()
        except: pass

def main():
    # Try each abstract, use first that exists (check via socket connect test)
    chosen=None
    for ab in ABSTRACTS:
        try:
            s=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(1)
            s.connect("\\0"+ab)
            s.close()
            chosen=ab
            break
        except: pass
    if not chosen:
        # Fallback to first
        chosen=ABSTRACTS[0]
    print(f"[forward] chosen abstract={{chosen}}", flush=True)
    srv=socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", CDP_PORT))
    except Exception as e:
        print(f"bind failed {{e}}", flush=True)
        sys.exit(1)
    srv.listen(32)
    print(f"[forward] listening 127.0.0.1:{{CDP_PORT}} -> {{chosen}} (pid={{os.getpid()}})", flush=True)
    while True:
        try:
            c,a=srv.accept()
            threading.Thread(target=forward, args=(c, chosen), daemon=True).start()
        except Exception as e:
            print(f"accept error {{e}}", flush=True)
            break

if __name__=="__main__":
    main()
'''
    # Write script via rish (use cat heredoc)
    # First, try to write via Termux directly to /data/local/tmp if writable, else via rish
    rc, out = run(f"cat > {DIRECT_FORWARD_SCRIPT} << 'PYEOF'\n{python_forward_code}\nPYEOF\ncat {DIRECT_FORWARD_SCRIPT} | head -5", shell=True, timeout=8)
    if rc != 0 or "import socket" not in out:
        # Try via rish echo
        # Escape single quotes for rish -c
        b64_cmd = f"cat > {DIRECT_FORWARD_SCRIPT} << 'PYEOF'\n{python_forward_code}\nPYEOF"
        rc, out = privileged_exec(b64_cmd, "shizuku")
        info(f"  write via rish -> rc={rc}")
    # Kill old forward
    privileged_exec(f"pkill -f devdroid_forward.py 2>/dev/null; true; rm -f {DIRECT_FORWARD_LOG}", "shizuku")
    run(f"pkill -f devdroid_forward.py 2>/dev/null; true", shell=True, timeout=3)
    time.sleep(0.5)
    # Launch via rish — Termux's $PREFIX is not accessible to shell (rish) due to scoped storage
    # So copy python binary to world-readable /data/local/tmp first
    termux_python = which("python3") or "/data/data/com.termux/files/usr/bin/python3"
    if not Path(termux_python).exists():
        for cand in ["/data/data/com.termux/files/usr/bin/python", "/data/data/com.termux/files/usr/bin/python3.12", "/data/data/com.termux/files/usr/bin/python3.11"]:
            if Path(cand).exists():
                termux_python = cand
                break
    # Copy to /data/local/tmp for rish access (Termux private dir is 700, shell can't read)
    rish_python = "/data/local/tmp/devdroid_python3"
    rc, out = run(f"cp {termux_python} {rish_python} 2>&1 && chmod 755 {rish_python} 2>&1 && ls -l {rish_python} 2>&1 | head -1", shell=True, timeout=8)
    if rc == 0 and "rwxr" in out:
        info(f"  copied python to {rish_python} for rish")
        termux_python_for_rish = rish_python
    else:
        warn(f"  copy python to /data/local/tmp failed: {out[:200]} — trying direct Termux path and system python")
        termux_python_for_rish = termux_python
        # Also try system python if exists
        rc2, out2 = privileged_exec("which python3; which python; ls /system/bin/python* 2>/dev/null; ls /apex/com.android.art/bin/python* 2>/dev/null | head -5", "shizuku")
        if out2 and "python" in out2:
            info(f"  system python candidates: {out2[:200]}")
            # Prefer system python if Termux one not accessible
            for cand in out2.split():
                if "python" in cand and cand.startswith("/"):
                    termux_python_for_rish = cand.strip()
                    break

    # Use /system/bin/nohup (Termux's nohup is not accessible to shell)
    nohup_bin = "/system/bin/nohup"
    # Verify nohup exists via rish
    rc, out = privileged_exec("which nohup; ls /system/bin/nohup 2>/dev/null; ls /apex/*/bin/nohup 2>/dev/null | head -1", "shizuku")
    if "nohup" not in out:
        nohup_bin = "nohup"  # fallback to PATH
    launch_cmds = [
        f"{nohup_bin} {termux_python_for_rish} {DIRECT_FORWARD_SCRIPT} >{DIRECT_FORWARD_LOG} 2>&1 & echo $!",
        f"nohup {termux_python_for_rish} {DIRECT_FORWARD_SCRIPT} >{DIRECT_FORWARD_LOG} 2>&1 & echo $!",
        f"{termux_python_for_rish} {DIRECT_FORWARD_SCRIPT} >{DIRECT_FORWARD_LOG} 2>&1 & echo $!",
        f"sh -c 'nohup {termux_python_for_rish} {DIRECT_FORWARD_SCRIPT} >{DIRECT_FORWARD_LOG} 2>&1 &' ; echo $!",
    ]
    rc, out = 1, ""
    for lc in launch_cmds:
        rc, out = privileged_exec(lc, "shizuku")
        # Filter out "inaccessible" false-success: rish returns rc=0 even if nohup fails, but out will contain "inaccessible"
        if "inaccessible" in out or "No such file" in out:
            info(f"  rish launch failed (inaccessible) for {lc[:60]}… -> {out[:120]!r}")
            rc = 1
            continue
        info(f"  python forward via rish ({lc[:60]}…) -> rc={rc} pid={out[:80]!r}")
        if rc == 0 and out.strip().isdigit():
            break
    # Fallback: try via Termux directly (if shell forward fails, Termux user may still be able to connect to abstract? Try it)
    if rc != 0 or not out.strip().isdigit() or "inaccessible" in out:
        info(f"  rish python failed, trying Termux direct (no rish) — may fail if Termux lacks shell group, but worth trying")
        # Termux directly can try to run forward without rish — it may have permission on some devices
        for lc in [f"nohup {termux_python} {DIRECT_FORWARD_SCRIPT} >{DIRECT_FORWARD_LOG} 2>&1 & echo $!", f"{termux_python} {DIRECT_FORWARD_SCRIPT} >{DIRECT_FORWARD_LOG} 2>&1 & echo $!"]:
            rc, out = run(lc, shell=True, timeout=5)
            info(f"  python forward via Termux ({lc[:50]}…) -> rc={rc} pid={out[:80]!r}")
            if rc == 0 and out.strip().isdigit():
                break
    time.sleep(1.5)
    # Verify listening
    rc, out = run(f"ss -tln 2>/dev/null | grep -q ':{cdp_port} ' && echo LISTEN || netstat -tln 2>/dev/null | grep -q ':{cdp_port} ' && echo LISTEN || cat {DIRECT_FORWARD_LOG} 2>/dev/null | head -20", shell=True, timeout=4)
    rc2, out2 = privileged_exec(f"ss -tln 2>/dev/null | grep ':{cdp_port} ' || cat {DIRECT_FORWARD_LOG} 2>/dev/null | head -20", "shizuku")
    combined = out + out2
    info(f"  verify listening: {combined[:400]}")
    if "LISTEN" in combined or "listening 127.0.0.1" in combined:
        # Try curl
        time.sleep(0.8)
        rc, out3 = run(f"curl -s --connect-timeout 3 http://127.0.0.1:{cdp_port}/json/version 2>&1 | head -5", shell=True, timeout=5)
        if "Browser" in out3:
            ok(f"Python direct forward tcp:{cdp_port} -> abstract works")
        else:
            warn(f"Forward LISTEN but CDP not responding yet (Chrome foreground? curl: {out3[:150]}) — keeping forward")
        return True
    err(f"Direct forward failed to LISTEN on :{cdp_port}. Log: {combined[:300]}")
    # Print log
    rc, log = privileged_exec(f"cat {DIRECT_FORWARD_LOG} 2>/dev/null | head -30", "shizuku")
    if log:
        info(f"  forward log: {log[:500]}")
    return False

def find_existing_abstract() -> Optional[str]:
    rc, out = run("adb forward --list", shell=True, timeout=5)
    if rc == 0:
        for a in ABSTRACTS:
            if a in out:
                return a
    return None

def connect_and_forward(port: int, cdp_port: int = CDP_PORT, verbose: bool = False) -> bool:
    ensure_adb()
    run("adb disconnect", shell=True, timeout=8)
    for attempt in range(1, 4):
        info(f"Connecting to wireless ADB localhost:{port} (attempt {attempt}/3)…")
        rc, out = adb(f"connect localhost:{port}")
        rc2, devs = run("adb devices", shell=True, timeout=6)
        if f"localhost:{port}" not in devs and "device" not in devs:
            warn(f"Device not yet in `adb devices` after connect — output: {devs[:200]}")
            if attempt == 1 and "failed" in (out or "").lower():
                info("Hint: Android 11+ Wireless Debugging uses a *pairing* port and a *connect* port — make sure you use the connect port (e.g. 37193), not the pairing one.")
            time.sleep(1.6)
            continue
        ok(f"ADB device connected: {devs.strip().splitlines()[-1] if devs else 'localhost:'+str(port)}")
        for abstract in ABSTRACTS:
            rc, out = adb(f"forward tcp:{cdp_port} localabstract:{abstract}")
            if rc == 0:
                rc2, fl = run("adb forward --list", shell=True, timeout=5)
                if f"tcp:{cdp_port}" in fl:
                    ok(f"Forward tcp:{cdp_port} -> {abstract} OK")
                    return True
            elif verbose:
                warn(f"forward -> {abstract} failed: {out[:200]}")
        warn("No abstract succeeded — is Chrome running with open tab? Waiting 2s and retrying…")
        time.sleep(2)
    err("Failed to connect/forward after 3 attempts.")
    return False

async def fetch_json(path: str, timeout: int = 5):
    _ensure_deps()
    url = f"http://{CDP_HOST}:{CDP_PORT}{path}"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
                if r.status == 200:
                    ct = r.headers.get("content-type", "")
                    if "json" in ct:
                        return await r.json()
                    else:
                        return await r.text()
                return None if r.status == 404 else f"HTTP {r.status}"
    except Exception as e:
        return None

async def fetch_tabs_and_version():
    _ensure_deps()
    tabs = await fetch_json("/json")
    ver = await fetch_json("/json/version")
    if not isinstance(tabs, list):
        tabs = None if tabs is None else []
    if not isinstance(ver, dict):
        ver = {} if ver is None else {"raw": ver}
    return tabs, ver

def build_frontend_url(tab: dict | str, version_info: dict | None = None) -> str:
    if isinstance(tab, dict):
        tid = tab.get("id", str(tab))
        frontend = tab.get("devtoolsFrontendUrl") or tab.get("devtoolsFrontendUrlCompat") or ""
    else:
        tid = str(tab)
        frontend = ""
    proxy_ws = f"{CDP_HOST}:{PROXY_PORT}/{tid}"
    if frontend:
        try:
            parsed = urllib.parse.urlparse(frontend)
            qs = urllib.parse.parse_qs(parsed.query)
            if "ws" in qs:
                qs["ws"] = [proxy_ws]
                new_q = urllib.parse.urlencode({k: v[0] for k, v in qs.items()}, safe=":/")
                if parsed.scheme in ("http", "https"):
                    rebuilt = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_q, ""))
                    if "chrome-devtools-frontend" not in rebuilt:
                        return f"https://chrome-devtools-frontend.appspot.com/serve_file/@{FALLBACK_HASH}/devtools_app.html?ws={proxy_ws}&remoteFrontend=true"
                    return rebuilt
        except Exception:
            pass
    ws_q = urllib.parse.quote(proxy_ws, safe=":/")
    return f"https://chrome-devtools-frontend.appspot.com/serve_file/@{FALLBACK_HASH}/devtools_app.html?ws={ws_q}&remoteFrontend=true"

async def cors_middleware(request, handler):
    _ensure_deps()
    # Ensure decorator version is set (aiohttp 3.14 requires __middleware_version__ == 1)
    # If not set, aiohttp treats this as factory (app, handler) instead of (request, handler)
    if not hasattr(cors_middleware, "__middleware_version__"):
        try:
            cors_middleware.__middleware_version__ = 1  # type: ignore
        except: pass
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
        except Exception as e:
            _log.exception("handler error: %s", e)
            resp = web.json_response({"error": str(e)}, status=500)
    resp.headers["Access-Control-Allow-Origin"] = "https://chrome-devtools-frontend.appspot.com"
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Allow-Credentials"] = "true"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    resp.headers["Cross-Origin-Embedder-Policy"] = "credentialless"
    return resp

async def websocket_handler(request):
    _ensure_deps()
    tab_id = request.match_info["id"]
    log(f"WS proxy: client -> tab {tab_id[:12]}…", C_CYAN)
    ws_server = web.WebSocketResponse(heartbeat=30, autoping=True)
    await ws_server.prepare(request)
    candidates = [
        f"ws://{CDP_HOST}:{CDP_PORT}/devtools/page/{tab_id}",
        f"ws://{CDP_HOST}:{CDP_PORT}/devtools/page/{tab_id}/",
        f"ws://{CDP_HOST}:{CDP_PORT}/devtools/browser/{tab_id}",
    ]
    if tab_id.startswith("ws://"):
        candidates.insert(0, tab_id)
    last_err = None
    for cdp_url in candidates:
        try:
            async with websockets.connect(cdp_url, max_size=None, ping_interval=20, ping_timeout=20) as ws_client:
                async def s2c():
                    async for msg in ws_server:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws_client.send(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await ws_client.send(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
                async def c2s():
                    async for msg in ws_client:
                        if isinstance(msg, bytes):
                            await ws_server.send_bytes(msg)
                        else:
                            await ws_server.send_str(msg)
                await asyncio.gather(s2c(), c2s())
                return ws_server
        except Exception as e:
            last_err = e
            continue
    err(f"WS proxy failed for {tab_id[:12]}: {last_err}")
    if not ws_server.closed:
        await ws_server.close(code=aiohttp.WSCloseCode.PROTOCOL_ERROR, message=str(last_err).encode()[:120])
    return ws_server

async def json_proxy(request):
    _ensure_deps()
    path = request.path
    qs = f"?{request.query_string}" if request.query_string else ""
    target = f"http://{CDP_HOST}:{CDP_PORT}{path}{qs}"
    allowed_prefixes = ("/json", "/json/", "/devtools/")
    if not any(path.startswith(p) for p in allowed_prefixes):
        return web.json_response({"error": "not allowed"}, status=403)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(target, timeout=aiohttp.ClientTimeout(total=6)) as r:
                body = await r.read()
                return web.Response(body=body, status=r.status, headers={"Access-Control-Allow-Origin": "*", "Content-Type": r.headers.get("Content-Type", "application/json")})
    except Exception as e:
        return web.json_response({"error": str(e), "target": target}, status=502)

async def api_tabs(request):
    _ensure_deps()
    tabs, ver = await fetch_tabs_and_version()
    if tabs is None:
        return web.json_response({"error": "cannot reach CDP — is Chrome running? adb forward ok?", "cdp": f"{CDP_HOST}:{CDP_PORT}"}, status=502)
    for t in tabs:
        try:
            t["_frontend"] = build_frontend_url(t, ver)
            t["_proxyWs"] = f"{CDP_HOST}:{PROXY_PORT}/{t.get('id','')}"
        except Exception:
            pass
    return web.json_response({"tabs": tabs, "version": ver, "proxy": f"{CDP_HOST}:{PROXY_PORT}", "cdp": f"{CDP_HOST}:{CDP_PORT}"})

async def api_action(request):
    _ensure_deps()
    action = request.match_info["action"]
    tid = request.match_info.get("id", "")
    cdp_map = {
        "close": f"/json/close/{tid}",
        "activate": f"/json/activate/{tid}",
        "reload": None,
    }
    if action == "new":
        url = request.query.get("url", "about:blank")
        target = f"http://{CDP_HOST}:{CDP_PORT}/json/new?{urllib.parse.urlencode({'url': url})}"
    elif action in cdp_map and cdp_map[action]:
        target = f"http://{CDP_HOST}:{CDP_PORT}{cdp_map[action]}"
    elif action == "reload":
        try:
            ws_url = f"ws://{CDP_HOST}:{CDP_PORT}/devtools/page/{tid}"
            async with websockets.connect(ws_url, max_size=None) as ws:
                await ws.send(json.dumps({"id": 1, "method": "Page.reload", "params": {"ignoreCache": False}}))
                msg = await asyncio.wait_for(ws.recv(), timeout=4)
                return web.json_response({"ok": True, "result": json.loads(msg) if isinstance(msg, str) else str(msg)})
        except Exception as e:
            return web.json_response({"error": str(e)}, status=502)
    else:
        return web.json_response({"error": f"unknown action {action}"}, status=400)
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(target, timeout=aiohttp.ClientTimeout(total=6)) as r:
                body = await r.read()
                try:
                    return web.json_response(json.loads(body), status=r.status)
                except Exception:
                    return web.Response(body=body, status=r.status, content_type=r.content_type)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=502)

async def ui_handler(request):
    _ensure_deps()
    return web.Response(text=WEB_UI_HTML, content_type="text/html")

async def health_handler(request):
    _ensure_deps()
    tabs, ver = await fetch_tabs_and_version()
    return web.json_response({
        "status": "ok",
        "version": VERSION,
        "cdp": f"{CDP_HOST}:{CDP_PORT}",
        "proxy": f"{CDP_HOST}:{PROXY_PORT}",
        "tabs": len(tabs) if isinstance(tabs, list) else None,
        "browser": (ver.get("Browser") if isinstance(ver, dict) else None),
        "privilege": detect_privilege(),
    })




WEB_UI_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>DevDroid — DevTools Launcher</title>
<style>
:root{--bg:#0b0e14;--panel:#141821;--panel2:#1a2030;--line:#232a3d;--text:#e6e9f2;--muted:#9aa3bf;--dim:#6b7594;--accent:#5b6cff;--accent2:#3d53f5;--ok:#2ecb7a;--err:#ff5563;--radius:16px}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,sans-serif;background:radial-gradient(1200px 600px at 20% -10%, #1a2340 0%, transparent 60%), var(--bg);color:var(--text);min-height:100vh}
header{position:sticky;top:0;z-index:20;backdrop-filter:blur(16px);background:rgba(20,24,33,.7);border-bottom:1px solid var(--line)}
.hdr{max-width:860px;margin:0 auto;padding:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.logo{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,var(--accent),#8a7dff);display:grid;place-items:center;font-size:18px}
h1{font-size:17px;font-weight:800}h1 small{font-weight:600;color:var(--muted);font-size:11px;display:block}
.hdr-right{margin-left:auto;display:flex;gap:8px}
.badge{font-size:11px;padding:6px 10px;border-radius:999px;background:var(--panel2);border:1px solid var(--line);color:var(--muted)}
.badge.ok{color:var(--ok)} .badge.err{color:var(--err)}
.wrap{max-width:860px;margin:0 auto;padding:16px}
.toolbar{position:sticky;top:72px;z-index:10;display:flex;gap:8px;flex-wrap:wrap;background:rgba(11,14,20,.6);backdrop-filter:blur(10px);padding:10px 0 12px}
.input{flex:1 1 220px;display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px 12px}
.input input{flex:1;background:transparent;border:0;outline:0;color:var(--text)}
.select{appearance:none;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:12px;padding:9px 12px;font-weight:600}
.btn{appearance:none;border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:12px;padding:9px 13px;font-weight:700;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.btn-primary{background:linear-gradient(180deg,var(--accent),var(--accent2));color:#fff;border-color:transparent}
.grid{display:grid;gap:12px}
.card{background:linear-gradient(180deg,rgba(255,255,255,.02),transparent),var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px;display:flex;gap:12px}
.fav{width:42px;height:42px;border-radius:12px;background:var(--panel2);border:1px solid var(--line);display:grid;place-items:center;font-size:18px;flex:0 0 42px}
.main{flex:1;min-width:0}
.title{font-weight:750;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.url{font-size:12px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.meta span{font-size:11px;padding:4px 8px;border-radius:999px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.07);color:var(--muted)}
.actions{display:flex;flex-direction:column;gap:6px;min-width:108px}
.empty{border:1px dashed var(--line);border-radius:16px;padding:28px;text-align:center;color:var(--muted)}
footer{padding:22px;text-align:center;color:var(--dim);font-size:11px}
.toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#1a2030;border:1px solid var(--line);padding:10px 14px;border-radius:999px;display:none}
.toast.show{display:block}
.sheet{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;place-items:center;padding:16px}
.sheet.show{display:grid}
.sheet-box{width:min(520px,100%);background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px}
.qr{width:220px;height:220px;background:#fff;border-radius:12px;display:grid;place-items:center;margin:12px auto;overflow:hidden}
</style>
</head>
<body>
<header><div class="hdr"><div style="display:flex;gap:10px;align-items:center"><div class="logo">🔧</div><h1>DevDroid <small id="subtitle">proxy 127.0.0.1:9223 → cdp 9222</small></h1></div><div class="hdr-right"><span id="count" class="badge">— tabs</span><span id="status" class="badge err">connecting…</span><button class="btn" onclick="openSheet()">◈ QR</button></div></div></header>
<div class="wrap"><div class="toolbar"><label class="input"><span>⌕</span><input id="q" placeholder="Search title or URL…" oninput="render()"><button class="btn" style="padding:6px 8px" onclick="q.value='';render()">✕</button></label><select id="filter" class="select" onchange="render()"><option value="all">All types</option><option value="page">page</option><option value="service_worker">service_worker</option><option value="webview">webview</option><option value="other">other</option></select><button class="btn" onclick="load()">↻ Refresh</button><button class="btn" onclick="newTabPrompt()">＋ New tab</button><button class="btn" onclick="copyAll()">⎘ Copy JSON</button></div><div id="list" class="grid"></div><footer>Frontend via <code>chrome-devtools-frontend.appspot.com</code> · local proxy <code id="proxyLbl">127.0.0.1:9223</code><br>Tip: <span style="background:var(--panel2);border:1px solid var(--line);padding:2px 6px;border-radius:6px;font-family:monospace;font-size:11px">r</span> in terminal to refresh</footer></div>
<div id="toast" class="toast"></div>
<div id="sheet" class="sheet" onclick="if(event.target===this) closeSheet()"><div class="sheet-box"><h3>Scan to open launcher</h3><div class="qr"><canvas id="qr"></canvas></div><div style="display:flex;gap:8px;justify-content:center"><button class="btn" onclick="copyShare()">Copy link</button><button class="btn btn-primary" onclick="closeSheet()">Done</button></div><p id="shareUrl" style="text-align:center;font-size:11px;word-break:break-all;color:var(--muted);margin-top:6px"></p></div></div>
<script>
const PROXY=location.host;let data={tabs:[],version:{}};
function toast(m,ms=2200){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),ms)}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function favIcon(u){try{if(!u) return '🌐';if(u.startsWith('chrome')) return '⚙';const h=new URL(u).hostname;return h[0]?h[0].toUpperCase():'🌐'}catch{return '🌐'}}
function favUrl(u){try{const h=new URL(u).hostname;if(!h) return '';return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(h)}&sz=64`}catch{return ''}}
function frontendUrl(id){const ws=`${PROXY}/${id}`;return `https://chrome-devtools-frontend.appspot.com/serve_file/@cea0d32fc090e98b9b4ac74a01fd7001acca04ff/devtools_app.html?ws=${encodeURIComponent(ws)}&remoteFrontend=true`}
async function load(){
  const s=document.getElementById('status'),c=document.getElementById('count'),L=document.getElementById('list');
  s.textContent='fetching…';s.className='badge';
  try{
    const r=await fetch('/api/tabs');if(!r.ok) throw new Error('HTTP '+r.status);
    data=await r.json();const tabs=data.tabs||[];
    document.getElementById('subtitle').textContent=`proxy ${data.proxy||PROXY} → cdp ${data.cdp||'127.0.0.1:9222'} · ${data.version?.Browser||''}`;
    document.getElementById('proxyLbl').textContent=data.proxy||PROXY;
    if(!Array.isArray(tabs)||tabs.length===0){c.textContent='0 tabs';s.textContent='no tabs — open Chrome';s.className='badge err';L.innerHTML=`<div class="empty">📭 No debuggable tabs<br><div style="margin-top:8px">Open Chrome, browse to a page, then <b>Refresh</b>.</div></div>`;return}
    s.textContent='connected';s.className='badge ok';c.textContent=`${tabs.length} tab${tabs.length>1?'s':''}`;render();
  }catch(e){c.textContent='— tabs';s.textContent='offline — is Chrome running?';s.className='badge err';L.innerHTML=`<div class="empty">⚠️ ${esc(e.message)}</div>`}
}
function render(){
  const L=document.getElementById('list');const q=(document.getElementById('q').value||'').toLowerCase();const f=document.getElementById('filter').value;let tabs=[...(data.tabs||[])];
  if(q) tabs=tabs.filter(t=> (t.title||'').toLowerCase().includes(q)||(t.url||'').toLowerCase().includes(q));
  if(f!=='all'){
    if(f==='webview') tabs=tabs.filter(t=> (t.url||'').includes('webview')||(t.type||'').includes('webview'));
    else if(f==='other') tabs=tabs.filter(t=> !['page','service_worker','background_page','webview'].includes(t.type));
    else tabs=tabs.filter(t=> t.type===f);
  }
  if(tabs.length===0){L.innerHTML=`<div class="empty">No matches for “${esc(q)}”</div>`;return}
  L.innerHTML='';tabs.forEach((t,i)=>{
    const id=t.id,title=t.title||'(no title)',url=t.url||'',type=t.type||'page';const fw=t._frontend||frontendUrl(id);
    const icon=`<img src="${favUrl(url)}" onerror="this.style.display='none'">`;
    const row=document.createElement('div');row.className='card';
    row.innerHTML=`<div class="fav">${icon}<span style="position:absolute">${favIcon(url)}</span></div><div class="main"><div class="title" title="${esc(title)}">${esc(title)}</div><div class="url" title="${esc(url)}">${esc(url)}</div><div class="meta"><span>${esc(type)}</span><span style="font-family:monospace">${esc(id.slice(0,8))}…</span><span>${i+1}/${data.tabs.length}</span></div><div style="display:flex;gap:6px;margin-top:6px;flex-wrap:wrap"><button class="btn" style="padding:6px 8px;font-size:11px;border-radius:999px" onclick="act('activate','${id}')">◎ Activate</button><button class="btn" style="padding:6px 8px;font-size:11px;border-radius:999px" onclick="act('reload','${id}')">↻ Reload</button><button class="btn" style="padding:6px 8px;font-size:11px;border-radius:999px" onclick="copyText('${fw.replace(/'/g,"\\'")}','Link copied')">⎘ Copy link</button><button class="btn" style="padding:6px 8px;font-size:11px;border-radius:999px;color:var(--err)" onclick="act('close','${id}')">✕ Close</button></div></div><div class="actions"><a class="btn btn-primary" href="${fw}" target="_blank" rel="noopener">Open →</a><button class="btn" onclick="copyText('${esc(url).replace(/'/g,"\\'")}','URL copied')">Copy URL</button></div>`;
    L.appendChild(row);
  });
}
async function act(a,id){try{const r=await fetch(`/api/${a}/${encodeURIComponent(id)}`);if(!r.ok) throw new Error(await r.text());toast(a+' ✓');setTimeout(load,600)}catch(e){toast('Failed: '+e.message,3000)}}
function copyText(s,m){navigator.clipboard.writeText(s).then(()=>toast(m),()=>toast('Copy failed'))}
function copyAll(){copyText(JSON.stringify(data,null,2),'JSON copied')}
function newTabPrompt(){const url=prompt('Open URL in new tab:','https://example.com');if(url) fetch(`/api/new?url=${encodeURIComponent(url)}`).then(()=>{toast('New tab ✓');setTimeout(load,800)}).catch(e=>toast(String(e)))}
function openSheet(){const url=location.href;document.getElementById('shareUrl').textContent=url;document.getElementById('sheet').classList.add('show');const c=document.getElementById('qr');const ctx=c.getContext('2d');c.width=220;c.height=220;ctx.fillStyle='#fff';ctx.fillRect(0,0,220,220);const img=new Image();img.crossOrigin='anonymous';img.onload=()=>{ctx.clearRect(0,0,220,220);ctx.drawImage(img,0,0,220,220)};img.onerror=()=>{ctx.fillStyle='#0b0e14';ctx.font='10px monospace';ctx.fillText('QR requires internet',110,100)};img.src=`https://api.qrserver.com/v1/create-qr-code/?size=220x220&data=${encodeURIComponent(url)}`}
function closeSheet(){document.getElementById('sheet').classList.remove('show')}
function copyShare(){copyText(location.href,'Link copied')}
load();setInterval(load,5000);
</script>
</body>
</html>
"""

async def fetch_tabs_retry():
    _ensure_deps()
    for _ in range(3):
        t,v=await fetch_tabs_and_version()
        if t is not None: return t,v
        await asyncio.sleep(1)
    return None,{}

async def print_tabs(tabs,version):
    if not tabs:
        warn("No tabs — open Chrome and keep it foreground, then press r to refresh.")
        return
    browser=(version or {}).get("Browser","") if isinstance(version,dict) else ""
    if browser: info(f"Browser: {browser}")
    print(f"\n{C_BOLD}Tabs ({len(tabs)}):{C_RESET}")
    for idx,t in enumerate(tabs):
        title=(t.get("title") or "(no title)")[:70]
        url=(t.get("url") or "")[:100]
        typ=t.get("type","page")
        col=C_GREEN if idx%2==0 else C_CYAN
        print(f"  {col}[{idx}] {title}{C_RESET}")
        print(f"      {C_DIM}{url}{C_RESET}")
        print(f"      {C_DIM}id={t.get('id','')} type={typ}{C_RESET}")
        print(f"      {C_DIM}DevTools: {build_frontend_url(t,version)}{C_RESET}")

async def terminal_loop(no_browser,verbose):
    _ensure_deps()
    await asyncio.sleep(1.2)
    tabs,ver=await fetch_tabs_retry()
    if tabs is None:
        err("Cannot reach CDP at 127.0.0.1:9222. Is Chrome running? Did adb forward succeed?")
        return
    await print_tabs(tabs,ver)
    print()
    ok(f"Web UI → http://{CDP_HOST}:{PROXY_PORT}/   (search, QR, reload/close)")
    ok(f"API    → http://{CDP_HOST}:{PROXY_PORT}/api/tabs  · /health")
    if not no_browser: info("Type an index (0-N) or ID prefix to open DevTools; r=refresh, n=new tab, q=quit.")
    else: info("--no-browser: not auto-opening. Use Web UI or type index to get link.")
    print(f"{C_BOLD}▶ Enter index/ID, r=refresh, n=new tab, q=quit: {C_RESET}",end="",flush=True)
    loop=asyncio.get_event_loop()
    while True:
        try:
            line=await loop.run_in_executor(None, sys.stdin.readline)
            if not line: await asyncio.sleep(0.5); continue
            s=line.strip()
            if not s: print(f"Enter index/ID, r, n, q: ",end="",flush=True); continue
            low=s.lower()
            if low in ("r","refresh","ls","list"):
                tabs,ver=await fetch_tabs_retry()
                await print_tabs(tabs or [],ver)
                print(f"\nEnter index/ID, r, n, q: ",end="",flush=True); continue
            if low in ("q","quit","exit"): raise KeyboardInterrupt
            if low in ("n","new"):
                print("URL for new tab (https://...): ",end="",flush=True)
                url=(await loop.run_in_executor(None, sys.stdin.readline)).strip() or "about:blank"
                try:
                    async with aiohttp.ClientSession() as cs:
                        async with cs.get(f"http://{CDP_HOST}:{CDP_PORT}/json/new?url={urllib.parse.quote(url,safe='')}") as r:
                            j=await r.json() if r.headers.get("content-type","").count("json") else await r.text()
                            ok(f"New tab: {j}")
                except Exception as e: err(str(e))
                tabs,ver=await fetch_tabs_retry()
                await print_tabs(tabs or [],ver)
                print(f"\nEnter index/ID, r, n, q: ",end="",flush=True); continue
            target=None
            if s.isdigit():
                i=int(s)
                if tabs and 0<=i<len(tabs): target=tabs[i]["id"]
                else: target=s
            else:
                if tabs:
                    ms=[t for t in tabs if t.get("id","").startswith(s)]
                    if len(ms)==1: target=ms[0]["id"]
                    elif len(ms)>1:
                        warn(f"Ambiguous prefix matches {len(ms)} tabs — be more specific")
                        print("Enter index/ID: ",end="",flush=True); continue
                    else: target=s
                else: target=s
            if not target: warn("No match"); print("Enter index/ID: ",end="",flush=True); continue
            fw=build_frontend_url(target,ver)
            print(f"DevTools for {target[:12]}…:\n  {C_CYAN}{fw}{C_RESET}",flush=True)
            opened=False
            for opener in ("termux-open-url","xdg-open","open"):
                if which(opener):
                    rc,out=run([opener,fw],timeout=8)
                    if rc==0: ok(f"Opened via {opener}"); opened=True; break
            if not opened: warn("No opener — copy the URL above into Chrome.")
            await asyncio.sleep(0.6)
            print(f"\nEnter another index/ID, r, n, q: ",end="",flush=True)
        except (EOFError, KeyboardInterrupt):
            print("\nBye.",flush=True)
            asyncio.get_event_loop().stop()
            return
        except Exception as e:
            err(f"input error: {e}")
            await asyncio.sleep(1)

def create_app():
    _ensure_deps()
    # Fix for aiohttp 3.14: middleware must have __middleware_version__ == 1, else aiohttp treats it as factory (app, handler)
    try:
        cors_middleware.__middleware_version__ = 1  # type: ignore
    except: pass
    app=web.Application(middlewares=[cors_middleware])
    app.router.add_get("/",ui_handler)
    app.router.add_get("/health",health_handler)
    app.router.add_get("/api/tabs",api_tabs)
    app.router.add_get("/api/{action}/{id}",api_action)
    async def api_new_wrapper(request):
        request.match_info["action"]="new"
        request.match_info["id"]=""
        return await api_action(request)
    app.router.add_get("/api/new",api_new_wrapper)
    app.router.add_get("/json",json_proxy)
    app.router.add_get("/json/{tail:.*}",json_proxy)
    app.router.add_get("/{id}",websocket_handler)
    app.router.add_get("/devtools/page/{id}",websocket_handler)
    app.router.add_get("/devtools/browser/{id}",websocket_handler)
    return app

async def run_proxy(host=CDP_HOST,port=PROXY_PORT):
    _ensure_deps()
    app=create_app()
    runner=web.AppRunner(app)
    await runner.setup()
    site=web.TCPSite(runner,host,port)
    await site.start()
    ok(f"Proxy listening on http://{host}:{port}  → CDP {CDP_HOST}:{CDP_PORT}")
    try:
        import socket as _sock
        hn=_sock.gethostname()
        ips=_sock.gethostbyname_ex(hn)[2]
        for ip in ips:
            if not ip.startswith("127."):
                info(f"LAN share: http://{ip}:{port}/  (if adb forwarded, use 127.0.0.1)")
                break
    except Exception: pass
    await asyncio.Event().wait()

def banner():
    print(f"{C_BOLD}{C_CYAN}DevDroid v{VERSION}{C_RESET} — Chrome DevTools on-device (ADB/Shizuku)")
    print(f"{C_DIM}CDP {CDP_HOST}:{CDP_PORT}  ·  Proxy {CDP_HOST}:{PROXY_PORT}  ·  https://github.com/SayCrazyy2/DevDroid{C_RESET}")
    if have_termux(): print(f"{C_DIM}Termux {os.environ.get('PREFIX','')}  ·  rish={'yes' if has_rish() else 'no'}  adb={'yes' if which('adb') else 'no'}{C_RESET}")
    print()

def parse_args():
    p=argparse.ArgumentParser(description="DevDroid — full Chrome DevTools on Android via ADB/Shizuku",formatter_class=argparse.RawDescriptionHelpFormatter,epilog="""
examples:
  %(prog)s                    # auto (root or shizuku if rish present)
  %(prog)s --shizuku          # force Shizuku (non-root, no PC after first setup)
  %(prog)s --root             # force root (su)
  %(prog)s 1234               # use existing wireless ADB on 1234 (PC's `adb tcpip 1234`)
  %(prog)s --port 5555 --shizuku
  %(prog)s --list             # just list tabs and exit
  %(prog)s --open 0           # open tab index 0 and exit (print URL)
  %(prog)s --no-browser --cdp-port 9222 --proxy-port 9223 --host 127.0.0.1
        """)
    p.add_argument("port",nargs="?",help="existing wireless ADB port (manual mode)")
    p.add_argument("--port",dest="port_opt",type=int,help="wireless ADB port (alt)")
    p.add_argument("--shizuku",action="store_true",help="use Shizuku/rish")
    p.add_argument("--sui",action="store_true",help="use Sui (alias of --shizuku)")
    p.add_argument("--root",action="store_true",help="force root via su")
    p.add_argument("--host",default=CDP_HOST,help=f"proxy host (default {CDP_HOST})")
    p.add_argument("--cdp-port",type=int,default=CDP_PORT,help=f"CDP port (default {CDP_PORT})")
    p.add_argument("--proxy-port",type=int,default=PROXY_PORT,help=f"proxy port (default {PROXY_PORT})")
    p.add_argument("--no-browser",action="store_true",help="don't try termux-open-url")
    p.add_argument("--list",action="store_true",help="list tabs and exit")
    p.add_argument("--open",metavar="INDEX_OR_ID",help="open one tab and exit (print URL)")
    p.add_argument("--verbose",action="store_true",help="verbose adb/frontend logs")
    p.add_argument("--keep-adb",action="store_true",help="don't remove adb forward on exit")
    p.add_argument("--version",action="store_true",help="show version and exit")
    return p.parse_args()

async def one_shot_list(cdp_port):
    _ensure_deps()
    tabs,ver=await fetch_tabs_and_version()
    if tabs is None:
        err(f"CDP not reachable at 127.0.0.1:{cdp_port}. Is Chrome running?")
        sys.exit(1)
    await print_tabs(tabs,ver)
    print("\n--- JSON ---")
    print(json.dumps({"tabs":tabs,"version":ver},indent=2))

async def main_async(args):
    global CDP_HOST,CDP_PORT,PROXY_PORT
    CDP_HOST=args.host if args.host else CDP_HOST
    CDP_PORT=args.cdp_port
    PROXY_PORT=args.proxy_port
    if args.verbose: logging.getLogger().setLevel(logging.DEBUG)
    banner()
    if args.list or args.open:
        tabs,ver=await fetch_tabs_and_version()
        if tabs is None:
            err("CDP not reachable — trying to ensure adb forward…")
        tabs,ver=await fetch_tabs_and_version()
        if args.list:
            await one_shot_list(CDP_PORT)
            return
        if args.open is not None:
            if tabs is None:
                err("No tabs"); sys.exit(1)
            target=args.open
            if target.isdigit() and tabs and 0<=int(target)<len(tabs): target=tabs[int(target)]["id"]
            fw=build_frontend_url(target,ver)
            print(fw)
            if not args.no_browser:
                for op in ("termux-open-url","xdg-open","open"):
                    if which(op): run([op,fw]); break
            return
    port=args.port_opt if args.port_opt is not None else (int(args.port) if args.port and args.port.isdigit() else None)
    if args.port and not str(args.port).isdigit() and args.port_opt is None:
        err(f"Invalid port {args.port!r} — must be integer"); sys.exit(1)
    use_shizuku=args.shizuku or args.sui
    use_root=args.root
    if port is None:
        detected=detect_privilege()
        info(f"Privilege: {detected} (root={has_root()}, rish={has_rish()}, shizuku_app={has_shizuku_app()}, sui={has_sui()}, dhizuku={has_dhizuku()})")
        method=None
        if use_root: method="root"
        elif use_shizuku: method="shizuku"
        else:
            if detected=="shizuku": method="shizuku"
            elif detected=="root": method="root"
            else:
                err("No wireless ADB port and no privileged method found.")
                print(f"\n{C_BOLD}Options:{C_RESET}")
                print(f"  1) Shizuku (no PC, no root):  ./devtools.py --shizuku  (see --help for one-time setup)")
                print(f"  2) With PC (once per boot):    adb tcpip 1234  (on PC)  →  ./devtools.py 1234")
                print(f"  3) Rooted:                     ./devtools.py  (auto)")
                sys.exit(1)
        if method=="root" and not has_root():
            err("Root requested but `su -c 'id'` failed. Try --shizuku if you have Shizuku (rish)."); sys.exit(1)
        if method in ("shizuku","sui") and not has_rish():
            err("Shizuku requested but rish failed.")
            if has_shizuku_app(): err("Fix: Shizuku → Use in terminal apps → Export files → cp /data/local/tmp/shizuku/rish* $PREFIX/bin/ && chmod +x $PREFIX/bin/rish")
            sys.exit(1)
        port=random.randint(10000,60000)
        log(f"Using random wireless ADB port {port} via {method}",C_YELLOW)
        wireless_ok = setup_wireless_adb(port,method)
        if not wireless_ok:
            if method in ("shizuku","sui","shizuku-pending"):
                warn("Wireless ADB failed — trying Shizuku direct-forward (bypasses adb tcp, uses rish socat/python)…")
                if start_shizuku_direct_forward(cdp_port=CDP_PORT, verbose=args.verbose):
                    ok("Direct forward active — will use it instead of adb forward")
                    # Mark port as None to skip adb connect/forward
                    port = None
                else:
                    err("Wireless ADB setup failed and direct forward also failed."); sys.exit(1)
            else:
                err("Wireless ADB setup failed."); sys.exit(1)
    else: info(f"Manual wireless ADB port {port}")

    # Try adb forward if we have a wireless port, otherwise we already have direct forward
    use_direct = (port is None)
    if not use_direct:
        ensure_adb()
        if not connect_and_forward(port,cdp_port=CDP_PORT,verbose=args.verbose):
            if (use_shizuku or detect_privilege()=="shizuku") and start_shizuku_direct_forward(cdp_port=CDP_PORT, verbose=args.verbose):
                warn("adb forward failed — but direct Shizuku forward succeeded, continuing")
                use_direct = True
            else:
                err("ADB forward failed — troubleshooting:")
                err("  adb devices; adb forward --list; curl -v http://127.0.0.1:9222/json; rish -c 'id'  (if Shizuku)")
                err("  Also try: rish -c 'curl -v http://127.0.0.1:9222/json'  after starting Chrome")
                sys.exit(1)
    else:
        # Verify direct forward is actually listening (retry if needed)
        time.sleep(0.5)
        rc, out = run(f"curl -s --connect-timeout 2 http://127.0.0.1:{CDP_PORT}/json/version 2>&1 | head -3", shell=True, timeout=5)
        if "Browser" not in out:
            warn(f"Direct forward on :{CDP_PORT} not yet responding (Chrome foreground?). Will still start proxy — open Chrome and retry")
            # Also try to ensure Chrome is running
            info("Hint: keep Chrome in foreground with a tab open, then refresh Web UI")
    keep=args.keep_adb
    # capture use_direct for cleanup closure
    _use_direct = locals().get("use_direct", False)
    def _sig(*_):
        print()
        if not keep:
            warn("Removing adb forward…")
            run(f"adb forward --remove tcp:{CDP_PORT}",shell=True)
            if _use_direct:
                info("Stopping direct forward…")
                privileged_exec(f"pkill -f devdroid_forward.py 2>/dev/null; pkill -f 'socat.*:{CDP_PORT}' 2>/dev/null; true", "shizuku")
                run(f"pkill -f devdroid_forward.py 2>/dev/null; pkill -f 'socat.*:{CDP_PORT}' 2>/dev/null; true", shell=True)
        sys.exit(0)
    for sig in (signal.SIGINT,signal.SIGTERM):
        try: signal.signal(sig,_sig)
        except Exception: pass
    try: await asyncio.gather(run_proxy(host=args.host,port=PROXY_PORT),terminal_loop(args.no_browser,args.verbose))
    except asyncio.CancelledError: pass
    finally:
        if not keep:
            try: run(f"adb forward --remove tcp:{CDP_PORT}",shell=True,timeout=5)
            except Exception: pass
            if locals().get("use_direct") or locals().get("_use_direct"):
                try:
                    privileged_exec(f"pkill -f devdroid_forward.py 2>/dev/null; pkill -f 'socat.*:{CDP_PORT}' 2>/dev/null; true", "shizuku")
                    run(f"pkill -f devdroid_forward.py 2>/dev/null; true", shell=True)
                except Exception: pass

def main():
    args=parse_args()
    if args.version: print(VERSION); sys.exit(0)
    try: asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\nBye.",flush=True)
        if not getattr(args,"keep_adb",False):
            try: run(f"adb forward --remove tcp:{CDP_PORT}",shell=True,timeout=5)
            except Exception: pass

if __name__=="__main__": main()
