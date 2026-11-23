/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { useX2ManyCrud } from "@web/views/fields/relational_utils";
import { isBinarySize } from "@web/core/utils/binary";
import { formatDateTime } from "@web/core/l10n/dates";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { Dialog } from "@web/core/dialog/dialog";
import { browser } from "@web/core/browser/browser";
import { session } from "@web/session";
import { _t } from "@web/core/l10n/translation";
import {
    Component,
    proxy,
    signal,
    useProps,
    onMounted,
    onWillUnmount,
} from "@odoo/owl";

// ─── configuration ────────────────────────────────────────────────────────────

const BRIDGE_URL        = "ws://localhost:8765";
const CONNECT_TIMEOUT   = 5000;
const PING_TIMEOUT      = 5000;
const LIST_TIMEOUT      = 180000;
const SCAN_TIMEOUT      = 180000;
const ADF_IDLE_TIMEOUT  = 240000;   // max silence between two bridge messages
const PDF_TIMEOUT       = 180000;
const DETECT_TIMEOUT    = 60000;
const INBOX_WAIT_SECONDS = 180;

const PAGE_MAX_W  = 1654;           // A4 @ 200 dpi
const PAGE_MAX_H  = 2339;
const PAGE_JPEG_Q = 0.82;

const MIN_CROP_W = 40;
const MIN_CROP_H = 25;

const DEFAULT_MAX_UPLOAD = 128 * 1024 * 1024;
const UPLOAD_ROUTE = "/web/binary/upload_attachment";
const OPTIONS_KEY = "scanner_integration.adf_options.v1";

const DEFAULT_ADF_OPTIONS = {
    duplex          : false,
    skipBlank       : false,
    blankSensitivity: "normal",
    doubleFeed      : true,
    imprintEnabled  : false,
    imprintMode     : "auto",
    imprintText     : "{date} {counter}",
    imprintStart    : 1,
    imprintDigits   : 5,
    imprintPosition : "top",
};

const CROP_HANDLES = [
    { id: "nw", cursor: "nw-resize", nx: 0,   ny: 0   },
    { id: "n",  cursor: "n-resize",  nx: 0.5, ny: 0   },
    { id: "ne", cursor: "ne-resize", nx: 1,   ny: 0   },
    { id: "e",  cursor: "e-resize",  nx: 1,   ny: 0.5 },
    { id: "se", cursor: "se-resize", nx: 1,   ny: 1   },
    { id: "s",  cursor: "s-resize",  nx: 0.5, ny: 1   },
    { id: "sw", cursor: "sw-resize", nx: 0,   ny: 1   },
    { id: "w",  cursor: "w-resize",  nx: 0,   ny: 0.5 },
];

let _scannerListCache = null;


// ─── helpers ──────────────────────────────────────────────────────────────────

/** Replacement for useExternalListener (not available in Odoo 20 / Owl 3). */
function useWindowListener(type, handler, options) {
    onMounted(() => window.addEventListener(type, handler, options));
    onWillUnmount(() => window.removeEventListener(type, handler, options));
}

function clamp(v, lo, hi) {
    return Math.min(Math.max(v, lo), hi);
}

function loadAdfOptions() {
    try {
        const raw = browser.localStorage.getItem(OPTIONS_KEY);
        return { ...DEFAULT_ADF_OPTIONS, ...(raw ? JSON.parse(raw) : {}) };
    } catch {
        return { ...DEFAULT_ADF_OPTIONS };
    }
}

function saveAdfOptions(options) {
    try {
        browser.localStorage.setItem(OPTIONS_KEY, JSON.stringify({ ...options }));
    } catch {
        // storage disabled — ignore
    }
}

function _b64Mime(b64) {
    try {
        const h = atob((b64 || "").slice(0, 24));
        const c = (i) => h.charCodeAt(i);
        if (c(0) === 0x89 && h.slice(1, 4) === "PNG")              return "image/png";
        if (c(0) === 0xff && c(1) === 0xd8)                        return "image/jpeg";
        if (h.slice(0, 4) === "GIF8")                              return "image/gif";
        if (h.slice(0, 4) === "RIFF" && h.slice(8, 12) === "WEBP") return "image/webp";
        if (h.slice(0, 2) === "BM")                                return "image/bmp";
    } catch {
        // fall through
    }
    return "image/png";
}

function _dataUrl(b64) {
    return `data:${_b64Mime(b64)};base64,${b64}`;
}

function _loadImage(b64) {
    return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = () => reject(new Error(_t("The image could not be decoded by the browser.")));
        img.src = _dataUrl(b64);
    });
}

function _canvasB64(canvas, mime, quality) {
    return canvas.toDataURL(mime, quality).split(",")[1];
}

function _whiteCanvas(w, h) {
    const cv = document.createElement("canvas");
    cv.width = Math.max(1, Math.round(w));
    cv.height = Math.max(1, Math.round(h));
    const ctx = cv.getContext("2d");
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, cv.width, cv.height);
    return { cv, ctx };
}

/** base64 string → Blob (for multipart upload). */
function b64ToBlob(b64, mimetype) {
    const bin = atob(b64 || "");
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) {
        bytes[i] = bin.charCodeAt(i);
    }
    return new Blob([bytes], { type: mimetype || "application/octet-stream" });
}

async function compressPageImage(b64, maxW = PAGE_MAX_W, maxH = PAGE_MAX_H, quality = PAGE_JPEG_Q) {
    let img;
    try {
        img = await _loadImage(b64);
    } catch {
        return b64;
    }
    const s = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight, 1);
    if (s === 1 && _b64Mime(b64) === "image/jpeg") {
        return b64; // already a right-sized JPEG (bridge output) → no re-encoding
    }
    const { cv, ctx } = _whiteCanvas(img.naturalWidth * s, img.naturalHeight * s);
    ctx.drawImage(img, 0, 0, cv.width, cv.height);
    return _canvasB64(cv, "image/jpeg", quality);
}

async function makeThumbnail(b64, maxW = 150, maxH = 200) {
    try {
        const img = await _loadImage(b64);
        const s = Math.min(maxW / img.naturalWidth, maxH / img.naturalHeight, 1);
        const { cv, ctx } = _whiteCanvas(img.naturalWidth * s, img.naturalHeight * s);
        ctx.drawImage(img, 0, 0, cv.width, cv.height);
        return _canvasB64(cv, "image/jpeg", 0.75);
    } catch {
        return null;
    }
}

async function rotateImage(b64, degrees) {
    const img = await _loadImage(b64);
    const rad = (degrees * Math.PI) / 180;
    const sin = Math.abs(Math.sin(rad));
    const cos = Math.abs(Math.cos(rad));
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    const cv = document.createElement("canvas");
    cv.width = Math.round(w * cos + h * sin);
    cv.height = Math.round(w * sin + h * cos);
    const ctx = cv.getContext("2d");
    ctx.translate(cv.width / 2, cv.height / 2);
    ctx.rotate(rad);
    ctx.drawImage(img, -w / 2, -h / 2);
    return _canvasB64(cv, "image/png");
}

function readFileAsBase64(file) {
    return new Promise((resolve, reject) => {
        const fr = new FileReader();
        fr.onload = (e) => resolve(String(e.target.result).split(",")[1] || "");
        fr.onerror = () => reject(new Error(_t('Could not read file "%s".', file.name)));
        fr.readAsDataURL(file);
    });
}

function sanitizeFilename(name) {
    return (name || "").replace(/[\\/:*?"<>|\u0000-\u001f]+/g, "_").trim();
}

/** Human-readable guidance per bridge error code. */
function issueInfo(code, pagesDone) {
    const cont = pagesDone ? _t("Continue scanning") : _t("Retry");
    switch (code) {
        case "double_feed":
            return {
                level: "warning", icon: "fa-clone", title: _t("Double feed detected"),
                body: _t("Two or more sheets were pulled in together, so the scanner stopped. Take the last sheet(s) from the output tray, put them back on top of the remaining stack in the feeder, then continue."),
                resumable: true, resumeLabel: cont,
            };
        case "paper_jam":
            return {
                level: "warning", icon: "fa-exclamation-triangle", title: _t("Paper jam / misfeed"),
                body: _t("Open the feeder cover, carefully remove the stuck sheet, put it back with the remaining pages and continue."),
                resumable: true, resumeLabel: cont,
            };
        case "cover_open":
            return {
                level: "warning", icon: "fa-folder-open-o", title: _t("Scanner cover open"),
                body: _t("Close the scanner / ADF cover and try again."),
                resumable: true, resumeLabel: cont,
            };
        case "feeder_empty":
            return {
                level: "warning", icon: "fa-inbox", title: _t("The document feeder is empty"),
                body: _t("Load the documents into the feeder and try again."),
                resumable: true, resumeLabel: _t("Retry"),
            };
        case "no_feeder":
            return {
                level: "danger", icon: "fa-ban", title: _t("No document feeder"),
                body: _t("This scanner has no automatic document feeder. Use \"Add Page\" instead."),
                resumable: false,
            };
        case "offline":
            return {
                level: "danger", icon: "fa-plug", title: _t("Scanner offline"),
                body: _t("The scanner is not reachable. Check power, cable / network and the driver."),
                resumable: true, resumeLabel: cont,
            };
        case "busy":
            return {
                level: "warning", icon: "fa-hourglass-half", title: _t("Scanner busy"),
                body: _t("The scanner is busy or warming up. Close other scanning applications and retry."),
                resumable: true, resumeLabel: cont,
            };
        case "attention":
            return {
                level: "warning", icon: "fa-bell", title: _t("Scanner needs attention"),
                body: _t("Check the scanner display / LEDs, fix the problem and retry."),
                resumable: true, resumeLabel: cont,
            };
        case "inbox_empty":
            return {
                level: "warning", icon: "fa-folder-o", title: _t("No documents arrived"),
                body: _t("Nothing was delivered to the folder inbox in time. Start the job on the scanner (PaperStream NX Manager / ScanSnap Home) and click Retry."),
                resumable: true, resumeLabel: _t("Retry"),
            };
        case "all_blank":
            return {
                level: "warning", icon: "fa-file-o", title: _t("Only blank pages"),
                body: _t("All pages were detected as blank. Check the document orientation or lower the blank-page sensitivity."),
                resumable: true, resumeLabel: _t("Retry"),
            };
        case "timeout":
            return {
                level: "danger", icon: "fa-clock-o", title: _t("The scanner stopped responding"),
                body: _t("No answer from the scanner bridge. Check the scanner and the bridge window."),
                resumable: true, resumeLabel: cont,
            };
        default:
            return null;
    }
}


// ─── SVG icon & full-screen preview dialog ────────────────────────────────────

class ScannerIcon extends Component {
    static template = "scanner_integration.ScannerIcon";
    // Owl 3: props must be explicitly requested (static props is ignored).
    props = useProps();

    get iconClass() {
        const cls = (this.props && this.props.cls) || "";
        return `o_scn_ico ${cls}`.trim();
    }
}

/**
 * Opened with this.dialog.add(ScannerPreviewDialog, { name, url, downloadUrl, isPdf }).
 * The dialog service injects `close` and handles Esc / the header ✕.
 */
export class ScannerPreviewDialog extends Component {
    static template = "scanner_integration.PreviewDialog";
    static components = { Dialog, ScannerIcon };
    // Owl 3: props must be explicitly requested (static props is ignored).
    props = useProps();
}


// ─── WebSocket bridge client (no Owl dependency) ─────────────────────────────

class BridgeClient {
    constructor(onStatus) {
        this.ws = null;
        this._connecting = null;
        this._pending = new Map();
        this._seq = 0;
        this._onStatus = onStatus;
        this._disposed = false;
    }

    get connected() {
        return !!this.ws && this.ws.readyState === WebSocket.OPEN;
    }

    _status(s) {
        if (!this._disposed) {
            this._onStatus(s);
        }
    }

    connect() {
        if (this._disposed) {
            return Promise.reject(new Error(_t("Scanner widget closed.")));
        }
        if (this.connected) {
            return Promise.resolve(this.ws);
        }
        if (this._connecting) {
            return this._connecting;
        }
        this._connecting = new Promise((resolve, reject) => {
            const unreachable = _t("Cannot reach the scanner bridge at %s.", BRIDGE_URL);
            let ws = null;
            let settled = false;
            let timer = null;
            const fail = (message) => {
                if (settled) {
                    return;
                }
                settled = true;
                clearTimeout(timer);
                this._connecting = null;
                if (ws) {
                    ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
                    try { ws.close(); } catch { /* ignore */ }
                }
                this._status("disconnected");
                reject(new Error(message));
            };
            try {
                ws = new WebSocket(BRIDGE_URL);
            } catch {
                fail(unreachable);
                return;
            }
            timer = setTimeout(
                () => fail(_t("Cannot reach the scanner bridge at %s (connection timeout).", BRIDGE_URL)),
                CONNECT_TIMEOUT
            );
            ws.onopen = () => {
                if (settled) {
                    return;
                }
                settled = true;
                clearTimeout(timer);
                this._connecting = null;
                if (this._disposed) {
                    try { ws.close(); } catch { /* ignore */ }
                    reject(new Error(_t("Scanner widget closed.")));
                    return;
                }
                this.ws = ws;
                ws.onmessage = (ev) => this._onMessage(ev);
                ws.onclose = () => this._onClose(ws);
                ws.onerror = () => {};
                this._status("connected");
                resolve(ws);
            };
            ws.onerror = () => fail(unreachable);
            ws.onclose = () => fail(unreachable);
        });
        return this._connecting;
    }

    _onMessage(ev) {
        let msg;
        try {
            msg = JSON.parse(ev.data);
        } catch {
            return;
        }
        const entry = msg.id !== undefined && msg.id !== null
            ? this._pending.get(msg.id)
            : this._pending.values().next().value; // legacy bridge (no id echo)
        if (!entry) {
            return;
        }
        this._arm(entry); // activity → reset idle timeout
        switch (msg.status) {
            case "scanning":
            case "progress":
                entry.onProgress?.(msg);
                return;
            case "page":
                entry.onPage?.(msg);
                return;
            case "document":
                entry.onDocument?.(msg);
                return;
            case "ok":
                this._settle(entry, null, msg);
                return;
            default: {
                const err = new Error(msg.message || _t("Scanner bridge request failed."));
                err.code = msg.code || null;
                err.data = msg;
                this._settle(entry, err);
            }
        }
    }

    _arm(entry) {
        clearTimeout(entry.timer);
        entry.timer = setTimeout(() => {
            const err = new Error(
                _t("The scanner bridge did not answer in time (%s s).", Math.round(entry.timeout / 1000))
            );
            err.code = "timeout";
            this._settle(entry, err);
            if (entry.cancelOnTimeout) {
                this.cancel(entry.id);
            }
        }, entry.timeout);
    }

    _settle(entry, error, msg) {
        if (!this._pending.has(entry.id)) {
            return;
        }
        this._pending.delete(entry.id);
        clearTimeout(entry.timer);
        if (error) {
            entry.reject(error);
        } else {
            entry.resolve(msg);
        }
    }

    _onClose(ws) {
        if (this.ws === ws) {
            this.ws = null;
        }
        this._status("disconnected");
        for (const entry of [...this._pending.values()]) {
            this._settle(entry, new Error(_t("The scanner bridge closed the connection.")));
        }
    }

    async request(action, payload = {}, options = {}) {
        const {
            timeout = SCAN_TIMEOUT, onProgress = null, onPage = null,
            onDocument = null, onStart = null, cancelOnTimeout = false,
        } = options;
        const ws = await this.connect();
        const id = ++this._seq;
        return new Promise((resolve, reject) => {
            const entry = {
                id, resolve, reject, onProgress, onPage, onDocument,
                timeout, cancelOnTimeout, timer: null,
            };
            this._pending.set(id, entry);
            this._arm(entry);
            try {
                ws.send(JSON.stringify({ ...payload, action, id }));
                onStart?.(id);
            } catch (e) {
                this._settle(entry, e instanceof Error ? e : new Error(String(e)));
            }
        });
    }

    cancel(jobId) {
        if (!jobId || !this.connected) {
            return Promise.resolve();
        }
        return this.request("cancel", { job: jobId }, { timeout: 10000 }).catch(() => {});
    }

    async ping() {
        try {
            await this.request("ping", {}, { timeout: PING_TIMEOUT });
            return true;
        } catch {
            return false;
        }
    }

    dispose() {
        this._disposed = true;
        const ws = this.ws;
        this.ws = null;
        if (ws) {
            ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
            try { ws.close(); } catch { /* ignore */ }
        }
        for (const entry of [...this._pending.values()]) {
            this._settle(entry, new Error(_t("Scanner widget closed.")));
        }
    }
}


// ─── shared base (bridge + crop editor + messages) ───────────────────────────

class ScannerFieldBase extends Component {
    static components = { ScannerIcon };
    // Owl 3: props must be explicitly requested (static props is ignored).
    props = useProps();

    setup() {
        this.state = proxy({
            bridgeStatus: "unknown",
            statusMsg   : "",
            errorMsg    : "",
            scanIssue   : null,
            scanning    : false,
            showCropper : false,
            rawImage    : null,
            cropPageIdx : -1,
            cropReady   : false,
            cropBusy    : false,
            cropInfo    : "",
            cropX       : 0,
            cropY       : 0,
            cropW       : 0,
            cropH       : 0,
        });

        this.cropHandles = CROP_HANDLES;

        // Owl 3: refs are signals, bound with t-ref="this.xxx", read with this.xxx()
        this.cropImageRef = signal(null);
        this.fileInputRef = signal(null);

        this._timers           = new Set();
        this._dragCleanup      = null;
        this._destroyed        = false;
        this._cropDisplayW     = 0;
        this._autoDetectOnLoad = false;

        this.bridge = new BridgeClient((status) => {
            if (!this._destroyed) {
                this.state.bridgeStatus = status;
            }
        });

        this.onCropHandlePointerDown = this.onCropHandlePointerDown.bind(this);
        this.onCropBoxPointerDown    = this.onCropBoxPointerDown.bind(this);

        useWindowListener("resize", this._onWindowResize.bind(this));

        onMounted(() => {
            // No bridge traffic (and no console noise) in readonly views.
            if (!this.isReadonly) {
                this.bridge.ping();
            }
        });
        onWillUnmount(() => {
            this._destroyed = true;
            if (this._dragCleanup) {
                this._dragCleanup();
            }
            for (const t of this._timers) {
                clearTimeout(t);
            }
            this._timers.clear();
            this.bridge.dispose();
        });
    }

    // ── generic ───────────────────────────────────────────────────────────────

    get isReadonly() {
        return !!this.props.readonly;
    }

    get isBusy() {
        return this.state.scanning || this.state.cropBusy;
    }

    get showStatusAlert() {
        return !!this.state.statusMsg && !this.isBusy;
    }

    get scanBusyLabel() {
        return this.state.statusMsg || _t("Scanning…");
    }

    _setError(e) {
        this.state.errorMsg = (e && e.message) || String(e || _t("Unknown error"));
        this.state.statusMsg = "";
    }

    _later(fn, ms) {
        const t = setTimeout(() => {
            this._timers.delete(t);
            if (!this._destroyed) {
                fn();
            }
        }, ms);
        this._timers.add(t);
    }

    _flash(msg, ms = 3000) {
        this.state.statusMsg = msg;
        this._later(() => {
            if (this.state.statusMsg === msg) {
                this.state.statusMsg = "";
            }
        }, ms);
    }

    _handleScanError(e, context, pagesDone = 0) {
        const code = e && e.code;
        if (code === "cancelled") {
            this.state.errorMsg = "";
            this._flash(
                pagesDone ? _t("Scan stopped. %s page(s) kept.", pagesDone) : _t("Scan stopped."),
                4000
            );
            return;
        }
        const info = code ? issueInfo(code, pagesDone) : null;
        if (info) {
            this.state.scanIssue = { ...info, code, context, pagesDone, detail: e.message || "" };
            this.state.errorMsg = "";
            this.state.statusMsg = "";
            return;
        }
        this._setError(e);
    }

    onDismissError() {
        this.state.errorMsg = "";
    }

    onDismissIssue() {
        this.state.scanIssue = null;
    }

    /** Overridden by subclasses. */
    onResumeScan() {
        this.state.scanIssue = null;
    }

    onRetryBridge() {
        this.state.bridgeStatus = "unknown";
        this.bridge.ping();
    }

    // ── crop editor ───────────────────────────────────────────────────────────

    get rawImageSrc() {
        return this.state.rawImage ? _dataUrl(this.state.rawImage) : "";
    }

    get cropStyles() {
        const { cropX: x, cropY: y, cropW: w, cropH: h } = this.state;
        return {
            overlayTop   : `top:0;left:0;right:0;height:${y}px;`,
            overlayBottom: `left:0;right:0;top:${y + h}px;bottom:0;`,
            overlayLeft  : `top:${y}px;left:0;width:${x}px;height:${h}px;`,
            overlayRight : `top:${y}px;left:${x + w}px;right:0;height:${h}px;`,
            cropBox      : `top:${y}px;left:${x}px;width:${w}px;height:${h}px;`,
        };
    }

    getHandleStyle(handle) {
        return `left:${handle.nx * 100}%;top:${handle.ny * 100}%;cursor:${handle.cursor};`;
    }

    get cropDimensionsLabel() {
        const img = this.cropImageRef();
        if (!img || !img.naturalWidth) {
            return "";
        }
        const rect = img.getBoundingClientRect();
        if (!rect.width || !rect.height) {
            return "";
        }
        const sx = img.naturalWidth / rect.width;
        const sy = img.naturalHeight / rect.height;
        return `${Math.round(this.state.cropW * sx)} × ${Math.round(this.state.cropH * sy)} px`;
    }

    _openCropEditor(b64, pageIdx = -1, { autoDetect = false } = {}) {
        this._autoDetectOnLoad = autoDetect;
        Object.assign(this.state, {
            rawImage   : b64,
            cropPageIdx: pageIdx,
            showCropper: true,
            cropReady  : false,
            cropBusy   : false,
            cropInfo   : "",
            errorMsg   : "",
        });
    }

    _closeCropEditor() {
        if (this._dragCleanup) {
            this._dragCleanup();
        }
        this._autoDetectOnLoad = false;
        Object.assign(this.state, {
            showCropper: false,
            rawImage   : null,
            cropPageIdx: -1,
            cropReady  : false,
            cropBusy   : false,
            cropInfo   : "",
        });
    }

    onCropImageLoad(ev) {
        const img = ev.target;
        const rect = img.getBoundingClientRect();
        const w = rect.width || img.clientWidth;
        const h = rect.height || img.clientHeight;
        const m = Math.min(16, w * 0.03, h * 0.03);
        this._cropDisplayW = w;
        Object.assign(this.state, {
            cropX: m,
            cropY: m,
            cropW: Math.max(1, w - 2 * m),
            cropH: Math.max(1, h - 2 * m),
            cropReady: true,
        });
        if (this._autoDetectOnLoad) {
            this._autoDetectOnLoad = false;
            if (this.bridge.connected) {
                this.onAutoDetect();
            }
        }
    }

    onCropImageError() {
        this._closeCropEditor();
        this._setError(new Error(_t("This image format cannot be displayed by the browser. Please use PNG or JPEG.")));
    }

    async onAutoDetect() {
        if (!this.state.rawImage || this.state.cropBusy || !this.state.cropReady) {
            return;
        }
        this.state.cropBusy = true;
        try {
            const res = await this.bridge.request(
                "detect_bounds",
                { image: this.state.rawImage, background: "auto" },
                { timeout: DETECT_TIMEOUT }
            );
            this._applyBounds(res);
        } catch (e) {
            this._setError(e);
        } finally {
            if (!this._destroyed) {
                this.state.cropBusy = false;
            }
        }
    }

    _applyBounds(res) {
        const el = this.cropImageRef();
        const b = res && res.bounds;
        if (!el || !b || !el.naturalWidth) {
            return;
        }
        if (res.found === false) {
            this.state.cropInfo = _t("No document edges found — adjust manually");
            return;
        }
        const rect = el.getBoundingClientRect();
        const fx = rect.width / el.naturalWidth;
        const fy = rect.height / el.naturalHeight;
        const x = clamp(b.x * fx, 0, Math.max(0, rect.width - MIN_CROP_W));
        const y = clamp(b.y * fy, 0, Math.max(0, rect.height - MIN_CROP_H));
        Object.assign(this.state, {
            cropX: x,
            cropY: y,
            cropW: clamp(b.width * fx, MIN_CROP_W, rect.width - x),
            cropH: clamp(b.height * fy, MIN_CROP_H, rect.height - y),
            cropInfo: res.background === "dark"
                ? _t("Document detected (dark background)")
                : _t("Document detected"),
        });
    }

    _onWindowResize() {
        if (!this.state.showCropper || !this.state.cropReady || !this._cropDisplayW) {
            return;
        }
        const el = this.cropImageRef();
        const w = el && el.getBoundingClientRect().width;
        if (!w) {
            return;
        }
        const f = w / this._cropDisplayW;
        if (Math.abs(f - 1) < 0.001) {
            return;
        }
        this._cropDisplayW = w;
        Object.assign(this.state, {
            cropX: this.state.cropX * f,
            cropY: this.state.cropY * f,
            cropW: this.state.cropW * f,
            cropH: this.state.cropH * f,
        });
    }

    _startDrag(ev, onMove) {
        if (ev.button !== undefined && ev.button !== 0) {
            return;
        }
        const img = this.cropImageRef();
        if (!img || this.state.cropBusy) {
            return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        if (this._dragCleanup) {
            this._dragCleanup();
        }
        const rect = img.getBoundingClientRect();
        const start = {
            px: ev.clientX, py: ev.clientY,
            x: this.state.cropX, y: this.state.cropY,
            w: this.state.cropW, h: this.state.cropH,
            maxW: rect.width, maxH: rect.height,
        };
        const move = (e) => {
            e.preventDefault();
            onMove(e.clientX - start.px, e.clientY - start.py, start);
        };
        const cleanup = () => {
            window.removeEventListener("pointermove", move);
            window.removeEventListener("pointerup", cleanup);
            window.removeEventListener("pointercancel", cleanup);
            this._dragCleanup = null;
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", cleanup);
        window.addEventListener("pointercancel", cleanup);
        this._dragCleanup = cleanup;
    }

    onCropBoxPointerDown(ev) {
        this._startDrag(ev, (dx, dy, s) => {
            this.state.cropX = clamp(s.x + dx, 0, Math.max(0, s.maxW - s.w));
            this.state.cropY = clamp(s.y + dy, 0, Math.max(0, s.maxH - s.h));
        });
    }

    onCropHandlePointerDown(ev, handleId) {
        this._startDrag(ev, (dx, dy, s) => {
            let l = s.x;
            let t = s.y;
            let r = s.x + s.w;
            let b = s.y + s.h;
            if (handleId.includes("w")) { l = clamp(l + dx, 0, r - MIN_CROP_W); }
            if (handleId.includes("e")) { r = clamp(r + dx, l + MIN_CROP_W, s.maxW); }
            if (handleId.includes("n")) { t = clamp(t + dy, 0, b - MIN_CROP_H); }
            if (handleId.includes("s")) { b = clamp(b + dy, t + MIN_CROP_H, s.maxH); }
            Object.assign(this.state, {
                cropX: l, cropY: t,
                cropW: Math.max(1, r - l), cropH: Math.max(1, b - t),
            });
        });
    }

    async _getCroppedImage() {
        const el = this.cropImageRef();
        const b64 = this.state.rawImage;
        if (!el || !b64) {
            return null;
        }
        const img = await _loadImage(b64);
        const rect = el.getBoundingClientRect();
        const sx = img.naturalWidth / (rect.width || 1);
        const sy = img.naturalHeight / (rect.height || 1);
        const x = clamp(Math.round(this.state.cropX * sx), 0, img.naturalWidth - 1);
        const y = clamp(Math.round(this.state.cropY * sy), 0, img.naturalHeight - 1);
        const w = clamp(Math.round(this.state.cropW * sx), 1, img.naturalWidth - x);
        const h = clamp(Math.round(this.state.cropH * sy), 1, img.naturalHeight - y);
        const cv = document.createElement("canvas");
        cv.width = w;
        cv.height = h;
        cv.getContext("2d").drawImage(img, x, y, w, h, 0, 0, w, h);
        return _canvasB64(cv, "image/png");
    }

    async _rotate(deg) {
        if (!this.state.rawImage || this.state.cropBusy) {
            return;
        }
        this.state.cropBusy = true;
        try {
            const rotated = await rotateImage(this.state.rawImage, deg);
            if (this._destroyed) {
                return;
            }
            this.state.cropReady = false;
            this.state.cropInfo = "";
            this.state.rawImage = rotated;
        } catch (e) {
            this._setError(e);
        } finally {
            if (!this._destroyed) {
                this.state.cropBusy = false;
            }
        }
    }

    onRotateLeft()  { return this._rotate(-90); }
    onRotateRight() { return this._rotate(90); }

    async onApplyCrop() {
        if (this.state.cropBusy) {
            return;
        }
        this.state.cropBusy = true;
        try {
            const b64 = await this._getCroppedImage();
            if (b64) {
                await this._commitCrop(b64);
            }
        } catch (e) {
            this._setError(e);
        } finally {
            if (!this._destroyed) {
                this.state.cropBusy = false;
            }
        }
    }

    async onUseFullImage() {
        if (this.state.cropBusy || !this.state.rawImage) {
            return;
        }
        this.state.cropBusy = true;
        try {
            await this._commitCrop(this.state.rawImage);
        } catch (e) {
            this._setError(e);
        } finally {
            if (!this._destroyed) {
                this.state.cropBusy = false;
            }
        }
    }

    onCancelCrop() {
        this._closeCropEditor();
    }

    /** @abstract */
    async _commitCrop(_b64) {}
}


// ─── 1. CHEQUE IMAGE WIDGET ───────────────────────────────────────────────────

class ScannerImageField extends ScannerFieldBase {
    static template = "scanner_integration.ScannerImageField";

    get hasValue() {
        return !!this.props.record.data[this.props.name];
    }

    get imageSrc() {
        const value = this.props.record.data[this.props.name];
        if (!value) {
            return null;
        }
        if (isBinarySize(value)) {
            const { resModel, resId } = this.props.record;
            if (!resId) {
                return null;
            }
            const wd = this.props.record.data.write_date;
            const unique = wd && wd.ts ? wd.ts : "";
            return `/web/image/${resModel}/${resId}/${this.props.name}?unique=${unique}`;
        }
        if (typeof value === "string") {
            return value.startsWith("data:") ? value : _dataUrl(value);
        }
        return null;
    }

    async _commitCrop(b64) {
        await this.props.record.update({ [this.props.name]: b64 });
        this._closeCropEditor();
        this._flash(_t("✓ Image updated"));
    }

    onResumeScan() {
        this.state.scanIssue = null;
        this.onScanClick();
    }

    async onScanClick() {
        if (this.isBusy) {
            return;
        }
        Object.assign(this.state, {
            scanning : true,
            errorMsg : "",
            scanIssue: null,
            statusMsg: _t("Connecting to scanner bridge…"),
        });
        try {
            await this.bridge.connect();
            this.state.statusMsg = _t("Waiting for scanner…");
            const res = await this.bridge.request("scan", {}, {
                timeout: SCAN_TIMEOUT,
                onProgress: (m) => {
                    this.state.statusMsg = m.message || _t("Scanning…");
                },
            });
            if (!res.image) {
                throw new Error(_t("The scanner bridge returned no image."));
            }
            this.state.statusMsg = "";
            this._openCropEditor(res.image, -1, { autoDetect: true });
        } catch (e) {
            this._handleScanError(e, "cheque");
        } finally {
            if (!this._destroyed) {
                this.state.scanning = false;
            }
        }
    }

    onUploadClick() {
        this.fileInputRef()?.click();
    }

    async onFileChange(ev) {
        const input = ev.target;
        const file = input.files && input.files[0];
        input.value = "";
        if (!file) {
            return;
        }
        this.state.errorMsg = "";
        if (file.type && !file.type.startsWith("image/")) {
            this._setError(new Error(_t("Please select an image file.")));
            return;
        }
        try {
            this._openCropEditor(await readFileAsBase64(file));
        } catch (e) {
            this._setError(e);
        }
    }

    async onClearClick() {
        await this.props.record.update({ [this.props.name]: false });
        this.state.statusMsg = "";
        this.state.errorMsg = "";
    }
}

export const scannerImageField = {
    component: ScannerImageField,
    displayName: _t("Scanner Image"),
    supportedTypes: ["binary"],
    fieldDependencies: [{ name: "write_date", type: "datetime" }],
    isEmpty: () => false,
};


// ─── 2. PDF DOCUMENT SCANNER WIDGET ──────────────────────────────────────────

class ScannerPdfField extends ScannerFieldBase {
    static template = "scanner_integration.ScannerPdfField";

    setup() {
        super.setup();
        this.orm    = useService("orm");
        this.dialog = useService("dialog");
        this.http   = useService("http");

        this.fileDirectRef    = signal(null);
        this.filenameInputRef = signal(null);

        this.operations = useX2ManyCrud(() => this.props.record.data[this.props.name], true);

        this._pageIdSeq     = 0;
        this._pickerToken   = 0;
        this._pageChain     = Promise.resolve();
        this._lastSelection = null;

        Object.assign(this.state, {
            pages             : [],
            scanSource        : "",
            scanJobId         : null,
            stopRequested     : false,
            adfReceived       : 0,
            saving            : false,
            uploading         : false,
            importing         : false,
            showFilenameDialog: false,
            pendingFilename   : "",
            showScannerDialog : false,
            loadingScanners   : false,
            availableScanners : [],
            scannerListError  : "",
            pendingScanAction : null,
            adfOptions        : loadAdfOptions(),
        });

        for (const m of [
            "onScannerConfirm", "onScannerCancel", "onPreviewAttachment",
            "onRemoveAttachment", "onCropPage", "onRemovePage", "onMovePage",
            "onAdfOptionChange",
        ]) {
            this[m] = this[m].bind(this);
        }

        // Capture phase: runs before the hotkey service / parent dialogs,
        // so Escape only closes our own overlays (scanner picker / filename).
        // The preview is an Odoo Dialog and handles Escape itself.
        useWindowListener("keydown", this._onWindowKeydown.bind(this), { capture: true });
    }

    // ── computed ──────────────────────────────────────────────────────────────

    get isBusy() {
        const s = this.state;
        return s.scanning || s.saving || s.uploading || s.importing || s.cropBusy;
    }

    get adfBusyLabel() {
        return this.state.statusMsg || _t("Scanning feeder…");
    }

    get uploadBusyLabel() {
        return this.state.statusMsg || _t("Uploading…");
    }

    get currentAttachments() {
        const list = this.props.record.data[this.props.name];
        if (!list || !list.records) {
            return [];
        }
        return list.records.map((r) => {
            const mimetype = r.data.mimetype || "";
            const isPdf = mimetype === "application/pdf";
            const isImage = mimetype.startsWith("image/");
            let dateLabel = "";
            try {
                dateLabel = r.data.create_date ? formatDateTime(r.data.create_date) : "";
            } catch {
                dateLabel = "";
            }
            return {
                id        : r.resId,
                name      : r.data.name || _t("Unnamed"),
                mimetype,
                dateLabel,
                canPreview: isPdf || isImage,
                iconClass : isPdf ? "fa-file-pdf-o text-danger"
                          : isImage ? "fa-file-image-o text-primary"
                          : "fa-file-o text-secondary",
            };
        });
    }

    get imprintPreview() {
        const o = this.state.adfOptions;
        const date = new Date().toISOString().slice(0, 10);
        const start = String(parseInt(o.imprintStart, 10) || 0).padStart(parseInt(o.imprintDigits, 10) || 5, "0");
        return (o.imprintText || "").replaceAll("{date}", date).replaceAll("{counter}", start);
    }

    // ── ADF options (replaces t-model) ───────────────────────────────────────

    onAdfOptionChange(key, ev) {
        const el = ev.target;
        let value;
        if (el.type === "checkbox") {
            value = el.checked;
        } else if (el.type === "number") {
            const n = parseInt(el.value, 10);
            value = Number.isNaN(n) ? DEFAULT_ADF_OPTIONS[key] : n;
        } else {
            value = el.value;
        }
        this.state.adfOptions[key] = value;
    }

    // ── preview (full-screen Odoo dialog) ─────────────────────────────────────

    _openPreview({ id, name, mimetype }) {
        if (!id) {
            return;
        }
        const fileName = name || _t("Document");
        const mime = (mimetype || "").toLowerCase();
        const isPdf = mime === "application/pdf" || (!mime && /\.pdf$/i.test(fileName));
        const base = `/web/content/${id}`;
        this.dialog.add(ScannerPreviewDialog, {
            name       : fileName,
            isPdf,
            url        : base,
            downloadUrl: `${base}?download=true`,
        });
    }

    onPreviewAttachment(att) {
        this._openPreview(att);
    }

    _onWindowKeydown(ev) {
        if (ev.key !== "Escape") {
            return;
        }
        let handled = true;
        if (this.state.showScannerDialog) {
            this.onScannerCancel();
        } else if (this.state.showFilenameDialog) {
            this.onFilenameCancel();
        } else {
            handled = false;
        }
        if (handled) {
            ev.preventDefault();
            ev.stopPropagation();
        }
    }

    // ── record / attachment helpers ───────────────────────────────────────────

    async _ensureRecordSaved() {
        const record = this.props.record;
        if (record.resId) {
            return;
        }
        const ok = await record.save();
        if (!ok || !record.resId) {
            throw new Error(_t("The record must be saved before documents can be attached. Please fill in the required fields and try again."));
        }
    }

    async _linkAttachments(ids) {
        await this.operations.saveRecord(ids);
        const ok = await this.props.record.save();
        if (!ok) {
            throw new Error(_t("The document was created but the record could not be saved. Please save it manually."));
        }
    }

    async _postNote(body, attachmentIds = []) {
        const resId = this.props.record.resId;
        if (!resId) {
            return;
        }
        try {
            const kwargs = { body, message_type: "comment", subtype_xmlid: "mail.mt_note" };
            if (attachmentIds.length) {
                kwargs.attachment_ids = attachmentIds;
            }
            await this.orm.call(this.props.record.resModel, "message_post", [[resId]], kwargs);
        } catch (e) {
            // model without mail.thread, or no access — not critical
            console.warn("[ScannerPdfField] chatter post failed:", e);
        }
    }

    /** Upload one file through Odoo's multipart upload route → attachment id. */
    async _uploadOne(file, name) {
        const { resModel, resId } = this.props.record;
        const csrfToken = globalThis.odoo && globalThis.odoo.csrf_token;
        if (!csrfToken) {
            throw new Error(_t("Missing CSRF token — please reload the page and try again."));
        }
        const upload = file instanceof File
            ? file
            : new File([file], name, { type: file.type || "application/octet-stream" });

        const formData = new FormData();
        formData.append("csrf_token", csrfToken);
        formData.append("ufile", upload, name);
        formData.append("model", resModel);
        formData.append("id", String(resId));

        let res;
        try {
            res = await this.http.post(UPLOAD_ROUTE, formData);
        } catch (e) {
            // Non-JSON answer (e.g. HTML error page for 400 / 413)
            throw new Error(_t('Upload of "%(name)s" failed: %(error)s', {
                name,
                error: (e && e.message) || _t("server error"),
            }));
        }
        const info = Array.isArray(res) ? res[0] : res;
        if (!info || info.error || !info.id) {
            throw new Error((info && info.error) || _t('Upload of "%s" failed.', name));
        }
        return info.id;
    }

    /** entries: [{ name, file: Blob|File }] → ir.attachment linked to the field */
    async _uploadAttachments(entries) {
        if (!entries.length) {
            return [];
        }
        await this._ensureRecordSaved();
        const ids = [];
        for (let i = 0; i < entries.length; i++) {
            const e = entries[i];
            this.state.statusMsg = _t('Uploading "%(name)s" (%(index)s / %(total)s)…', {
                name: e.name, index: i + 1, total: entries.length,
            });
            ids.push(await this._uploadOne(e.file, e.name));
        }
        await this._linkAttachments(ids);
        await this._postNote(
            _t("Document(s) attached: %s", entries.map((e) => e.name).join(", ")),
            ids
        );
        return ids;
    }

// ★ trash button → confirm → delete the ir.attachment (the m2m link goes with it)
    onRemoveAttachment(id) {
        if (this.isBusy) {
            return;
        }
        const list = this.props.record.data[this.props.name];
        const rec = list && list.records.find((r) => r.resId === id);
        if (!rec) {
            return;
        }
        const name = rec.data.name || _t("Unnamed");
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete document"),
            body: _t('"%s" will be permanently deleted. This cannot be undone.', name),
            confirmLabel: _t("Delete"),
            confirm: () => this._deleteAttachment(rec.resId, name),
            cancel: () => {},
        });
    }

    async _deleteAttachment(attId, name) {
        if (this.state.removing || !attId) {
            return;
        }
        const record = this.props.record;
        // Not tied to this component, so it still works if the field
        // re-renders or unmounts while the form saves and reloads.
        const orm = this.env.services.orm;

        Object.assign(this.state, {
            removing : true,
            errorMsg : "",
            statusMsg: _t('Deleting "%s"…', name),
        });
        try {
            // 0) Save pending edits first, so the reload below doesn't lose them
            if (record.resId) {
                const ok = await record.save();
                if (!ok) {
                    throw new Error(_t("Please save the record first (check the required fields). The document was not deleted."));
                }
            }

            // 1) Delete the file. The database also removes the many2many link.
            await orm.unlink("ir.attachment", [attId]);
            console.info("[ScannerPdfField] ir.attachment deleted:", attId);

            // 2) Reload the record so the list matches the database
            if (record.resId) {
                await record.load();
            }

            // 3) Chatter note (not critical)
            if (record.resId) {
                try {
                    await orm.call(record.resModel, "message_post", [[record.resId]], {
                        body: _t("Document deleted: %s", name),
                        message_type: "comment",
                        subtype_xmlid: "mail.mt_note",
                    });
                } catch (e) {
                    console.warn("[ScannerPdfField] chatter post failed:", e);
                }
            }

            if (!this._destroyed) {
                this.state.removing = false;
                this._flash(_t('✓ "%s" deleted', name));
            }
        } catch (e) {
            // Always log it, even if the widget is gone
            console.error("[ScannerPdfField] delete failed:", e);
            if (!this._destroyed) {
                const detail = (e && e.data && e.data.message) || (e && e.message) || String(e);
                this._setError(new Error(_t('"%(name)s" could not be deleted: %(error)s', { name, error: detail })));
            }
        } finally {
            if (!this._destroyed) {
                this.state.removing = false;
            }
        }
    }
    // ── scanner picker ────────────────────────────────────────────────────────

    async _openScannerPicker(action, refresh = false) {
        const token = ++this._pickerToken;
        Object.assign(this.state, {
            pendingScanAction: action,
            showScannerDialog: true,
            scannerListError : "",
            errorMsg         : "",
            scanIssue        : null,
        });
        if (!refresh && _scannerListCache) {
            this.state.availableScanners = _scannerListCache;
            this.state.loadingScanners = false;
            return;
        }
        this.state.loadingScanners = true;
        this.state.availableScanners = [];
        try {
            const res = await this.bridge.request("list_scanners", { refresh }, { timeout: LIST_TIMEOUT });
            if (token !== this._pickerToken) {
                return;
            }
            const list = Array.isArray(res.scanners) ? res.scanners : [];
            _scannerListCache = list;
            this.state.availableScanners = list;
        } catch (e) {
            if (token !== this._pickerToken) {
                return;
            }
            if (!this.bridge.connected) {
                this.state.showScannerDialog = false;
                this.state.pendingScanAction = null;
                this._setError(e);
            } else {
                this.state.scannerListError = e.message;
            }
        } finally {
            if (token === this._pickerToken && !this._destroyed) {
                this.state.loadingScanners = false;
            }
        }
    }

    onRefreshScanners() {
        this._openScannerPicker(this.state.pendingScanAction, true);
    }

    onScannerCancel() {
        this._pickerToken++;
        Object.assign(this.state, {
            showScannerDialog: false,
            pendingScanAction: null,
            loadingScanners  : false,
        });
    }

    onScannerConfirm(scanner) {
        const action = this.state.pendingScanAction;
        saveAdfOptions(this.state.adfOptions);
        this.onScannerCancel();
        const selection = scanner
            ? { name: scanner.name || null, display: scanner.display || null, source: scanner.source || "" }
            : null;
        if (action === "adf" || (selection && selection.source === "inbox")) {
            this._doScanAdf(selection);
        } else {
            this._doScanFlatbed(selection);
        }
    }

    onScanFlatbed() {
        if (!this.isBusy) {
            this._openScannerPicker("flatbed");
        }
    }

    onScanAdf() {
        if (!this.isBusy) {
            this._openScannerPicker("adf");
        }
    }

    onResumeScan() {
        const issue = this.state.scanIssue;
        this.state.scanIssue = null;
        if (!issue) {
            return;
        }
        if (issue.context === "adf") {
            this._doScanAdf(this._lastSelection);
        } else {
            this._doScanFlatbed(this._lastSelection);
        }
    }

    onStopScan() {
        if (!this.state.scanJobId || this.state.stopRequested) {
            return;
        }
        this.state.stopRequested = true;
        this.state.statusMsg = _t("Stopping after the current page…");
        this.bridge.cancel(this.state.scanJobId);
    }

    _scanPayload(selection) {
        return {
            scanner        : selection ? selection.name : null,
            scanner_display: selection ? selection.display : null,
        };
    }

    _imprintPayload() {
        const o = this.state.adfOptions;
        if (!o.imprintEnabled) {
            return null;
        }
        return {
            enabled      : true,
            mode         : o.imprintMode,
            text         : o.imprintText || "{counter}",
            counter_start: parseInt(o.imprintStart, 10) || 0,
            digits       : parseInt(o.imprintDigits, 10) || 5,
            position     : o.imprintPosition,
        };
    }

    _rememberImprint(next) {
        if (Number.isInteger(next) && this.state.adfOptions.imprintEnabled) {
            this.state.adfOptions.imprintStart = next;
            saveAdfOptions(this.state.adfOptions);
        }
    }

    async _doScanFlatbed(selection) {
        if (this.isBusy) {
            return;
        }
        this._lastSelection = selection;
        Object.assign(this.state, {
            scanning  : true,
            scanSource: "flatbed",
            errorMsg  : "",
            scanIssue : null,
            statusMsg : _t("Connecting to scanner bridge…"),
        });
        try {
            await this.bridge.connect();
            this.state.statusMsg = selection && selection.display
                ? _t('Waiting for "%s"…', selection.display)
                : _t("Waiting for default scanner…");
            const res = await this.bridge.request("scan", this._scanPayload(selection), {
                timeout: SCAN_TIMEOUT,
                onProgress: (m) => {
                    this.state.statusMsg = m.message || _t("Scanning…");
                },
            });
            if (!res.image) {
                throw new Error(_t("The scanner bridge returned no image."));
            }
            this.state.statusMsg = "";
            this._openCropEditor(res.image, -1, { autoDetect: true });
        } catch (e) {
            this._handleScanError(e, "flatbed");
        } finally {
            if (!this._destroyed) {
                this.state.scanning = false;
                this.state.scanSource = "";
            }
        }
    }

    async _doScanAdf(selection) {
        if (this.isBusy) {
            return;
        }
        this._lastSelection = selection;
        const isInbox = !!(selection && selection.source === "inbox");
        const o = this.state.adfOptions;
        const documents = [];
        let streamed = 0;
        this._pageChain = Promise.resolve();

        Object.assign(this.state, {
            scanning     : true,
            scanSource   : "adf",
            scanJobId    : null,
            stopRequested: false,
            adfReceived  : 0,
            errorMsg     : "",
            scanIssue    : null,
            statusMsg    : _t("Connecting to scanner bridge…"),
        });
        try {
            await this.bridge.connect();
            this.state.statusMsg = isInbox
                ? _t("Waiting for documents…")
                : _t("Loading document feeder — please wait…");
            const res = await this.bridge.request(
                "scan_adf",
                {
                    ...this._scanPayload(selection),
                    stream           : true,
                    duplex           : !!o.duplex,
                    skip_blank       : !!o.skipBlank,
                    blank_sensitivity: o.blankSensitivity,
                    double_feed      : !!o.doubleFeed,
                    imprint          : this._imprintPayload(),
                    inbox_wait       : INBOX_WAIT_SECONDS,
                },
                {
                    timeout: ADF_IDLE_TIMEOUT,
                    cancelOnTimeout: true,
                    onStart: (id) => {
                        this.state.scanJobId = id;
                    },
                    onProgress: (m) => {
                        if (!this.state.stopRequested) {
                            this.state.statusMsg = m.message || _t("Scanning feeder…");
                        }
                    },
                    onPage: (m) => {
                        const image = m.image;
                        if (!image) {
                            return;
                        }
                        streamed++;
                        this.state.adfReceived++;
                        if (!this.state.stopRequested) {
                            this.state.statusMsg = _t("Page %s received…", this.state.adfReceived);
                        }
                        this._pageChain = this._pageChain
                            .then(() => this._addPage(image))
                            .catch((err) => console.warn("[ScannerPdfField] page add failed:", err));
                    },
                    onDocument: (m) => {
                        if (!m.data) {
                            return;
                        }
                        const mimetype = m.mimetype || "application/pdf";
                        documents.push({
                            name: m.name || "document.pdf",
                            file: b64ToBlob(m.data, mimetype),
                        });
                    },
                }
            );
            await this._pageChain;
            if (!streamed && Array.isArray(res.images)) {
                // legacy bridge (< 1.9): all pages at once
                for (const img of res.images) {
                    if (img) {
                        await this._addPage(img);
                        this.state.adfReceived++;
                    }
                }
            }
            this._rememberImprint(res.imprint && res.imprint.next);
            if (documents.length) {
                await this._uploadAttachments(documents);
            }
            const parts = [_t("✓ %s page(s) received", this.state.adfReceived)];
            if (res.skipped_blank) {
                parts.push(_t("%s blank page(s) removed", res.skipped_blank));
            }
            if (documents.length) {
                parts.push(_t("%s document(s) attached", documents.length));
            }
            if (res.imprint && (res.imprint.hardware || res.imprint.digital)) {
                parts.push(res.imprint.hardware ? _t("imprinted by the scanner") : _t("digitally stamped"));
            }
            this.state.scanning = false;
            this._flash(parts.join(" — "), 5000);
            if (Array.isArray(res.warnings) && res.warnings.length) {
                this.state.errorMsg = res.warnings.join("\n");
            }
        } catch (e) {
            await this._pageChain.catch(() => {});
            if (this._destroyed) {
                return;
            }
            if (e.data) {
                this._rememberImprint(e.data.imprint_next);
            }
            if (documents.length) {
                try {
                    await this._uploadAttachments(documents);
                } catch (e2) {
                    console.warn("[ScannerPdfField] inbox document upload failed:", e2);
                }
            }
            this._handleScanError(e, "adf", this.state.adfReceived);
        } finally {
            if (!this._destroyed) {
                Object.assign(this.state, {
                    scanning     : false,
                    scanSource   : "",
                    scanJobId    : null,
                    stopRequested: false,
                });
            }
        }
    }

    // ── pages ─────────────────────────────────────────────────────────────────

    async _addPage(b64) {
        const compressed = await compressPageImage(b64);
        const thumb = await makeThumbnail(compressed);
        this.state.pages.push({ id: ++this._pageIdSeq, b64: compressed, thumb });
    }

    async _commitCrop(b64) {
        const compressed = await compressPageImage(b64);
        const thumb = await makeThumbnail(compressed);
        const idx = this.state.cropPageIdx;
        if (idx === -1 || !this.state.pages[idx]) {
            this.state.pages.push({ id: ++this._pageIdSeq, b64: compressed, thumb });
        } else {
            this.state.pages.splice(idx, 1, { ...this.state.pages[idx], b64: compressed, thumb });
        }
        this._closeCropEditor();
    }

    onCropPage(idx) {
        if (this.isBusy || !this.state.pages[idx]) {
            return;
        }
        this._openCropEditor(this.state.pages[idx].b64, idx);
    }

    onRemovePage(idx) {
        if (this.isBusy) {
            return;
        }
        this.state.pages.splice(idx, 1);
    }

    onMovePage(idx, dir) {
        if (this.isBusy) {
            return;
        }
        const to = idx + dir;
        const pages = this.state.pages;
        if (to < 0 || to >= pages.length) {
            return;
        }
        const tmp = pages[idx];
        pages[idx] = pages[to];
        pages[to] = tmp;
    }

    onClearPages() {
        this.state.pages.splice(0);
        this.state.statusMsg = "";
        this.state.errorMsg = "";
    }

    // ── image upload (→ pages) ────────────────────────────────────────────────

    onUploadClick() {
        this.fileInputRef()?.click();
    }

    async onFileChange(ev) {
        const input = ev.target;
        const files = Array.from(input.files || []);
        input.value = "";
        if (!files.length) {
            return;
        }
        this.state.errorMsg = "";
        const images = files.filter((f) => !f.type || f.type.startsWith("image/"));
        if (images.length !== files.length) {
            this._setError(new Error(_t("Only image files can be added as pages. Use \"Upload File\" for other documents.")));
            if (!images.length) {
                return;
            }
        }
        try {
            if (images.length === 1) {
                this._openCropEditor(await readFileAsBase64(images[0]), -1);
                return;
            }
            this.state.importing = true;
            for (let i = 0; i < images.length; i++) {
                this.state.statusMsg = _t("Adding image %(index)s / %(total)s…", {
                    index: i + 1, total: images.length,
                });
                await this._addPage(await readFileAsBase64(images[i]));
            }
            this.state.importing = false;
            this._flash(_t("✓ %s images added", images.length));
        } catch (e) {
            this._setError(e);
        } finally {
            if (!this._destroyed) {
                this.state.importing = false;
            }
        }
    }

    // ── save as PDF ───────────────────────────────────────────────────────────

    onSavePdf() {
        if (!this.state.pages.length || this.isBusy) {
            return;
        }
        const today = new Date().toISOString().slice(0, 10);
        this.state.pendingFilename = `Scanned_Document_${today}`;
        this.state.showFilenameDialog = true;
        this._focusFilenameInput();
    }

    /** Replaces the old useEffect: wait until the input is rendered, then focus it. */
    _focusFilenameInput(tries = 10) {
        this._later(() => {
            const el = this.filenameInputRef();
            if (el && el.isConnected) {
                el.focus();
                el.select();
            } else if (tries > 0 && this.state.showFilenameDialog) {
                this._focusFilenameInput(tries - 1);
            }
        }, 30);
    }

    onFilenameInput(ev) {
        this.state.pendingFilename = ev.target.value;
    }

    onFilenameKeydown(ev) {
        if (ev.key === "Enter") {
            ev.preventDefault();
            this.state.pendingFilename = ev.target.value;
            this.onFilenameConfirm();
        }
    }

    onFilenameConfirm() {
        let name = sanitizeFilename(this.state.pendingFilename);
        if (!name) {
            return;
        }
        if (!name.toLowerCase().endsWith(".pdf")) {
            name += ".pdf";
        }
        this.state.showFilenameDialog = false;
        this._doSavePdf(name);
    }

    onFilenameCancel() {
        this.state.showFilenameDialog = false;
    }

    async _doSavePdf(filename) {
        const images = this.state.pages.map((p) => p.b64).filter(Boolean);
        const pageCount = images.length;
        if (!pageCount) {
            return;
        }
        Object.assign(this.state, {
            saving   : true,
            errorMsg : "",
            statusMsg: _t("Connecting to bridge…"),
        });
        try {
            await this.bridge.connect();
            this.state.statusMsg = _t("Generating PDF (%s page(s))…", pageCount);
            const res = await this.bridge.request("make_pdf", { images }, { timeout: PDF_TIMEOUT });
            if (!res.pdf) {
                throw new Error(_t("PDF generation failed."));
            }
            this.state.statusMsg = _t("Saving attachment…");
            const [attachId] = await this._uploadAttachments([
                { name: filename, file: b64ToBlob(res.pdf, "application/pdf") },
            ]);
            this.state.pages.splice(0);
            this.state.saving = false;
            this._flash(_t("✓ PDF saved — %s page(s)", pageCount), 4000);
            this._openPreview({ id: attachId, name: filename, mimetype: "application/pdf" });
        } catch (e) {
            this._setError(e);
        } finally {
            if (!this._destroyed) {
                this.state.saving = false;
            }
        }
    }

    // ── direct file upload ────────────────────────────────────────────────────

    onDirectUploadClick() {
        this.fileDirectRef()?.click();
    }

    async onDirectFileChange(ev) {
        const input = ev.target;
        const files = Array.from(input.files || []);
        input.value = "";
        if (!files.length) {
            return;
        }
        // Multipart upload → no base64 overhead, the full server limit applies.
        const maxSize = session.max_file_upload_size || DEFAULT_MAX_UPLOAD;
        const tooBig = files.filter((f) => f.size > maxSize);
        if (tooBig.length) {
            this._setError(new Error(_t("File too large: %(files)s (max %(max)s MB)", {
                files: tooBig.map((f) => f.name).join(", "),
                max: Math.floor(maxSize / 1024 / 1024),
            })));
            return;
        }
        Object.assign(this.state, { uploading: true, errorMsg: "" });
        try {
            await this._uploadAttachments(files.map((f) => ({ name: f.name, file: f })));
            this.state.uploading = false;
            this._flash(_t("✓ %s file(s) uploaded", files.length));
        } catch (e) {
            this._setError(e);
        } finally {
            if (!this._destroyed) {
                this.state.uploading = false;
            }
        }
    }
}

export const scannerPdfField = {
    component: ScannerPdfField,
    displayName: _t("Scanner PDF Documents"),
    supportedTypes: ["many2many"],
    isEmpty: () => false,
    relatedFields: [
        { name: "name", type: "char" },
        { name: "mimetype", type: "char" },
        { name: "create_date", type: "datetime" },
    ],
};

registry.category("fields").add("scanner_image", scannerImageField);
registry.category("fields").add("scanner_pdf", scannerPdfField);