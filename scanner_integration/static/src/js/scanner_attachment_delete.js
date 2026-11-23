/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";
import { _t } from "@web/core/l10n/translation";
import { ScannerPdfField } from "@scanner_integration/js/scanner_image_widget";

patch(ScannerPdfField.prototype, {
    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
        this.dialog = useService("dialog");
        this.state.removing = false;
    },

    /** Also block the toolbar while a delete is running */
    get isBusy() {
        return super.isBusy || this.state.removing;
    },

    /** Trash button → ask first, then delete */
    onRemoveAttachment(attId) {
        const att = (this.currentAttachments || []).find((a) => a.id === attId);
        this.dialog.add(ConfirmationDialog, {
            title: _t("Delete document"),
            body: _t(
                "“%s” will be permanently deleted. This cannot be undone.",
                att?.name || `#${attId}`
            ),
            confirmLabel: _t("Delete"),
            confirm: () => this._deleteAttachment(attId),
            cancel: () => {},
        });
    },

    async _deleteAttachment(attId) {
        if (this.state.removing) {
            return;
        }
        this.state.removing = true;
        this.state.errorMsg = "";
        this.state.statusMsg = "";
        const record = this.props.record;
        try {
            // 1) Remove the link from the many2many field
            const list = record.data[this.props.name];
            const line = list?.records?.find((r) => r.resId === attId);
            if (line) {
                await list.forget(line);
            }

            // 2) Save the record so the link is removed in the database
            if (!record.isNew) {
                const saved = await record.save();
                if (saved === false) {
                    throw new Error(
                        _t("The record could not be saved (check the required fields). The document was not deleted.")
                    );
                }
            }

            // 3) Delete the ir.attachment row itself
            await this.orm.unlink("ir.attachment", [attId]);

            this.state.statusMsg = _t("Document deleted.");
        } catch (e) {
            this.state.errorMsg = e?.data?.message || e?.message || String(e);
            // Reload the form from the database if something failed part-way
            if (!record.isNew) {
                try {
                    await record.load();
                } catch {
                    // ignore
                }
            }
        } finally {
            this.state.removing = false;
        }
    },
});