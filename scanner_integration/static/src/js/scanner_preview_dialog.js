/** @odoo-module **/
import { Component } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";

/**
 * Full-screen attachment preview opened with:
 *   this.dialog.add(ScannerPreviewDialog, { name, url, downloadUrl, isPdf })
 * The dialog service supplies `close` and handles Esc / the header ✕.
 */
export class ScannerPreviewDialog extends Component {
    static template = "scanner_integration.PreviewDialog";
    static components = { Dialog };
    static props = {
        close: Function,
        name: String,
        url: String,
        downloadUrl: String,
        isPdf: Boolean,
    };
}