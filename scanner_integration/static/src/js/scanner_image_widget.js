/** @odoo-module **/

import { registry }             from "@web/core/registry";
import { useService }           from "@web/core/utils/hooks";
import { standardFieldProps }   from "@web/views/fields/standard_field_props";
import { Component, useState, useRef, onMounted, onWillUnmount }
                                 from "@odoo/owl";

const BRIDGE_URL      = "ws://localhost:8765";
const CONNECT_TIMEOUT = 5000;
const SCAN_TIMEOUT    = 30000;
const ADF_TIMEOUT     = 120000;
const PDF_TIMEOUT     = 60000;

const PAGE_MAX_W  = 1654;
const PAGE_MAX_H  = 2339;
const PAGE_JPEG_Q = 0.82;

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


// ─── module-level helpers ─────────────────────────────────────────────────────

function _b64Mime(b64) {
    try {
        const h = atob(b64.slice(0, 16));
        if (h.charCodeAt(0) === 0x89 && h[1] === "P")             return "image/png";
        if (h.charCodeAt(0) === 0xFF && h.charCodeAt(1) === 0xD8) return "image/jpeg";
    } catch (_) {}
    return "image/png";
}

function compressPageImage(b64, maxW, maxH, quality) {
    maxW    = maxW    === undefined ? PAGE_MAX_W  : maxW;
    maxH    = maxH    === undefined ? PAGE_MAX_H  : maxH;
    quality = quality === undefined ? PAGE_JPEG_Q : quality;
    return new Promise(function (resolve) {
        var mime = _b64Mime(b64);
        var img  = new Image();
        img.onload = function () {
            var w = img.width, h = img.height;
            var s = Math.min(maxW / w, maxH / h, 1);
            w = Math.max(1, Math.round(w * s));
            h = Math.max(1, Math.round(h * s));
            var cv = document.createElement("canvas");
            cv.width = w; cv.height = h;
            cv.getContext("2d").drawImage(img, 0, 0, w, h);
            resolve(cv.toDataURL("image/jpeg", quality).split(",")[1]);
        };
        img.onerror = function () { resolve(b64); };
        img.src = "data:" + mime + ";base64," + b64;
    });
}

function _rotateImage(b64, degrees) {
    return new Promise(function (resolve) {
        var mime = _b64Mime(b64);
        var img  = new Image();
        img.onload = function () {
            var rad  = (degrees * Math.PI) / 180;
            var sin  = Math.abs(Math.sin(rad));
            var cos  = Math.abs(Math.cos(rad));
            var newW = Math.round(img.width * cos + img.height * sin);
            var newH = Math.round(img.width * sin + img.height * cos);
            var cv   = document.createElement("canvas");
            cv.width  = newW;
            cv.height = newH;
            var ctx = cv.getContext("2d");
            ctx.translate(newW / 2, newH / 2);
            ctx.rotate(rad);
            ctx.drawImage(img, -img.width / 2, -img.height / 2);
            resolve(cv.toDataURL("image/png").split(",")[1]);
        };
        img.onerror = function () { resolve(b64); };
        img.src = "data:" + mime + ";base64," + b64;
    });
}


// ─── 1. CHEQUE IMAGE WIDGET ───────────────────────────────────────────────────

class ScannerImageField extends Component {

    setup() {
        this.state = useState({
            scanning    : false,
            bridgeStatus: "unknown",
            statusMsg   : "",
            errorMsg    : "",
            showCropper : false,
            rawImage    : null,
            cropX       : 0,
            cropY       : 0,
            cropW       : 100,
            cropH       : 100,
        });

        this.cropHandles  = CROP_HANDLES;
        this.fileInputRef = useRef("fileInput");
        this.cropImageRef = useRef("cropImage");
        this._ws          = null;

        this.onCropHandleMouseDown = this.onCropHandleMouseDown.bind(this);

        onMounted(()     => this._pingBridge());
        onWillUnmount(() => this._closeSocket());
    }

    get fieldValue() { return this.props.record.data[this.props.name]; }

    get imageSrc() {
        const value = this.props.record.data[this.props.name];
        if (!value) return null;
        const id    = this.props.record.resId;
        const model = this.props.record.resModel;
        const field = this.props.name;
        if (id) return `/web/image/${model}/${id}/${field}`;
        if (typeof value === "string")
            return value.startsWith("data:") ? value : `data:image/png;base64,${value}`;
        return null;
    }

    get isReadonly() { return this.props.readonly || false; }

    get cropStyles() {
        const { cropX: x, cropY: y, cropW: w, cropH: h } = this.state;
        const dark = "background:rgba(0,0,0,0.55);pointer-events:none;";
        return {
            overlayTop   : `position:absolute;top:0;left:0;right:0;height:${y}px;${dark}`,
            overlayBottom: `position:absolute;left:0;right:0;top:${y + h}px;bottom:0;${dark}`,
            overlayLeft  : `position:absolute;top:${y}px;left:0;width:${x}px;height:${h}px;${dark}`,
            overlayRight : `position:absolute;top:${y}px;left:${x + w}px;right:0;height:${h}px;${dark}`,
            cropBox      :
                `position:absolute;top:${y}px;left:${x}px;` +
                `width:${w}px;height:${h}px;` +
                `border:2px solid #0d6efd;cursor:move;box-sizing:border-box;`,
        };
    }

    getHandleStyle(handle) {
        return (
            `position:absolute;` +
            `left:calc(${handle.nx * 100}% - 8px);top:calc(${handle.ny * 100}% - 8px);` +
            `width:16px;height:16px;` +
            `background:white;border:2px solid #0d6efd;border-radius:3px;` +
            `cursor:${handle.cursor};z-index:10;box-sizing:border-box;`
        );
    }

    get cropDimensionsLabel() {
        const img = this.cropImageRef && this.cropImageRef.el;
        if (!img || !img.naturalWidth) return "";
        const rect = img.getBoundingClientRect();
        if (!rect.width) return "";
        const sx = img.naturalWidth  / rect.width;
        const sy = img.naturalHeight / rect.height;
        return `${Math.round(this.state.cropW * sx)} \xD7 ${Math.round(this.state.cropH * sy)} px`;
    }

    _showCropper(imageB64) {
        this.state.rawImage    = imageB64;
        this.state.showCropper = true;
        this.state.cropX = 0; this.state.cropY = 0;
        this.state.cropW = 100; this.state.cropH = 100;
    }

    onCropImageLoad(ev) {
        const img  = ev.target;
        const rect = img.getBoundingClientRect();
        const w    = rect.width  || img.clientWidth;
        const h    = rect.height || img.clientHeight;
        const m    = Math.min(16, w * 0.03);
        this.state.cropX = m; this.state.cropY = m;
        this.state.cropW = w - m * 2; this.state.cropH = h - m * 2;
    }

    async onRotateLeft() {
        if (!this.state.rawImage) return;
        this.state.rawImage = await _rotateImage(this.state.rawImage, -90);
    }

    async onRotateRight() {
        if (!this.state.rawImage) return;
        this.state.rawImage = await _rotateImage(this.state.rawImage, 90);
    }

    onCropBoxMouseDown(ev) {
        ev.preventDefault();
        const startX = ev.clientX, startY = ev.clientY;
        const sx = this.state.cropX, sy = this.state.cropY;
        const cw = this.state.cropW, ch = this.state.cropH;
        const rect = this.cropImageRef.el.getBoundingClientRect();
        const maxW = rect.width, maxH = rect.height;

        const onMove = (e) => {
            this.state.cropX = Math.max(0, Math.min(sx + e.clientX - startX, maxW - cw));
            this.state.cropY = Math.max(0, Math.min(sy + e.clientY - startY, maxH - ch));
        };
        const onUp = () => {
            window.removeEventListener("mousemove", onMove);
            window.removeEventListener("mouseup",   onUp);
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup",   onUp);
    }

    onCropHandleMouseDown(ev, handleId) {
        ev.preventDefault();
        ev.stopPropagation();
        const startX = ev.clientX, startY = ev.clientY;
        const sx = this.state.cropX, sy = this.state.cropY;
        const sw = this.state.cropW, sh = this.state.cropH;
        const rect = this.cropImageRef.el.getBoundingClientRect();
        const maxW = rect.width, maxH = rect.height;

        const onMove = (e) => {
            const dx = e.clientX - startX, dy = e.clientY - startY;
            let x = sx, y = sy, w = sw, h = sh;
            if (handleId.includes("n")) { y = sy + dy; h = sh - dy; }
            if (handleId.includes("s")) { h = sh + dy; }
            if (handleId.includes("w")) { x = sx + dx; w = sw - dx; }
            if (handleId.includes("e")) { w = sw + dx; }
            w = Math.max(40, w); h = Math.max(25, h);
            x = Math.max(0, Math.min(x, maxW - w));
            y = Math.max(0, Math.min(y, maxH - h));
            w = Math.min(w, maxW - x); h = Math.min(h, maxH - y);
            this.state.cropX = x; this.state.cropY = y;
            this.state.cropW = w; this.state.cropH = h;
        };
        const onUp = () => {
            window.removeEventListener("mousemove", onMove);
            window.removeEventListener("mouseup",   onUp);
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup",   onUp);
    }

    async onApplyCrop() {
        const b64 = this.state.rawImage;
        const el  = this.cropImageRef.el;
        if (!b64 || !el) return;

        const mime    = _b64Mime(b64);
        const natural = new Image();
        await new Promise(res => {
            natural.onload = res;
            natural.src = `data:${mime};base64,` + b64;
        });

        const rect   = el.getBoundingClientRect();
        const scaleX = natural.naturalWidth  / (rect.width  || 1);
        const scaleY = natural.naturalHeight / (rect.height || 1);

        const canvas = document.createElement("canvas");
        canvas.width  = Math.round(this.state.cropW * scaleX);
        canvas.height = Math.round(this.state.cropH * scaleY);
        canvas.getContext("2d").drawImage(
            natural,
            this.state.cropX * scaleX, this.state.cropY * scaleY,
            this.state.cropW * scaleX, this.state.cropH * scaleY,
            0, 0, canvas.width, canvas.height
        );
        const croppedB64 = canvas.toDataURL("image/png").split(",")[1];
        await this.props.record.update({ [this.props.name]: croppedB64 });
        this._closeCropper("\u2713 Image cropped and saved");
    }

    async onUseFullImage() {
        await this.props.record.update({ [this.props.name]: this.state.rawImage });
        this._closeCropper("\u2713 Image saved");
    }

    onCancelCrop() { this._closeCropper(""); }

    _closeCropper(msg) {
        this.state.showCropper = false;
        this.state.rawImage    = null;
        this.state.statusMsg   = msg;
        if (msg) setTimeout(() => { this.state.statusMsg = ""; }, 3000);
    }

    _restoreOnClose(ws) {
        ws.onclose = () => {
            this.state.bridgeStatus = "disconnected";
            this._ws = null;
        };
    }

    _closeSocket() {
        if (this._ws) {
            this._ws.onclose   = null;
            this._ws.onerror   = null;
            this._ws.onmessage = null;
            try { this._ws.close(); } catch (_) {}
            this._ws = null;
        }
    }

    async _getSocket() {
        if (this._ws && this._ws.readyState === WebSocket.OPEN)
            return this._ws;

        if (this._ws) {
            this._ws.onclose   = null;
            this._ws.onerror   = null;
            this._ws.onmessage = null;
            try { this._ws.close(); } catch (_) {}
            this._ws = null;
        }

        return new Promise((resolve, reject) => {
            let settled = false;
            const settle = (fn, val) => { if (!settled) { settled = true; fn(val); } };

            const ws    = new WebSocket(BRIDGE_URL);
            const timer = setTimeout(() => {
                try { ws.close(); } catch (_) {}
                settle(reject, new Error(
                    `Cannot reach scanner bridge at ${BRIDGE_URL}.\n` +
                    `Run:  python scanner_bridge.py`
                ));
            }, CONNECT_TIMEOUT);

            ws.onopen = () => {
                clearTimeout(timer);
                this._ws = ws;
                this.state.bridgeStatus = "connected";
                this._restoreOnClose(ws);
                settle(resolve, ws);
            };
            ws.onerror = () => {
                clearTimeout(timer);
                this.state.bridgeStatus = "disconnected";
                settle(reject, new Error(
                    `Scanner bridge not reachable at ${BRIDGE_URL}.\n` +
                    `Start it with:  python scanner_bridge.py`
                ));
            };
        });
    }

    async _pingBridge() {
        try {
            const ws = await this._getSocket();
            await new Promise((resolve) => {
                const tid  = setTimeout(resolve, 2000);
                const prev = ws.onmessage;
                ws.onmessage = (ev) => {
                    clearTimeout(tid);
                    ws.onmessage = prev;
                    try {
                        const d = JSON.parse(ev.data);
                        if (d.status === "ok") this.state.bridgeStatus = "connected";
                    } catch (_) {}
                    resolve();
                };
                ws.send(JSON.stringify({ action: "ping" }));
            });
        } catch (_) { this.state.bridgeStatus = "disconnected"; }
    }

    async onScanClick() {
        if (this.state.scanning) return;
        this.state.scanning  = true;
        this.state.errorMsg  = "";
        this.state.statusMsg = "Connecting to scanner bridge\u2026";

        try {
            const ws = await this._getSocket();
            this.state.statusMsg = "Waiting for scanner\u2026";

            const imageB64 = await new Promise((resolve, reject) => {
                const timer = setTimeout(
                    () => reject(new Error("Scan timed out (30 s).")),
                    SCAN_TIMEOUT
                );

                const cleanup = (restoreClose) => {
                    restoreClose = restoreClose === undefined ? true : restoreClose;
                    clearTimeout(timer);
                    ws.onmessage = null;
                    ws.onerror   = null;
                    if (restoreClose) this._restoreOnClose(ws);
                };

                ws.onclose = () => {
                    cleanup(false);
                    this._ws = null;
                    this.state.bridgeStatus = "disconnected";
                    reject(new Error("Bridge closed the connection during scan."));
                };
                ws.onmessage = (ev) => {
                    let msg;
                    try { msg = JSON.parse(ev.data); } catch (_) { return; }
                    if (msg.status === "scanning") {
                        this.state.statusMsg = msg.message || "Scanning\u2026";
                        return;
                    }
                    cleanup();
                    if (msg.status === "ok" && msg.image) resolve(msg.image);
                    else reject(new Error(msg.message || "Scan failed."));
                };
                ws.onerror = () => {
                    cleanup();
                    reject(new Error("WebSocket error during scan."));
                };
                ws.send(JSON.stringify({ action: "scan" }));
            });

            this._showCropper(imageB64);
            this.state.statusMsg = "Adjust the crop area then click Apply & Save";
        } catch (e) {
            this.state.errorMsg  = e.message;
            this.state.statusMsg = "";
        } finally {
            this.state.scanning = false;
        }
    }

    onUploadClick() {
        if (this.fileInputRef.el) this.fileInputRef.el.click();
    }

    async onFileChange(ev) {
        const file = ev.target.files && ev.target.files[0];
        if (!file) return;
        this.state.errorMsg = "";
        const b64 = await new Promise((resolve, reject) => {
            const fr   = new FileReader();
            fr.onload  = (e) => resolve(e.target.result.split(",")[1]);
            fr.onerror = reject;
            fr.readAsDataURL(file);
        });
        this._showCropper(b64);
        ev.target.value = "";
    }

    async onClearClick() {
        await this.props.record.update({ [this.props.name]: false });
        this.state.statusMsg = "";
        this.state.errorMsg  = "";
    }
}



ScannerImageField.template = "scanner_integration.ScannerImageField";
ScannerImageField.props = { ...standardFieldProps };

const scannerImageField = {
    component: ScannerImageField,
    supportedTypes: ["binary"],
    isEmpty: () => false,
};

// ─── 2. PDF DOCUMENT SCANNER WIDGET ──────────────────────────────────────────

class ScannerPdfField extends Component {

    setup() {
        this.orm           = useService("orm");
        this.cropHandles   = CROP_HANDLES;
        this.fileInputRef  = useRef("fileInput");
        this.fileDirectRef = useRef("fileDirect");
        this.cropImageRef  = useRef("cropImage");
        this._ws           = null;
        this._pageIdSeq    = 0;

        this.state = useState({
            pages              : [],
            scanning           : false,
            scanSource         : "",
            saving             : false,
            uploading          : false,
            bridgeStatus       : "unknown",
            statusMsg          : "",
            errorMsg           : "",
            showCropper        : false,
            cropPageIdx        : -1,
            rawImage           : null,
            cropX              : 0,
            cropY              : 0,
            cropW              : 100,
            cropH              : 100,
            showPreview        : false,
            previewAttachId    : null,
            previewFilename    : "",
            attachments        : [],
            showFilenameDialog : false,
            pendingFilename    : "",
            showScannerDialog  : false,
            loadingScanners    : false,
            availableScanners  : [],
            pendingScanAction  : null,
        });

        this.onScannerConfirm      = this.onScannerConfirm.bind(this);
        this.onScannerCancel       = this.onScannerCancel.bind(this);
        this.onPreviewAttachment   = this.onPreviewAttachment.bind(this);
        this.onRemoveAttachment    = this.onRemoveAttachment.bind(this);
        this.onCropPage            = this.onCropPage.bind(this);
        this.onRemovePage          = this.onRemovePage.bind(this);
        this.onMovePage            = this.onMovePage.bind(this);
        this.onCropHandleMouseDown = this.onCropHandleMouseDown.bind(this);

        onMounted(() => {
            this._pingBridge();
            this._loadExistingAttachments();
        });
        onWillUnmount(() => this._closeSocket());
    }

    // ── computed ──────────────────────────────────────────────────────────────

    get isReadonly() { return this.props.readonly || false; }

    get currentAttachments() { return this.state.attachments; }

    get _currentIds() {
        const list = this.props.record.data[this.props.name];
        if (!list) return [];
        if (Array.isArray(list.currentIds)) return list.currentIds;
        if (list.records && list.records.length)
            return list.records.map(r => r.resId).filter(Boolean);
        return [];
    }

    // ── preview ───────────────────────────────────────────────────────────────

    get previewUrl() {
        if (this.state.previewAttachId)
            return `/web/content/${this.state.previewAttachId}?inline=true`;
        return null;
    }

    get downloadUrl() {
        if (this.state.previewAttachId)
            return `/web/content/${this.state.previewAttachId}?download=true`;
        return null;
    }

    onPreviewAttachment(id, name) {
        this.state.previewAttachId = id;
        this.state.previewFilename = name || "Document.pdf";
        this.state.showPreview     = true;
    }

    onClosePreview() {
        this.state.showPreview     = false;
        this.state.previewAttachId = null;
        this.state.previewFilename = "";
    }

    // ── Scanner picker ────────────────────────────────────────────────────────

    async _openScannerPicker(action) {
        this.state.pendingScanAction  = action;
        this.state.showScannerDialog  = true;
        this.state.loadingScanners    = true;
        this.state.availableScanners  = [];

        try {
            const ws = await this._getSocket();
            const scanners = await new Promise((resolve, reject) => {
                const timer = setTimeout(
                    () => reject(new Error("Scanner list request timed out.")),
                    8000
                );
                const prev = ws.onmessage;
                ws.onmessage = (ev) => {
                    clearTimeout(timer);
                    ws.onmessage = prev;
                    try {
                        const d = JSON.parse(ev.data);
                        if (d.status === "ok" && Array.isArray(d.scanners))
                            resolve(d.scanners);
                        else
                            reject(new Error(d.message || "Could not retrieve scanner list."));
                    } catch (_) {
                        reject(new Error("Invalid response from bridge."));
                    }
                };
                ws.send(JSON.stringify({ action: "list_scanners" }));
            });
            this.state.availableScanners = scanners;
        } catch (e) {
            this.state.errorMsg          = e.message;
            this.state.showScannerDialog = false;
            this.state.pendingScanAction = null;
        } finally {
            this.state.loadingScanners = false;
        }
    }

    onScannerCancel() {
        this.state.showScannerDialog = false;
        this.state.pendingScanAction = null;
    }

    onScannerConfirm(scannerName) {
        const action = this.state.pendingScanAction;
        this.state.showScannerDialog = false;
        this.state.pendingScanAction = null;
        if (action === "adf") this._doScanAdf(scannerName);
        else                  this._doScanFlatbed(scannerName);
    }

    async onRemoveAttachment(id) {
        const attachment = this.state.attachments.find(a => a.id === id);
        const newIds = this._currentIds.filter(i => i !== id);

        await this.props.record.update({ [this.props.name]: newIds });
        await this.props.record.save();

        this.state.attachments = this.state.attachments.filter(a => a.id !== id);

        const resId = this.props.record.resId;
        if (resId && attachment) {
            try {
                await this.orm.call(
                    this.props.record.resModel,
                    "message_post",
                    [[resId]],
                    {
                        body         : attachment.name,
                        message_type : "comment",
                        subtype_xmlid: "mail.mt_note",
                    }
                );
            } catch (e) {
                console.warn("[ScannerPdfField] chatter post failed on delete:", e);
            }
        }
    }

    // ── crop styles ───────────────────────────────────────────────────────────

    get cropStyles() {
        const { cropX: x, cropY: y, cropW: w, cropH: h } = this.state;
        const dark = "background:rgba(0,0,0,0.55);pointer-events:none;";
        return {
            overlayTop   : `position:absolute;top:0;left:0;right:0;height:${y}px;${dark}`,
            overlayBottom: `position:absolute;left:0;right:0;top:${y + h}px;bottom:0;${dark}`,
            overlayLeft  : `position:absolute;top:${y}px;left:0;width:${x}px;height:${h}px;${dark}`,
            overlayRight : `position:absolute;top:${y}px;left:${x + w}px;right:0;height:${h}px;${dark}`,
            cropBox      :
                `position:absolute;top:${y}px;left:${x}px;` +
                `width:${w}px;height:${h}px;` +
                `border:2px solid #0d6efd;cursor:move;box-sizing:border-box;`,
        };
    }

    getHandleStyle(handle) {
        return (
            `position:absolute;` +
            `left:calc(${handle.nx * 100}% - 8px);top:calc(${handle.ny * 100}% - 8px);` +
            `width:16px;height:16px;` +
            `background:white;border:2px solid #0d6efd;border-radius:3px;` +
            `cursor:${handle.cursor};z-index:10;box-sizing:border-box;`
        );
    }

    get cropDimensionsLabel() {
        const img = this.cropImageRef && this.cropImageRef.el;
        if (!img || !img.naturalWidth) return "";
        const rect = img.getBoundingClientRect();
        if (!rect.width) return "";
        const sx = img.naturalWidth  / rect.width;
        const sy = img.naturalHeight / rect.height;
        return `${Math.round(this.state.cropW * sx)} \xD7 ${Math.round(this.state.cropH * sy)} px`;
    }

    // ── crop mouse interaction ────────────────────────────────────────────────

    onCropImageLoad(ev) {
        const img  = ev.target;
        const rect = img.getBoundingClientRect();
        const w    = rect.width  || img.clientWidth;
        const h    = rect.height || img.clientHeight;
        const m    = Math.min(16, w * 0.03);
        this.state.cropX = m; this.state.cropY = m;
        this.state.cropW = w - m * 2; this.state.cropH = h - m * 2;
    }

    onCropBoxMouseDown(ev) {
        ev.preventDefault();
        const startX = ev.clientX, startY = ev.clientY;
        const sx = this.state.cropX, sy = this.state.cropY;
        const cw = this.state.cropW, ch = this.state.cropH;
        const rect = this.cropImageRef.el.getBoundingClientRect();
        const maxW = rect.width, maxH = rect.height;

        const onMove = (e) => {
            this.state.cropX = Math.max(0, Math.min(sx + e.clientX - startX, maxW - cw));
            this.state.cropY = Math.max(0, Math.min(sy + e.clientY - startY, maxH - ch));
        };
        const onUp = () => {
            window.removeEventListener("mousemove", onMove);
            window.removeEventListener("mouseup",   onUp);
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup",   onUp);
    }

    onCropHandleMouseDown(ev, handleId) {
        ev.preventDefault();
        ev.stopPropagation();
        const startX = ev.clientX, startY = ev.clientY;
        const sx = this.state.cropX, sy = this.state.cropY;
        const sw = this.state.cropW, sh = this.state.cropH;
        const rect = this.cropImageRef.el.getBoundingClientRect();
        const maxW = rect.width, maxH = rect.height;

        const onMove = (e) => {
            const dx = e.clientX - startX, dy = e.clientY - startY;
            let x = sx, y = sy, w = sw, h = sh;
            if (handleId.includes("n")) { y = sy + dy; h = sh - dy; }
            if (handleId.includes("s")) { h = sh + dy; }
            if (handleId.includes("w")) { x = sx + dx; w = sw - dx; }
            if (handleId.includes("e")) { w = sw + dx; }
            w = Math.max(40, w); h = Math.max(25, h);
            x = Math.max(0, Math.min(x, maxW - w));
            y = Math.max(0, Math.min(y, maxH - h));
            w = Math.min(w, maxW - x); h = Math.min(h, maxH - y);
            this.state.cropX = x; this.state.cropY = y;
            this.state.cropW = w; this.state.cropH = h;
        };
        const onUp = () => {
            window.removeEventListener("mousemove", onMove);
            window.removeEventListener("mouseup",   onUp);
        };
        window.addEventListener("mousemove", onMove);
        window.addEventListener("mouseup",   onUp);
    }

    // ── rotation ──────────────────────────────────────────────────────────────

    async onRotateLeft() {
        if (!this.state.rawImage) return;
        this.state.rawImage = await _rotateImage(this.state.rawImage, -90);
    }

    async onRotateRight() {
        if (!this.state.rawImage) return;
        this.state.rawImage = await _rotateImage(this.state.rawImage, 90);
    }

    // ── crop open / close ─────────────────────────────────────────────────────

    _openCropEditor(b64, pageIdx) {
        this.state.rawImage    = b64;
        this.state.cropPageIdx = pageIdx;
        this.state.showCropper = true;
        this.state.cropX = 0; this.state.cropY = 0;
        this.state.cropW = 100; this.state.cropH = 100;
    }

    _closeCropEditor() {
        this.state.showCropper = false;
        this.state.rawImage    = null;
        this.state.cropPageIdx = -1;
    }

    // ── crop save / discard ───────────────────────────────────────────────────

    async onApplyCrop() {
        const b64 = this.state.rawImage;
        const el  = this.cropImageRef.el;
        if (!b64 || !el) return;

        const mime    = _b64Mime(b64);
        const natural = new Image();
        await new Promise(res => {
            natural.onload = res;
            natural.src = `data:${mime};base64,` + b64;
        });

        const rect   = el.getBoundingClientRect();
        const scaleX = natural.naturalWidth  / (rect.width  || 1);
        const scaleY = natural.naturalHeight / (rect.height || 1);

        const canvas = document.createElement("canvas");
        canvas.width  = Math.round(this.state.cropW * scaleX);
        canvas.height = Math.round(this.state.cropH * scaleY);
        canvas.getContext("2d").drawImage(
            natural,
            this.state.cropX * scaleX, this.state.cropY * scaleY,
            this.state.cropW * scaleX, this.state.cropH * scaleY,
            0, 0, canvas.width, canvas.height
        );
        const croppedB64 = canvas.toDataURL("image/png").split(",")[1];
        await this._commitCroppedPage(croppedB64);
    }

    async onUseFullImage() {
        await this._commitCroppedPage(this.state.rawImage);
    }

    onCancelCrop() { this._closeCropEditor(); }

    async _commitCroppedPage(b64) {
        const compressed = await compressPageImage(b64);
        const thumb      = await this._makeThumbnail(compressed);
        const idx        = this.state.cropPageIdx;

        if (idx === -1) {
            this.state.pages.push({ id: ++this._pageIdSeq, b64: compressed, thumb });
        } else {
            this.state.pages.splice(idx, 1, Object.assign({}, this.state.pages[idx], {
                b64: compressed,
                thumb,
            }));
        }
        this._closeCropEditor();
    }

    // ── page management ───────────────────────────────────────────────────────

    async _addPage(b64) {
        const compressed = await compressPageImage(b64);
        const thumb      = await this._makeThumbnail(compressed);
        this.state.pages.push({ id: ++this._pageIdSeq, b64: compressed, thumb });
    }

    onCropPage(idx)   { this._openCropEditor(this.state.pages[idx].b64, idx); }
    onRemovePage(idx) { this.state.pages.splice(idx, 1); }

    onMovePage(idx, dir) {
        const to = idx + dir;
        if (to < 0 || to >= this.state.pages.length) return;
        const a = this.state.pages[idx];
        const b = this.state.pages[to];
        this.state.pages.splice(Math.min(idx, to), 2,
            dir < 0 ? a : b,
            dir < 0 ? b : a
        );
    }

    onClearPages() {
        this.state.pages.splice(0);
        this.state.statusMsg = "";
        this.state.errorMsg  = "";
    }

    _makeThumbnail(b64, maxW, maxH) {
        maxW = maxW === undefined ? 150 : maxW;
        maxH = maxH === undefined ? 200 : maxH;
        return new Promise((resolve) => {
            const mime = _b64Mime(b64);
            const img  = new Image();
            img.onload = () => {
                const scale  = Math.min(maxW / img.width, maxH / img.height, 1);
                const canvas = document.createElement("canvas");
                canvas.width  = Math.max(1, Math.round(img.width  * scale));
                canvas.height = Math.max(1, Math.round(img.height * scale));
                canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
                resolve(canvas.toDataURL("image/jpeg", 0.75).split(",")[1]);
            };
            img.onerror = () => resolve(b64.slice(0, 500));
            img.src = `data:${mime};base64,` + b64;
        });
    }

    // ── WebSocket helpers ─────────────────────────────────────────────────────

    _restoreOnClose(ws) {
        ws.onclose = () => {
            this.state.bridgeStatus = "disconnected";
            this._ws = null;
        };
    }

    _closeSocket() {
        if (this._ws) {
            this._ws.onclose   = null;
            this._ws.onerror   = null;
            this._ws.onmessage = null;
            try { this._ws.close(); } catch (_) {}
            this._ws = null;
        }
    }

    async _getSocket() {
        if (this._ws && this._ws.readyState === WebSocket.OPEN)
            return this._ws;

        if (this._ws) {
            this._ws.onclose   = null;
            this._ws.onerror   = null;
            this._ws.onmessage = null;
            try { this._ws.close(); } catch (_) {}
            this._ws = null;
        }

        return new Promise((resolve, reject) => {
            let settled = false;
            const settle = (fn, val) => { if (!settled) { settled = true; fn(val); } };

            const ws    = new WebSocket(BRIDGE_URL);
            const timer = setTimeout(() => {
                try { ws.close(); } catch (_) {}
                settle(reject, new Error("Cannot reach scanner bridge (connect timeout)."));
            }, CONNECT_TIMEOUT);

            ws.onopen = () => {
                clearTimeout(timer);
                this._ws = ws;
                this.state.bridgeStatus = "connected";
                this._restoreOnClose(ws);
                settle(resolve, ws);
            };
            ws.onerror = () => {
                clearTimeout(timer);
                this.state.bridgeStatus = "disconnected";
                settle(reject, new Error("Cannot connect to scanner bridge."));
            };
        });
    }

    async _pingBridge() {
        try {
            const ws = await this._getSocket();
            await new Promise((resolve) => {
                const tid  = setTimeout(resolve, 2000);
                const prev = ws.onmessage;
                ws.onmessage = (ev) => {
                    clearTimeout(tid);
                    ws.onmessage = prev;
                    try {
                        const d = JSON.parse(ev.data);
                        if (d.status === "ok") this.state.bridgeStatus = "connected";
                    } catch (_) {}
                    resolve();
                };
                ws.send(JSON.stringify({ action: "ping" }));
            });
        } catch (_) { this.state.bridgeStatus = "disconnected"; }
    }

    // ── save as PDF ───────────────────────────────────────────────────────────

    onSavePdf() {
        if (!this.state.pages.length || this.state.saving) return;
        const today = new Date().toISOString().slice(0, 10);
        this.state.pendingFilename    = `Scanned_Document_${today}`;
        this.state.showFilenameDialog = true;
    }

    onFilenameConfirm() {
        const name = (this.state.pendingFilename || "").trim();
        if (!name) return;
        const final = name.endsWith(".pdf") ? name : name + ".pdf";
        this.state.showFilenameDialog = false;
        this._doSavePdf(final);
    }

    onFilenameCancel() {
        this.state.showFilenameDialog = false;
    }

    async _doSavePdf(filename) {
        const pageCount = this.state.pages.length;
        this.state.saving    = true;
        this.state.errorMsg  = "";
        this.state.statusMsg = "Connecting to bridge\u2026";

        try {
            const ws = await this._getSocket();
            this.state.statusMsg =
                `Generating PDF (${pageCount} page${pageCount > 1 ? "s" : ""})\u2026`;

            const pdfB64 = await new Promise((resolve, reject) => {
                const timer = setTimeout(
                    () => reject(new Error(`PDF generation timed out (${PDF_TIMEOUT / 1000} s).`)),
                    PDF_TIMEOUT
                );

                const cleanup = (restoreClose) => {
                    restoreClose = restoreClose === undefined ? true : restoreClose;
                    clearTimeout(timer);
                    ws.onmessage = null;
                    ws.onerror   = null;
                    if (restoreClose) this._restoreOnClose(ws);
                };

                ws.onclose = () => {
                    cleanup(false);
                    this._ws = null;
                    this.state.bridgeStatus = "disconnected";
                    reject(new Error("Bridge closed the connection while generating PDF."));
                };
                ws.onmessage = (ev) => {
                    let msg;
                    try { msg = JSON.parse(ev.data); } catch (_) { return; }
                    cleanup();
                    if (msg.status === "ok" && msg.pdf) resolve(msg.pdf);
                    else reject(new Error(msg.message || "PDF generation failed."));
                };
                ws.onerror = () => {
                    cleanup();
                    reject(new Error("WebSocket error during PDF generation."));
                };
                ws.send(JSON.stringify({
                    action: "make_pdf",
                    images: this.state.pages.map(p => p.b64),
                }));
            });

            this.state.statusMsg = "Saving attachment\u2026";

            const [attachId] = await this.orm.create("ir.attachment", [{
                name     : filename,
                type     : "binary",
                datas    : pdfB64,
                mimetype : "application/pdf",
                res_model: this.props.record.resModel,
                res_id   : this.props.record.resId || 0,
            }]);

            await this.props.record.update({
                [this.props.name]: this._currentIds.concat([attachId]),
            });

            // ── persist M2M link to the database immediately ──────────────
            await this.props.record.save();

            this.state.attachments = this.state.attachments.concat([
                { id: attachId, name: filename, create_date: new Date() },
            ]);

            // ── log to chatter ────────────────────────────────────────────
            const resId = this.props.record.resId;
            if (resId) {
                try {
                    await this.orm.call(
                        this.props.record.resModel,
                        "message_post",
                        [[resId]],
                        {
                            body: filename,
                            attachment_ids: [attachId],
                            message_type : "comment",
                            subtype_xmlid: "mail.mt_note",
                        }
                    );
                } catch (e) {
                    console.warn("[ScannerPdfField] chatter post failed:", e);
                }
            }

            this.state.previewAttachId = attachId;
            this.state.previewFilename = filename;
            this.state.showPreview     = true;

            this.state.pages.splice(0);
            this.state.statusMsg =
                `\u2713 PDF saved \u2014 ${pageCount} page${pageCount > 1 ? "s" : ""}`;
            setTimeout(() => { this.state.statusMsg = ""; }, 4000);

        } catch (e) {
            this.state.errorMsg  = e.message;
            this.state.statusMsg = "";
        } finally {
            this.state.saving = false;
        }
    }

    async _loadExistingAttachments() {
        const resId    = this.props.record.resId;
        const resModel = this.props.record.resModel;
        if (!resId) return;

        try {
                const attachments = await this.orm.searchRead(
                    "ir.attachment",
                    [
                        ["res_model", "=", resModel],
                        ["res_id",    "=", resId],
                    ],
                    ["id", "name", "mimetype", "create_date"],
                    { order: "id asc" }
                );
                this.state.attachments = attachments.map(r => ({
                    id         : r.id,
                    name       : r.name,
                    create_date: r.create_date ? new Date(r.create_date) : null,
                }));
        } catch (e) {
            console.warn("[ScannerPdfField] _loadExistingAttachments failed:", e);
        }
    }

    // ── direct file upload ────────────────────────────────────────────────────

    onDirectUploadClick() {
        if (this.fileDirectRef.el) this.fileDirectRef.el.click();
    }

    async onDirectFileChange(ev) {
        const files = Array.from(ev.target.files || []);
        if (!files.length) return;

        this.state.uploading = true;
        this.state.errorMsg  = "";
        this.state.statusMsg =
            `Uploading ${files.length} file${files.length > 1 ? "s" : ""}\u2026`;

        try {
            const newIds     = this._currentIds.slice();
            const newEntries = [];

            for (let i = 0; i < files.length; i++) {
                const file = files[i];
                this.state.statusMsg =
                    `Uploading "${file.name}" (${i + 1} / ${files.length})\u2026`;

                const b64 = await new Promise((resolve, reject) => {
                    const fr   = new FileReader();
                    fr.onload  = (e) => resolve(e.target.result.split(",")[1]);
                    fr.onerror = reject;
                    fr.readAsDataURL(file);
                });

                const [attachId] = await this.orm.create("ir.attachment", [{
                    name     : file.name,
                    type     : "binary",
                    datas    : b64,
                    mimetype : file.type || "application/octet-stream",
                    res_model: this.props.record.resModel,
                    res_id   : this.props.record.resId || 0,
                }]);

                newIds.push(attachId);
                newEntries.push({ id: attachId, name: file.name, create_date: new Date() });
            }

            await this.props.record.update({ [this.props.name]: newIds });

            // ── persist M2M link to the database immediately ──────────────
            await this.props.record.save();

            this.state.attachments = this.state.attachments.concat(newEntries);

            // ── log to chatter ────────────────────────────────────────────
            const resId = this.props.record.resId;
            if (resId) {
                try {
                    await this.orm.call(
                        this.props.record.resModel,
                        "message_post",
                        [[resId]],
                        {
                            body: newEntries.map(e => e.name).join(", "),
                            attachment_ids: newEntries.map(e => e.id),
                            message_type : "comment",
                            subtype_xmlid: "mail.mt_note",
                        }
                    );
                } catch (e) {
                    console.warn("[ScannerPdfField] chatter post failed:", e);
                }
            }

            this.state.statusMsg =
                `\u2713 ${files.length} file${files.length > 1 ? "s" : ""} uploaded`;
            setTimeout(() => { this.state.statusMsg = ""; }, 3000);

        } catch (e) {
            this.state.errorMsg  = e.message;
            this.state.statusMsg = "";
        } finally {
            this.state.uploading = false;
            ev.target.value = "";
        }
    }


    // ── scan button handlers ──────────────────────────────────────────────────

    onScanFlatbed() { this._openScannerPicker("flatbed"); }
    onScanAdf()     { this._openScannerPicker("adf"); }

    async _doScanFlatbed(scannerName) {
        scannerName = scannerName === undefined ? null : scannerName;
        if (this.state.scanning) return;
        this.state.scanning   = true;
        this.state.scanSource = "flatbed";
        this.state.errorMsg   = "";
        this.state.statusMsg  = "Connecting to scanner bridge\u2026";

        try {
            const ws = await this._getSocket();
            this.state.statusMsg = scannerName
                ? `Waiting for "${scannerName}"\u2026`
                : "Waiting for default scanner\u2026";

            const imageB64 = await new Promise((resolve, reject) => {
                const timer = setTimeout(
                    () => reject(new Error("Scan timed out (30 s).")),
                    SCAN_TIMEOUT
                );

                const cleanup = (restoreClose) => {
                    restoreClose = restoreClose === undefined ? true : restoreClose;
                    clearTimeout(timer);
                    ws.onmessage = null;
                    ws.onerror   = null;
                    if (restoreClose) this._restoreOnClose(ws);
                };

                ws.onclose = () => {
                    cleanup(false);
                    this._ws = null;
                    this.state.bridgeStatus = "disconnected";
                    reject(new Error("Bridge closed the connection during scan."));
                };
                ws.onmessage = (ev) => {
                    let msg;
                    try { msg = JSON.parse(ev.data); } catch (_) { return; }
                    if (msg.status === "scanning") {
                        this.state.statusMsg = msg.message || "Scanning\u2026";
                        return;
                    }
                    cleanup();
                    if (msg.status === "ok" && msg.image) resolve(msg.image);
                    else reject(new Error(msg.message || "Scan failed."));
                };
                ws.onerror = () => {
                    cleanup();
                    reject(new Error("WebSocket error during scan."));
                };
                ws.send(JSON.stringify({ action: "scan", scanner: scannerName }));
            });

            this._openCropEditor(imageB64, -1);
            this.state.statusMsg = "Adjust crop then click Apply";

        } catch (e) {
            this.state.errorMsg  = e.message;
            this.state.statusMsg = "";
        } finally {
            this.state.scanning   = false;
            this.state.scanSource = "";
        }
    }

    async _doScanAdf(scannerName) {
        scannerName = scannerName === undefined ? null : scannerName;
        if (this.state.scanning) return;
        this.state.scanning   = true;
        this.state.scanSource = "adf";
        this.state.errorMsg   = "";
        this.state.statusMsg  = "Connecting to scanner bridge\u2026";

        try {
            const ws = await this._getSocket();
            this.state.statusMsg = scannerName
                ? `Loading feeder on "${scannerName}"\u2026`
                : "Loading document feeder \u2014 please wait\u2026";

            const images = await new Promise((resolve, reject) => {
                const timer = setTimeout(
                    () => reject(new Error("ADF scan timed out (2 min).")),
                    ADF_TIMEOUT
                );

                const cleanup = (restoreClose) => {
                    restoreClose = restoreClose === undefined ? true : restoreClose;
                    clearTimeout(timer);
                    ws.onmessage = null;
                    ws.onerror   = null;
                    if (restoreClose) this._restoreOnClose(ws);
                };

                ws.onclose = () => {
                    cleanup(false);
                    this._ws = null;
                    this.state.bridgeStatus = "disconnected";
                    reject(new Error("Bridge closed the connection during ADF scan."));
                };
                ws.onmessage = (ev) => {
                    let msg;
                    try { msg = JSON.parse(ev.data); } catch (_) { return; }
                    if (msg.status === "scanning") {
                        this.state.statusMsg = msg.message || "Scanning feeder\u2026";
                        return;
                    }
                    cleanup();
                    if (msg.status === "ok" && msg.images) resolve(msg.images);
                    else reject(new Error(msg.message || "ADF scan failed."));
                };
                ws.onerror = () => {
                    cleanup();
                    reject(new Error("WebSocket error during ADF scan."));
                };
                ws.send(JSON.stringify({ action: "scan_adf", scanner: scannerName }));
            });

            const total = images.length;
            for (let i = 0; i < total; i++) {
                this.state.statusMsg = `Processing page ${i + 1} of ${total}\u2026`;
                await this._addPage(images[i]);
            }

            this.state.statusMsg =
                `\u2713 ${total} page${total > 1 ? "s" : ""} scanned from feeder`;
            setTimeout(() => { this.state.statusMsg = ""; }, 4000);

        } catch (e) {
            this.state.errorMsg  = e.message;
            this.state.statusMsg = "";
        } finally {
            this.state.scanning   = false;
            this.state.scanSource = "";
        }
    }

    onUploadClick() {
        if (this.fileInputRef.el) this.fileInputRef.el.click();
    }

    async onFileChange(ev) {
        const files = Array.from(ev.target.files || []);
        if (!files.length) return;
        this.state.errorMsg = "";

        const readFile = (file) => new Promise((resolve, reject) => {
            const fr   = new FileReader();
            fr.onload  = (e) => resolve(e.target.result.split(",")[1]);
            fr.onerror = reject;
            fr.readAsDataURL(file);
        });

        if (files.length === 1) {
            const b64 = await readFile(files[0]);
            this._openCropEditor(b64, -1);
        } else {
            for (let i = 0; i < files.length; i++) {
                this.state.statusMsg = `Adding image ${i + 1} of ${files.length}\u2026`;
                const b64 = await readFile(files[i]);
                await this._addPage(b64);
            }
            this.state.statusMsg = `\u2713 ${files.length} images added`;
            setTimeout(() => { this.state.statusMsg = ""; }, 3000);
        }

        ev.target.value = "";
    }
}


ScannerPdfField.template = "scanner_integration.ScannerPdfField";
ScannerPdfField.props = { ...standardFieldProps };

const scannerPdfField = {
    component: ScannerPdfField,
    supportedTypes: ["many2many"],
    isEmpty: () => false,
    fieldDependencies: [],
    // optional if you later want Odoo to preload attachment fields:
     relatedFields: [
         { name: "name", type: "char" },
         { name: "mimetype", type: "char" },
         { name: "create_date", type: "datetime" },
     ],
};


registry.category("fields").add("scanner_image", scannerImageField);
registry.category("fields").add("scanner_pdf", scannerPdfField);