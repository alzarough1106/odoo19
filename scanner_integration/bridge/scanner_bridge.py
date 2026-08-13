#!/usr/bin/env python3
# scanner_bridge.py
# Copyright (C) 2026 alzarough1106
# License OPL-1 - See LICENSE file
# Unauthorized use is strictly prohibited!
"""
Cheque Scanner Bridge  v1.7.11
==============================
ws://localhost:8765  →  Odoo Cheque Book / Document Scanner

Changes in v1.7.11
------------------
- FIX: Content-signature duplicate detection was too aggressive for
  cheque-book scans.  Cheques from the same book share an identical
  printed template (bank logo, layout, cheque numbers, MICR line) and
  only the handwritten date/amount/signature vary — at 64×64 those
  variations averaged below the old threshold of 3.0 → false positive.
  Now:
    * Signature raised to 128×128 grayscale for finer resolution.
    * AVG_DIFF_THRESHOLD lowered to 1.0 (only near-perfect duplicates).
    * Two-factor check: BOTH byte-size within 300 bytes AND content
      signature match must hold before a scan is flagged as duplicate.
- NEW: Per-page progress notifications during ADF batch scans.  The
  bridge now sends {"status": "progress", "page": N} messages via the
  WebSocket after every page, so the JS client stays alive during long
  scans and can update the UI incrementally.  Prevents "Request timed
  out" errors from short JS timeouts.
- scan_adf() and _scan_wia_adf() accept a progress_cb callback; the
  WebSocket handler wires it up via an asyncio.Queue + run_coroutine_
  threadsafe bridge from the executor thread.

Changes in v1.7.10
------------------
- _scan_wia_adf() overhauled: WIA property enumeration by PropertyID
  (fixes "Index out of range" on PaperStream WIA), content-signature
  duplicate detection, cached TWAIN "known broken" state.

Changes in v1.7.9
-----------------
- _scan_wia_adf() rewritten for Fujitsu fi-7xxx / PaperStream WIA.
- File logging added; _is_frozen() for compiled builds.

Changes in v1.7.8
-----------------
- _scan_twain_adf() rewritten for PaperStream IP.

Install
-------
  Windows : pip install websockets Pillow twain pywin32
  Linux   : pip install websockets Pillow python-sane
  macOS   : pip install websockets Pillow
            brew install sane-backends

Run
---
  python cheque_scanner_bridge.py
  python cheque_scanner_bridge.py --show-ui
  python cheque_scanner_bridge.py --port 9000
"""

import asyncio
import websockets
import json
import base64
import io
import logging
import sys
import argparse
import os
import subprocess
import tempfile
import time
import re
import urllib.request
import urllib.error
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor


# ─── Nuitka / PyInstaller frozen-build compatibility ─────────────────────────
def _is_frozen() -> bool:
    """True when running as a compiled .exe (Nuitka --onefile or PyInstaller)."""
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


if _is_frozen():
    if len(sys.argv) > 0:
        sys.argv[0] = "scanner_bridge.exe"


# ─── Logging (file + stdout, works even with hidden console) ─────────────────
_LOG_FILE = os.path.join(tempfile.gettempdir(), "scanner_bridge.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(_LOG_FILE, mode="a", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("scanner_bridge")

executor = ThreadPoolExecutor(max_workers=1)
SHOW_UI = False

# Cache TWAIN's "doesn't work with this driver" state so we don't waste
# ~2 seconds retrying it on every scan_adf request.
_TWAIN_KNOWN_BROKEN = False

# Suppress CMD flash on Windows for all subprocess calls
_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32"
    else {}
)

# Cache file written whenever eSCL discovery succeeds.
_ESCL_URL_CACHE = os.path.join(tempfile.gettempdir(), ".escl_url_cache_bridge")


# ─── Device-name humanisation helpers ────────────────────────────────────────

def _sane_backend_vendor(dev_name: str) -> str:
    backend = dev_name.split(":")[0].lower().rstrip("0123456789")
    return {
        "hpaio":    "HP",
        "hpoj":     "HP",
        "pixma":    "Canon",
        "bjnp":     "Canon",
        "epson":    "Epson",
        "epson2":   "Epson",
        "epsonds":  "Epson",
        "brother":  "Brother",
        "brother4": "Brother",
        "xerox":    "Xerox",
        "lexmark":  "Lexmark",
        "samsung":  "Samsung",
        "ricoh":    "Ricoh",
        "fujitsu":  "Fujitsu",
        "kodak":    "Kodak",
        "mustek":   "Mustek",
        "umax":     "UMAX",
    }.get(backend, "")


def _humanise_sane_device(dev_name: str, vendor: str = "", model: str = "") -> str:
    display = f"{vendor} {model}".strip()
    if display:
        return display

    m = re.search(r"/(?:usb|net|tcp)/([^?;/#]+)", dev_name)
    if m:
        name = m.group(1).replace("_", " ").replace("-", " ").strip()
        prefix = _sane_backend_vendor(dev_name)
        return f"{prefix} {name}".strip() if prefix else name

    m = re.search(r"^[a-z0-9]+:([A-Za-z][A-Za-z0-9_\-]+)", dev_name)
    if m:
        name = m.group(1).replace("_", " ").replace("-", " ").strip()
        prefix = _sane_backend_vendor(dev_name)
        return f"{prefix} {name}".strip() if prefix else name

    m = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", dev_name)
    if m:
        prefix = _sane_backend_vendor(dev_name)
        return f"{prefix} ({m.group(1)})".strip() if prefix else dev_name

    return dev_name


def _humanise_wia_device(dev_id: str, name: str = "", manufacturer: str = "",
                          description: str = "") -> str:
    mfr  = manufacturer.strip()
    nm   = name.strip()
    desc = description.strip()

    _generic = {"wia", "wia scanner", "scanner", "image", "unknown"}
    if nm.lower() in _generic:
        nm = ""

    if mfr and nm:
        return f"{mfr} {nm}"
    if nm:
        return nm
    if desc and desc.lower() not in _generic:
        return desc

    _vid_map = {
        "03F0": "HP",
        "04A9": "Canon",
        "04B8": "Epson",
        "04F9": "Brother",
        "04E8": "Samsung",
        "0924": "Xerox",
        "043D": "Lexmark",
        "04DA": "Panasonic",
        "08F0": "Microtek",
        "055F": "Mustek",
        "0638": "Avision",
        "04C5": "Fujitsu",
    }
    m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", dev_id)
    if m:
        vid    = m.group(1).upper()
        pid    = m.group(2).upper()
        vendor = _vid_map.get(vid, f"VID_{vid}")
        return f"{vendor} Scanner (PID {pid})"

    m2 = re.search(r"}\s*\\\s*(\d+)\s*$", dev_id)
    if m2:
        return f"Scanner (device {m2.group(1)})"

    return dev_id or "Unknown Scanner"


# ─── Windows scanner discovery ────────────────────────────────────────────────

def get_scanners_wia() -> list:
    scanners = []
    seen = set()

    def _add(name: str, display: str, source: str, ready: bool, note: str = ""):
        key = display.strip().lower()
        if key and key not in seen:
            seen.add(key)
            scanners.append({
                "name":    name,
                "display": display.strip(),
                "source":  source,
                "ready":   ready,
                "note":    note,
            })

    try:
        import wia
        for d in wia.DeviceManager().devices:
            display = _humanise_wia_device(
                d.id,
                name=getattr(d, "name", ""),
                manufacturer=getattr(d, "manufacturer", ""),
            )
            _add(d.id, display, "wia-package", True)
        log.info("get_scanners_wia: wia package found %d device(s)", len(scanners))
    except ImportError:
        log.info("get_scanners_wia: wia package not installed")
    except Exception as e:
        log.warning("get_scanners_wia: wia package failed: %s", e)

    try:
        import win32com.client
        mgr = win32com.client.Dispatch("WIA.DeviceManager")

        for i in range(1, mgr.DeviceInfos.Count + 1):
            try:
                info = mgr.DeviceInfos.Item(i)
                dev_id = ""
                name = ""
                manufacturer = ""
                description = ""

                for prop_name, target in [
                    ("DeviceID",     "dev_id"),
                    ("Name",         "name"),
                    ("Manufacturer", "manufacturer"),
                    ("Description",  "description"),
                ]:
                    try:
                        val = str(info.Properties(prop_name).Value or "")
                        if   target == "dev_id":        dev_id = val
                        elif target == "name":          name = val
                        elif target == "manufacturer":  manufacturer = val
                        elif target == "description":   description = val
                    except Exception:
                        pass

                display = _humanise_wia_device(dev_id, name, manufacturer, description)

                device_type = 0
                try:
                    device_type = info.Type
                except Exception:
                    pass

                type_label = {1: "Scanner", 2: "Camera", 3: "Video"}.get(
                    device_type, "Device"
                )
                note = type_label if device_type != 1 else ""
                _add(dev_id or name, display, "WIA-COM", True, note)

            except Exception as ex:
                log.warning("get_scanners_wia: WIA COM item %d failed: %s", i, ex)

        log.info(
            "get_scanners_wia: WIA COM found %d device(s) total so far",
            len(scanners),
        )
    except ImportError:
        log.info("get_scanners_wia: win32com not installed (pip install pywin32)")
    except Exception as e:
        log.warning("get_scanners_wia: WIA COM failed: %s", e)

    try:
        ps_cmd = (
            "Get-PnpDevice -Class 'Image','Printer' -Status 'OK','Unknown' "
            "| Select-Object InstanceId, FriendlyName, Manufacturer, Class, Status "
            "| ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
            **_NO_WINDOW,
        )
        if result.stdout.strip():
            raw = json.loads(result.stdout.strip())
            if isinstance(raw, dict):
                raw = [raw]
            for d in raw:
                dev_id   = d.get("InstanceId", "")
                friendly = d.get("FriendlyName", "")
                mfr      = d.get("Manufacturer", "")
                cls      = d.get("Class", "")
                display  = _humanise_wia_device(dev_id, friendly, mfr)

                note  = ""
                ready = True
                if cls.lower() == "printer":
                    note  = "Printer — may have scan capability"
                    ready = False

                if cls.lower() == "printer" and not re.search(
                    r"scan|mfp|multifunction|aio|officejet|deskjet|"
                    r"envy|pixma|workforce|mfc|laserjet|color\s*laser|"
                    r"fi-\d|fujitsu|scansnap|kodak\s*i\d|kv-s\d",
                    friendly, re.I,
                ):
                    continue

                _add(dev_id, display, "PnpDevice", ready, note)

        log.info(
            "get_scanners_wia: PnpDevice found %d device(s) total so far",
            len(scanners),
        )
    except FileNotFoundError:
        log.info("get_scanners_wia: powershell not available")
    except Exception as e:
        log.warning("get_scanners_wia: Get-PnpDevice failed: %s", e)

    try:
        ps_cmd = (
            "Get-WmiObject -Query "
            "\"SELECT Name, DeviceID, Manufacturer, Description "
            "FROM Win32_PnPEntity "
            "WHERE PNPClass = 'Image'\" "
            "| Select-Object Name, DeviceID, Manufacturer, Description "
            "| ConvertTo-Json -Compress"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=30,
            **_NO_WINDOW,
        )
        if result.stdout.strip():
            raw = json.loads(result.stdout.strip())
            if isinstance(raw, dict):
                raw = [raw]
            for d in raw:
                dev_id  = d.get("DeviceID", "")
                display = _humanise_wia_device(
                    dev_id,
                    name=d.get("Name", ""),
                    manufacturer=d.get("Manufacturer", ""),
                    description=d.get("Description", ""),
                )
                already_ready = any(
                    display.lower() == s["display"].lower() for s in scanners
                )
                _add(
                    dev_id, display, "WMI", already_ready,
                    "" if already_ready
                    else "Detected via WMI — WIA driver may be missing",
                )
        log.info(
            "get_scanners_wia: WMI found %d device(s) total so far",
            len(scanners),
        )
    except Exception as e:
        log.warning("get_scanners_wia: WMI fallback failed: %s", e)

    _STI_CLASS = "{6bdd1fc6-810f-11d0-bec7-08002be2092f}"
    try:
        import winreg
        base = rf"SYSTEM\CurrentControlSet\Control\Class\{_STI_CLASS}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            idx = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, idx)
                    idx += 1
                    if not re.match(r"^\d{4}$", sub_name):
                        continue
                    with winreg.OpenKey(root, sub_name) as sub:
                        def _reg(key, default=""):
                            try:
                                return winreg.QueryValueEx(sub, key)[0] or default
                            except Exception:
                                return default

                        friendly = _reg("FriendlyName")
                        model    = _reg("Model") or _reg("DeviceDesc")
                        mfr      = _reg("Mfg")
                        dev_id   = _reg("DevicePath") or sub_name

                        display = _humanise_wia_device(
                            dev_id, friendly or model, mfr
                        )
                        already_ready = any(
                            display.lower() == s["display"].lower()
                            for s in scanners
                        )
                        _add(
                            dev_id, display, "Registry-STI", already_ready,
                            "" if already_ready
                            else "Found in STI registry; driver status unknown",
                        )
                except OSError:
                    break

        log.info(
            "get_scanners_wia: Registry STI scan done, %d device(s) total",
            len(scanners),
        )
    except ImportError:
        log.info("get_scanners_wia: winreg not available")
    except FileNotFoundError:
        log.info("get_scanners_wia: STI registry key not found")
    except Exception as e:
        log.warning("get_scanners_wia: Registry STI failed: %s", e)

    return scanners


# ─── macOS scanner discovery ──────────────────────────────────────────────────

def get_scanners_macos() -> list:
    scanners = []
    seen_names = set()

    def _add(name: str, display: str, source: str, ready: bool, note: str = ""):
        key = display.strip().lower()
        if key and key not in seen_names:
            seen_names.add(key)
            scanners.append({
                "name":    name,
                "display": display.strip(),
                "source":  source,
                "ready":   ready,
                "note":    note,
            })

    try:
        result = subprocess.run(
            ["scanimage", "-L"],
            capture_output=True, text=True, timeout=30,
            **_NO_WINDOW,
        )
        combined = result.stdout + "\n" + result.stderr
        for line in combined.splitlines():
            if "`" in line and "'" in line:
                s = line.find("`")
                e = line.find("'", s + 1)
                if s != -1 and e != -1:
                    device_name = line[s + 1:e]
                    label_match = re.search(r"\bis\s+a\s+(.+)$", line, re.I)
                    if label_match:
                        display = label_match.group(1).strip()
                    else:
                        display = _humanise_sane_device(device_name)
                    _add(device_name, display, "scanimage", True)
        log.info("get_scanners_macos: scanimage found %d device(s)", len(scanners))
    except FileNotFoundError:
        log.info("get_scanners_macos: scanimage not installed")
    except Exception as e:
        log.warning("get_scanners_macos: scanimage -L failed: %s", e)

    try:
        import ImageCaptureCore as ICC
        from Foundation import NSRunLoop, NSDate, NSObject
        import threading

        found_devices = []
        ev = threading.Event()

        class _ListDelegate(NSObject):
            def deviceBrowser_didAddDevice_moreComing_(
                self, browser, device, more
            ):
                if device.type() == ICC.ICScannerDeviceType:
                    raw_name = str(device.name() or "")
                    uuid_str = str(
                        getattr(device, "UUIDString", lambda: "")() or ""
                    )
                    found_devices.append({
                        "name": raw_name,
                        "usnStr": uuid_str,
                    })
                if not more:
                    ev.set()

            def deviceBrowser_didRemoveDevice_moreGoing_(
                self, browser, device, more
            ):
                pass

        delegate = _ListDelegate.alloc().init()
        browser  = ICC.ICDeviceBrowser.alloc().init()
        browser.setDelegate_(delegate)
        browser.setBrowsedDeviceTypeMask_(ICC.ICDeviceTypeMaskScanner | 0xFFFF00)
        browser.start()

        rl      = NSRunLoop.currentRunLoop()
        elapsed = 0.0
        while not ev.is_set() and elapsed < 10.0:
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
            elapsed += 0.1

        browser.stop()

        for d in found_devices:
            name = d["name"] or d["usnStr"] or "Unknown Scanner"
            _add(d["name"] or name, name, "ImageCaptureCore", True)

        log.info(
            "get_scanners_macos: ImageCaptureCore found %d device(s)",
            len(found_devices),
        )
    except ImportError:
        log.info("get_scanners_macos: pyobjc ImageCaptureCore not installed")
    except Exception as e:
        log.warning("get_scanners_macos: ImageCaptureCore probe failed: %s", e)

    try:
        escl_url = _find_escl_url(browse_timeout=10)
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        caps_url     = f"{escl_url}/ScannerCapabilities"
        display_name = "eSCL Scanner"
        try:
            with urllib.request.urlopen(caps_url, timeout=10, context=ctx) as r:
                body = r.read(2048).decode(errors="replace")
            m = re.search(
                r"<(?:\w+:)?MakeAndModel[^>]*>\s*([^<]+)\s*<", body, re.I
            )
            if m:
                display_name = m.group(1).strip()
        except Exception:
            pass
        _add(escl_url, display_name, "eSCL", True, f"relay at {escl_url}")
        log.info("get_scanners_macos: eSCL found → %s", escl_url)
    except Exception as e:
        log.info("get_scanners_macos: eSCL probe found nothing (%s)", e)

    try:
        result = subprocess.run(
            ["system_profiler", "SPUSBDataType", "-json"],
            capture_output=True, text=True, timeout=30,
            **_NO_WINDOW,
        )
        import json as _json
        data = _json.loads(result.stdout)

        def _walk(node):
            if isinstance(node, list):
                for item in node:
                    _walk(item)
            elif isinstance(node, dict):
                name = node.get("_name", "")
                if re.search(
                    r"scan|printer|mfp|multifunction|aio|laser|inkjet|"
                    r"officejet|deskjet|envy|pixma|workforce|mfc|dcpl|"
                    r"fi-\d|fujitsu|scansnap",
                    name, re.I,
                ):
                    vendor  = node.get("manufacturer", "")
                    display = f"{vendor} {name}".strip()
                    ready   = any(
                        d["display"].lower() in display.lower()
                        for d in scanners
                    )
                    _add(
                        node.get("serial_num", name),
                        display,
                        "USB-hardware",
                        ready,
                        "Detected via USB; driver availability not confirmed"
                        if not ready else "",
                    )
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        _walk(v)

        _walk(data)
        log.info("get_scanners_macos: system_profiler walk done")
    except Exception as e:
        log.warning("get_scanners_macos: system_profiler failed: %s", e)

    return scanners


# ─── Linux scanner discovery ──────────────────────────────────────────────────

def get_scanners_linux() -> list:
    scanners = []
    seen = set()

    def _add(name: str, display: str, source: str, ready: bool, note: str = ""):
        key = display.strip().lower()
        if key and key not in seen:
            seen.add(key)
            scanners.append({
                "name":    name,
                "display": display.strip(),
                "source":  source,
                "ready":   ready,
                "note":    note,
            })

    try:
        import sane
        sane.init()
        try:
            devices = sane.get_devices()
            for dev in devices:
                dev_name, vendor, model, dev_type = dev
                display = _humanise_sane_device(dev_name, vendor, model)
                _add(dev_name, display, "SANE", True)
            log.info(
                "get_scanners_linux: SANE found %d device(s)", len(devices)
            )
        finally:
            sane.exit()
    except ImportError:
        log.info("get_scanners_linux: python-sane not installed")
    except Exception as e:
        log.warning("get_scanners_linux: SANE init failed: %s", e)

    try:
        result = subprocess.run(
            ["scanimage", "-L"],
            capture_output=True, text=True, timeout=15,
            **_NO_WINDOW,
        )
        combined = result.stdout + "\n" + result.stderr
        for line in combined.splitlines():
            if "`" in line and "'" in line:
                s = line.find("`")
                e = line.find("'", s + 1)
                if s != -1 and e != -1:
                    device_name = line[s + 1:e]
                    label_match = re.search(r"\bis\s+a\s+(.+)$", line, re.I)
                    if label_match:
                        display = label_match.group(1).strip()
                    else:
                        display = _humanise_sane_device(device_name)
                    _add(device_name, display, "scanimage", True)
    except FileNotFoundError:
        log.info("get_scanners_linux: scanimage not installed")
    except Exception as e:
        log.warning("get_scanners_linux: scanimage -L failed: %s", e)

    try:
        result = subprocess.run(
            ["lsusb"], capture_output=True, text=True, timeout=20,
            **_NO_WINDOW,
        )
        for line in result.stdout.splitlines():
            if re.search(
                r"scan|printer|mfp|multifunction|aio|officejet|deskjet|"
                r"envy|pixma|workforce|mfc|laser|inkjet|canon|epson|hp\b|"
                r"fi-\d|fujitsu|scansnap",
                line, re.I,
            ):
                m = re.search(r"ID\s+[\da-f:]+\s+(.+)", line, re.I)
                display = m.group(1).strip() if m else line.strip()
                m2      = re.search(r"Bus\s+(\d+)\s+Device\s+(\d+)", line)
                dev_id  = (
                    f"usb:{m2.group(1)}:{m2.group(2)}" if m2 else display
                )
                already_ready = any(
                    display.lower() in d["display"].lower()
                    for d in scanners
                )
                _add(
                    dev_id, display, "USB-hardware", already_ready,
                    "Detected via lsusb; SANE driver may be needed"
                    if not already_ready else "",
                )
    except FileNotFoundError:
        log.info("get_scanners_linux: lsusb not available")
    except Exception as e:
        log.warning("get_scanners_linux: lsusb failed: %s", e)

    try:
        result = subprocess.run(
            ["udevadm", "info", "--export-db"],
            capture_output=True, text=True, timeout=15,
            **_NO_WINDOW,
        )
        current_block: dict = {}
        for line in result.stdout.splitlines():
            if line.startswith("P:"):
                current_block = {"path": line[2:].strip()}
            elif "=" in line:
                key, _, val = line.partition("=")
                current_block[key.strip()] = val.strip()
            elif not line.strip() and current_block:
                id_model  = current_block.get("E:ID_MODEL", "")
                id_vendor = current_block.get("E:ID_VENDOR", "")
                subsystem = current_block.get("E:SUBSYSTEM", "")
                if (
                    "scanner" in subsystem.lower()
                    or (
                        "usb" in subsystem.lower()
                        and re.search(r"scan|mfp|multifunction|fi-\d|fujitsu",
                                      id_model, re.I)
                    )
                ):
                    display = f"{id_vendor} {id_model}".strip()
                    if display:
                        node          = current_block.get("E:DEVNAME", display)
                        already_ready = any(
                            display.lower() in d["display"].lower()
                            for d in scanners
                        )
                        _add(
                            node, display, "udev", already_ready,
                            "Detected via udev" if not already_ready else "",
                        )
                current_block = {}
    except FileNotFoundError:
        log.info("get_scanners_linux: udevadm not available")
    except Exception as e:
        log.warning("get_scanners_linux: udevadm failed: %s", e)

    return scanners


def list_scanners() -> list:
    if sys.platform == "win32":
        return get_scanners_wia()
    elif sys.platform == "darwin":
        return get_scanners_macos()
    elif sys.platform.startswith("linux"):
        return get_scanners_linux()
    return []


# ─── PNG / image helpers ──────────────────────────────────────────────────────

def _png_from_path(path: str) -> bytes:
    from PIL import Image
    img = Image.open(path)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _png_from_bytes(raw: bytes) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(raw))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _img_to_png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ─── Image content signature (for duplicate-page detection) ──────────────────

def _image_content_signature(png_bytes: bytes, size: int = 128) -> bytes:
    """
    Compute a compact perceptual signature of a scanned page.

    Method: grayscale + downscale to (size × size), return raw pixel bytes.
    128×128 (default) preserves enough detail to distinguish cheques from
    the same book that differ only in handwritten date/amount/signature.
    """
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("L")
        small = img.resize((size, size))
        return bytes(small.getdata())
    except Exception as ex:
        log.warning("content_signature: failed to hash page (%s)", ex)
        return b""


def _pages_are_duplicates(sig_a: bytes, sig_b: bytes,
                            avg_diff_threshold: float = 1.0) -> tuple:
    """
    Return (is_duplicate, avg_diff) for two content signatures.

    avg_diff is the mean absolute per-pixel difference (0–255 range).
      - Identical re-scans of the same physical sheet: ~0.0–0.8
      - Two cheques from the same cheque book (diff date/amount only): ~2–8
      - Two cheques from different books: ~10–40
      - Different documents entirely: ~30–120

    Threshold 1.0 = only near-perfect duplicates trigger.
    """
    if not sig_a or not sig_b or len(sig_a) != len(sig_b):
        return False, -1.0
    total = 0
    for a, b in zip(sig_a, sig_b):
        total += abs(a - b)
    avg = total / len(sig_a)
    return avg < avg_diff_threshold, avg


# ─── Document detection & cropping ───────────────────────────────────────────

def _detect_document_bounds(img) -> tuple:
    from PIL import Image, ImageFilter

    SCALE     = 4
    THRESHOLD = 245
    DILATION  = 9
    PADDING   = 30

    small = img.resize(
        (max(1, img.width // SCALE), max(1, img.height // SCALE)),
        Image.LANCZOS,
    )
    gray   = small.convert("L").filter(ImageFilter.GaussianBlur(3))
    binary = gray.point(lambda p: 255 if p < THRESHOLD else 0)
    binary = binary.filter(ImageFilter.MaxFilter(DILATION))
    bbox   = binary.getbbox()

    if not bbox:
        log.info("detect_bounds: no document found — returning full image")
        return 0, 0, img.width, img.height

    x1 = max(0, bbox[0] * SCALE - PADDING)
    y1 = max(0, bbox[1] * SCALE - PADDING)
    x2 = min(img.width,  bbox[2] * SCALE + PADDING)
    y2 = min(img.height, bbox[3] * SCALE + PADDING)

    log.info("detect_bounds: %d,%d  %dx%d px", x1, y1, x2 - x1, y2 - y1)
    return x1, y1, x2 - x1, y2 - y1


def _crop_png(png_bytes: bytes, x: int, y: int, width: int, height: int) -> bytes:
    from PIL import Image
    img    = Image.open(io.BytesIO(png_bytes))
    x      = max(0, min(x, img.width - 1))
    y      = max(0, min(y, img.height - 1))
    width  = max(1, min(width,  img.width  - x))
    height = max(1, min(height, img.height - y))
    return _img_to_png_bytes(img.crop((x, y, x + width, y + height)))


def _auto_crop_png(png_bytes: bytes) -> tuple:
    from PIL import Image
    img     = Image.open(io.BytesIO(png_bytes))
    x, y, w, h = _detect_document_bounds(img)
    cropped = img.crop((x, y, x + w, y + h))
    return _img_to_png_bytes(cropped), {"x": x, "y": y, "width": w, "height": h}


# ─── Fast PDF builder (JPEG DCTDecode, no re-encoding) ───────────────────────

def _build_pdf_from_jpegs(pages: list) -> bytes:
    buf          = io.BytesIO()
    xref_offsets = []
    obj_n        = 0

    def _w(data):
        if isinstance(data, str):
            data = data.encode()
        buf.write(data)

    def _begin_obj() -> int:
        nonlocal obj_n
        obj_n += 1
        xref_offsets.append(buf.tell())
        _w(f"{obj_n} 0 obj\n")
        return obj_n

    _w(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    pages_ob_n = len(pages) * 3 + 1

    page_ids = []

    for jpeg, w_px, h_px, dpi, colorspace in pages:
        w_pt = round(w_px * 72 / dpi, 4)
        h_pt = round(h_px * 72 / dpi, 4)

        img_obj = _begin_obj()
        _w(
            f"<< /Type /XObject /Subtype /Image\n"
            f"   /Width {w_px} /Height {h_px}\n"
            f"   /ColorSpace {colorspace} /BitsPerComponent 8\n"
            f"   /Filter /DCTDecode /Length {len(jpeg)}\n"
            f">>\nstream\n"
        )
        _w(jpeg)
        _w(b"\nendstream\nendobj\n")

        cs     = f"q {w_pt} 0 0 {h_pt} 0 0 cm /Im0 Do Q\n".encode()
        cs_obj = _begin_obj()
        _w(f"<< /Length {len(cs)} >>\nstream\n")
        _w(cs)
        _w(b"\nendstream\nendobj\n")

        page_obj = _begin_obj()
        _w(
            f"<< /Type /Page\n"
            f"   /Parent {pages_ob_n} 0 R\n"
            f"   /MediaBox [0 0 {w_pt} {h_pt}]\n"
            f"   /Contents {cs_obj} 0 R\n"
            f"   /Resources << /XObject << /Im0 {img_obj} 0 R >> >>\n"
            f">>\nendobj\n"
        )
        page_ids.append(page_obj)

    kids     = " ".join(f"{p} 0 R" for p in page_ids)
    pages_ob = _begin_obj()
    _w(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\nendobj\n")

    cat_obj = _begin_obj()
    _w(f"<< /Type /Catalog /Pages {pages_ob} 0 R >>\nendobj\n")

    xref_pos = buf.tell()
    _w(f"xref\n0 {obj_n + 1}\n")
    _w("0000000000 65535 f \n")
    for off in xref_offsets:
        _w(f"{off:010d} 00000 n \n")

    _w(
        f"trailer\n<< /Size {obj_n + 1} /Root {cat_obj} 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    )
    return buf.getvalue()


def _images_to_pdf(images_b64: list) -> bytes:
    from PIL import Image

    if not images_b64:
        raise ValueError("make_pdf: no images provided")

    page_tuples = []

    for i, b64 in enumerate(images_b64):
        try:
            raw = base64.b64decode(b64)
            img = Image.open(io.BytesIO(raw))
        except Exception as e:
            raise ValueError(f"make_pdf: page {i + 1} is not a valid image — {e}")

        dpi = 200
        try:
            dpi_info = img.info.get("dpi")
            if dpi_info:
                dpi = int(dpi_info[0]) or 200
        except Exception:
            pass

        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode == "P":
            img = img.convert("RGBA")
            bg  = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        elif img.mode == "L":
            pass
        elif img.mode != "RGB":
            img = img.convert("RGB")

        colorspace = "/DeviceGray" if img.mode == "L" else "/DeviceRGB"

        jpeg_buf = io.BytesIO()
        img.save(jpeg_buf, format="JPEG", quality=85, optimize=True)
        jpeg_bytes = jpeg_buf.getvalue()

        log.info(
            "make_pdf: page %d  %d×%d px  dpi=%d  %s bytes JPEG",
            i + 1, img.width, img.height, dpi, f"{len(jpeg_bytes):,}",
        )
        page_tuples.append((jpeg_bytes, img.width, img.height, dpi, colorspace))

    pdf_bytes = _build_pdf_from_jpegs(page_tuples)
    log.info(
        "make_pdf: %d page(s) → %s bytes PDF",
        len(page_tuples), f"{len(pdf_bytes):,}",
    )
    return pdf_bytes


# ─── Shared scanimage device discovery ───────────────────────────────────────

def _get_scanimage_device() -> str:
    result  = subprocess.run(
        ["scanimage", "-L"],
        capture_output=True, text=True, timeout=15,
        **_NO_WINDOW,
    )
    combined = result.stdout + "\n" + result.stderr
    for line in combined.splitlines():
        if "`" in line and "'" in line:
            s = line.find("`")
            e = line.find("'", s + 1)
            if s != -1 and e != -1:
                device = line[s + 1:e]
                log.info("scanimage: device found → %s", device)
                return device
    raise RuntimeError(
        "scanimage: no devices found.\n"
        "  USB scanner  → check cable, then: scanimage -L\n"
        "  Not listed?  → brew install sane-backends"
    )


def _pick_adf_source(device: str) -> str:
    try:
        result  = subprocess.run(
            ["scanimage", f"--device={device}", "--help"],
            capture_output=True, text=True, timeout=10,
            **_NO_WINDOW,
        )
        combined = result.stdout + result.stderr
        for line in combined.splitlines():
            if "--source" in line.lower():
                tokens = re.findall(
                    r"[\w\s]+(?:Document\s+Feeder|ADF|Feeder)", line, re.I
                )
                if tokens:
                    src = tokens[0].strip()
                    log.info("_pick_adf_source: found %r for device %s", src, device)
                    return src
    except Exception as ex:
        log.warning("_pick_adf_source: query failed (%s) — will guess", ex)

    log.info("_pick_adf_source: guessing 'ADF'")
    return "ADF"


# ─── eSCL status parsing helpers ─────────────────────────────────────────────

def _escl_parse_scanner_status(xml: str) -> tuple:
    state = "Unknown"
    for pattern in (
        r"<(?:\w+:)?State\b[^>]*>\s*(\w+)\s*<",
        r"<(?:\w+:)?ScannerState\b[^>]*>\s*(\w+)\s*<",
    ):
        m = re.search(pattern, xml, re.I)
        if m:
            state = m.group(1).strip()
            break

    job_uris = re.findall(
        r"<(?:\w+:)?JobUri\b[^>]*>\s*([^\s<]+)\s*<", xml, re.I
    )
    return state, job_uris


def _escl_cancel_active_jobs(base_url: str, ctx) -> int:
    status_url = f"{base_url}/ScannerStatus"
    try:
        with urllib.request.urlopen(status_url, timeout=5, context=ctx) as resp:
            body = resp.read().decode(errors="replace")
    except Exception as ex:
        log.warning("eSCL cancel_jobs: ScannerStatus unreachable (%s)", ex)
        return 0

    state, job_uris = _escl_parse_scanner_status(body)
    log.info(
        "eSCL cancel_jobs: scanner state=%s  active jobs=%s",
        state, job_uris if job_uris else "(none)",
    )

    if not job_uris:
        return 0

    cancelled = 0
    for uri in job_uris:
        job_url = urljoin(base_url, uri)
        try:
            req = urllib.request.Request(job_url, method="DELETE")
            with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
                log.info("eSCL cancel_jobs: DELETE %s → HTTP %s", job_url, r.status)
            cancelled += 1
        except urllib.error.HTTPError as e:
            if e.code == 404:
                log.info("eSCL cancel_jobs: DELETE %s → HTTP 404 (already gone)", job_url)
                cancelled += 1
            else:
                log.warning("eSCL cancel_jobs: DELETE %s → HTTP %s", job_url, e.code)
        except Exception as ex:
            log.warning("eSCL cancel_jobs: DELETE %s → %s", job_url, ex)

    return cancelled


def _escl_wait_for_idle(base_url: str, ctx, timeout: int = 20) -> str:
    status_url    = f"{base_url}/ScannerStatus"
    deadline      = time.time() + timeout
    attempt       = 0
    cancel_tried  = False

    while time.time() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(
                status_url, timeout=5, context=ctx
            ) as resp:
                body = resp.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "Unknown"
            return "Unknown"
        except Exception:
            return "Unknown"

        state, job_uris = _escl_parse_scanner_status(body)
        if state.lower() == "idle":
            return "Idle"

        if not cancel_tried and (job_uris or state.lower() != "unknown"):
            cancel_tried = True
            n = _escl_cancel_active_jobs(base_url, ctx)
            if n:
                time.sleep(2)
                continue

        time.sleep(2)

    return state


def _scan_escl_http_adf(base_url: str, progress_cb=None) -> list:
    import ssl

    scan_ns = "http://schemas.hp.com/imaging/escl/2011/05/03"
    pwg_ns  = "http://www.pwg.org/schemas/2010/12/sm"

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<scan:ScanSettings xmlns:scan="{scan_ns}" xmlns:pwg="{pwg_ns}">'
        "<pwg:Version>2.6</pwg:Version>"
        "<pwg:ScanRegions><pwg:ScanRegion>"
        "<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>"
        "<pwg:Width>2480</pwg:Width><pwg:Height>3508</pwg:Height>"
        "<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>"
        "</pwg:ScanRegion></pwg:ScanRegions>"
        "<pwg:InputSource>Feeder</pwg:InputSource>"
        "<scan:ColorMode>RGB24</scan:ColorMode>"
        "<scan:XResolution>200</scan:XResolution>"
        "<scan:YResolution>200</scan:YResolution>"
        "<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>"
        "</scan:ScanSettings>"
    ).encode("utf-8")

    _escl_wait_for_idle(base_url, ctx, timeout=20)

    location = None
    for post_attempt in range(1, 7):
        req = urllib.request.Request(
            f"{base_url}/ScanJobs", data=body,
            headers={"Content-Type": "text/xml; charset=utf-8"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                location = resp.headers.get("Location", "").strip()
            break
        except urllib.error.HTTPError as e:
            if e.code in (409, 503):
                _escl_cancel_active_jobs(base_url, ctx)
                time.sleep(2)
                continue
            raise RuntimeError(f"eSCL ADF POST failed: HTTP {e.code}")

    if not location:
        raise RuntimeError("eSCL ADF: no Location header")

    job_url = urljoin(base_url, location).rstrip("/")
    doc_url = f"{job_url}/NextDocument"

    pages = []
    page_num = 0

    while True:
        page_num += 1
        raw_image = None
        feeder_end = False

        for attempt in range(1, 31):
            try:
                with urllib.request.urlopen(doc_url, timeout=60, context=ctx) as resp:
                    raw_image = resp.read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    feeder_end = True
                    break
                if e.code in (503, 409):
                    time.sleep(1.0 if attempt <= 10 else 2.0)
                    continue
                raise RuntimeError(f"eSCL NextDocument failed: HTTP {e.code}")

        if feeder_end or raw_image is None:
            break

        png = _png_from_bytes(raw_image)
        pages.append(png)
        log.info("eSCL ADF: page %d OK", len(pages))
        if progress_cb:
            try:
                progress_cb({
                    "status": "progress",
                    "page":   len(pages),
                    "message": f"Scanned page {len(pages)}…",
                })
            except Exception:
                pass

    if not pages:
        raise RuntimeError("eSCL ADF returned no pages")

    return pages


def _save_escl_url_cache(url: str) -> None:
    try:
        with open(_ESCL_URL_CACHE, "w") as f:
            f.write(url.strip())
    except Exception:
        pass


def _load_cached_escl_url() -> str:
    try:
        with open(_ESCL_URL_CACHE) as f:
            return f.read().strip()
    except Exception:
        return ""


def _verify_escl_url(base_url: str, timeout: float = 5.0) -> bool:
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    probe_url = f"{base_url.rstrip('/')}/ScannerCapabilities"
    try:
        with urllib.request.urlopen(probe_url, timeout=timeout, context=ctx) as resp:
            body = resp.read(1024).decode(errors="replace")
        return bool(re.search(r"(?i)scanner", body))
    except Exception:
        return False


def _probe_localhost_escl_ports(
    port_range: range = range(59000, 59201),
    per_port_timeout: float = 1.5,
) -> str:
    for port in port_range:
        url = f"http://localhost:{port}/eSCL/ScannerCapabilities"
        try:
            with urllib.request.urlopen(url, timeout=per_port_timeout) as resp:
                body = resp.read(512).decode(errors="replace")
            if re.search(r"(?i)scanner", body):
                return f"http://localhost:{port}/eSCL"
        except Exception:
            pass
    return ""


def _find_escl_url(browse_timeout: int = 5) -> str:
    cached = _load_cached_escl_url()
    if cached and _verify_escl_url(cached):
        return cached

    probed = _probe_localhost_escl_ports()
    if probed:
        _save_escl_url_cache(probed)
        return probed

    url = _find_escl_url_bonjour(browse_timeout=browse_timeout)
    _save_escl_url_cache(url)
    return url


def _find_escl_url_bonjour(browse_timeout: int = 5) -> str:
    for service_type in ("_uscan._tcp", "_uscans._tcp", "_scanner._tcp"):
        browse = subprocess.Popen(
            ["dns-sd", "-B", service_type, "local"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            **_NO_WINDOW,
        )
        time.sleep(browse_timeout)
        browse.terminate()
        raw = browse.stdout.read()

        service_name = None
        for line in raw.splitlines():
            if "Add" in line:
                parts = line.split()
                if len(parts) >= 7:
                    service_name = (
                        " ".join(parts[6:]).replace("\\032", " ").strip()
                    )
                    break

        if not service_name:
            continue

        lookup = subprocess.Popen(
            ["dns-sd", "-L", service_name, service_type, "local"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            **_NO_WINDOW,
        )
        time.sleep(3)
        lookup.terminate()
        lookup_out = lookup.stdout.read()

        host = None
        port = 8080
        path = "/eSCL"
        for line in lookup_out.splitlines():
            m = re.search(
                r"can be reached at\s+([A-Za-z0-9._-]+):(\d+)", line, re.I
            )
            if m:
                host = m.group(1).rstrip(".")
                port = int(m.group(2))
            m2 = re.search(r"(?:^|(?<=\s))rs=(/\S*)", line)
            if m2:
                path = m2.group(1)

        if not host:
            continue

        scheme = "https" if "uscans" in service_type else "http"
        return f"{scheme}://{host}:{port}{path}"

    raise RuntimeError("No eSCL scanner found via Bonjour.")


# ─── Windows TWAIN ADF ───────────────────────────────────────────────────────

def _scan_twain_adf(show_ui: bool, progress_cb=None) -> list:
    """
    TWAIN-driven ADF batch scan (Windows).  Tuned for PaperStream IP.
    """
    import twain
    from PIL import Image

    pages = []
    sm    = twain.SourceManager(0)
    try:
        sources = sm.GetSourceList()
        if not sources:
            raise RuntimeError("No TWAIN scanner found.")
        log.info("TWAIN ADF: sources = %s", sources)
        ss = sm.OpenSource(sources[0])
        try:
            for cap, val in [
                (twain.ICAP_XRESOLUTION, 200.0),
                (twain.ICAP_YRESOLUTION, 200.0),
            ]:
                try:
                    ss.SetCapability(cap, twain.TWTY_FIX32, val)
                except Exception:
                    pass

            try:
                ss.SetCapability(twain.ICAP_PIXELTYPE, twain.TWTY_UINT16, 2)
                ss.SetCapability(twain.ICAP_BITDEPTH,  twain.TWTY_UINT16, 8)
            except Exception:
                pass

            for cap, val in [
                (twain.CAP_FEEDERENABLED, True),
                (twain.CAP_AUTOFEED,      True),
                (twain.CAP_AUTOSCAN,      True),
                (twain.CAP_DUPLEXENABLED, False),
                (twain.CAP_INDICATORS,    False),
            ]:
                try:
                    ss.SetCapability(cap, twain.TWTY_BOOL, val)
                except Exception:
                    pass

            try:
                ss.SetCapability(twain.CAP_XFERCOUNT, twain.TWTY_INT16, -1)
            except Exception:
                pass

            ss.RequestAcquire(int(show_ui), 1)

            page_num = 0
            while True:
                page_num += 1
                try:
                    rv = ss.XferImageNatively()
                except Exception as e:
                    if pages:
                        break
                    raise RuntimeError(f"TWAIN ADF page 1 failed: {e}")

                if rv is None:
                    if not pages:
                        raise RuntimeError(
                            "TWAIN ADF: no image on page 1"
                        )
                    break

                handle, more = rv
                bmp_data = twain.DIBToBMFile(handle)
                twain.GlobalHandleFree(handle)

                img = Image.open(io.BytesIO(bmp_data))
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                pages.append(buf.getvalue())
                log.info("TWAIN ADF: page %d acquired", len(pages))
                if progress_cb:
                    try:
                        progress_cb({
                            "status": "progress",
                            "page":   len(pages),
                            "message": f"Scanned page {len(pages)}…",
                        })
                    except Exception:
                        pass

                if not more:
                    break
        finally:
            ss.destroy()
    finally:
        sm.destroy()

    if not pages:
        raise RuntimeError("TWAIN ADF returned no pages")
    return pages


# ─── Linux SANE ADF ──────────────────────────────────────────────────────────

def _scan_sane_adf(progress_cb=None) -> list:
    import sane
    from PIL import Image

    sane.init()
    pages = []
    try:
        devices = sane.get_devices()
        if not devices:
            raise RuntimeError("No SANE scanner found.")
        dev = sane.open(devices[0][0])
        try:
            dev.resolution = 200
            dev.mode       = "color"

            for src_name in ("ADF", "Automatic Document Feeder", "adf"):
                try:
                    dev.source = src_name
                    break
                except Exception:
                    pass

            while True:
                try:
                    img = dev.scan()
                except Exception:
                    break

                buf = io.BytesIO()
                img.save(buf, format="PNG")
                pages.append(buf.getvalue())
                log.info("SANE ADF: page %d scanned", len(pages))
                if progress_cb:
                    try:
                        progress_cb({
                            "status": "progress",
                            "page":   len(pages),
                            "message": f"Scanned page {len(pages)}…",
                        })
                    except Exception:
                        pass
        finally:
            dev.close()
    finally:
        sane.exit()

    if not pages:
        raise RuntimeError("SANE ADF returned no pages")
    return pages


def _scan_scanimage_adf(progress_cb=None) -> list:
    import glob

    device     = _get_scanimage_device()
    adf_source = _pick_adf_source(device)

    with tempfile.TemporaryDirectory() as tmpdir:
        pattern = os.path.join(tmpdir, "page%04d.png")
        cmd = [
            "scanimage",
            f"--device={device}",
            "--format=png",
            "--resolution=200",
            "--mode=Color",
            "--batch",
            "--batch-start=1",
            f"--output-file={pattern}",
        ]
        if adf_source:
            cmd.append(f"--source={adf_source}")

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            **_NO_WINDOW,
        )
        page_files = sorted(glob.glob(os.path.join(tmpdir, "page*.png")))

        if not page_files and adf_source:
            cmd_retry = [c for c in cmd if not c.startswith("--source=")]
            result = subprocess.run(
                cmd_retry, capture_output=True, text=True, timeout=300,
                **_NO_WINDOW,
            )
            page_files = sorted(glob.glob(os.path.join(tmpdir, "page*.png")))

        if not page_files:
            raise RuntimeError(
                f"scanimage ADF returned no pages.\nstderr: {result.stderr.strip()}"
            )

        pages = []
        for path in page_files:
            with open(path, "rb") as f:
                pages.append(f.read())
            if progress_cb:
                try:
                    progress_cb({
                        "status": "progress",
                        "page":   len(pages),
                        "message": f"Scanned page {len(pages)}…",
                    })
                except Exception:
                    pass

    return pages


def _scan_macos_adf(progress_cb=None) -> list:
    try:
        return _scan_scanimage_adf(progress_cb=progress_cb)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("scanimage ADF failed (%s) → trying eSCL ADF…", e)

    return _scan_escl_http_adf(_find_escl_url(), progress_cb=progress_cb)


# ─── ADF platform dispatch ────────────────────────────────────────────────────

def scan_adf(progress_cb=None) -> list:
    global _TWAIN_KNOWN_BROKEN

    if sys.platform == "win32":
        if not _TWAIN_KNOWN_BROKEN:
            try:
                return _scan_twain_adf(SHOW_UI, progress_cb=progress_cb)
            except ImportError:
                log.info("TWAIN package not installed — using WIA ADF")
                _TWAIN_KNOWN_BROKEN = True
            except Exception as e:
                log.warning(
                    "TWAIN ADF failed (%s) — using WIA ADF (cached)", e,
                )
                _TWAIN_KNOWN_BROKEN = True
        else:
            log.info("TWAIN ADF known-broken — using WIA ADF")
        return _scan_wia_adf(progress_cb=progress_cb)
    elif sys.platform.startswith("linux"):
        return _scan_sane_adf(progress_cb=progress_cb)
    elif sys.platform == "darwin":
        return _scan_macos_adf(progress_cb=progress_cb)
    else:
        raise RuntimeError(f"ADF not supported on platform: {sys.platform}")


# ─── WIA property helpers (enumerate by PropertyID) ──────────────────────────

def _wia_find_prop(obj, prop_id: int):
    try:
        props = obj.Properties
        count = props.Count
    except Exception:
        return None
    for i in range(1, count + 1):
        try:
            p = props.Item(i)
            if int(p.PropertyID) == int(prop_id):
                return p
        except Exception:
            continue
    return None


def _wia_set_prop(obj, prop_id: int, value, label: str) -> bool:
    p = _wia_find_prop(obj, prop_id)
    if p is None:
        log.info("WIA ADF: %s prop %s not found on this device", label, prop_id)
        return False
    try:
        p.Value = value
        log.info("WIA ADF: %s prop %s = %s ✓", label, prop_id, value)
        return True
    except Exception as ex:
        log.info("WIA ADF: %s prop %s = %s ✗ (%s)", label, prop_id, value, ex)
        return False


def _wia_get_prop(obj, prop_id: int, default=None):
    p = _wia_find_prop(obj, prop_id)
    if p is None:
        return default
    try:
        return p.Value
    except Exception:
        return default


# ─── Windows WIA ADF (v1.7.11 — two-factor duplicate detection) ──────────────

def _scan_wia_adf(progress_cb=None) -> list:
    """
    WIA-driven ADF batch scan (Windows).

    End-of-feeder detection strategy (in priority order):
      1. WIA_ERROR_PAPER_EMPTY  (proper COM error — cleanest).
      2. WIA_DPS_DOCUMENT_HANDLING_STATUS bit0 (FEED_READY) polled.
      3. TWO-FACTOR duplicate check (v1.7.11 fix for cheque-book scans):
           a. PNG byte-size within BYTE_DIFF_TOLERANCE of previous, AND
           b. Content signature avg per-pixel diff < AVG_DIFF_THRESHOLD.
         Both conditions must hold — this eliminates the v1.7.10 false-
         positives on cheques from the same book that share a printed
         template.
      4. MAX_PAGES hard safety cap.
    """
    import win32com.client
    import pywintypes

    PNG_FMT               = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
    WIA_ERROR_PAPER_EMPTY = -2145320957   # 0x80210003
    WIA_ERROR_PAPER_JAM   = -2145320958   # 0x80210002

    WIA_DPS_DOCUMENT_HANDLING_SELECT = 3088   # 1=FEEDER, 2=FLATBED
    WIA_DPS_DOCUMENT_HANDLING_STATUS = 3087   # bitmask; bit0 = FEED_READY
    WIA_DPS_PAGES                    = 3096
    WIA_IPS_PAGES                    = 3096
    WIA_IPS_XRES                     = 6147
    WIA_IPS_YRES                     = 6148
    WIA_IPS_CUR_INTENT               = 6146

    # Two-factor duplicate detection thresholds
    AVG_DIFF_THRESHOLD  = 1.0   # per-pixel avg diff on 128×128 grayscale
    BYTE_DIFF_TOLERANCE = 300   # PNG byte-size difference in bytes

    MAX_PAGES = 500

    dm     = win32com.client.Dispatch("WIA.DeviceManager")
    device = None
    for i in range(1, dm.DeviceInfos.Count + 1):
        try:
            info = dm.DeviceInfos.Item(i)
            if info.Type == 1:
                device = info.Connect()
                log.info(
                    "WIA ADF: connected to %s",
                    device.Properties("Name").Value,
                )
                break
        except Exception:
            pass

    if device is None:
        raise RuntimeError("WIA ADF: no scanner device found")

    _wia_set_prop(device, WIA_DPS_DOCUMENT_HANDLING_SELECT, 1, "device")
    _wia_set_prop(device, WIA_DPS_PAGES,                    0, "device")

    if device.Items.Count == 0:
        raise RuntimeError("WIA ADF: scanner returned no scan items")

    item = device.Items.Item(1)

    _wia_set_prop(item, WIA_IPS_PAGES,      0,   "item")
    _wia_set_prop(item, WIA_IPS_XRES,       200, "item")
    _wia_set_prop(item, WIA_IPS_YRES,       200, "item")
    _wia_set_prop(item, WIA_IPS_CUR_INTENT, 1,   "item")

    status = _wia_get_prop(device, WIA_DPS_DOCUMENT_HANDLING_STATUS)
    if isinstance(status, int) and status >= 0:
        log.info(
            "WIA ADF: DOCUMENT_HANDLING_STATUS = 0x%X (bit0=FEED_READY)",
            status,
        )
    else:
        log.info("WIA ADF: DOCUMENT_HANDLING_STATUS not exposed")

    pages     = []
    prev_sig  = None
    prev_size = 0
    page_num  = 0

    while True:
        page_num += 1

        if page_num > MAX_PAGES:
            log.warning("WIA ADF: hit safety limit of %d pages", MAX_PAGES)
            break

        status = _wia_get_prop(device, WIA_DPS_DOCUMENT_HANDLING_STATUS)
        if isinstance(status, int) and status >= 0:
            feed_ready = bool(status & 0x01)
            if not feed_ready and pages:
                log.info(
                    "WIA ADF: STATUS=0x%X — feeder empty after %d page(s)",
                    status, len(pages),
                )
                break

        log.info("WIA ADF: acquiring page %d…", page_num)
        tmp_path = tempfile.mktemp(suffix=".png")
        try:
            image = item.Transfer(PNG_FMT)
            image.SaveFile(tmp_path)
            with open(tmp_path, "rb") as f:
                raw_data = f.read()

            if not raw_data:
                log.info("WIA ADF: empty image on page %d — end-of-feeder", page_num)
                break

            try:
                png_data = _png_from_bytes(raw_data)
            except Exception as conv_err:
                if pages:
                    log.info(
                        "WIA ADF: invalid image on page %d (%s) — end-of-feeder",
                        page_num, conv_err,
                    )
                    break
                raise RuntimeError(f"WIA ADF: page 1 invalid: {conv_err}")

            # ── Two-factor duplicate check ───────────────────────────────
            size = len(png_data)
            curr_sig = _image_content_signature(png_data)

            if pages and prev_sig is not None:
                byte_diff = abs(size - prev_size)
                byte_match = byte_diff <= BYTE_DIFF_TOLERANCE

                if byte_match:
                    is_dup, avg_diff = _pages_are_duplicates(
                        prev_sig, curr_sig, AVG_DIFF_THRESHOLD,
                    )
                    if is_dup:
                        log.warning(
                            "WIA ADF: page %d is a re-scan of page %d "
                            "(byte Δ=%d ≤ %d, pixel avg diff=%.2f < %.1f) "
                            "— feeder empty; discarding & stopping.",
                            page_num, len(pages),
                            byte_diff, BYTE_DIFF_TOLERANCE,
                            avg_diff, AVG_DIFF_THRESHOLD,
                        )
                        break
                    else:
                        log.info(
                            "WIA ADF: page %d byte-close but content differs "
                            "(byte Δ=%d, pixel avg diff=%.2f) — keeping",
                            page_num, byte_diff, avg_diff,
                        )
                else:
                    log.info(
                        "WIA ADF: page %d byte-size differs (Δ=%d > %d) — keeping",
                        page_num, byte_diff, BYTE_DIFF_TOLERANCE,
                    )

            pages.append(png_data)
            prev_sig  = curr_sig or prev_sig
            prev_size = size
            log.info(
                "WIA ADF: page %d OK (%s bytes PNG)",
                len(pages), f"{len(png_data):,}",
            )
            if progress_cb:
                try:
                    progress_cb({
                        "status":  "progress",
                        "page":    len(pages),
                        "message": f"Scanned page {len(pages)}…",
                    })
                except Exception:
                    pass

        except pywintypes.com_error as e:
            hresult = e.args[0] if e.args else 0
            if hresult == WIA_ERROR_PAPER_EMPTY:
                log.info("WIA ADF: feeder empty after %d page(s)", len(pages))
            elif hresult == WIA_ERROR_PAPER_JAM:
                raise RuntimeError("WIA ADF: paper jam detected")
            elif pages:
                log.warning(
                    "WIA ADF: page %d COM error (0x%08X) — end-of-feeder: %s",
                    page_num, hresult & 0xFFFFFFFF, e,
                )
            else:
                raise RuntimeError(
                    f"WIA ADF: page 1 failed (0x{hresult & 0xFFFFFFFF:08X}): {e}"
                )
            break

        except Exception as e:
            if pages:
                log.warning("WIA ADF: page %d error — end: %s", page_num, e)
                break
            raise RuntimeError(f"WIA ADF: scan failed: {e}")

        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)

    if not pages:
        raise RuntimeError(
            "WIA ADF returned no pages — is the document feeder loaded?"
        )

    log.info("WIA ADF: total %d page(s)", len(pages))
    return pages


# ─── Windows: single-page TWAIN / WIA ────────────────────────────────────────

def _scan_twain(show_ui: bool) -> bytes:
    import twain
    from PIL import Image

    sm = twain.SourceManager(0)
    try:
        sources = sm.GetSourceList()
        if not sources:
            raise RuntimeError("No TWAIN scanner found.")
        log.info("TWAIN sources: %s", sources)
        ss = sm.OpenSource(sources[0])
        try:
            for cap, val in [
                (twain.ICAP_XRESOLUTION, 200.0),
                (twain.ICAP_YRESOLUTION, 200.0),
            ]:
                try:
                    ss.SetCapability(cap, twain.TWTY_FIX32, val)
                except Exception:
                    pass

            try:
                ss.SetCapability(twain.ICAP_PIXELTYPE, twain.TWTY_UINT16, 2)
                ss.SetCapability(twain.ICAP_BITDEPTH,  twain.TWTY_UINT16, 8)
            except Exception:
                pass

            try:
                ss.SetCapability(twain.CAP_INDICATORS, twain.TWTY_BOOL, False)
            except Exception:
                pass

            ss.RequestAcquire(int(show_ui), 1)
            rv = ss.XferImageNatively()
            if not rv:
                raise RuntimeError("Scanner returned no image.")

            handle, _ = rv
            bmp_data  = twain.DIBToBMFile(handle)
            twain.GlobalHandleFree(handle)

            img = Image.open(io.BytesIO(bmp_data))
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue()
        finally:
            ss.destroy()
    finally:
        sm.destroy()


def _scan_wia() -> bytes:
    import win32com.client

    PNG_FMT  = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
    dlg      = win32com.client.Dispatch("WIA.CommonDialog")
    img      = dlg.ShowAcquireImage(1, 4, 2, PNG_FMT, False, True, False)
    if img is None:
        raise RuntimeError("Scan cancelled or no image returned.")

    tmp      = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp_path = tmp.name
    tmp.close()
    os.unlink(tmp_path)

    try:
        img.SaveFile(tmp_path)
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def scan_windows(show_ui: bool) -> bytes:
    try:
        return _scan_twain(show_ui)
    except ImportError:
        log.info("twain package missing — trying WIA…")
    except Exception as e:
        log.warning("TWAIN failed (%s) — trying WIA…", e)
    return _scan_wia()


# ─── Linux: SANE single-page ─────────────────────────────────────────────────

def scan_linux() -> bytes:
    import sane
    from PIL import Image

    sane.init()
    try:
        devices = sane.get_devices()
        if not devices:
            raise RuntimeError("No SANE scanner found.")
        dev = sane.open(devices[0][0])
        try:
            dev.resolution = 200
            dev.mode       = "color"
            img = dev.scan()
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            dev.close()
    finally:
        sane.exit()


# ─── macOS: single-page paths ─────────────────────────────────────────────────

def _scan_via_scanimage() -> bytes:
    device = _get_scanimage_device()

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        result = subprocess.run(
            [
                "scanimage",
                f"--device={device}",
                "--format=png",
                "--resolution=200",
                "--mode=Color",
                f"--output-file={tmp_path}",
            ],
            capture_output=True, text=True, timeout=60,
            **_NO_WINDOW,
        )
        if result.returncode != 0:
            raise RuntimeError(f"scanimage failed: {result.stderr.strip()}")
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _scan_escl_http(base_url: str) -> bytes:
    scan_ns = "http://schemas.hp.com/imaging/escl/2011/05/03"
    pwg_ns  = "http://www.pwg.org/schemas/2010/12/sm"
    body    = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<scan:ScanSettings xmlns:scan="{scan_ns}" xmlns:pwg="{pwg_ns}">'
        "<pwg:Version>2.6</pwg:Version>"
        "<pwg:ScanRegions><pwg:ScanRegion>"
        "<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>"
        "<pwg:Width>2480</pwg:Width><pwg:Height>3508</pwg:Height>"
        "<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>"
        "</pwg:ScanRegion></pwg:ScanRegions>"
        "<pwg:InputSource>Platen</pwg:InputSource>"
        "<scan:ColorMode>RGB24</scan:ColorMode>"
        "<scan:XResolution>200</scan:XResolution>"
        "<scan:YResolution>200</scan:YResolution>"
        "<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>"
        "</scan:ScanSettings>"
    ).encode("utf-8")

    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE

    req = urllib.request.Request(
        f"{base_url}/ScanJobs", data=body,
        headers={"Content-Type": "text/xml; charset=utf-8"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
            location = resp.headers.get("Location", "").strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"eSCL ScanJobs POST failed: HTTP {e.code}")

    if not location:
        raise RuntimeError("eSCL: no Location header")

    job_url   = urljoin(base_url, location).rstrip("/")
    image_url = f"{job_url}/NextDocument"

    for attempt in range(1, 13):
        try:
            with urllib.request.urlopen(image_url, timeout=60, context=ctx) as resp:
                raw_image = resp.read()
            break
        except urllib.error.HTTPError as e:
            if e.code in (404, 503):
                time.sleep(2 if attempt < 6 else 4)
                continue
            raise RuntimeError(f"eSCL NextDocument failed: HTTP {e.code}")
    else:
        raise RuntimeError("eSCL NextDocument still not ready")

    return _png_from_bytes(raw_image)


_ICA_DELEGATE_CLS = None


def _get_ica_delegate_cls(ICC):
    global _ICA_DELEGATE_CLS
    if _ICA_DELEGATE_CLS is not None:
        return _ICA_DELEGATE_CLS

    from Foundation import NSObject

    class _ICABridgeDelegate(NSObject):
        def deviceBrowser_didAddDevice_moreComing_(self, browser, device, more):
            if self._state.get("device") is not None:
                return
            if device.type() == ICC.ICScannerDeviceType:
                self._state["device"] = device
                device.setDelegate_(self)
                device.requestOpenSession()
                self._ev_found.set()

        def deviceBrowser_didRemoveDevice_moreGoing_(self, browser, device, more):
            pass

        def device_didOpenSessionWithError_(self, device, error):
            if error:
                self._state["err"] = str(error.localizedDescription())
                self._ev_opened.set()
                self._ev_done.set()
                return
            device.requestSelectFunctionalUnit_(ICC.ICScannerFunctionalUnitTypeFlatbed)

        def device_didCloseSessionWithError_(self, device, error):
            pass

        def didRemoveDevice_(self, device):
            if not self._ev_done.is_set():
                self._state["err"] = "ICA: scanner disconnected."
                self._ev_done.set()

        def scannerDevice_didSelectFunctionalUnit_error_(self, scanner, unit, error):
            if error:
                self._state["err"] = str(error.localizedDescription())
                self._ev_opened.set()
                self._ev_done.set()
                return
            unit.setResolution_(200)
            unit.setPixelDataType_(0)
            self._ev_opened.set()
            scanner.requestScan()

        def scannerDevice_didScanToURL_(self, scanner, url):
            try:
                self._state["png"] = _png_from_path(str(url.path()))
            except Exception as e:
                self._state["err"] = f"ICA convert: {e}"
            finally:
                self._ev_done.set()

        def scannerDevice_didScanToURL_data_(self, scanner, url, data):
            self.scannerDevice_didScanToURL_(scanner, url)

        def scannerDevice_didCompleteScannedDocumentToURL_error_imageData_thumbnailURL_(
            self, scanner, url, error, imageData, thumbnailURL
        ):
            if error:
                self._state["err"] = str(error.localizedDescription())
                self._ev_done.set()
                return
            try:
                if imageData:
                    self._state["png"] = _png_from_bytes(bytes(imageData))
                elif url:
                    self._state["png"] = _png_from_path(str(url.path()))
                else:
                    self._state["err"] = "ICA: no image."
            except Exception as e:
                self._state["err"] = f"ICA convert: {e}"
            finally:
                self._ev_done.set()

        def scannerDevice_didEncounterError_(self, scanner, error):
            self._state["err"] = str(error.localizedDescription())
            self._ev_done.set()

    _ICA_DELEGATE_CLS = _ICABridgeDelegate
    return _ICABridgeDelegate


def _ica_location_mask(ICC) -> int:
    mask = 0
    for attr in (
        "ICDeviceLocationTypeMaskLocal",
        "ICDeviceLocationTypeMaskBonjour",
        "ICDeviceLocationTypeMaskShared",
        "ICDeviceLocationTypeMaskBluetooth",
    ):
        try:
            mask |= getattr(ICC, attr)
        except AttributeError:
            pass
    return mask if mask else 0xFFFF00


def _scan_macos_usb_pyobjc() -> bytes:
    try:
        from Foundation import NSObject, NSRunLoop, NSDate  # noqa: F401
        import ImageCaptureCore as ICC
    except ImportError:
        raise ImportError("pyobjc-framework-ImageCaptureCore not installed.")
    import threading

    _ev_found  = threading.Event()
    _ev_opened = threading.Event()
    _ev_done   = threading.Event()
    _state     = {"device": None, "png": None, "err": None}

    DelegateCls = _get_ica_delegate_cls(ICC)
    delegate    = DelegateCls.alloc().init()
    delegate._ev_found  = _ev_found
    delegate._ev_opened = _ev_opened
    delegate._ev_done   = _ev_done
    delegate._state     = _state

    browser = ICC.ICDeviceBrowser.alloc().init()
    browser.setDelegate_(delegate)
    browser.setBrowsedDeviceTypeMask_(
        ICC.ICDeviceTypeMaskScanner | _ica_location_mask(ICC)
    )
    browser.start()

    rl = NSRunLoop.currentRunLoop()

    def _spin(event, timeout, label):
        elapsed = 0.0
        while not event.is_set() and elapsed < timeout:
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
            elapsed += 0.1
        if not event.is_set():
            raise RuntimeError(f"ICA: timeout waiting for {label}")

    try:
        _spin(_ev_found,  10.0, "discovery")
        if _state["err"]:
            raise RuntimeError(_state["err"])
        _spin(_ev_opened, 15.0, "unit select")
        if _state["err"]:
            raise RuntimeError(_state["err"])
        _spin(_ev_done,   90.0, "scan completion")
    finally:
        browser.stop()
        if _state["device"]:
            _state["device"].requestCloseSession()

    if _state["err"]:
        raise RuntimeError(_state["err"])
    if not _state["png"]:
        raise RuntimeError("ICA: no image data.")
    return _state["png"]


def scan_macos() -> bytes:
    try:
        return _scan_via_scanimage()
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("scanimage failed (%s) → trying eSCL HTTP…", e)
    try:
        return _scan_escl_http(_find_escl_url())
    except Exception as e:
        log.warning("eSCL HTTP failed (%s) → trying ImageCaptureCore…", e)
    return _scan_macos_usb_pyobjc()


# ─── Platform dispatch ────────────────────────────────────────────────────────

def do_scan() -> bytes:
    if sys.platform == "win32":
        return scan_windows(SHOW_UI)
    elif sys.platform.startswith("linux"):
        return scan_linux()
    elif sys.platform == "darwin":
        return scan_macos()
    else:
        raise RuntimeError(f"Unsupported platform: {sys.platform}")


# ─── WebSocket handler ────────────────────────────────────────────────────────

async def handle(websocket):
    peer = websocket.remote_address
    log.info("Connected  : %s", peer)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send(
                    json.dumps({"status": "error", "message": "Invalid JSON"})
                )
                continue

            action = msg.get("action", "")
            log.info("← %s", action)

            if action == "ping":
                await websocket.send(json.dumps({
                    "status":   "ok",
                    "message":  "pong",
                    "platform": sys.platform,
                    "show_ui":  SHOW_UI,
                }))

            elif action == "list_scanners":
                loop = asyncio.get_event_loop()
                try:
                    scanners = await loop.run_in_executor(executor, list_scanners)
                    await websocket.send(
                        json.dumps({"status": "ok", "scanners": scanners})
                    )
                except Exception as e:
                    await websocket.send(
                        json.dumps({"status": "error", "message": str(e)})
                    )

            elif action == "scan":
                auto_crop = bool(msg.get("auto_crop", False))
                await websocket.send(json.dumps({
                    "status":  "scanning",
                    "message": "Scanner acquiring image…",
                }))
                loop = asyncio.get_event_loop()
                try:
                    png_bytes = await loop.run_in_executor(executor, do_scan)
                    if auto_crop:
                        cropped, bounds = await loop.run_in_executor(
                            executor, _auto_crop_png, png_bytes
                        )
                        await websocket.send(json.dumps({
                            "status":   "ok",
                            "image":    base64.b64encode(cropped).decode(),
                            "original": base64.b64encode(png_bytes).decode(),
                            "bounds":   bounds,
                        }))
                    else:
                        log.info("→ image  %s bytes", f"{len(png_bytes):,}")
                        await websocket.send(json.dumps({
                            "status": "ok",
                            "image":  base64.b64encode(png_bytes).decode(),
                        }))
                except Exception as e:
                    log.error("Scan error: %s", e)
                    await websocket.send(
                        json.dumps({"status": "error", "message": str(e)})
                    )

            elif action == "scan_adf":
                await websocket.send(json.dumps({
                    "status":  "scanning",
                    "message": "Loading document feeder — please wait…",
                }))
                loop = asyncio.get_event_loop()
                progress_q = asyncio.Queue()

                def _progress_cb(pmsg):
                    # Called from the executor (worker) thread.  Push
                    # the message onto the main-loop's queue thread-safely.
                    try:
                        asyncio.run_coroutine_threadsafe(
                            progress_q.put(pmsg), loop
                        )
                    except Exception as ex:
                        log.debug("progress_cb enqueue failed: %s", ex)

                def _do_scan_adf():
                    return scan_adf(progress_cb=_progress_cb)

                scan_future = loop.run_in_executor(executor, _do_scan_adf)

                # Drain progress messages until the scan future completes.
                try:
                    while True:
                        get_task = asyncio.create_task(progress_q.get())
                        done, _ = await asyncio.wait(
                            {scan_future, get_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if get_task in done:
                            pmsg = get_task.result()
                            try:
                                await websocket.send(json.dumps(pmsg))
                            except Exception:
                                pass
                            if scan_future.done():
                                break
                        else:
                            # scan_future finished first — cancel pending get
                            get_task.cancel()
                            try:
                                await get_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            break

                    # Drain any remaining queued progress messages
                    while not progress_q.empty():
                        try:
                            pmsg = progress_q.get_nowait()
                            await websocket.send(json.dumps(pmsg))
                        except asyncio.QueueEmpty:
                            break

                    pages_png  = scan_future.result()
                    images_b64 = [base64.b64encode(p).decode() for p in pages_png]
                    log.info("→ ADF scan OK  %d page(s)", len(images_b64))
                    await websocket.send(json.dumps({
                        "status":     "ok",
                        "images":     images_b64,
                        "page_count": len(images_b64),
                    }))
                except Exception as e:
                    log.error("ADF scan error: %s", e)
                    try:
                        await websocket.send(
                            json.dumps({"status": "error", "message": str(e)})
                        )
                    except Exception:
                        pass

            elif action == "make_pdf":
                images_b64 = msg.get("images", [])
                if not images_b64:
                    await websocket.send(json.dumps({
                        "status":  "error",
                        "message": "No images provided",
                    }))
                    continue

                loop = asyncio.get_event_loop()
                try:
                    pdf_bytes = await loop.run_in_executor(
                        executor, _images_to_pdf, images_b64
                    )
                    log.info(
                        "→ make_pdf OK  %d page(s)  %s bytes",
                        len(images_b64), f"{len(pdf_bytes):,}",
                    )
                    await websocket.send(json.dumps({
                        "status":     "ok",
                        "pdf":        base64.b64encode(pdf_bytes).decode(),
                        "page_count": len(images_b64),
                    }))
                except Exception as e:
                    log.error("make_pdf error: %s", e)
                    await websocket.send(
                        json.dumps({"status": "error", "message": str(e)})
                    )

            elif action == "detect_bounds":
                raw_b64 = msg.get("image", "")
                if not raw_b64:
                    await websocket.send(json.dumps({
                        "status":  "error",
                        "message": "No image provided",
                    }))
                    continue
                loop = asyncio.get_event_loop()
                try:
                    png_bytes       = base64.b64decode(raw_b64)
                    cropped, bounds = await loop.run_in_executor(
                        executor, _auto_crop_png, png_bytes
                    )
                    log.info("→ detect_bounds: %s", bounds)
                    await websocket.send(json.dumps({
                        "status": "ok",
                        "bounds": bounds,
                        "image":  base64.b64encode(cropped).decode(),
                    }))
                except Exception as e:
                    log.error("detect_bounds error: %s", e)
                    await websocket.send(
                        json.dumps({"status": "error", "message": str(e)})
                    )

            elif action == "crop":
                raw_b64 = msg.get("image", "")
                if not raw_b64:
                    await websocket.send(json.dumps({
                        "status":  "error",
                        "message": "No image provided",
                    }))
                    continue
                try:
                    x      = int(msg["x"])
                    y      = int(msg["y"])
                    width  = int(msg["width"])
                    height = int(msg["height"])
                except (KeyError, ValueError) as e:
                    await websocket.send(json.dumps({
                        "status":  "error",
                        "message": f"Invalid crop parameters: {e}",
                    }))
                    continue
                loop = asyncio.get_event_loop()
                try:
                    png_bytes = base64.b64decode(raw_b64)
                    cropped   = await loop.run_in_executor(
                        executor, _crop_png, png_bytes, x, y, width, height
                    )
                    log.info("→ crop OK  %s bytes", f"{len(cropped):,}")
                    await websocket.send(json.dumps({
                        "status": "ok",
                        "image":  base64.b64encode(cropped).decode(),
                    }))
                except Exception as e:
                    log.error("crop error: %s", e)
                    await websocket.send(
                        json.dumps({"status": "error", "message": str(e)})
                    )

            else:
                await websocket.send(json.dumps({
                    "status":  "error",
                    "message": f"Unknown action: {action!r}",
                }))

    except websockets.exceptions.ConnectionClosed:
        log.info("Disconnected: %s", peer)


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    global SHOW_UI

    parser = argparse.ArgumentParser(description="Cheque Scanner Bridge for Odoo")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--show-ui", action="store_true",
        help="Show scanner's own UI dialog (TWAIN/WIA)",
    )
    args    = parser.parse_args()
    SHOW_UI = args.show_ui

    async def _run():
        async with websockets.serve(
            handle, "localhost", args.port, max_size=None
        ):
            bar = "━" * 56
            log.info(bar)
            log.info("  Cheque Scanner Bridge  v1.7.11")
            log.info("  ws://localhost:%s", args.port)
            log.info("  Platform : %s", sys.platform)
            log.info("  Show UI  : %s", SHOW_UI)
            log.info("  URL cache: %s", _ESCL_URL_CACHE)
            log.info("  Log file : %s", _LOG_FILE)
            log.info("  Frozen   : %s", _is_frozen())
            log.info(bar)
            log.info("  Actions:")
            log.info("    ping                     — health check")
            log.info("    list_scanners            — enumerate connected scanners")
            log.info("    scan                     — acquire single page (flatbed)")
            log.info("    scan + auto_crop:true    — acquire + auto-crop")
            log.info("    scan_adf                 — acquire ALL pages from feeder")
            log.info("                                (emits status:'progress' per page)")
            log.info("    detect_bounds            — find document on scanned page")
            log.info("    crop                     — crop image by coordinates")
            log.info("    make_pdf                 — combine images into PDF")
            log.info(bar)
            await asyncio.Future()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Bridge stopped.")


if __name__ == "__main__":
    main()
