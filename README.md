# DevDroid v2.2

<p>
  <a href="https://github.com/SayCrazyy2/DevDroid/actions"><img alt="python" src="https://img.shields.io/badge/python-3.9%20%2B-blue"></a>
  <a href="https://shizuku.rikka.app"><img alt="shizuku" src="https://img.shields.io/badge/Shizuku-non--root%20%7C%20Adb%20shell-7c5cff"></a>
  <a href="LICENSE"><img alt="license" src="https://img.shields.io/badge/license-MIT-green"></a>
  <a href="https://github.com/SayCrazyy2/DevDroid/issues"><img alt="issues" src="https://img.shields.io/badge/issues-welcome-orange"></a>
</p>

**Full Chrome DevTools on your phone** — *DevDroid is a rebrand & enhanced fork of [lyc8503/AndroidChromeDevTools](https://github.com/lyc8503/AndroidChromeDevTools) with Shizuku support & modern UI.* — inspect, debug JS, network, performance, WebViews, without a PC.

<p float="left">
  <img src="docs/img1.jpg" width="200" height="400" />
  <img src="docs/img2.jpg" width="200" height="400"/> 
  <img src="docs/img3.jpg" width="200" height="400"/>
</p>

### What's new in v2.2

| Feature | Detail |
|---|---|
| **Shizuku / Sui / Dhizuku** | Non-root wireless ADB via `rish -c` (auto-detected). No PC after first Shizuku boot. Also handles MIUI/HyperOS/ColorOS + Android 14+ wireless debugging quirks. |
| **Web launcher** | `http://127.0.0.1:9223/` — search, filter by type, favicons, **Open/Activate/Reload/Close/New-tab**, QR share, copy link/JSON, auto-refresh. |
| **Smarter proxy** | Rewrites tab's `devtoolsFrontendUrl` (no hardcoded hash drift), tries `chrome_devtools_remote*` + `webview_devtools_remote*`, real CORS preflight, `127.0.0.1` only. |
| **Terminal UI** | Colors, `r`/`n`/`q`, index *or* ID prefix, prints frontend URL, `--list` / `--open` one-shots, `--verbose`. |
| **Setup** | `setup.sh` with `--check/--update/--uninstall`, wrapper `devtools` in `$PREFIX/bin`, `.pyproject.toml` + pinned `requirements.txt`. |

### Prerequisites

- Android phone, Chrome/Chromium/WebView app
- [Termux](https://f-droid.org/packages/com.termux/) (F-Droid) — Play Store build is broken
- For non-root PC-free: [Shizuku](https://shizuku.rikka.app/) (or Sui/Dhizuku)

### Quick setup (Termux)

```bash
# One-liner (installs python, adb, deps, wrapper, rish if available)
curl -fsSL https://raw.githubusercontent.com/SayCrazyy2/DevDroid/main/setup.sh | bash

# Verify
devtools --help
devtools --check   # via setup.sh --check

# Manual alternative
pkg up -y && pkg install -y python android-tools termux-tools
pip install -q aiohttp websockets
curl -o devtools.py https://raw.githubusercontent.com/SayCrazyy2/DevDroid/main/devtools.py && chmod +x devtools.py
```

> **Wake-lock tip:** `termux-wake-lock` prevents Android killing the proxy in background. Run it before `devtools` on some ROMs.

### Usage

#### A — Non-root with Shizuku (recommended, no PC)

One-time (after each reboot, redo step 2 only):

1. **Start Shizuku** via Wireless Debugging — Developer options → **Wireless debugging** ON → Shizuku → **Start** (pair if asked). Android 14+ can use a Quick Tile / Termux `shizuku` script — see [Shizuku #462](https://github.com/RikkaApps/Shizuku/discussions/462).
2. **Export `rish`**: Shizuku → *Use Shizuku in terminal apps* → **Export files** → in Termux:
   ```bash
   cp /data/local/tmp/shizuku/rish* $PREFIX/bin/
   chmod +x $PREFIX/bin/rish
   rish -c 'id'   # must show uid=2000(shell)
   ```
3. Run:
   ```bash
   devtools --shizuku
   # or ./devtools.py --shizuku
   # custom port: devtools --shizuku --port 5555
   ```

Reboot → Shizuku shows red → toggle Wireless Debugging → `rish -c 'id'` again → `devtools --shizuku`.

#### B — Non-root with PC (once per boot, legacy)

```bash
# On PC with USB connected:
adb tcpip 1234
# On phone in Termux:
devtools 1234   # or devtools --port 1234
# until reboot, just: devtools 1234
```

> **Pairing vs connect port:** Android 11+ Wireless Debugging shows *two* ports — use the **connect** port (e.g. 37193) for `adb connect`, not the 6-digit pairing port.

#### C — Rooted

```bash
devtools              # auto random port via su
devtools --root
devtools --root --port 5555
```

Auto-detection: if `rish` exists it prefers Shizuku, else `su` — override with `--shizuku` / `--root`.

### Web UI & terminal

After `adb forward` succeeds:

- **Web UI:** `http://127.0.0.1:9223/` → card list with title, URL, favicon, type, ID. Tap **Open DevTools →** (opens `chrome-devtools-frontend.appspot.com` via our WS proxy). Also **Activate / Reload / Close / New tab** (uses `POST /json/*`), **Copy link / Copy JSON**, **QR** for sharing to a PC on same `adb forward`.
- **Terminal:** colored index list + full IDs + clickable frontend URLs. Commands:
  - `0`…`n` — open by index
  - `abc123` — open by ID or prefix
  - `r` — refresh
  - `n` — new tab (prompt URL)
  - `q` — quit (removes `adb forward` unless `--keep-adb`)
- **CLI one-shots:**
  ```bash
  devtools --list                 # print tabs + JSON and exit
  devtools --open 0               # print+open frontend for tab 0
  devtools --open <id> --no-browser  # just print URL (for SSH)
  ```

Direct links look like:
```
https://chrome-devtools-frontend.appspot.com/serve_file/@<hash>/devtools_app.html?ws=127.0.0.1:9223/<id>&remoteFrontend=true
```

### Advanced options

```bash
devtools --help
# --port / --cdp-port / --proxy-port / --host
# --shizuku / --sui / --root / --no-browser / --verbose / --keep-adb
# --list / --open INDEX_OR_ID

devtools --shizuku --cdp-port 9222 --proxy-port 9223 --host 127.0.0.1
devtools --port 5555 --proxy-port 9224 --verbose
devtools --list --cdp-port 9222
```

Endpoints while running: `/` (launcher) · `/health` · `/api/tabs` · `/api/{close,activate,reload}/{id}` · `/api/new?url=` · `/json` · `/json/version` · `/{id}` (WS) · `/devtools/page/{id}`.

### How it works

1. Enable `service.adb.tcp.port` + `ctl.restart adbd` via `su` or `rish -c` (shell `uid=2000`) — or reuse existing `adb tcpip`.
2. `adb connect localhost:<port>` + `adb forward tcp:9222 localabstract:chrome_devtools_remote` (falls back to `_local`, `_0`, `webview_devtools_remote*`).
3. Python proxy on `127.0.0.1:9223` bridges WS `ws://127.0.0.1:9222/devtools/...` with CORS for `chrome-devtools-frontend.appspot.com`.
4. UI fetches `http://127.0.0.1:9222/json`, rewrites `devtoolsFrontendUrl`'s `ws=` to our proxy; frontend then talks CDP through us.

### Troubleshooting

<details><summary><b>Shizuku: <code>rish: not found</code> / <code>uid != 2000</code></b></summary>

Shizuku app → *Use in terminal apps* → Export files → Termux:
```bash
cp /data/local/tmp/shizuku/rish* $PREFIX/bin/
chmod +x $PREFIX/bin/rish
rish -c 'id'   # expect uid=2000(shell)
```
If red in Shizuku, re-enable Wireless Debugging and tap Start again. Some launchers need *Disable permission monitoring* (MIUI/HyperOS) OFF and battery optimization OFF for Shizuku.

</details>

<details><summary><b><code>Failed to connect to wireless ADB</code> / <code>adb devices</code> empty</b></summary>

```bash
adb kill-server; adb devices; adb forward --list; curl -v http://127.0.0.1:9222/json
rish -c 'getprop service.adb.tcp.port; id'
```
- Confirm you used the **connect** port, not pairing port.
- Try another port: `devtools --shizuku --port 5555`.
- OEMs that kill `adbd`: retry, or `rish -c 'setprop ctl.restart adbd'`.

</details>

<details><summary><b><code>Failed to fetch tabs</code> — Chrome not reachable</b></summary>

Bring Chrome to foreground with a tab open, wait 2s, press `r` or refresh web UI. Check `adb forward --list` contains `tcp:9222`. Some WebViews use `webview_devtools_remote_0` — the script now tries those automatically; if still empty, enable *WebView debugging* in the target app or use Chrome.

</details>

<details><summary><b>Blank DevTools / websocket error</b></summary>

Hard refresh DevTools, open `/health`. The frontend hash can be stale — the proxy now prefers the tab's own `devtoolsFrontendUrl`; fallback hash auto-resolves via appspot `serve_file`. If blocked (China / no internet), host a mirror or use `chrome://inspect` fallback.

</details>

<details><summary><b><code>Process completed (signal 9)</code> — phantom killer</b></summary>

Android 12+ kills Termux. If `rish` available:

```bash
rish -c 'device_config put activity_manager max_phantom_processes 2147483647'
rish -c 'settings put global settings_enable_monitor_phantom_procs false'  # some ROMs
```

Also: `termux-wake-lock`, and exempt Termux/Shizuku from battery optimization. See [Willie169/Android-Non-Root](https://willie169.github.io/Android-Non-Root/).

</details>

Please attach `setup.sh --check`, `adb devices`, `adb forward --list`, `rish -c 'id'` and Chrome version when filing an issue.

### Alternatives

[ChromeXt](https://github.com/JingMatrix/ChromeXt) (LSPosed module that embeds DevTools in Chrome) — good if you prefer not using Termux.

### Changelog

- **2.2** — UI redesign (search/filter/QR/actions), `devtoolsFrontendUrl` rewrite, WebView abstracts, `/api/*`, `--list/--open/--verbose/--keep-adb`, hardened `setup.sh`, `pyproject.toml`.
- **2.1** — Shizuku via `rish`, web launcher, auto privilege detection.
- **2.0** — `aiohttp` + `websockets` modern, CORS preflight, cleanup, retry.
- **1.x** — Initial `su` / `adb tcpip` flow.

### License

MIT — see `LICENSE`.
