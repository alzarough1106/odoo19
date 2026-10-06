#!/usr/bin/env python3
# scanner_bridge.py
# Copyright (C) 2026 alzarough1106
# License OPL-1 - See LICENSE file
# Unauthorized use is strictly prohibited!
"""
Cheque / Document Scanner Bridge  v1.9.0
=========================================
ws://localhost:8765  →  Odoo Cheque Book / Document Scanner

v1.9.0
------
- Structured scanner errors ("code"): double_feed, paper_jam, cover_open,
  feeder_empty, offline, busy, no_feeder, inbox_empty, all_blank, cancelled.
  WIA HRESULT + handling status, TWAIN condition codes, SANE status + Fujitsu
  double-feed sensor, eSCL AdfState, ICA errors.
- Double-feed detection enabled where the driver exposes it
  (SANE fujitsu df-action/df-thickness, TWAIN CAP_DOUBLEFEEDDETECTION).
- Imprinter / endorser: hardware (SANE endorser-*, TWAIN CAP_PRINTER*,
  WIA ENDORSER_STRING) or digital stamp fallback.
- Page streaming: scan_adf with stream:true sends one "page" message per
  sheet; "cancel" action stops a running batch.
- Folder inboxes (--inbox) for PaperStream NX Manager / ScanSnap Home
  "save to folder" output (PDF, TIFF/multi-TIFF, JPEG, PNG).
- Background-aware document detection & blank-page detection
  (white AND black scanner backgrounds).
- Fujitsu / Ricoh (PFU) specifics: A4/legal page size, feeder-only models,
  duplex, ScanSnap flagged, TWAIN (PaperStream IP) sources listed.

Install
-------
  Windows : pip install websockets Pillow twain pywin32
  Linux   : pip install websockets Pillow python-sane
  macOS   : pip install websockets Pillow pyobjc-framework-ImageCaptureCore
            brew install sane-backends

Run
---
  python scanner_bridge.py --allowed-origin https://odoo.example.com
  python scanner_bridge.py --inbox "NX Manager=\\\\server\\scans\\desk1"
  python scanner_bridge.py --port 9000 --log-level DEBUG
"""

import argparse
import asyncio
import base64
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urljoin

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scanner_bridge")

VERSION = "1.9.0"

# Device access must be serialised (SANE / WIA / TWAIN are not thread-safe).
DEVICE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scanner-device")
# Pure image work runs in parallel with a scan.
CPU_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scanner-cpu")

SHOW_UI = False
MAX_MESSAGE_SIZE = 256 * 1024 * 1024
SCANNER_CACHE_TTL = 300
MAX_PAGES = 2000
LONG_PAGE_MM = 355.6          # US legal: covers A4 (297 mm) + legal

INBOXES = []                  # [{"label": str, "path": str}]
INBOX_KEEP = False
INBOX_SETTLE = 2.0            # seconds a file must be unchanged before import
IMPRINT_FONT = None

_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32"
    else {}
)

_ESCL_URL_CACHE = os.path.join(tempfile.gettempdir(), ".escl_url_cache_bridge")


# ═════════════════════════════════════════════════════════════════════════════
#  Errors
# ═════════════════════════════════════════════════════════════════════════════

class ScannerError(RuntimeError):
    """Error with a machine-readable code understood by the Odoo widget."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code = code
        self.extra = extra


class JobCancelled(ScannerError):
    def __init__(self):
        super().__init__("cancelled", "Scan cancelled by the user.")


ERROR_HINTS = {
    "double_feed": "Double feed detected: two or more sheets went through together. "
                   "Put the last sheet(s) from the output tray back on top of the "
                   "remaining stack and continue.",
    "paper_jam": "Paper jam / misfeed. Open the ADF, remove the sheet, reload it and continue.",
    "feeder_empty": "The document feeder is empty — load the documents and try again.",
    "cover_open": "The scanner cover / ADF hatch is open — close it and try again.",
    "offline": "The scanner is offline or disconnected.",
    "busy": "The scanner is busy or warming up (used by another application?).",
    "attention": "The scanner needs attention — check its display / LEDs.",
    "no_feeder": "This scanner has no document feeder.",
    "inbox_empty": "No documents arrived in the folder inbox.",
    "all_blank": "All scanned pages were detected as blank.",
    "cancelled": "Scan cancelled.",
}

_ERROR_PATTERNS = [
    ("double_feed", r"double[\s_-]?feed|multi[\s_-]?feed|multi[\s_-]?pick|doublefeed|multipick"),
    ("cover_open", r"cover (is )?open|door ?open|hatch ?open|interlock|coveropen"),
    ("paper_jam", r"jam|mis-?pick|mis-?feed|paper problem"),
    ("feeder_empty", r"out of documents|no docs|feeder (is )?empty|no paper|paper empty|"
                     r"nomedia|no media|adfempty|paperempty"),
    ("offline", r"offline|checkdeviceonline|no such device|device not (found|connected)"),
    ("busy", r"device busy|\bbusy\b|warming up"),
]


def _classify_text(text):
    t = str(text or "").lower()
    for code, pat in _ERROR_PATTERNS:
        if re.search(pat, t):
            return code
    return None


def _hint_message(code, detail=None):
    base = ERROR_HINTS.get(code, "")
    d = str(detail or "").strip()
    if base and d and d.lower() not in base.lower():
        return f"{base}\n(device: {d[:300]})"
    return base or d


def _as_scanner_error(e):
    if isinstance(e, ScannerError):
        return e
    code = _classify_text(f"{type(e).__name__} {e}")
    return ScannerError(code, _hint_message(code, e)) if code else None


HARD_STOP_CODES = {"double_feed", "paper_jam", "cover_open", "cancelled"}


# ═════════════════════════════════════════════════════════════════════════════
#  Scan job (options, cancellation, page sink)
# ═════════════════════════════════════════════════════════════════════════════

class Imprint:
    """Unified imprint / endorser spec. Template placeholders: {counter} {date}."""

    def __init__(self, spec=None):
        spec = spec if isinstance(spec, dict) else {}
        self.enabled = bool(spec.get("enabled"))
        mode = spec.get("mode")
        self.mode = mode if mode in ("auto", "hardware", "digital") else "auto"
        self.template = str(spec.get("text") or "{date} {counter}")[:120]
        self.start = self._int(spec.get("counter_start"), 1, 0, 99999999)
        self.step = self._int(spec.get("step"), 1, 1, 1000)
        self.digits = self._int(spec.get("digits"), 5, 1, 8)
        self.position = "bottom" if spec.get("position") == "bottom" else "top"
        self.side = "back" if spec.get("side") == "back" else "front"
        self.date = datetime.now().strftime("%Y-%m-%d")

    @staticmethod
    def _int(v, default, lo, hi):
        try:
            return max(lo, min(hi, int(v)))
        except (TypeError, ValueError):
            return default

    def split(self):
        """→ (prefix, suffix, has_counter) with {date} already rendered."""
        text = self.template.replace("{date}", self.date)
        if "{counter}" in text:
            pre, _, post = text.partition("{counter}")
            return pre, post.replace("{counter}", ""), True
        return text, "", False

    def render(self, n):
        return (self.template.replace("{date}", self.date)
                .replace("{counter}", str(n).zfill(self.digits)))

    @property
    def wants_hardware(self):
        return self.enabled and self.mode in ("auto", "hardware")


class ScanJob:
    def __init__(self, job_id=None, *, duplex=False, skip_blank=False,
                 blank_sensitivity="normal", double_feed=True, imprint=None,
                 stream=False, inbox_wait=120, max_pages=MAX_PAGES):
        self.id = job_id
        self.duplex = bool(duplex)
        self.skip_blank = bool(skip_blank)
        self.blank_sensitivity = blank_sensitivity if blank_sensitivity in BLANK_THRESHOLDS else "normal"
        self.double_feed = bool(double_feed)
        self.imprint = imprint or Imprint()
        self.stream = bool(stream)
        self.inbox_wait = inbox_wait
        self.max_pages = max_pages
        self.cancel_event = threading.Event()
        self.sink = None
        self.raw_pages = 0
        self.documents = 0
        self.proc = None
        self.imprint_hw_applied = False
        self.warnings = []

    @classmethod
    def from_message(cls, rid, msg):
        duplex = bool(msg.get("duplex"))
        try:
            wait = max(10, min(900, int(msg.get("inbox_wait", 120))))
        except (TypeError, ValueError):
            wait = 120
        return cls(
            rid, duplex=duplex,
            skip_blank=bool(msg.get("skip_blank", duplex)),
            blank_sensitivity=msg.get("blank_sensitivity", "normal"),
            double_feed=msg.get("double_feed", True) is not False,
            imprint=Imprint(msg.get("imprint")),
            stream=bool(msg.get("stream")),
            inbox_wait=wait,
        )

    @property
    def cancelled(self):
        return self.cancel_event.is_set()

    @property
    def full(self):
        return self.raw_pages >= self.max_pages

    @property
    def digital_active(self):
        im = self.imprint
        return im.enabled and (im.mode == "digital" or (im.mode == "auto" and not self.imprint_hw_applied))

    def check(self):
        if self.cancelled:
            raise JobCancelled()

    def emit(self, data: bytes):
        self.raw_pages += 1
        if self.sink:
            self.sink("page", data)

    def emit_document(self, name, data, mimetype):
        self.documents += 1
        if self.sink:
            self.sink("document", (name, data, mimetype))

    def progress(self, message):
        if self.sink:
            self.sink("progress", message)

    def cancel(self):
        self.cancel_event.set()
        p = self.proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass

    def imprint_next(self, sent):
        im = self.imprint
        if not im.enabled:
            return None
        if self.digital_active:
            return im.start + sent * im.step
        if self.imprint_hw_applied:
            sheets = math.ceil(self.raw_pages / (2 if self.duplex else 1))
            return im.start + sheets * im.step
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Generic helpers
# ═════════════════════════════════════════════════════════════════════════════

_GENERIC_TOKENS = {
    "scanner", "scan", "printer", "series", "usb", "network", "device", "the",
    "and", "inc", "corp", "corporation", "ltd", "wia", "twain", "driver",
    "flatbed", "all", "one", "mfp",
}


def _tokens(text) -> set:
    return {
        t for t in re.split(r"[^a-z0-9]+", str(text or "").lower())
        if len(t) >= 3 and t not in _GENERIC_TOKENS
    }


def _best_match(candidates, selector=None, display=None,
                labels=lambda c: [c], strict=False):
    """Pick the candidate matching the scanner chosen in Odoo."""
    if not candidates:
        return None
    if not selector and not display:
        return candidates[0]

    def _labs(c):
        return [str(x) for x in (labels(c) or []) if x]

    for needle in (selector, display):
        if needle:
            for c in candidates:
                if needle in _labs(c):
                    return c

    for needle in (display, selector):
        if needle:
            n = needle.lower()
            for c in candidates:
                for lab in _labs(c):
                    ll = lab.lower()
                    if n in ll or ll in n:
                        return c

    wanted = _tokens(display) | _tokens(selector)
    best, score = None, 0
    for c in candidates:
        have = set()
        for lab in _labs(c):
            have |= _tokens(lab)
        s = len(wanted & have)
        if s > score:
            best, score = c, s
    if best is not None:
        return best
    if strict:
        return None
    log.info("No device matches %r / %r — using the first one", selector, display)
    return candidates[0]


@contextmanager
def _com_apartment():
    pythoncom = None
    if sys.platform == "win32":
        try:
            import pythoncom as _pc
            _pc.CoInitialize()
            pythoncom = _pc
        except Exception as e:
            log.debug("CoInitialize failed/unavailable: %s", e)
    try:
        yield
    finally:
        if pythoncom is not None:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass


def _escl_base_from_selector(selector):
    if selector and str(selector).lower().startswith(("http://", "https://")):
        return str(selector).rstrip("/")
    return None


def _parse_scanimage_list(text: str) -> list:
    out = []
    for line in (text or "").splitlines():
        m = re.search(r"device\s+[`'\u2018\"](.+?)['\u2019\"]\s+is\s+an?\s+(.+)$", line, re.I)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
            continue
        if "`" in line and "'" in line:
            s = line.find("`")
            e = line.find("'", s + 1)
            if s != -1 and e != -1:
                out.append((line[s + 1:e], ""))
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  Fujitsu / Ricoh (PFU) helpers
# ═════════════════════════════════════════════════════════════════════════════

_FUJITSU_RE = re.compile(
    r"fujitsu|ricoh|\bpfu\b|pfufs|epjitsu|scansnap|paperstream|\bfi-\d{3,4}|\bsp-1\d{3}", re.I
)
_SCANSNAP_MODEL_RE = re.compile(r"\b(ix\d{3,4}|s1[13]00i?|s1500m?|s300m?|sv600)\b", re.I)


def _is_fujitsu(*labels) -> bool:
    return any(l and _FUJITSU_RE.search(str(l)) for l in labels)


def _is_scansnap(*labels) -> bool:
    text = " ".join(str(l) for l in labels if l)
    if re.search(r"scansnap", text, re.I):
        return True
    return _is_fujitsu(text) and bool(_SCANSNAP_MODEL_RE.search(text))


def _annotate_fujitsu(scanners: list) -> list:
    for s in scanners:
        if s.get("source") == "inbox":
            continue
        name, disp = s.get("name", ""), s.get("display", "")
        if not sys.platform.startswith("linux") and _is_scansnap(name, disp):
            s["ready"] = False
            s["note"] = ("ScanSnap has no TWAIN/WIA/ICA driver. Configure ScanSnap Home "
                         "'save to folder' and start the bridge with --inbox.")
        elif str(name).startswith("epjitsu:"):
            s["note"] = ((s.get("note") or "") +
                         " epjitsu backend: requires the firmware file (man sane-epjitsu).").strip()
    return scanners


# ═════════════════════════════════════════════════════════════════════════════
#  Device-name humanisation
# ═════════════════════════════════════════════════════════════════════════════

def _sane_backend_vendor(dev_name: str) -> str:
    backend = dev_name.split(":")[0].lower().rstrip("0123456789")
    return {
        "hpaio": "HP", "hpoj": "HP", "pixma": "Canon", "bjnp": "Canon",
        "epson": "Epson", "epson2": "Epson", "epsonds": "Epson",
        "brother": "Brother", "brother4": "Brother", "xerox": "Xerox",
        "lexmark": "Lexmark", "samsung": "Samsung", "ricoh": "Ricoh",
        "fujitsu": "Fujitsu", "epjitsu": "Fujitsu", "pfufs": "Ricoh/Fujitsu",
        "kodak": "Kodak", "mustek": "Mustek", "umax": "UMAX",
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
    mfr = (manufacturer or "").strip()
    nm = (name or "").strip()
    desc = (description or "").strip()
    _generic = {"wia", "wia scanner", "scanner", "image", "unknown"}
    if nm.lower() in _generic:
        nm = ""
    if mfr and nm:
        return nm if nm.lower().startswith(mfr.lower()) else f"{mfr} {nm}"
    if nm:
        return nm
    if desc and desc.lower() not in _generic:
        return desc
    _vid_map = {
        "03F0": "HP", "04A9": "Canon", "04B8": "Epson", "04F9": "Brother",
        "04E8": "Samsung", "0924": "Xerox", "043D": "Lexmark", "04DA": "Panasonic",
        "08F0": "Microtek", "055F": "Mustek", "0638": "Avision", "04C5": "Fujitsu",
        "05CA": "Ricoh",
    }
    m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", dev_id or "")
    if m:
        vid, pid = m.group(1).upper(), m.group(2).upper()
        return f"{_vid_map.get(vid, f'VID_{vid}')} Scanner (PID {pid})"
    m2 = re.search(r"}\s*\\\s*(\d+)\s*$", dev_id or "")
    if m2:
        return f"Scanner (device {m2.group(1)})"
    return dev_id or "Unknown Scanner"


# ═════════════════════════════════════════════════════════════════════════════
#  Discovery
# ═════════════════════════════════════════════════════════════════════════════

def _make_adder(scanners, seen):
    def _add(name, display, source, ready, note=""):
        key = (display or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            scanners.append({
                "name": name, "display": display.strip(),
                "source": source, "ready": ready, "note": note,
            })
    return _add


def get_scanners_wia() -> list:
    """Windows discovery (call inside _com_apartment)."""
    scanners, seen = [], set()
    _add = _make_adder(scanners, seen)

    # Tier 0: TWAIN sources (exact names used by the TWAIN scan path)
    try:
        import twain
        sm = twain.SourceManager(0)
        try:
            fn = getattr(sm, "GetSourceList", None)
            for src_name in (fn() if callable(fn) else getattr(sm, "source_list", []) or []):
                note = "PaperStream IP (TWAIN)" if "paperstream" in str(src_name).lower() else "TWAIN"
                _add(str(src_name), str(src_name), "TWAIN", True, note)
        finally:
            _twain_close(sm)
    except ImportError:
        log.debug("TWAIN package not installed")
    except Exception as e:
        log.warning("TWAIN listing failed: %s", e)

    # Tier 1: wia package
    try:
        import wia
        for d in wia.DeviceManager().devices:
            display = _humanise_wia_device(d.id, name=getattr(d, "name", ""),
                                           manufacturer=getattr(d, "manufacturer", ""))
            _add(d.id, display, "wia-package", True)
    except ImportError:
        pass
    except Exception as e:
        log.warning("wia package failed: %s", e)

    # Tier 2: WIA COM
    try:
        import win32com.client
        mgr = win32com.client.Dispatch("WIA.DeviceManager")
        for i in range(1, mgr.DeviceInfos.Count + 1):
            try:
                info = mgr.DeviceInfos.Item(i)
                vals = {}
                for prop_name in ("DeviceID", "Name", "Manufacturer", "Description"):
                    try:
                        vals[prop_name] = str(info.Properties(prop_name).Value or "")
                    except Exception:
                        vals[prop_name] = ""
                if not vals["DeviceID"]:
                    try:
                        vals["DeviceID"] = str(info.DeviceID)
                    except Exception:
                        pass
                try:
                    device_type = int(info.Type)
                except Exception:
                    device_type = 0
                if device_type not in (0, 1):
                    continue
                display = _humanise_wia_device(vals["DeviceID"], vals["Name"],
                                               vals["Manufacturer"], vals["Description"])
                _add(vals["DeviceID"] or vals["Name"], display, "WIA-COM", True)
            except Exception as ex:
                log.warning("WIA COM item %d failed: %s", i, ex)
    except ImportError:
        log.info("win32com not installed (pip install pywin32)")
    except Exception as e:
        log.warning("WIA COM failed: %s", e)

    # Tier 3: Get-PnpDevice
    try:
        ps_cmd = (
            "Get-PnpDevice -Class 'Image','Printer' -Status 'OK','Unknown' "
            "| Select-Object InstanceId, FriendlyName, Manufacturer, Class, Status "
            "| ConvertTo-Json -Compress"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                                capture_output=True, text=True, timeout=60, **_NO_WINDOW)
        if result.stdout.strip():
            raw = json.loads(result.stdout.strip())
            raw = [raw] if isinstance(raw, dict) else raw
            for d in raw:
                dev_id = d.get("InstanceId", "") or ""
                friendly = d.get("FriendlyName", "") or ""
                mfr = d.get("Manufacturer", "") or ""
                cls = (d.get("Class", "") or "").lower()
                if cls == "printer" and not re.search(
                    r"scan|mfp|multifunction|aio|officejet|deskjet|envy|pixma|workforce|"
                    r"mfc|laserjet|color\s*laser", friendly, re.I):
                    continue
                display = _humanise_wia_device(dev_id, friendly, mfr)
                if cls == "printer":
                    _add(dev_id, display, "PnpDevice", False, "Printer — may have scan capability")
                else:
                    _add(dev_id, display, "PnpDevice", True)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Get-PnpDevice failed: %s", e)

    # Tier 4: WMI
    try:
        ps_cmd = (
            "Get-WmiObject -Query \"SELECT Name, DeviceID, Manufacturer, Description "
            "FROM Win32_PnPEntity WHERE PNPClass = 'Image'\" "
            "| Select-Object Name, DeviceID, Manufacturer, Description | ConvertTo-Json -Compress"
        )
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
                                capture_output=True, text=True, timeout=60, **_NO_WINDOW)
        if result.stdout.strip():
            raw = json.loads(result.stdout.strip())
            raw = [raw] if isinstance(raw, dict) else raw
            for d in raw:
                dev_id = d.get("DeviceID", "") or ""
                display = _humanise_wia_device(dev_id, d.get("Name", "") or "",
                                               d.get("Manufacturer", "") or "",
                                               d.get("Description", "") or "")
                _add(dev_id, display, "WMI", False, "Detected via WMI — WIA driver may be missing")
    except Exception as e:
        log.warning("WMI fallback failed: %s", e)

    # Tier 5: Registry STI
    _STI_CLASS = "{6bdd1fc6-810f-11d0-bec7-08002be2092f}"
    try:
        import winreg
        base = rf"SYSTEM\CurrentControlSet\Control\Class\{_STI_CLASS}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            idx = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(root, idx)
                except OSError:
                    break
                idx += 1
                if not re.match(r"^\d{4}$", sub_name):
                    continue
                try:
                    with winreg.OpenKey(root, sub_name) as sub:
                        def _reg(key, default=""):
                            try:
                                return winreg.QueryValueEx(sub, key)[0] or default
                            except Exception:
                                return default
                        friendly = _reg("FriendlyName")
                        model = _reg("Model") or _reg("DeviceDesc")
                        display = _humanise_wia_device(_reg("DevicePath") or sub_name,
                                                       friendly or model, _reg("Mfg"))
                        _add(_reg("DevicePath") or sub_name, display, "Registry-STI", False,
                             "Found in STI registry; driver status unknown")
                except OSError:
                    continue
    except (ImportError, FileNotFoundError):
        pass
    except Exception as e:
        log.warning("Registry STI failed: %s", e)

    return scanners


def get_scanners_macos() -> list:
    scanners, seen = [], set()
    _add = _make_adder(scanners, seen)

    try:
        result = subprocess.run(["scanimage", "-L"], capture_output=True, text=True,
                                timeout=60, **_NO_WINDOW)
        for dev, label in _parse_scanimage_list(result.stdout + "\n" + result.stderr):
            _add(dev, label or _humanise_sane_device(dev), "scanimage", True)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("scanimage -L failed: %s", e)

    try:
        import ImageCaptureCore as ICC
        from Foundation import NSDate, NSRunLoop

        delegate = _get_ica_delegate_cls(ICC).alloc().init()
        delegate._ev_found = threading.Event()
        delegate._ev_opened = threading.Event()
        delegate._ev_done = threading.Event()
        delegate._state = _ica_new_state()
        browser = ICC.ICDeviceBrowser.alloc().init()
        browser.setDelegate_(delegate)
        browser.setBrowsedDeviceTypeMask_(
            getattr(ICC, "ICDeviceTypeMaskScanner", 0x2) | _ica_location_mask(ICC))
        browser.start()
        rl = NSRunLoop.currentRunLoop()
        elapsed = 0.0
        while not delegate._ev_found.is_set() and elapsed < 8.0:
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
            elapsed += 0.1
        browser.stop()
        for dev in delegate._state["found"]:
            name = str(dev.name() or "")
            try:
                uuid = str(dev.UUIDString() or "")
            except Exception:
                uuid = ""
            _add(name or uuid, name or uuid or "Unknown Scanner", "ImageCaptureCore", True)
    except ImportError:
        pass
    except Exception as e:
        log.warning("ImageCaptureCore probe failed: %s", e)

    try:
        escl_url = _find_escl_url(browse_timeout=5)
        display_name = "eSCL Scanner"
        try:
            with urllib.request.urlopen(f"{escl_url}/ScannerCapabilities", timeout=10,
                                        context=_escl_ssl_ctx()) as r:
                body = r.read(4096).decode(errors="replace")
            m = re.search(r"<(?:\w+:)?MakeAndModel[^>]*>\s*([^<]+)\s*<", body, re.I)
            if m:
                display_name = m.group(1).strip()
        except Exception:
            pass
        _add(escl_url, display_name, "eSCL", True, f"eSCL at {escl_url}")
    except Exception as e:
        log.info("eSCL probe found nothing (%s)", e)

    try:
        result = subprocess.run(["system_profiler", "SPUSBDataType", "-json"],
                                capture_output=True, text=True, timeout=60, **_NO_WINDOW)
        data = json.loads(result.stdout or "{}")

        def _walk(node):
            if isinstance(node, list):
                for item in node:
                    _walk(item)
            elif isinstance(node, dict):
                name = node.get("_name", "")
                if re.search(r"scan|printer|mfp|multifunction|aio|laser|inkjet|officejet|"
                             r"deskjet|envy|pixma|workforce|mfc|dcpl|fi-\d", name, re.I):
                    display = f"{node.get('manufacturer', '')} {name}".strip()
                    _add(node.get("serial_num", name), display, "USB-hardware", False,
                         "Detected via USB; driver availability not confirmed")
                for v in node.values():
                    if isinstance(v, (list, dict)):
                        _walk(v)

        _walk(data)
    except Exception as e:
        log.warning("system_profiler failed: %s", e)

    return scanners


def get_scanners_linux() -> list:
    scanners, seen = [], set()
    _add = _make_adder(scanners, seen)

    try:
        import sane
        sane.init()
        try:
            for dev_name, vendor, model, _t in sane.get_devices():
                _add(dev_name, _humanise_sane_device(dev_name, vendor, model), "SANE", True)
        finally:
            sane.exit()
    except ImportError:
        pass
    except Exception as e:
        log.warning("SANE failed: %s", e)

    try:
        result = subprocess.run(["scanimage", "-L"], capture_output=True, text=True,
                                timeout=30, **_NO_WINDOW)
        for dev, label in _parse_scanimage_list(result.stdout + "\n" + result.stderr):
            _add(dev, label or _humanise_sane_device(dev), "scanimage", True)
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("scanimage -L failed: %s", e)

    try:
        result = subprocess.run(["lsusb"], capture_output=True, text=True, timeout=40, **_NO_WINDOW)
        for line in result.stdout.splitlines():
            if re.search(r"scan|printer|mfp|multifunction|aio|officejet|deskjet|envy|pixma|"
                         r"workforce|mfc|laser|inkjet|canon|epson|fujitsu|pfu|hp\b", line, re.I):
                m = re.search(r"ID\s+[\da-f:]+\s+(.+)", line, re.I)
                display = m.group(1).strip() if m else line.strip()
                m2 = re.search(r"Bus\s+(\d+)\s+Device\s+(\d+)", line)
                dev_id = f"usb:{m2.group(1)}:{m2.group(2)}" if m2 else display
                _add(dev_id, display, "USB-hardware", False,
                     "Detected via lsusb; SANE driver may be needed")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("lsusb failed: %s", e)

    return scanners


def _inbox_entries() -> list:
    return [{
        "name": f"inbox:{ib['label']}",
        "display": f"{ib['label']} (folder inbox)",
        "source": "inbox",
        "ready": os.path.isdir(ib["path"]),
        "note": ("Press the job button on the scanner (NX Manager / ScanSnap Home "
                 "save-to-folder) — files are imported automatically."
                 if os.path.isdir(ib["path"]) else f"Folder not reachable: {ib['path']}"),
    } for ib in INBOXES]


def list_scanners() -> list:
    if sys.platform == "win32":
        with _com_apartment():
            found = get_scanners_wia()
    elif sys.platform == "darwin":
        found = get_scanners_macos()
    elif sys.platform.startswith("linux"):
        found = get_scanners_linux()
    else:
        found = []
    return _inbox_entries() + _annotate_fujitsu(found)


_scanner_cache = {"ts": 0.0, "data": None}


def list_scanners_cached(refresh: bool = False) -> list:
    now = time.time()
    if (not refresh and _scanner_cache["data"] is not None
            and now - _scanner_cache["ts"] < SCANNER_CACHE_TTL):
        return _inbox_entries() + [s for s in _scanner_cache["data"] if s.get("source") != "inbox"]
    data = list_scanners()
    _scanner_cache.update(ts=now, data=data)
    return data


# ═════════════════════════════════════════════════════════════════════════════
#  Image helpers
# ═════════════════════════════════════════════════════════════════════════════

_PNG_MODES = {"1", "L", "LA", "P", "RGB", "RGBA", "I", "I;16"}


def _img_to_png_bytes(img, optimize: bool = False) -> bytes:
    if img.mode not in _PNG_MODES:
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=optimize)
    return buf.getvalue()


def _png_from_path(path: str) -> bytes:
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        return _img_to_png_bytes(img)


def _png_from_bytes(raw: bytes) -> bytes:
    from PIL import Image
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return raw
    with Image.open(io.BytesIO(raw)) as img:
        img.load()
        return _img_to_png_bytes(img)


def _image_bytes(raw: bytes) -> bytes:
    """Keep PNG / JPEG untouched (browser-decodable), convert anything else to PNG."""
    if raw[:8] == b"\x89PNG\r\n\x1a\n" or raw[:3] == b"\xff\xd8\xff":
        return raw
    return _png_from_bytes(raw)


def _flatten_rgb(img):
    from PIL import Image
    if "A" in img.getbands() or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        return bg
    if img.mode == "1":
        return img.convert("L")
    if img.mode not in ("RGB", "L"):
        return img.convert("RGB")
    return img


def _to_jpeg_bytes(data: bytes, quality: int = 90) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        if img.format == "JPEG" and img.mode in ("RGB", "L"):
            return data
        img.load()
        img = _flatten_rgb(img)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()


# ═════════════════════════════════════════════════════════════════════════════
#  Background-aware document detection & blank-page detection
# ═════════════════════════════════════════════════════════════════════════════

BLANK_THRESHOLDS = {"low": 0.0008, "normal": 0.0025, "high": 0.006}


def _small_rgb(img, max_side=600):
    small = img.convert("RGB") if img.mode != "RGB" else img.copy()
    small.thumbnail((max_side, max_side))
    return small


def _estimate_background(small):
    """Estimate scanner background colour from the image border strips."""
    from PIL import ImageStat
    w, h = small.size
    b = max(2, int(min(w, h) * 0.03))
    strips = [small.crop((0, 0, w, b)), small.crop((0, h - b, w, h)),
              small.crop((0, 0, b, h)), small.crop((w - b, 0, w, h))]
    meds = [ImageStat.Stat(s).median for s in strips]
    lums = [0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2] for m in meds]

    def _avg(ms):
        return tuple(int(sum(m[c] for m in ms) / len(ms)) for c in range(3))

    dark = [m for m, l in zip(meds, lums) if l < 90]
    light = [m for m, l in zip(meds, lums) if l > 170]
    if len(dark) >= 2:
        return _avg(dark), "dark"
    if len(light) >= 2:
        return _avg(light), "light"
    order = sorted(range(4), key=lambda i: lums[i])
    return _avg([meds[order[1]], meds[order[2]]]), "mid"


def _bounds_on_small(small, bg_rgb, kind):
    """Bounding box (small-image coords) of what differs from the background."""
    from PIL import Image, ImageChops, ImageFilter
    threshold = {"dark": 50, "light": 30}.get(kind, 38)
    diff = ImageChops.difference(small, Image.new("RGB", small.size, bg_rgb))
    r, g, b = diff.split()
    d = ImageChops.lighter(ImageChops.lighter(r, g), b)
    mask = d.point(lambda p: 255 if p > threshold else 0)
    mask = mask.filter(ImageFilter.MedianFilter(5))
    if kind != "dark":
        mask = mask.filter(ImageFilter.MaxFilter(5))

    w, h = mask.size
    data = mask.tobytes()
    rows = [data[y * w:(y + 1) * w].count(255) for y in range(h)]
    transpose = getattr(getattr(Image, "Transpose", Image), "TRANSPOSE")
    tdata = mask.transpose(transpose).tobytes()
    cols = [tdata[x * h:(x + 1) * h].count(255) for x in range(w)]

    frac = 0.12 if kind == "dark" else 0.01

    def _span(counts, limit):
        idx = [i for i, c in enumerate(counts) if c >= limit]
        return (idx[0], idx[-1] + 1) if idx else None

    ys = _span(rows, max(1, frac * w))
    xs = _span(cols, max(1, frac * h))
    if not ys or not xs:
        return None
    x1, x2 = xs
    y1, y2 = ys
    if (x2 - x1) < w * 0.05 or (y2 - y1) < h * 0.05:
        return None
    return x1, y1, x2 - x1, y2 - y1


def _detect_document_bounds(img, background="auto") -> dict:
    small = _small_rgb(img)
    sx, sy = img.width / small.width, img.height / small.height
    bg_rgb, kind = _estimate_background(small)
    if background == "black" and kind != "dark":
        bg_rgb, kind = (15, 15, 15), "dark"
    elif background == "white" and kind != "light":
        bg_rgb, kind = (250, 250, 250), "light"

    box = _bounds_on_small(small, bg_rgb, kind)
    result = {"background": kind, "background_rgb": list(bg_rgb)}
    if not box:
        result.update(x=0, y=0, width=img.width, height=img.height, found=False)
        return result

    x, y, w, h = box
    pad = 0 if kind == "dark" else int(round(max(sx, sy) * 6))
    x1 = max(0, int(x * sx) - pad)
    y1 = max(0, int(y * sy) - pad)
    x2 = min(img.width, int(math.ceil((x + w) * sx)) + pad)
    y2 = min(img.height, int(math.ceil((y + h) * sy)) + pad)
    result.update(x=x1, y=y1, width=x2 - x1, height=y2 - y1, found=True)
    log.info("detect_bounds: bg=%s → %d,%d %dx%d", kind, x1, y1, x2 - x1, y2 - y1)
    return result


def _detect_bounds_bytes(data: bytes, background="auto") -> dict:
    from PIL import Image
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        return _detect_document_bounds(img, background)


def _crop_png(png_bytes: bytes, x: int, y: int, width: int, height: int) -> bytes:
    from PIL import Image
    with Image.open(io.BytesIO(png_bytes)) as img:
        img.load()
        x = max(0, min(x, img.width - 1))
        y = max(0, min(y, img.height - 1))
        width = max(1, min(width, img.width - x))
        height = max(1, min(height, img.height - y))
        return _img_to_png_bytes(img.crop((x, y, x + width, y + height)), optimize=True)


def _auto_crop_png(png_bytes: bytes, background="auto") -> tuple:
    from PIL import Image
    with Image.open(io.BytesIO(png_bytes)) as img:
        img.load()
        b = _detect_document_bounds(img, background)
        cropped = img.crop((b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]))
        return _img_to_png_bytes(cropped, optimize=True), b


def _is_blank_page(data: bytes, sensitivity: str = "normal") -> bool:
    """
    Conservative blank test that works on white AND black scanner backgrounds:
    ink is measured relative to the paper's own brightness inside the page.
    """
    from PIL import Image, ImageFilter
    with Image.open(io.BytesIO(data)) as img:
        try:
            img.draft("RGB", (1200, 1200))
        except Exception:
            pass
        small = _small_rgb(img)
    w, h = small.size
    bg_rgb, kind = _estimate_background(small)

    if kind == "dark":
        box = _bounds_on_small(small, bg_rgb, kind)
        if not box:
            return False                       # all dark → not a blank sheet
        x, y, bw, bh = box
        inset = int(min(bw, bh) * 0.05)
        if bw - 2 * inset < 20 or bh - 2 * inset < 20:
            return False
        region = small.crop((x + inset, y + inset, x + bw - inset, y + bh - inset))
    else:
        mx, my = int(w * 0.06), int(h * 0.06)
        if w - 2 * mx < 20 or h - 2 * my < 20:
            return False
        region = small.crop((mx, my, w - mx, h - my))

    g = region.convert("L").filter(ImageFilter.MedianFilter(3))
    hist = g.histogram()
    total = sum(hist)
    if total < 500:
        return False
    acc, paper = 0, 255
    for v in range(255, -1, -1):
        acc += hist[v]
        if acc >= total * 0.10:
            paper = v
            break
    if paper < 110:
        return False                           # dark / coloured sheet → keep
    ink = sum(hist[:max(0, paper - 70)])
    return ink / total < BLANK_THRESHOLDS.get(sensitivity, BLANK_THRESHOLDS["normal"])


# ═════════════════════════════════════════════════════════════════════════════
#  Digital imprint (software endorser)
# ═════════════════════════════════════════════════════════════════════════════

_FONT_CACHE = {}


def _imprint_font(size):
    from PIL import ImageFont
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for name in (IMPRINT_FONT, "DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "arialbd.ttf",
                 "arial.ttf", "Arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc"):
        if not name:
            continue
        try:
            font = ImageFont.truetype(name, size)
            break
        except Exception:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()
    _FONT_CACHE[size] = font
    return font


def _digital_imprint(data: bytes, text: str, position: str = "top") -> bytes:
    from PIL import Image, ImageDraw
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        img = _flatten_rgb(img).convert("RGB")
    w, h = img.size
    size = max(12, int(h * 0.012))
    font = _imprint_font(size)
    draw = ImageDraw.Draw(img)
    try:
        l, t, r, b = draw.textbbox((0, 0), text, font=font)
    except AttributeError:
        tw, th = draw.textsize(text, font=font)
        l, t, r, b = 0, 0, tw, th
    tw, th = r - l, b - t
    pad = max(4, size // 3)
    x = max(pad, w - tw - pad * 3)
    y = pad * 2 if position == "top" else max(pad, h - th - pad * 3)
    draw.rectangle((x - pad, y - pad, x + tw + pad, y + th + pad),
                   fill=(255, 255, 255), outline=(200, 0, 0))
    draw.text((x - l, y - t), text, fill=(200, 0, 0), font=font)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    return buf.getvalue()


# ═════════════════════════════════════════════════════════════════════════════
#  PDF builder
# ═════════════════════════════════════════════════════════════════════════════

def _build_pdf_from_jpegs(pages: list) -> bytes:
    buf = io.BytesIO()
    xref_offsets = []
    obj_n = 0

    def _w(data):
        buf.write(data.encode() if isinstance(data, str) else data)

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
        _w(f"<< /Type /XObject /Subtype /Image /Width {w_px} /Height {h_px} "
           f"/ColorSpace {colorspace} /BitsPerComponent 8 /Filter /DCTDecode "
           f"/Length {len(jpeg)} >>\nstream\n")
        _w(jpeg)
        _w(b"\nendstream\nendobj\n")
        cs = f"q {w_pt} 0 0 {h_pt} 0 0 cm /Im0 Do Q\n".encode()
        cs_obj = _begin_obj()
        _w(f"<< /Length {len(cs)} >>\nstream\n")
        _w(cs)
        _w(b"\nendstream\nendobj\n")
        page_obj = _begin_obj()
        _w(f"<< /Type /Page /Parent {pages_ob_n} 0 R /MediaBox [0 0 {w_pt} {h_pt}] "
           f"/Contents {cs_obj} 0 R /Resources << /XObject << /Im0 {img_obj} 0 R >> >> >>\nendobj\n")
        page_ids.append(page_obj)

    kids = " ".join(f"{p} 0 R" for p in page_ids)
    pages_ob = _begin_obj()
    _w(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\nendobj\n")
    cat_obj = _begin_obj()
    _w(f"<< /Type /Catalog /Pages {pages_ob} 0 R >>\nendobj\n")
    xref_pos = buf.tell()
    _w(f"xref\n0 {obj_n + 1}\n0000000000 65535 f \n")
    for off in xref_offsets:
        _w(f"{off:010d} 00000 n \n")
    _w(f"trailer\n<< /Size {obj_n + 1} /Root {cat_obj} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n")
    return buf.getvalue()


def _safe_dpi(dpi_info, default: int = 200) -> int:
    try:
        v = float(dpi_info[0]) if dpi_info else 0.0
    except Exception:
        v = 0.0
    return int(round(v)) if 50 <= v <= 2400 else default


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
        dpi = _safe_dpi(img.info.get("dpi"))
        if img.format == "JPEG" and img.mode in ("RGB", "L"):
            jpeg_bytes = raw
        else:
            img.load()
            img = _flatten_rgb(img)
            jb = io.BytesIO()
            img.save(jb, format="JPEG", quality=85, optimize=True)
            jpeg_bytes = jb.getvalue()
        colorspace = "/DeviceGray" if img.mode == "L" else "/DeviceRGB"
        page_tuples.append((jpeg_bytes, img.width, img.height, dpi, colorspace))
    return _build_pdf_from_jpegs(page_tuples)


# ═════════════════════════════════════════════════════════════════════════════
#  scanimage (SANE CLI) — Linux / macOS
# ═════════════════════════════════════════════════════════════════════════════

def _list_scanimage_devices(timeout: int = 30) -> list:
    result = subprocess.run(["scanimage", "-L"], capture_output=True, text=True,
                            timeout=timeout, **_NO_WINDOW)
    return _parse_scanimage_list(result.stdout + "\n" + result.stderr)


def _get_scanimage_device(selector=None, display=None) -> str:
    devices = _list_scanimage_devices()
    if not devices:
        raise ScannerError("offline", "scanimage: no devices found (check: scanimage -L)")
    chosen = _best_match(devices, selector, display,
                         labels=lambda d: [d[0], d[1], _humanise_sane_device(d[0])])
    return chosen[0]


def _scanimage_help(device: str) -> str:
    try:
        r = subprocess.run(["scanimage", f"--device={device}", "--help"],
                           capture_output=True, text=True, timeout=30, **_NO_WINDOW)
        return (r.stdout or "") + (r.stderr or "")
    except FileNotFoundError:
        raise
    except Exception as ex:
        log.warning("scanimage --help failed: %s", ex)
        return ""


def _scanimage_line(help_text: str, flag: str, allow_inactive=False):
    m = re.search(rf"(?:^|\s){re.escape(flag)}(?:\s|=|\[)([^\n]*)", help_text, re.M)
    if not m:
        return None
    if not allow_inactive and "[inactive]" in m.group(1):
        return None
    return m.group(1)


def _scanimage_choices(help_text: str, opt: str) -> list:
    line = _scanimage_line(help_text, f"--{opt}")
    if not line:
        return []
    return [o.strip() for o in line.split("[")[0].split("|") if o.strip()]


def _scanimage_range_max(help_text: str, flag: str):
    line = _scanimage_line(help_text, flag)
    m = re.match(r"\s*(-?[\d.]+)\.\.([\d.]+)", line or "")
    return float(m.group(2)) if m else None


def _scanimage_resolution(help_text: str, wanted: int = 200) -> int:
    line = _scanimage_line(help_text, "--resolution")
    if not line:
        return wanted
    spec = line.split("[")[0]
    rng = re.match(r"\s*([\d.]+)\.\.([\d.]+)", spec)
    if rng:
        return int(min(max(wanted, float(rng.group(1))), float(rng.group(2))))
    nums = [int(float(x)) for x in re.findall(r"\d+(?:\.\d+)?", spec)]
    return min(nums, key=lambda v: (abs(v - wanted), -v)) if nums else wanted


def _pick_adf_source(device: str, duplex: bool = False, help_text=None) -> str:
    h = help_text if help_text is not None else _scanimage_help(device)
    if not h:
        return "ADF"
    options = _scanimage_choices(h, "source")
    if not options:
        return ""                          # feeder-only device
    if duplex:
        for opt in options:
            if re.search(r"duplex", opt, re.I):
                return opt
    for pat in (r"^(adf|feeder|automatic document feeder|adf front|adf simplex)$",
                r"^(?!.*(duplex|back)).*(adf|feeder)", r"adf|feeder"):
        for opt in options:
            if re.search(pat, opt, re.I):
                return opt
    raise ScannerError("no_feeder", f"This scanner has no document feeder "
                                    f"(sources: {', '.join(options)}).")


def _scanimage_args(device: str, adf=False, duplex=False, job=None) -> list:
    h = _scanimage_help(device)
    args = []
    flatbed_selected = False
    if adf:
        src = _pick_adf_source(device, duplex, h)
        if src:
            args.append(f"--source={src}")
    else:
        flat = next((s for s in _scanimage_choices(h, "source")
                     if re.search(r"flatbed|normal", s, re.I)), None)
        if flat:
            args.append(f"--source={flat}")
            flatbed_selected = True

    modes = _scanimage_choices(h, "mode")
    mode = next((m for m in modes if re.fullmatch(r"(24bit )?colou?r(24)?", m, re.I)), None)
    args.append(f"--mode={mode or 'Color'}")
    args.append(f"--resolution={_scanimage_resolution(h, 200)}")

    if not flatbed_selected:
        ph = _scanimage_range_max(h, "--page-height")
        if ph:
            target = round(min(ph, LONG_PAGE_MM), 1)
            args += [f"--page-height={target}", "-y", f"{target}"]
        pw = _scanimage_range_max(h, "--page-width")
        if pw:
            args += [f"--page-width={pw}", "-x", f"{pw}"]
    for flag in ("swcrop", "swdeskew"):
        if _scanimage_line(h, f"--{flag}") is not None:
            args.append(f"--{flag}=yes")

    if job is not None and adf:
        # Double-feed detection (fujitsu backend)
        if job.double_feed:
            if "Stop" in _scanimage_choices(h, "df-action"):
                args.append("--df-action=Stop")
            if _scanimage_line(h, "--df-thickness") is not None:
                args.append("--df-thickness=yes")
        # Hardware endorser / imprinter (fujitsu backend, models with printer)
        im = job.imprint
        if im.wants_hardware and _scanimage_line(h, "--endorser", allow_inactive=True) is not None:
            pre, post, has_counter = im.split()
            s = pre + (f"%0{im.digits}ud" if has_counter else "") + post
            args += ["--endorser=yes", f"--endorser-string={s}"]
            if _scanimage_line(h, "--endorser-val", allow_inactive=True) is not None:
                args.append(f"--endorser-val={im.start}")
            if _scanimage_line(h, "--endorser-step", allow_inactive=True) is not None:
                args.append(f"--endorser-step={im.step}")
            job.imprint_hw_applied = True
    return args


def _scanimage_empty(stderr: str) -> bool:
    return _classify_text(stderr) == "feeder_empty"


def _last_line(text: str) -> str:
    lines = [l for l in (text or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines else ""


def _scan_via_scanimage(selector=None, display=None) -> bytes:
    device = _get_scanimage_device(selector, display)
    args = _scanimage_args(device, adf=False)
    with tempfile.TemporaryDirectory() as tmpdir:
        out = os.path.join(tmpdir, "scan.png")
        base = ["scanimage", f"--device={device}", "--format=png", f"--output-file={out}"]
        result = None
        for attempt in (args, ["--mode=Color"]):
            result = subprocess.run(base + attempt, capture_output=True, text=True,
                                    timeout=180, **_NO_WINDOW)
            if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out):
                break
            code = _classify_text(result.stderr)
            if code:
                raise ScannerError(code, _hint_message(code, _last_line(result.stderr)))
        if not os.path.exists(out) or not os.path.getsize(out):
            raise RuntimeError(f"scanimage scan failed: {result.stderr.strip()}")
        with open(out, "rb") as f:
            return f.read()


def _scanimage_stream(job, cmd) -> tuple:
    """Run a scanimage batch; emit every page as soon as it is written."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **_NO_WINDOW)
    job.proc = proc
    err_chunks = []
    t = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read() or ""), daemon=True)
    t.start()
    got = 0
    try:
        for line in proc.stdout:
            path = line.strip()
            if not path or not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            try:
                os.unlink(path)
            except OSError:
                pass
            job.emit(data)
            got += 1
            if job.full or job.cancelled:
                proc.terminate()
                break
        proc.wait()
    finally:
        job.proc = None
        if proc.poll() is None:
            proc.kill()
    t.join(timeout=5)
    return got, "".join(err_chunks)


def _scan_scanimage_adf(job, selector=None, display=None):
    device = _get_scanimage_device(selector, display)
    args = _scanimage_args(device, adf=True, duplex=job.duplex, job=job)
    source_args = [a for a in args if a.startswith("--source=")]
    log.info("scanimage ADF: device=%s args=%s", device, args)

    with tempfile.TemporaryDirectory() as tmpdir:
        pattern = os.path.join(tmpdir, "page%04d.png")
        base = ["scanimage", f"--device={device}", "--format=png",
                f"--batch={pattern}", "--batch-start=1", "--batch-print"]
        stderr = ""
        for attempt in (args, source_args + ["--mode=Color"], ["--mode=Color"]):
            if attempt is not args:
                job.imprint_hw_applied = False
            got, stderr = _scanimage_stream(job, base + attempt)
            job.check()
            if got or _scanimage_empty(stderr):
                break
            if not re.search(r"invalid|not supported|bad option|unrecognized|inactive", stderr, re.I):
                break
            log.warning("scanimage ADF rejected %s — retrying simpler", attempt)

    code = _classify_text(stderr) if stderr else None
    if code == "paper_jam" and job.double_feed:
        detail = _last_line(stderr) + " (may also be a double feed)"
    else:
        detail = _last_line(stderr)
    if job.raw_pages:
        if code in HARD_STOP_CODES:
            raise ScannerError(code, _hint_message(code, detail))
        return
    if code:
        raise ScannerError(code, _hint_message(code, detail))
    raise RuntimeError(f"scanimage ADF returned no pages.\nstderr: {stderr.strip()}")


# ═════════════════════════════════════════════════════════════════════════════
#  eSCL (HP Smart relay / network scanners)
# ═════════════════════════════════════════════════════════════════════════════

def _escl_ssl_ctx():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _escl_scan_settings(source: str, duplex: bool = False) -> bytes:
    scan_ns = "http://schemas.hp.com/imaging/escl/2011/05/03"
    pwg_ns = "http://www.pwg.org/schemas/2010/12/sm"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<scan:ScanSettings xmlns:scan="{scan_ns}" xmlns:pwg="{pwg_ns}">'
        "<pwg:Version>2.6</pwg:Version>"
        "<pwg:ScanRegions><pwg:ScanRegion>"
        "<pwg:XOffset>0</pwg:XOffset><pwg:YOffset>0</pwg:YOffset>"
        "<pwg:Width>2480</pwg:Width><pwg:Height>3508</pwg:Height>"
        "<pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>"
        "</pwg:ScanRegion></pwg:ScanRegions>"
        f"<pwg:InputSource>{source}</pwg:InputSource>"
        + ("<scan:Duplex>true</scan:Duplex>" if duplex else "") +
        "<scan:ColorMode>RGB24</scan:ColorMode>"
        "<scan:XResolution>200</scan:XResolution>"
        "<scan:YResolution>200</scan:YResolution>"
        "<pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>"
        "</scan:ScanSettings>"
    ).encode("utf-8")


_ESCL_ADF_CODES = {
    "scanneradfjam": "paper_jam",
    "scanneradfmispick": "paper_jam",
    "scanneradfinputtrayfailed": "paper_jam",
    "scanneradfmultipickdetected": "double_feed",
    "scanneradfdooropen": "cover_open",
    "scanneradfhatchopen": "cover_open",
    "scanneradfempty": "feeder_empty",
    "scanneradfinputtrayoverloaded": "attention",
}


def _escl_status(base_url, ctx) -> str:
    try:
        with urllib.request.urlopen(f"{base_url}/ScannerStatus", timeout=5, context=ctx) as r:
            return r.read().decode(errors="replace")
    except Exception:
        return ""


def _escl_adf_code(base_url, ctx):
    m = re.search(r"<(?:\w+:)?AdfState\b[^>]*>\s*(\w+)\s*<", _escl_status(base_url, ctx), re.I)
    return _ESCL_ADF_CODES.get(m.group(1).lower()) if m else None


def _escl_parse_scanner_status(xml: str) -> tuple:
    state = "Unknown"
    for pattern in (r"<(?:\w+:)?State\b[^>]*>\s*(\w+)\s*<",
                    r"<(?:\w+:)?ScannerState\b[^>]*>\s*(\w+)\s*<"):
        m = re.search(pattern, xml, re.I)
        if m:
            state = m.group(1).strip()
            break
    job_uris = re.findall(r"<(?:\w+:)?JobUri\b[^>]*>\s*([^\s<]+)\s*<", xml, re.I)
    return state, job_uris


def _escl_delete(url, ctx):
    try:
        req = urllib.request.Request(url, method="DELETE")
        with urllib.request.urlopen(req, timeout=5, context=ctx):
            pass
        return True
    except urllib.error.HTTPError as e:
        return e.code == 404
    except Exception:
        return False


def _escl_cancel_active_jobs(base_url: str, ctx) -> int:
    _state, job_uris = _escl_parse_scanner_status(_escl_status(base_url, ctx))
    return sum(1 for uri in job_uris if _escl_delete(urljoin(base_url, uri), ctx))


def _escl_wait_for_idle(base_url: str, ctx, timeout: int = 20) -> str:
    deadline = time.time() + timeout
    cancel_tried = False
    state = "Unknown"
    while time.time() < deadline:
        body = _escl_status(base_url, ctx)
        if not body:
            return "Unknown"
        state, job_uris = _escl_parse_scanner_status(body)
        if state.lower() == "idle":
            return "Idle"
        if not cancel_tried and (job_uris or state.lower() != "unknown"):
            cancel_tried = True
            if _escl_cancel_active_jobs(base_url, ctx):
                time.sleep(2)
                continue
        time.sleep(2)
    return state


def _escl_post_job(base_url, ctx, body, feeder=False) -> str:
    last_err = None
    cancel_attempted = False
    for attempt in range(1, 7):
        req = urllib.request.Request(f"{base_url}/ScanJobs", data=body,
                                     headers={"Content-Type": "text/xml; charset=utf-8"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
                location = resp.headers.get("Location", "").strip()
            if not location:
                raise RuntimeError("eSCL: job accepted but no Location header returned.")
            return urljoin(base_url, location).rstrip("/")
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}: {e.read().decode(errors='replace').strip()}"
            if e.code not in (409, 503):
                raise RuntimeError(f"eSCL ScanJobs POST failed: {last_err}")
            code = _escl_adf_code(base_url, ctx) if feeder else None
            if code in ("feeder_empty", "paper_jam", "double_feed", "cover_open"):
                raise ScannerError(code, _hint_message(code, last_err))
            if not cancel_attempted:
                cancel_attempted = True
                if _escl_cancel_active_jobs(base_url, ctx):
                    time.sleep(1)
                    continue
            if feeder and attempt >= 3 and e.code == 409:
                raise ScannerError("feeder_empty", _hint_message("feeder_empty", last_err))
            time.sleep(2 if attempt <= 3 else 3)
    raise RuntimeError(f"eSCL: scanner did not accept the job. Last error: {last_err}")


def _scan_escl_http_adf(base_url: str, job):
    ctx = _escl_ssl_ctx()
    _escl_wait_for_idle(base_url, ctx, timeout=20)
    job_url = _escl_post_job(base_url, ctx, _escl_scan_settings("Feeder", job.duplex), feeder=True)
    doc_url = f"{job_url}/NextDocument"
    try:
        while not job.full:
            job.check()
            raw = None
            ended = False
            for attempt in range(1, 31):
                job.check()
                try:
                    with urllib.request.urlopen(doc_url, timeout=90, context=ctx) as resp:
                        raw = resp.read()
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        ended = True
                        break
                    if e.code in (503, 409):
                        time.sleep(1.0 if attempt <= 10 else 2.0)
                        continue
                    code = _escl_adf_code(base_url, ctx)
                    if code:
                        raise ScannerError(code, _hint_message(code, f"HTTP {e.code}"))
                    raise RuntimeError(f"eSCL NextDocument failed: HTTP {e.code} {e.reason}")
            if ended or raw is None:
                break
            job.emit(_image_bytes(raw))
    except JobCancelled:
        _escl_delete(job_url, ctx)
        raise

    code = _escl_adf_code(base_url, ctx)
    if code in HARD_STOP_CODES:
        raise ScannerError(code, _hint_message(code))
    if not job.raw_pages:
        raise ScannerError(code or "feeder_empty", _hint_message(code or "feeder_empty"))


def _scan_escl_http(base_url: str) -> bytes:
    ctx = _escl_ssl_ctx()
    job_url = _escl_post_job(base_url, ctx, _escl_scan_settings("Platen"))
    image_url = f"{job_url}/NextDocument"
    last_err = None
    for attempt in range(1, 13):
        try:
            with urllib.request.urlopen(image_url, timeout=60, context=ctx) as resp:
                return _image_bytes(resp.read())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (404, 503):
                time.sleep(2 if attempt < 6 else 4)
                continue
            raise RuntimeError(f"eSCL NextDocument failed: HTTP {e.code} {e.reason}")
    raise RuntimeError(f"eSCL NextDocument not ready after 12 attempts: {last_err}")


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


def _verify_escl_url(base_url: str, timeout: float = 10.0) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/ScannerCapabilities",
                                    timeout=timeout, context=_escl_ssl_ctx()) as resp:
            return bool(re.search(r"(?i)scanner", resp.read(1024).decode(errors="replace")))
    except Exception:
        return False


def _probe_localhost_escl_ports(port_range=range(59000, 59201), per_port_timeout=3.0) -> str:
    for port in port_range:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/eSCL/ScannerCapabilities",
                                        timeout=per_port_timeout) as resp:
                if re.search(r"(?i)scanner", resp.read(512).decode(errors="replace")):
                    return f"http://localhost:{port}/eSCL"
        except Exception:
            pass
    return ""


def _find_escl_url_bonjour(browse_timeout: int = 10) -> str:
    for service_type in ("_uscan._tcp", "_uscans._tcp", "_scanner._tcp"):
        try:
            browse = subprocess.Popen(["dns-sd", "-B", service_type, "local"],
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, **_NO_WINDOW)
        except FileNotFoundError:
            raise RuntimeError("dns-sd not available — cannot browse Bonjour")
        time.sleep(browse_timeout)
        browse.terminate()
        raw = browse.stdout.read()
        service_name = None
        for line in raw.splitlines():
            if " Add " in f" {line} ":
                parts = line.split()
                if len(parts) >= 7:
                    service_name = " ".join(parts[6:]).replace("\\032", " ").strip()
                    break
        if not service_name:
            continue
        lookup = subprocess.Popen(["dns-sd", "-L", service_name, service_type, "local"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  text=True, **_NO_WINDOW)
        time.sleep(3)
        lookup.terminate()
        host, port, path = None, 8080, "/eSCL"
        for line in lookup.stdout.read().splitlines():
            m = re.search(r"can be reached at\s+([A-Za-z0-9._-]+):(\d+)", line, re.I)
            if m:
                host, port = m.group(1).rstrip("."), int(m.group(2))
            m2 = re.search(r"(?:^|(?<=\s))rs=(/?\S*)", line)
            if m2:
                path = m2.group(1) if m2.group(1).startswith("/") else "/" + m2.group(1)
        if host:
            scheme = "https" if "uscans" in service_type else "http"
            return f"{scheme}://{host}:{port}{path}"
    raise RuntimeError("No eSCL scanner found via Bonjour.")


def _find_escl_url(browse_timeout: int = 10) -> str:
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


# ═════════════════════════════════════════════════════════════════════════════
#  Windows: WIA
# ═════════════════════════════════════════════════════════════════════════════

WIA_FMT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"
WIA_FMT_JPEG = "{B96B3CAE-0728-11D3-9D7B-0000F81EF32E}"
WIA_FMT_PNG = "{B96B3CAF-0728-11D3-9D7B-0000F81EF32E}"

WIA_DEVICE_TYPE_SCANNER = 1
WIA_INTENT_COLOR = 1
WIA_BIAS_MAX_QUALITY = 131072

# Property IDs MUST be strings (an int is a 1-based collection index!)
WIA_DPS_DOCUMENT_HANDLING_STATUS = "3087"
WIA_DPS_DOCUMENT_HANDLING_SELECT = "3088"
WIA_DPS_ENDORSER_STRING = "3093"
WIA_DPS_PAGES = "3096"
WIA_DPS_PAGE_SIZE = "3097"
WIA_IPS_CUR_INTENT = "6146"
WIA_IPS_XRES = "6147"
WIA_IPS_YRES = "6148"
WIA_FEEDER, WIA_FLATBED, WIA_DUPLEX = 1, 2, 4
WIA_PAGE_AUTO = 100

_WIA_HR_CODES = {
    0x80210002: "paper_jam",      # WIA_ERROR_PAPER_JAM
    0x80210003: "feeder_empty",   # WIA_ERROR_PAPER_EMPTY
    0x80210004: "paper_jam",      # WIA_ERROR_PAPER_PROBLEM
    0x80210005: "offline",        # WIA_ERROR_OFFLINE
    0x80210006: "busy",           # WIA_ERROR_BUSY
    0x80210007: "busy",           # WIA_ERROR_WARMING_UP
    0x80210008: "attention",      # WIA_ERROR_USER_INTERVENTION
    0x8021000D: "busy",           # WIA_ERROR_DEVICE_LOCKED
    0x80210016: "cover_open",     # WIA_ERROR_COVER_OPEN
    0x80210020: "double_feed",    # WIA_ERROR_MULTI_FEED
}
_WIA_FORMAT_ERRORS = {0x80070057, 0x80004001, 0x80020005}


def _com_hresult(e) -> int:
    hr = e.args[0] if getattr(e, "args", None) else 0
    try:
        excepinfo = e.args[2]
        if excepinfo and excepinfo[5]:
            hr = excepinfo[5]
    except Exception:
        pass
    try:
        return int(hr) & 0xFFFFFFFF
    except Exception:
        return 0


def _wia_set(owner, prop_id: str, value) -> bool:
    try:
        owner.Properties(prop_id).Value = value
        return True
    except Exception as ex:
        log.debug("WIA: set %s=%s failed: %s", prop_id, value, ex)
        return False


def _wia_error(e, device=None):
    hr = _com_hresult(e)
    code = _WIA_HR_CODES.get(hr)
    if code is None and device is not None:
        try:
            st = int(device.Properties(WIA_DPS_DOCUMENT_HANDLING_STATUS).Value)
            if st & 0x20:
                code = "paper_jam"
            elif st & 0x10:
                code = "cover_open"
        except Exception:
            pass
    code = code or _classify_text(str(e))
    return ScannerError(code, _hint_message(code, f"0x{hr:08X}")) if code else None


def _wia_connect(selector=None, display=None):
    import win32com.client
    dm = win32com.client.Dispatch("WIA.DeviceManager")
    infos = []
    for i in range(1, dm.DeviceInfos.Count + 1):
        try:
            info = dm.DeviceInfos.Item(i)
            if int(info.Type) != WIA_DEVICE_TYPE_SCANNER:
                continue
            try:
                name = str(info.Properties("Name").Value or "")
            except Exception:
                name = ""
            infos.append((info, str(info.DeviceID), name))
        except Exception:
            pass
    if not infos:
        raise ScannerError("offline", "WIA: no scanner device found. Is the WIA driver installed?")
    info, dev_id, name = _best_match(infos, selector, display, labels=lambda t: [t[1], t[2]])
    device = info.Connect()
    log.info("WIA: connected to %s (%s)", name, dev_id)
    return device


def _wia_image_bytes(image) -> bytes:
    try:
        data = bytes(image.FileData.BinaryData)
        if data:
            return data
    except Exception:
        pass
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "scan.img")
        image.SaveFile(path)
        with open(path, "rb") as f:
            return f.read()


def _wia_transfer(item) -> bytes:
    import pywintypes
    last_err = None
    for fmt in (WIA_FMT_BMP, WIA_FMT_PNG, WIA_FMT_JPEG):
        try:
            image = item.Transfer(fmt)
        except pywintypes.com_error as e:
            if _com_hresult(e) in _WIA_FORMAT_ERRORS:
                last_err = e
                continue
            raise
        return _image_bytes(_wia_image_bytes(image))
    raise last_err or RuntimeError("WIA: no supported transfer format")


def _wia_item(device):
    if device.Items.Count == 0:
        raise RuntimeError("WIA: scanner returned no scan items")
    item = device.Items.Item(1)
    _wia_set(item, WIA_IPS_CUR_INTENT, WIA_INTENT_COLOR)
    _wia_set(item, WIA_IPS_XRES, 200)
    _wia_set(item, WIA_IPS_YRES, 200)
    return item


def _scan_wia(show_ui: bool, selector=None, display=None) -> bytes:
    import pywintypes
    import win32com.client
    if show_ui:
        dlg = win32com.client.Dispatch("WIA.CommonDialog")
        img = dlg.ShowAcquireImage(WIA_DEVICE_TYPE_SCANNER, WIA_INTENT_COLOR,
                                   WIA_BIAS_MAX_QUALITY, WIA_FMT_BMP, False, True, False)
        if img is None:
            raise ScannerError("cancelled", "Scan cancelled or no image returned.")
        return _image_bytes(_wia_image_bytes(img))

    device = _wia_connect(selector, display)
    if not _wia_set(device, WIA_DPS_DOCUMENT_HANDLING_SELECT, WIA_FLATBED):
        if _wia_set(device, WIA_DPS_DOCUMENT_HANDLING_SELECT, WIA_FEEDER):
            _wia_set(device, WIA_DPS_PAGES, 1)
            _wia_set(device, WIA_DPS_PAGE_SIZE, WIA_PAGE_AUTO)
    item = _wia_item(device)
    try:
        return _wia_transfer(item)
    except pywintypes.com_error as e:
        se = _wia_error(e, device)
        if se:
            raise se
        raise


def _scan_wia_adf(job, selector=None, display=None):
    import pywintypes
    device = _wia_connect(selector, display)
    mode = WIA_FEEDER | WIA_DUPLEX if job.duplex else WIA_FEEDER
    if not _wia_set(device, WIA_DPS_DOCUMENT_HANDLING_SELECT, mode):
        if job.duplex and _wia_set(device, WIA_DPS_DOCUMENT_HANDLING_SELECT, WIA_FEEDER):
            job.warnings.append("Duplex refused by the WIA driver — scanned one side only.")
        else:
            log.warning("WIA ADF: FEEDER mode refused — device may not have an ADF")
    _wia_set(device, WIA_DPS_PAGES, 0)
    _wia_set(device, WIA_DPS_PAGE_SIZE, WIA_PAGE_AUTO)

    im = job.imprint
    if im.wants_hardware:
        pre, post, has_counter = im.split()
        s = pre + ("$PAGE_COUNT$" if has_counter else "") + post
        if _wia_set(device, WIA_DPS_ENDORSER_STRING, s):
            job.imprint_hw_applied = True
            if has_counter and im.start != 1:
                job.warnings.append("WIA endorser: the counter start cannot be set; "
                                    "the driver's own page counter is printed.")

    item = _wia_item(device)
    while not job.full:
        job.check()
        try:
            job.emit(_wia_transfer(item))
        except pywintypes.com_error as e:
            se = _wia_error(e, device)
            if se and se.code == "feeder_empty":
                if job.raw_pages:
                    break
                raise se
            if se:
                raise se
            if job.raw_pages:
                log.warning("WIA ADF: page error 0x%08X — end of feeder", _com_hresult(e))
                break
            raise RuntimeError(f"WIA ADF: scan failed on page 1 (0x{_com_hresult(e):08X}): {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  Windows: TWAIN (pytwain 1.x / 2.x, PaperStream IP)
# ═════════════════════════════════════════════════════════════════════════════

_TW_FALLBACK = {
    "CAP_XFERCOUNT": 0x0001, "ICAP_PIXELTYPE": 0x0101,
    "CAP_FEEDERENABLED": 0x1002, "CAP_AUTOFEED": 0x1007, "CAP_DUPLEXENABLED": 0x1013,
    "CAP_PRINTER": 0x1026, "CAP_PRINTERENABLED": 0x1027, "CAP_PRINTERINDEX": 0x1028,
    "CAP_PRINTERMODE": 0x1029, "CAP_PRINTERSTRING": 0x102A, "CAP_PRINTERSUFFIX": 0x102B,
    "ICAP_XRESOLUTION": 0x1118, "ICAP_YRESOLUTION": 0x1119,
    "ICAP_SUPPORTEDSIZES": 0x1122, "ICAP_UNDEFINEDIMAGESIZE": 0x112D,
    "ICAP_AUTODISCARDBLANKPAGES": 0x1134, "CAP_DOUBLEFEEDDETECTION": 0x1146,
    "CAP_DOUBLEFEEDDETECTIONRESPONSE": 0x1149, "ICAP_AUTOMATICBORDERDETECTION": 0x1150,
    "ICAP_AUTOSIZE": 0x1156,
    "TWTY_INT16": 1, "TWTY_INT32": 2, "TWTY_UINT16": 4, "TWTY_UINT32": 5,
    "TWTY_BOOL": 6, "TWTY_FIX32": 7, "TWTY_STR255": 12,
}
TWPT_RGB, TWSS_NONE, TWAS_AUTO, TWBP_AUTO = 2, 0, 1, -1
TWDF_ULTRASONIC, TWDP_STOP = 0, 0
TWPM_SINGLESTRING, TWPM_COMPOUNDSTRING = 0, 2
_TWCC_CODES = {20: "paper_jam", 21: "double_feed", 23: "offline", 24: "cover_open", 29: "feeder_empty"}


def _tw(obj, *names):
    for n in names:
        attr = getattr(obj, n, None)
        if attr is not None:
            return attr
    raise AttributeError(f"{type(obj).__name__} has none of {names}")


def _twain_close(*objs):
    for o in objs:
        if o is None:
            continue
        for n in ("destroy", "close"):
            fn = getattr(o, n, None)
            if callable(fn):
                try:
                    fn()
                except Exception:
                    pass
                break


def _twain_open(selector=None, display=None):
    import twain
    sm = twain.SourceManager(0)
    try:
        fn = getattr(sm, "GetSourceList", None)
        sources = list(fn() if callable(fn) else getattr(sm, "source_list", []) or [])
        if not sources:
            raise RuntimeError("No TWAIN scanner found. Make sure the driver is installed.")
        name = _best_match(sources, selector, display, strict=bool(selector or display))
        if name is None:
            raise LookupError(f"No TWAIN source matches {display or selector!r}")
        src = _tw(sm, "OpenSource", "open_source")(name)
        if src is None:
            raise RuntimeError(f"TWAIN: could not open source {name!r}")
        log.info("TWAIN: using source %s", name)
        return twain, sm, src
    except Exception:
        _twain_close(sm)
        raise


def _twain_set_cap(twain, src, cap_name, typ_name, value) -> bool:
    cap = getattr(twain, cap_name, None)
    typ = getattr(twain, typ_name, None)
    cap = _TW_FALLBACK.get(cap_name) if cap is None else cap
    typ = _TW_FALLBACK.get(typ_name) if typ is None else typ
    if cap is None or typ is None:
        return False
    try:
        _tw(src, "SetCapability", "set_capability")(cap, typ, value)
        return True
    except Exception as e:
        log.debug("TWAIN cap %s=%s: %s", cap_name, value, e)
        return False


def _twain_imprint(twain, src, im) -> bool:
    pre, post, has_counter = im.split()
    # imprinter after/before, then endorser — whichever the driver accepts
    for printer in (1, 3, 0, 2, 5, 4):
        if _twain_set_cap(twain, src, "CAP_PRINTER", "TWTY_UINT16", printer):
            break
    if not _twain_set_cap(twain, src, "CAP_PRINTERENABLED", "TWTY_BOOL", True):
        return False
    _twain_set_cap(twain, src, "CAP_PRINTERMODE", "TWTY_UINT16",
                   TWPM_COMPOUNDSTRING if has_counter else TWPM_SINGLESTRING)
    _twain_set_cap(twain, src, "CAP_PRINTERSTRING", "TWTY_STR255", pre[:255])
    if has_counter:
        _twain_set_cap(twain, src, "CAP_PRINTERINDEX", "TWTY_UINT32", im.start)
        _twain_set_cap(twain, src, "CAP_PRINTERSUFFIX", "TWTY_STR255", post[:255])
    return True


def _twain_configure(twain, src, adf: bool, job=None) -> bool:
    duplex = bool(job and job.duplex)
    _twain_set_cap(twain, src, "ICAP_PIXELTYPE", "TWTY_UINT16", TWPT_RGB)
    _twain_set_cap(twain, src, "ICAP_XRESOLUTION", "TWTY_FIX32", 200.0)
    _twain_set_cap(twain, src, "ICAP_YRESOLUTION", "TWTY_FIX32", 200.0)
    if adf:
        _twain_set_cap(twain, src, "CAP_FEEDERENABLED", "TWTY_BOOL", True)
        _twain_set_cap(twain, src, "CAP_AUTOFEED", "TWTY_BOOL", True)
        _twain_set_cap(twain, src, "CAP_DUPLEXENABLED", "TWTY_BOOL", duplex)
        _twain_set_cap(twain, src, "CAP_XFERCOUNT", "TWTY_INT16", -1)
        if job is not None and job.double_feed:
            _twain_set_cap(twain, src, "CAP_DOUBLEFEEDDETECTION", "TWTY_UINT16", TWDF_ULTRASONIC)
            _twain_set_cap(twain, src, "CAP_DOUBLEFEEDDETECTIONRESPONSE", "TWTY_UINT16", TWDP_STOP)
    else:
        _twain_set_cap(twain, src, "CAP_XFERCOUNT", "TWTY_INT16", 1)
        _twain_set_cap(twain, src, "CAP_DUPLEXENABLED", "TWTY_BOOL", False)
    if not _twain_set_cap(twain, src, "ICAP_AUTOSIZE", "TWTY_UINT16", TWAS_AUTO):
        _twain_set_cap(twain, src, "ICAP_SUPPORTEDSIZES", "TWTY_UINT16", TWSS_NONE)
        _twain_set_cap(twain, src, "ICAP_UNDEFINEDIMAGESIZE", "TWTY_BOOL", True)
        _twain_set_cap(twain, src, "ICAP_AUTOMATICBORDERDETECTION", "TWTY_BOOL", True)
    if adf and job is not None and job.imprint.wants_hardware:
        return _twain_imprint(twain, src, job.imprint)
    return False


def _twain_error(e):
    text = f"{type(e).__name__} {e}".lower()
    if "doublefeed" in text or "double feed" in text:
        code = "double_feed"
    elif "paperjam" in text or "jam" in text:
        code = "paper_jam"
    elif "nomedia" in text:
        code = "feeder_empty"
    elif "interlock" in text:
        code = "cover_open"
    elif "checkdeviceonline" in text:
        code = "offline"
    else:
        m = re.search(r"(?:twcc|condition(?: code)?)\D{0,5}(\d{1,2})\b", text)
        code = _TWCC_CODES.get(int(m.group(1))) if m else _classify_text(text)
    return ScannerError(code, _hint_message(code, str(e))) if code else None


def _twain_xfer(twain, src):
    rv = _tw(src, "XferImageNatively", "xfer_image_natively")()
    if not rv:
        return None, 0
    handle, more = rv
    try:
        bmp = _tw(twain, "DIBToBMFile", "dib_to_bm_file")(handle)
    finally:
        try:
            _tw(twain, "GlobalHandleFree", "global_handle_free")(handle)
        except Exception:
            pass
    return _png_from_bytes(bmp), more


def _scan_twain(show_ui: bool, selector=None, display=None) -> bytes:
    twain, sm, src = _twain_open(selector, display)
    try:
        _twain_configure(twain, src, adf=False)
        _tw(src, "RequestAcquire", "request_acquire")(int(show_ui), int(show_ui))
        try:
            png, _more = _twain_xfer(twain, src)
        except Exception as e:
            se = _twain_error(e)
            if se:
                raise se
            raise
        if not png:
            raise ScannerError("feeder_empty", _hint_message("feeder_empty"))
        return png
    finally:
        _twain_close(src, sm)


def _scan_twain_adf(job, show_ui: bool, selector=None, display=None):
    twain, sm, src = _twain_open(selector, display)
    try:
        job.imprint_hw_applied = _twain_configure(twain, src, adf=True, job=job)
        _tw(src, "RequestAcquire", "request_acquire")(int(show_ui), int(show_ui))
        while not job.full:
            job.check()
            try:
                png, more = _twain_xfer(twain, src)
            except Exception as e:
                se = _twain_error(e)
                if se and se.code == "feeder_empty":
                    if job.raw_pages:
                        break
                    raise se
                if se:
                    raise se
                if job.raw_pages:
                    log.info("TWAIN ADF: end of feeder (%s)", e)
                    break
                raise RuntimeError(f"TWAIN ADF: scan failed before acquiring any page — {e}")
            if not png:
                break
            job.emit(png)
            if not more:
                break
    finally:
        _twain_close(src, sm)
    if not job.raw_pages:
        raise ScannerError("feeder_empty", _hint_message("feeder_empty"))


def scan_windows(show_ui: bool, selector=None, display=None) -> bytes:
    with _com_apartment():
        try:
            return _scan_twain(show_ui, selector, display)
        except ImportError:
            log.info("twain package missing — trying WIA…")
        except ScannerError as e:
            if e.code in HARD_STOP_CODES or e.code == "feeder_empty":
                raise
            log.warning("TWAIN failed (%s) — trying WIA…", e)
        except Exception as e:
            log.warning("TWAIN failed (%s) — trying WIA…", e)
        return _scan_wia(show_ui, selector, display)


# ═════════════════════════════════════════════════════════════════════════════
#  Linux: python-sane
# ═════════════════════════════════════════════════════════════════════════════

def _sane_pick(devices, selector=None, display=None) -> str:
    chosen = _best_match(devices, selector, display,
                         labels=lambda d: [d[0], f"{d[1]} {d[2]}".strip(),
                                           _humanise_sane_device(d[0], d[1], d[2])])
    return chosen[0]


def _sane_try_set(dev, attr, values):
    for v in values:
        try:
            setattr(dev, attr, v)
            return v
        except Exception:
            continue
    return None


def _sane_opt(dev, py_name):
    return (getattr(dev, "opt", None) or {}).get(py_name)


def _sane_constraint(dev, py_name):
    o = _sane_opt(dev, py_name)
    return getattr(o, "constraint", None) if o is not None else None


def _sane_range_max(dev, py_name):
    c = _sane_constraint(dev, py_name)
    if isinstance(c, tuple) and len(c) >= 2:
        return c[1]
    if isinstance(c, list):
        nums = [v for v in c if isinstance(v, (int, float))]
        return max(nums) if nums else None
    return None


def _sane_set_resolution(dev, wanted: int = 200):
    c = _sane_constraint(dev, "resolution")
    value = wanted
    if isinstance(c, list):
        nums = [v for v in c if isinstance(v, (int, float))]
        if nums:
            value = min(nums, key=lambda v: (abs(v - wanted), -v))
    elif isinstance(c, tuple) and len(c) >= 2:
        value = min(max(wanted, c[0]), c[1])
    return _sane_try_set(dev, "resolution", (value, 200, 150, 300))


def _sane_configure(dev, adf: bool, job=None):
    duplex = bool(job and job.duplex)
    if adf:
        choices = (("ADF Duplex", "Duplex") if duplex else ()) + (
            "ADF Front", "ADF", "Automatic Document Feeder", "ADF Simplex", "Feeder", "adf")
        src = _sane_try_set(dev, "source", choices)
        if src is None and _sane_opt(dev, "source") is not None:
            srcs = _sane_constraint(dev, "source") or []
            if isinstance(srcs, list) and not any(re.search(r"adf|feeder|duplex", str(s), re.I)
                                                  for s in srcs):
                raise ScannerError("no_feeder", f"This scanner has no document feeder "
                                                f"(sources: {', '.join(map(str, srcs))}).")
        if duplex and src and "duplex" not in str(src).lower() and job is not None:
            job.warnings.append("Duplex source not available — scanned one side only.")
    else:
        src = _sane_try_set(dev, "source", ("Flatbed", "FlatBed", "Normal"))

    _sane_try_set(dev, "mode", ("Color", "color", "24bit Color", "Color24"))
    _sane_set_resolution(dev, 200)

    ph = _sane_range_max(dev, "page_height")
    if ph:
        _sane_try_set(dev, "page_height", (min(ph, LONG_PAGE_MM),))
    pw = _sane_range_max(dev, "page_width")
    if pw:
        _sane_try_set(dev, "page_width", (pw,))
    for corner in ("br_x", "br_y"):
        mx = _sane_range_max(dev, corner)
        if mx:
            _sane_try_set(dev, corner, (mx,))
    for opt in ("swcrop", "swdeskew"):
        if _sane_opt(dev, opt) is not None:
            _sane_try_set(dev, opt, (True, 1))

    if adf and job is not None:
        if job.double_feed:
            if _sane_opt(dev, "df_action") is not None:
                _sane_try_set(dev, "df_action", ("Stop",))
            if _sane_opt(dev, "df_thickness") is not None:
                _sane_try_set(dev, "df_thickness", (True, 1))
        im = job.imprint
        if im.wants_hardware and _sane_opt(dev, "endorser") is not None:
            if _sane_try_set(dev, "endorser", (True, 1)) is not None:
                pre, post, has_counter = im.split()
                s = pre + (f"%0{im.digits}ud" if has_counter else "") + post
                _sane_try_set(dev, "endorser_string", (s,))
                _sane_try_set(dev, "endorser_val", (im.start,))
                _sane_try_set(dev, "endorser_step", (im.step,))
                if im.side == "back":
                    _sane_try_set(dev, "endorser_side", ("Back", "back"))
                job.imprint_hw_applied = True

    log.info("SANE configured: adf=%s duplex=%s source=%s", adf, duplex, src)
    return src


def _sane_error(e, dev=None, double_feed_enabled=False):
    msg = str(e)
    code = _classify_text(msg)
    if code in ("paper_jam", None) and dev is not None:
        try:
            if getattr(dev, "double_feed"):       # Fujitsu hardware sensor
                code = "double_feed"
        except Exception:
            pass
    if code == "paper_jam" and double_feed_enabled:
        msg += " (may also be a double feed)"
    return ScannerError(code, _hint_message(code, msg)) if code else None


def scan_linux(selector=None, display=None) -> bytes:
    try:
        import sane
    except ImportError:
        return _scan_via_scanimage(selector, display)
    sane.init()
    try:
        devices = sane.get_devices()
        if not devices:
            raise ScannerError("offline", "No SANE scanner found. Check: scanimage -L")
        dev = sane.open(_sane_pick(devices, selector, display))
        try:
            _sane_configure(dev, adf=False)
            try:
                img = dev.scan()
            except Exception as e:
                se = _sane_error(e, dev)
                if se:
                    raise se
                raise
            return _img_to_png_bytes(img)
        finally:
            dev.close()
    finally:
        sane.exit()


def _scan_sane_adf(job, selector=None, display=None):
    try:
        import sane
    except ImportError:
        return _scan_scanimage_adf(job, selector, display)
    sane.init()
    try:
        devices = sane.get_devices()
        if not devices:
            raise ScannerError("offline", "No SANE scanner found. Check: scanimage -L")
        dev = sane.open(_sane_pick(devices, selector, display))
        try:
            _sane_configure(dev, adf=True, job=job)
            while not job.full:
                if job.cancelled:
                    try:
                        dev.cancel()
                    except Exception:
                        pass
                    job.check()
                try:
                    img = dev.scan()
                except Exception as e:
                    se = _sane_error(e, dev, job.double_feed)
                    if se and se.code == "feeder_empty":
                        if job.raw_pages:
                            break
                        raise se
                    if se:
                        raise se
                    if job.raw_pages:
                        break
                    raise RuntimeError(f"SANE ADF scan failed: {e}")
                job.emit(_img_to_png_bytes(img))
        finally:
            dev.close()
    finally:
        sane.exit()
    if not job.raw_pages:
        raise ScannerError("feeder_empty", _hint_message("feeder_empty"))


# ═════════════════════════════════════════════════════════════════════════════
#  macOS: ImageCaptureCore (ICA) — incl. PFU "Image Scanner Driver for macOS"
# ═════════════════════════════════════════════════════════════════════════════

_ICA_DELEGATE_CLS = None


def _ica_new_state(**kw):
    st = {"found": [], "device": None, "pages": [], "seen": set(), "count": 0,
          "err": None, "tmpdir": None, "adf": False, "duplex": False,
          "unit_type": None, "job": None}
    st.update(kw)
    return st


def _ica_is_scanner(ICC, device) -> bool:
    mask = getattr(ICC, "ICDeviceTypeMaskScanner", 0x2)
    try:
        return bool(int(device.type()) & mask)
    except Exception:
        return False


def _ica_location_mask(ICC) -> int:
    mask = 0
    for attr in ("ICDeviceLocationTypeMaskLocal", "ICDeviceLocationTypeMaskBonjour",
                 "ICDeviceLocationTypeMaskShared", "ICDeviceLocationTypeMaskBluetooth"):
        mask |= getattr(ICC, attr, 0)
    return mask if mask else 0xFFFF00


def _ica_fail(d, msg):
    d._state["err"] = msg
    d._ev_opened.set()
    d._ev_done.set()


def _get_ica_delegate_cls(ICC):
    """Defined once per process — re-declaring an ObjC class raises."""
    global _ICA_DELEGATE_CLS
    if _ICA_DELEGATE_CLS is not None:
        return _ICA_DELEGATE_CLS

    from Foundation import NSObject, NSURL

    FLATBED = getattr(ICC, "ICScannerFunctionalUnitTypeFlatbed", 0)
    FEEDER = getattr(ICC, "ICScannerFunctionalUnitTypeDocumentFeeder", 3)

    class _ICABridgeDelegate(NSObject):
        def deviceBrowser_didAddDevice_moreComing_(self, browser, device, more):
            if _ica_is_scanner(ICC, device):
                self._state["found"].append(device)
            if not more:
                self._ev_found.set()

        def deviceBrowser_didRemoveDevice_moreGoing_(self, browser, device, more):
            pass

        def device_didOpenSessionWithError_(self, device, error):
            if error:
                _ica_fail(self, str(error.localizedDescription()))
                return
            try:
                types = [int(t) for t in (device.availableFunctionalUnitTypes() or [])]
            except Exception:
                types = []
            use_feeder = FEEDER in types and (self._state.get("adf") or FLATBED not in types)
            if self._state.get("adf") and FEEDER not in types and types:
                _ica_fail(self, "no feeder: this scanner has no document feeder")
                return
            self._state["unit_type"] = FEEDER if use_feeder else FLATBED
            device.requestSelectFunctionalUnit_(self._state["unit_type"])

        def device_didCloseSessionWithError_(self, device, error):
            pass

        def didRemoveDevice_(self, device):
            if not self._ev_done.is_set():
                _ica_fail(self, "ICA: scanner disconnected during scan (offline).")

        def scannerDevice_didSelectFunctionalUnit_error_(self, scanner, unit, error):
            if error:
                _ica_fail(self, str(error.localizedDescription()))
                return
            st = self._state
            is_feeder = st.get("unit_type") == FEEDER
            try:
                scanner.setTransferMode_(getattr(ICC, "ICScannerTransferModeFileBased", 0))
                if st.get("tmpdir"):
                    scanner.setDownloadsDirectory_(NSURL.fileURLWithPath_(st["tmpdir"]))
                scanner.setDocumentName_("scan")
                scanner.setDocumentUTI_("public.png")
            except Exception as e:
                log.debug("ICA: transfer setup: %s", e)
            try:
                res_set = unit.supportedResolutions()
                res = res_set.indexGreaterThanOrEqualToIndex_(200)
                if res > 100000:
                    res = res_set.lastIndex()
                unit.setResolution_(res)
            except Exception:
                try:
                    unit.setResolution_(200)
                except Exception:
                    pass
            try:
                unit.setPixelDataType_(getattr(ICC, "ICScannerPixelDataTypeRGB", 2))
                unit.setBitDepth_(getattr(ICC, "ICScannerBitDepth8Bits", 8))
            except Exception:
                pass
            if is_feeder:
                try:
                    if unit.supportsDuplexScanning():
                        unit.setDuplexScanningEnabled_(bool(st.get("duplex")))
                except Exception:
                    pass
            else:
                try:
                    size = unit.physicalSize()
                    unit.setScanArea_(((0.0, 0.0), (size.width, size.height)))
                except Exception:
                    pass
            self._ev_opened.set()
            scanner.requestScan()

        def scannerDevice_didScanToURL_(self, scanner, url):
            st = self._state
            path = str(url.path())
            if path in st["seen"]:
                return
            st["seen"].add(path)
            try:
                data = _png_from_path(path)
            except Exception as e:
                _ica_fail(self, f"ICA image convert: {e}")
                return
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            job = st.get("job")
            if st.get("adf") and job is not None:
                job.emit(data)
                st["count"] += 1
                if job.cancelled or job.full:
                    try:
                        scanner.cancelScan()
                    except Exception:
                        pass
                    self._ev_done.set()
                return
            st["pages"].append(data)
            if st.get("unit_type") == FEEDER:
                try:
                    scanner.cancelScan()
                except Exception:
                    pass
            self._ev_done.set()

        def scannerDevice_didScanToURL_data_(self, scanner, url, data):
            self.scannerDevice_didScanToURL_(scanner, url)

        def scannerDevice_didCompleteScanWithError_(self, scanner, error):
            if error:
                self._state["err"] = str(error.localizedDescription())
            self._ev_done.set()

        def scannerDevice_didEncounterError_(self, scanner, error):
            self._state["err"] = str(error.localizedDescription())
            self._ev_done.set()

    _ICA_DELEGATE_CLS = _ICABridgeDelegate
    return _ICABridgeDelegate


def _ica_scan(selector=None, display=None, adf=False, job=None) -> list:
    try:
        from Foundation import NSDate, NSRunLoop
        import ImageCaptureCore as ICC
    except ImportError:
        raise ImportError("pip install pyobjc-framework-ImageCaptureCore")

    tmp = tempfile.TemporaryDirectory()
    delegate = _get_ica_delegate_cls(ICC).alloc().init()
    delegate._ev_found = threading.Event()
    delegate._ev_opened = threading.Event()
    delegate._ev_done = threading.Event()
    delegate._state = _ica_new_state(tmpdir=tmp.name, adf=adf,
                                     duplex=bool(job and job.duplex), job=job)
    state = delegate._state

    browser = ICC.ICDeviceBrowser.alloc().init()
    browser.setDelegate_(delegate)
    browser.setBrowsedDeviceTypeMask_(
        getattr(ICC, "ICDeviceTypeMaskScanner", 0x2) | _ica_location_mask(ICC))
    browser.start()
    rl = NSRunLoop.currentRunLoop()

    def _spin(event, timeout):
        elapsed = 0.0
        while not event.is_set() and elapsed < timeout:
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
            elapsed += 0.1
            if job is not None and job.cancelled:
                return False
        return event.is_set()

    try:
        _spin(delegate._ev_found, 10.0)
        if not state["found"]:
            raise ScannerError("offline", "ICA: no scanner found. For Fujitsu fi-series install "
                                          "PFU's 'Image Scanner Driver for macOS'.")
        device = _best_match(state["found"], selector, display,
                             labels=lambda d: [str(d.name() or "")])
        state["device"] = device
        device.setDelegate_(delegate)
        device.requestOpenSession()
        if not _spin(delegate._ev_opened, 20.0) and not state["err"]:
            if job is not None:
                job.check()
            raise RuntimeError("ICA: timeout opening the scanner session.")
        if state["err"] and not delegate._ev_opened.is_set():
            pass
        if not state["err"]:
            if not _spin(delegate._ev_done, 900.0 if adf else 120.0):
                if job is not None and job.cancelled:
                    try:
                        device.cancelScan()
                    except Exception:
                        pass
    finally:
        browser.stop()
        if state["device"] is not None:
            try:
                state["device"].requestCloseSession()
            except Exception:
                pass
        tmp.cleanup()

    if job is not None:
        job.check()
    got = state["count"] if adf else len(state["pages"])
    if state["err"]:
        if "no feeder" in state["err"]:
            raise ScannerError("no_feeder", _hint_message("no_feeder"))
        se = _as_scanner_error(RuntimeError(state["err"]))
        if se and (se.code != "feeder_empty" or not got):
            raise se
        if not got:
            raise RuntimeError(state["err"])
    if not got:
        raise RuntimeError("ICA: scan completed but no image was received.")
    return state["pages"]


def scan_macos(selector=None, display=None) -> bytes:
    direct = _escl_base_from_selector(selector)
    if direct:
        return _scan_escl_http(direct)
    try:
        return _scan_via_scanimage(selector, display)
    except FileNotFoundError:
        pass
    except ScannerError as e:
        if e.code in HARD_STOP_CODES or e.code == "feeder_empty":
            raise
        log.warning("scanimage failed (%s)", e)
    except Exception as e:
        log.warning("scanimage failed (%s)", e)

    chain = [("ICA", lambda: _ica_scan(selector, display)[0]),
             ("eSCL", lambda: _scan_escl_http(_find_escl_url()))]
    if not _is_fujitsu(selector, display):
        chain.reverse()
    last = None
    for label, fn in chain:
        try:
            return fn()
        except ScannerError as e:
            if e.code in HARD_STOP_CODES or e.code == "feeder_empty":
                raise
            last = e
        except Exception as e:
            log.warning("%s failed (%s)", label, e)
            last = e
    raise RuntimeError(f"Scan failed on macOS: {last}")


def _scan_macos_adf(job, selector=None, display=None):
    direct = _escl_base_from_selector(selector)
    if direct:
        return _scan_escl_http_adf(direct, job)
    last_err = None
    for label, fn in (
        ("scanimage", lambda: _scan_scanimage_adf(job, selector, display)),
        ("ICA", lambda: _ica_scan(selector, display, adf=True, job=job)),
        ("eSCL", lambda: _scan_escl_http_adf(_find_escl_url(), job)),
    ):
        try:
            fn()
            return
        except FileNotFoundError:
            pass
        except ScannerError as e:
            if e.code in HARD_STOP_CODES or e.code == "feeder_empty" or job.raw_pages:
                raise
            last_err = e
        except Exception as e:
            if job.raw_pages:
                raise
            log.warning("%s ADF failed (%s)", label, e)
            last_err = e
    raise RuntimeError(f"ADF scan failed on macOS — scanimage, ICA and eSCL exhausted.\n"
                       f"Last error: {last_err}")


# ═════════════════════════════════════════════════════════════════════════════
#  Folder inbox (PaperStream NX Manager / ScanSnap Home "save to folder")
# ═════════════════════════════════════════════════════════════════════════════

_INBOX_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
_INBOX_DOC_EXT = {".pdf"}
_INBOX_MARKER = ".odoo_bridge_imported"


def _parse_inbox_arg(value: str) -> dict:
    if "=" in value and not re.match(r"^[A-Za-z]:\\", value):
        label, _, path = value.partition("=")
    else:
        path = value
        label = os.path.basename(os.path.normpath(value)) or "Inbox"
    return {"label": label.strip() or "Inbox", "path": os.path.expanduser(path.strip())}


def _inbox_for(selector):
    if not selector or not str(selector).startswith("inbox:"):
        return None
    label = str(selector)[6:]
    for ib in INBOXES:
        if ib["label"] == label:
            return ib
    raise ScannerError("offline", f"Unknown folder inbox {label!r} (start the bridge with --inbox).")


def _inbox_candidates(path):
    out = []
    try:
        it = os.scandir(path)
    except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
        raise ScannerError("offline", f"Inbox folder not reachable: {path} ({e})")
    with it:
        for e in it:
            try:
                if not e.is_file() or e.name.startswith((".", "~")):
                    continue
                ext = os.path.splitext(e.name)[1].lower()
                if ext in _INBOX_IMAGE_EXT or ext in _INBOX_DOC_EXT:
                    st = e.stat()
                    out.append((st.st_mtime, e.name, e.path, st.st_size))
            except OSError:
                continue
    out.sort()
    return out


def _inbox_seen_load(path) -> set:
    try:
        with open(os.path.join(path, _INBOX_MARKER), encoding="utf-8") as f:
            return {l.strip() for l in f if l.strip()}
    except OSError:
        return set()


def _inbox_readable(p) -> bool:
    try:
        with open(p, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def _inbox_archive(ib, fpath, key):
    if INBOX_KEEP:
        with open(os.path.join(ib["path"], _INBOX_MARKER), "a", encoding="utf-8") as f:
            f.write(key + "\n")
        return
    dest_dir = os.path.join(ib["path"], "_imported", datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(dest_dir, exist_ok=True)
    base = os.path.basename(fpath)
    dest = os.path.join(dest_dir, base)
    n = 1
    while os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        dest = os.path.join(dest_dir, f"{stem}_{n}{ext}")
        n += 1
    shutil.move(fpath, dest)


def _inbox_emit(job, fpath):
    from PIL import Image, ImageSequence
    name = os.path.basename(fpath)
    ext = os.path.splitext(name)[1].lower()
    with open(fpath, "rb") as f:
        data = f.read()
    if ext in _INBOX_DOC_EXT:
        job.emit_document(name, data, "application/pdf")
        return
    if ext in (".jpg", ".jpeg", ".png"):
        job.emit(data)
        return
    with Image.open(io.BytesIO(data)) as img:
        for frame in ImageSequence.Iterator(img):     # multi-page TIFF
            job.check()
            job.emit(_img_to_png_bytes(frame.copy()))


def _import_inbox(job, ib, idle_seconds=8):
    path = ib["path"]
    seen = _inbox_seen_load(path) if INBOX_KEEP else set()
    first_sight = {}
    start = time.time()
    last_activity = None
    last_notice = 0.0
    job.progress(f"Waiting for documents in '{ib['label']}' — start the job on the scanner…")

    while True:
        job.check()
        now = time.time()
        pending = False
        for mtime, name, fpath, size in _inbox_candidates(path):
            key = f"{name}|{size}|{int(mtime)}"
            if key in seen:
                continue
            if first_sight.get(fpath) != (size, mtime):
                first_sight[fpath] = (size, mtime)
                pending = True
                continue
            if now - mtime < INBOX_SETTLE or not _inbox_readable(fpath):
                pending = True
                continue
            _inbox_emit(job, fpath)
            _inbox_archive(ib, fpath, key)
            seen.add(key)
            first_sight.pop(fpath, None)
            last_activity = time.time()
            job.progress(f"Imported {name}")
            if job.full:
                return
        now = time.time()
        if last_activity and not pending and now - last_activity >= idle_seconds:
            return
        if not last_activity and not pending and now - start >= job.inbox_wait:
            raise ScannerError("inbox_empty",
                               f"No documents arrived in '{ib['label']}' within "
                               f"{job.inbox_wait} s ({path}).")
        if now - last_notice >= 10:
            left = max(0, int(job.inbox_wait - (now - start))) if not last_activity else None
            job.progress(f"Waiting for documents in '{ib['label']}'"
                         + (f" — {left} s left" if left is not None else " — receiving…"))
            last_notice = now
        time.sleep(1.0)


# ═════════════════════════════════════════════════════════════════════════════
#  Platform dispatch
# ═════════════════════════════════════════════════════════════════════════════

def do_scan(selector=None, display=None) -> bytes:
    if _inbox_for(selector):
        raise ScannerError("unsupported", "Folder inboxes are imported with 'Scan All (ADF)'.")
    if sys.platform == "win32":
        return scan_windows(SHOW_UI, selector, display)
    if sys.platform.startswith("linux"):
        return scan_linux(selector, display)
    if sys.platform == "darwin":
        return scan_macos(selector, display)
    raise RuntimeError(f"Unsupported platform: {sys.platform}")


def scan_adf(job, selector=None, display=None):
    if sys.platform == "win32":
        with _com_apartment():
            try:
                return _scan_twain_adf(job, SHOW_UI, selector, display)
            except ImportError:
                log.info("TWAIN package not installed — trying WIA ADF…")
            except (ScannerError, Exception) as e:
                code = getattr(e, "code", None)
                if job.raw_pages or code in HARD_STOP_CODES or code in ("feeder_empty", "no_feeder"):
                    raise
                log.warning("TWAIN ADF failed (%s) — trying WIA ADF…", e)
            return _scan_wia_adf(job, selector, display)
    if sys.platform.startswith("linux"):
        return _scan_sane_adf(job, selector, display)
    if sys.platform == "darwin":
        return _scan_macos_adf(job, selector, display)
    raise RuntimeError(f"ADF not supported on platform: {sys.platform}")


def _run_adf_job(job, selector, display):
    ib = _inbox_for(selector)
    if ib:
        return _import_inbox(job, ib)
    return scan_adf(job, selector, display)


# ═════════════════════════════════════════════════════════════════════════════
#  WebSocket protocol
# ═════════════════════════════════════════════════════════════════════════════

def _clean_selector(value):
    if value is None:
        return None
    value = str(value).strip()
    return value[:512] or None


async def _send(ws, payload: dict):
    try:
        await ws.send(json.dumps(payload))
    except websockets.exceptions.ConnectionClosed:
        pass


def _process_page(data: bytes, job, index: int):
    """CPU work per page: blank test, digital imprint, JPEG encode."""
    if job.skip_blank and _is_blank_page(data, job.blank_sensitivity):
        return None
    text = None
    if job.digital_active:
        text = job.imprint.render(job.imprint.start + index * job.imprint.step)
        data = _digital_imprint(data, text, job.imprint.position)
    return base64.b64encode(_to_jpeg_bytes(data)).decode(), text


async def _consume_pages(queue, reply, job, stats):
    loop = asyncio.get_running_loop()
    while True:
        item = await queue.get()
        if item is None:
            return
        kind, payload = item
        try:
            if kind == "progress":
                await reply({"status": "progress", "message": payload})
            elif kind == "document":
                name, data, mimetype = payload
                stats["documents"] += 1
                await reply({"status": "document", "name": name, "mimetype": mimetype,
                             "data": base64.b64encode(data).decode()})
            else:
                result = await loop.run_in_executor(CPU_EXECUTOR, _process_page,
                                                    payload, job, stats["sent"])
                if result is None:
                    stats["skipped"] += 1
                    continue
                b64, text = result
                if job.stream:
                    await reply({"status": "page", "index": stats["sent"],
                                 "image": b64, "imprint": text})
                else:
                    stats["images"].append(b64)
                stats["sent"] += 1
        except Exception as e:
            log.warning("page processing failed: %s", e)


async def _run_stream(reply, job, stats, fn, *args):
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()

    def sink(kind, payload):
        loop.call_soon_threadsafe(queue.put_nowait, (kind, payload))

    job.sink = sink
    consumer = asyncio.create_task(_consume_pages(queue, reply, job, stats))
    try:
        await loop.run_in_executor(DEVICE_EXECUTOR, fn, job, *args)
    finally:
        job.sink = None
        queue.put_nowait(None)
        await consumer


def _error_payload(e, job=None, stats=None):
    se = _as_scanner_error(e)
    payload = {"status": "error",
               "message": str(se or e) or e.__class__.__name__}
    if se is not None:
        payload["code"] = se.code
        payload.update(se.extra or {})
    if stats is not None:
        payload["pages_done"] = stats["sent"]
        payload["skipped_blank"] = stats["skipped"]
        payload["documents"] = stats["documents"]
    if job is not None:
        payload["raw_pages"] = job.raw_pages
        payload["imprint_next"] = job.imprint_next(stats["sent"] if stats else 0)
    return payload


async def _dispatch(ws, msg: dict, conn_jobs: dict):
    rid = msg.get("id")
    action = msg.get("action", "")
    loop = asyncio.get_running_loop()

    async def reply(payload: dict):
        if rid is not None:
            payload = dict(payload, id=rid)
        await _send(ws, payload)

    log.info("← %s%s", action, f"  (id={rid})" if rid is not None else "")
    try:
        if action == "ping":
            await reply({"status": "ok", "message": "pong", "version": VERSION,
                         "platform": sys.platform, "show_ui": SHOW_UI,
                         "features": ["stream", "cancel", "imprint", "double_feed",
                                      "inbox", "detect_bounds_bg"]})

        elif action == "list_scanners":
            scanners = await loop.run_in_executor(DEVICE_EXECUTOR, list_scanners_cached,
                                                  bool(msg.get("refresh")))
            await reply({"status": "ok", "scanners": scanners})

        elif action == "cancel":
            job = conn_jobs.get(msg.get("job"))
            if job is not None:
                job.cancel()
            await reply({"status": "ok", "cancelled": job is not None})

        elif action == "scan":
            sel = _clean_selector(msg.get("scanner"))
            disp = _clean_selector(msg.get("scanner_display"))
            await reply({"status": "progress", "message": "Scanner acquiring image…"})
            data = await loop.run_in_executor(DEVICE_EXECUTOR, do_scan, sel, disp)
            if msg.get("auto_crop"):
                cropped, bounds = await loop.run_in_executor(CPU_EXECUTOR, _auto_crop_png,
                                                             _png_from_bytes(data))
                await reply({"status": "ok", "image": base64.b64encode(cropped).decode(),
                             "original": base64.b64encode(data).decode(), "bounds": bounds})
            else:
                await reply({"status": "ok", "image": base64.b64encode(data).decode()})

        elif action == "scan_adf":
            sel = _clean_selector(msg.get("scanner"))
            disp = _clean_selector(msg.get("scanner_display"))
            job = ScanJob.from_message(rid, msg)
            stats = {"sent": 0, "skipped": 0, "documents": 0, "images": []}
            if rid is not None:
                conn_jobs[rid] = job
            try:
                await reply({"status": "progress",
                             "message": "Loading document feeder — please wait…"})
                await _run_stream(reply, job, stats, _run_adf_job, sel, disp)
                if not stats["sent"] and not stats["documents"]:
                    if stats["skipped"]:
                        raise ScannerError("all_blank", _hint_message("all_blank"))
                    raise ScannerError("feeder_empty", _hint_message("feeder_empty"))
                if job.imprint.enabled and job.imprint.mode == "hardware" and not job.imprint_hw_applied:
                    job.warnings.append("No controllable hardware imprinter found — "
                                        "pages were not imprinted.")
                payload = {
                    "status": "ok",
                    "page_count": stats["sent"],
                    "skipped_blank": stats["skipped"],
                    "documents": stats["documents"],
                    "imprint": {"hardware": job.imprint_hw_applied,
                                "digital": bool(job.digital_active),
                                "next": job.imprint_next(stats["sent"])},
                    "warnings": job.warnings,
                }
                if not job.stream:
                    payload["images"] = stats["images"]
                log.info("→ ADF OK  %d page(s), %d blank, %d document(s)",
                         stats["sent"], stats["skipped"], stats["documents"])
                await reply(payload)
            except asyncio.CancelledError:
                job.cancel()
                raise
            except Exception as e:
                log.error("scan_adf error: %s", e)
                await reply(_error_payload(e, job, stats))
            finally:
                conn_jobs.pop(rid, None)

        elif action == "make_pdf":
            images_b64 = msg.get("images") or []
            if not isinstance(images_b64, list) or not images_b64:
                raise ValueError("No images provided")
            pdf_bytes = await loop.run_in_executor(CPU_EXECUTOR, _images_to_pdf, images_b64)
            await reply({"status": "ok", "pdf": base64.b64encode(pdf_bytes).decode(),
                         "page_count": len(images_b64)})

        elif action == "detect_bounds":
            raw_b64 = msg.get("image") or ""
            if not raw_b64:
                raise ValueError("No image provided")
            background = msg.get("background", "auto")
            background = background if background in ("auto", "white", "black") else "auto"
            data = base64.b64decode(raw_b64)
            bounds = await loop.run_in_executor(CPU_EXECUTOR, _detect_bounds_bytes, data, background)
            payload = {"status": "ok", "bounds": {k: bounds[k] for k in ("x", "y", "width", "height")},
                       "found": bounds["found"], "background": bounds["background"]}
            if msg.get("return_image"):
                cropped = await loop.run_in_executor(CPU_EXECUTOR, _crop_png, _png_from_bytes(data),
                                                     bounds["x"], bounds["y"],
                                                     bounds["width"], bounds["height"])
                payload["image"] = base64.b64encode(cropped).decode()
            await reply(payload)

        elif action == "crop":
            raw_b64 = msg.get("image") or ""
            if not raw_b64:
                raise ValueError("No image provided")
            try:
                x, y = int(msg["x"]), int(msg["y"])
                width, height = int(msg["width"]), int(msg["height"])
            except (KeyError, ValueError, TypeError) as e:
                raise ValueError(f"Invalid crop parameters: {e}")
            cropped = await loop.run_in_executor(CPU_EXECUTOR, _crop_png,
                                                 _png_from_bytes(base64.b64decode(raw_b64)),
                                                 x, y, width, height)
            await reply({"status": "ok", "image": base64.b64encode(cropped).decode()})

        else:
            raise ValueError(f"Unknown action: {action!r}")

    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.error("%s error: %s", action or "request", e)
        await reply(_error_payload(e))


async def handle(websocket, *_):
    peer = getattr(websocket, "remote_address", None)
    log.info("Connected  : %s", peer)
    tasks = set()
    conn_jobs = {}
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await _send(websocket, {"status": "error", "message": "Invalid JSON"})
                continue
            if not isinstance(msg, dict):
                await _send(websocket, {"status": "error", "message": "Invalid message"})
                continue
            task = asyncio.create_task(_dispatch(websocket, msg, conn_jobs))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        for job in list(conn_jobs.values()):
            job.cancel()                 # stop the feeder if the tab is closed
        for t in list(tasks):
            t.cancel()
        log.info("Disconnected: %s", peer)


# ═════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    global SHOW_UI, INBOX_KEEP, IMPRINT_FONT

    parser = argparse.ArgumentParser(description="Cheque / Document Scanner Bridge for Odoo")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--show-ui", action="store_true",
                        help="Show the scanner's own UI dialog (TWAIN/WIA)")
    parser.add_argument("--allowed-origin", action="append", default=[], metavar="URL",
                        help="Allowed browser Origin, e.g. https://odoo.example.com (repeatable)")
    parser.add_argument("--inbox", action="append", default=[], metavar="LABEL=PATH",
                        help="Folder inbox for NX Manager / ScanSnap Home output (repeatable)")
    parser.add_argument("--inbox-keep", action="store_true",
                        help="Do not move imported files to _imported/ (remember them instead)")
    parser.add_argument("--imprint-font", default=None, help="TTF font for digital imprint")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(args.log_level)
    SHOW_UI = args.show_ui
    INBOX_KEEP = args.inbox_keep
    IMPRINT_FONT = args.imprint_font
    INBOXES.extend(_parse_inbox_arg(v) for v in args.inbox)
    origins = [o.rstrip("/") for o in args.allowed_origin] or None

    async def _run():
        async with websockets.serve(handle, args.host, args.port,
                                    max_size=MAX_MESSAGE_SIZE, origins=origins):
            bar = "━" * 60
            log.info(bar)
            log.info("  Scanner Bridge  v%s", VERSION)
            log.info("  ws://%s:%s", args.host, args.port)
            log.info("  Platform : %s", sys.platform)
            log.info("  Show UI  : %s", SHOW_UI)
            log.info("  Origins  : %s", ", ".join(origins) if origins else "ANY (insecure)")
            for ib in INBOXES:
                log.info("  Inbox    : %s → %s%s", ib["label"], ib["path"],
                         "" if os.path.isdir(ib["path"]) else "  (NOT FOUND)")
            log.info(bar)
            if not origins:
                log.warning("No --allowed-origin given: ANY website opened in this browser "
                            "can use the scanner.")
            await asyncio.Future()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        log.info("Bridge stopped.")


if __name__ == "__main__":
    main()