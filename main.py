import wx
import wx.lib.agw.aui as aui
import os
import json
import base64
from pathlib import Path
import tempfile
import subprocess
import threading
import io
import shutil  # For rmtree if send2trash is not used

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

try:
    from mistralai import Mistral
except ImportError:
    Mistral = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from send2trash import send2trash
except ImportError:
    send2trash = None
    wx.LogWarning(
        "send2trash library not found. Deletion will be permanent. Install with 'pip install send2trash' for safer deletion."
    )


# --- OCR Helper Functions (adapted and refined) ---
# ... (These helper functions: _get_mistral_api_key, _image_to_pdf, _encode_pdf_to_base64,
#      _request_mistral_ocr, _stitch_ocr_data remain largely the same as in the previous full version.
#      For brevity, I will assume they are present and correct from the prior version.)
def _get_mistral_api_key():
    return os.environ.get("MISTRAL_API_KEY")


def _image_to_pdf(input_image_path: Path, output_pdf_path: Path, status_callback=None):
    try:
        if status_callback:
            wx.CallAfter(
                status_callback, f"Converting {input_image_path.name} to PDF..."
            )
        if not PILImage:
            wx.CallAfter(
                wx.LogError, "Pillow library is not available to convert image to PDF."
            )
            return None
        with PILImage.open(input_image_path) as img:
            if img.mode == "P":
                img = img.convert("RGBA" if "A" in img.getbands() else "RGB")
            elif img.mode == "L":
                img = img.convert("RGB")
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            img.save(output_pdf_path, "PDF", resolution=100.0, save_all=False)
        if status_callback:
            wx.CallAfter(
                status_callback,
                f"Converted {input_image_path.name} to PDF: {output_pdf_path.name}",
            )
        return output_pdf_path
    except Exception as e:
        wx.CallAfter(
            wx.LogError, f"Error converting {input_image_path.name} to PDF: {e}"
        )
        if status_callback:
            wx.CallAfter(
                status_callback, f"Failed to convert {input_image_path.name} to PDF."
            )
        return None


def _encode_pdf_to_base64(pdf_path: Path, status_callback=None):
    try:
        if status_callback:
            wx.CallAfter(status_callback, f"Encoding {pdf_path.name} to base64...")
        with open(pdf_path, "rb") as pdf_file:
            encoded = base64.b64encode(pdf_file.read()).decode("utf-8")
        if status_callback:
            wx.CallAfter(status_callback, f"Encoded {pdf_path.name}.")
        return encoded
    except Exception as e:
        wx.CallAfter(wx.LogError, f"Error encoding PDF {pdf_path.name}: {e}")
        if status_callback:
            wx.CallAfter(status_callback, f"Encoding failed for {pdf_path.name}")
        return None


def _request_mistral_ocr(
    mistral_client, pdf_path: Path, base64_pdf: str, status_callback=None
):
    if not mistral_client:
        wx.CallAfter(wx.LogError, "Mistral client not initialized.")
        return None
    if not base64_pdf:
        return None
    try:
        if status_callback:
            wx.CallAfter(
                status_callback, f"Requesting Mistral OCR for {pdf_path.name}..."
            )
        ocr_response = mistral_client.ocr.process(
            model="mistral-ocr-latest",
            document={
                "type": "document_url",
                "document_url": f"data:application/pdf;base64,{base64_pdf}",
            },
            include_image_base64=True,
        )
        if status_callback:
            wx.CallAfter(status_callback, f"OCR received for {pdf_path.name}.")
        return ocr_response
    except Exception as e:
        wx.CallAfter(wx.LogError, f"Error during Mistral OCR for {pdf_path.name}: {e}")
    return None


def _stitch_ocr_data(
    json_files: list[Path],
    stitched_md_path: Path,
    images_base_output_dir: Path,
    status_callback=None,
):
    images_base_output_dir.mkdir(parents=True, exist_ok=True)
    stitched_md_path.parent.mkdir(parents=True, exist_ok=True)
    image_file_counter = 0
    with open(stitched_md_path, "w", encoding="utf-8") as stitched_md_file:
        for i, json_file_path in enumerate(json_files):
            if status_callback:
                wx.CallAfter(
                    status_callback,
                    f"Stitching {json_file_path.name} ({i + 1}/{len(json_files)})...",
                )
            try:
                with open(json_file_path, "r", encoding="utf-8") as jf:
                    ocr_data_obj = json.load(jf)
                if not isinstance(ocr_data_obj, dict):
                    wx.CallAfter(
                        wx.LogWarning, f"Skipping non-dict JSON: {json_file_path.name}"
                    )
                    continue
                for page_idx, page_data in enumerate(ocr_data_obj.get("pages", [])):
                    markdown_content = page_data.get("markdown")
                    if not markdown_content:
                        continue
                    for img_idx, image_info in enumerate(page_data.get("images", [])):
                        original_image_id = image_info.get("id")
                        base64_data_uri = image_info.get("image_base64")
                        if not original_image_id or not base64_data_uri:
                            continue
                        try:
                            image_type = "jpeg"
                            actual_encoded_data = ""
                            if "," in base64_data_uri:
                                header, b64_data = base64_data_uri.split(",", 1)
                                actual_encoded_data = b64_data
                                if header.startswith("data:image/"):
                                    potential_type = header.split(";")[0].split("/")[-1]
                                    if potential_type.isalnum():
                                        image_type = potential_type
                            else:
                                actual_encoded_data = base64_data_uri
                            image_bytes = base64.b64decode(actual_encoded_data)
                            new_image_filename = (
                                f"image_{image_file_counter:010d}.{image_type}"
                            )
                            image_actual_output_path = (
                                images_base_output_dir / new_image_filename
                            )
                            with open(image_actual_output_path, "wb") as img_f:
                                img_f.write(image_bytes)
                            relative_image_path = Path(
                                os.path.relpath(
                                    image_actual_output_path, stitched_md_path.parent
                                )
                            ).as_posix()
                            markdown_content = markdown_content.replace(
                                f"![]({original_image_id})",
                                f"![]({relative_image_path})",
                            )
                            markdown_content = markdown_content.replace(
                                f"![{original_image_id}]({original_image_id})",
                                f"![]({relative_image_path})",
                            )
                            image_file_counter += 1
                        except Exception as e:
                            wx.CallAfter(
                                wx.LogError,
                                f"Img proc. error {original_image_id} in {json_file_path.name}: {e}",
                            )
                    stitched_md_file.write(markdown_content + "\n\n")
            except Exception as e:
                wx.CallAfter(
                    wx.LogError, f"Stitching error for {json_file_path.name}: {e}"
                )
    if status_callback:
        wx.CallAfter(status_callback, f"Stitched markdown: {stitched_md_path}")


class ImageFrame(wx.Frame):
    def __init__(self, parent, title):
        super(ImageFrame, self).__init__(parent, title=title, size=(1200, 800))
        self.panel = wx.Panel(self)
        self.current_opened_folder_path = None
        self.focused_item_path: Path | None = None  # For single item preview
        self.all_selected_item_paths: list[Path] = []  # For multi-item operations

        self.supported_preview_formats = [
            ".png",
            ".jpg",
            ".jpeg",
            ".bmp",
            ".gif",
            ".jp2",
            ".json",
            ".md",
            ".pdf",
        ]
        self.supported_ocr_input_formats = [
            ".png",
            ".jpg",
            ".jpeg",
            ".bmp",
            ".gif",
            ".jp2",
        ]

        self.mistral_api_key = _get_mistral_api_key()
        self.mistral_client = None
        if self.mistral_api_key and Mistral:
            try:
                self.mistral_client = Mistral(api_key=self.mistral_api_key)
            except Exception as e:
                wx.LogError(f"Failed to init Mistral client: {e}")

        menubar = wx.MenuBar()
        fileMenu = wx.Menu()
        open_folder_menu_item = fileMenu.Append(wx.ID_OPEN, "&Open Folder\tCtrl+O")
        exit_item = fileMenu.Append(wx.ID_EXIT, "&Exit\tCtrl+Q")
        menubar.Append(fileMenu, "&File")
        self.SetMenuBar(menubar)
        self.CreateStatusBar(2)
        self.progress_bar = wx.Gauge(
            self, range=100, style=wx.GA_HORIZONTAL | wx.GA_SMOOTH
        )

        self.image_list = wx.ImageList(16, 16)
        self.folder_icon_idx = self.image_list.Add(
            wx.ArtProvider.GetBitmap(wx.ART_FOLDER, wx.ART_OTHER, (16, 16))
        )
        self.folder_open_icon_idx = self.image_list.Add(
            wx.ArtProvider.GetBitmap(wx.ART_FOLDER_OPEN, wx.ART_OTHER, (16, 16))
        )
        self.file_icon_idx = self.image_list.Add(
            wx.ArtProvider.GetBitmap(wx.ART_NORMAL_FILE, wx.ART_OTHER, (16, 16))
        )

        self.splitter = wx.SplitterWindow(self.panel, style=wx.SP_LIVE_UPDATE)
        # Use wx.TR_EXTENDED for multi-selection like standard file explorers
        self.tree_ctrl = wx.TreeCtrl(
            self.splitter,
            style=wx.TR_MULTIPLE | wx.TR_HAS_BUTTONS | wx.TR_LINES_AT_ROOT,
        )
        self.tree_ctrl.AssignImageList(self.image_list)

        self.preview_panel = wx.Panel(self.splitter)
        self.preview_sizer = wx.BoxSizer(wx.VERTICAL)
        self.image_preview_ctrl = wx.StaticBitmap(
            self.preview_panel, bitmap=wx.Bitmap(1, 1)
        )
        self.text_preview_ctrl = wx.TextCtrl(
            self.preview_panel, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2
        )
        self.preview_sizer.Add(self.image_preview_ctrl, 1, wx.EXPAND)
        self.preview_sizer.Add(self.text_preview_ctrl, 1, wx.EXPAND)
        self.text_preview_ctrl.Hide()
        self.preview_panel.SetSizer(self.preview_sizer)
        self.splitter.SplitVertically(self.tree_ctrl, self.preview_panel, 300)

        vbox_main_layout = wx.BoxSizer(wx.VERTICAL)
        vbox_main_layout.Add(self.splitter, 1, wx.EXPAND | wx.ALL, 5)
        hbox_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.open_folder_button = wx.Button(self.panel, label="Open Folder")
        self.run_ocr_button = wx.Button(self.panel, label="Run OCR on Selected")
        self.stitch_pdf_button = wx.Button(self.panel, label="Stitch All & Create PDF")
        self.delete_button = wx.Button(
            self.panel, label="Delete Selected"
        )  # New Delete Button
        hbox_buttons.Add(self.open_folder_button, 0, wx.ALL, 5)
        hbox_buttons.Add(self.run_ocr_button, 0, wx.ALL, 5)
        hbox_buttons.Add(self.stitch_pdf_button, 0, wx.ALL, 5)
        hbox_buttons.Add(self.delete_button, 0, wx.ALL, 5)  # Add to sizer
        vbox_main_layout.Add(hbox_buttons, 0, wx.ALIGN_CENTER | wx.TOP | wx.BOTTOM, 5)
        self.panel.SetSizer(vbox_main_layout)

        frame_sizer = wx.BoxSizer(wx.VERTICAL)
        frame_sizer.Add(self.panel, 1, wx.EXPAND)
        frame_sizer.Add(self.progress_bar, 0, wx.EXPAND | wx.ALL, 5)
        self.SetSizerAndFit(frame_sizer)

        self.fs_watcher = None
        self.Bind(wx.EVT_MENU, self.on_open_folder_selected, open_folder_menu_item)
        self.Bind(wx.EVT_MENU, self.on_exit, exit_item)
        self.tree_ctrl.Bind(wx.EVT_TREE_SEL_CHANGED, self.on_tree_selection_changed)
        self.open_folder_button.Bind(wx.EVT_BUTTON, self.on_open_folder_selected)
        self.run_ocr_button.Bind(wx.EVT_BUTTON, self.on_run_ocr_button_clicked)
        self.stitch_pdf_button.Bind(wx.EVT_BUTTON, self.on_stitch_button_clicked)
        self.delete_button.Bind(
            wx.EVT_BUTTON, self.on_delete_button_clicked
        )  # Bind delete button

        self.Centre()
        self.Show()
        self.update_button_states()
        if not self.mistral_client:
            self.SetStatusText("Mistral client/key missing. OCR disabled.", 0)

    def update_status(self, message, field=0):
        self.SetStatusText(message, field)

    def update_progress(self, value):
        self.progress_bar.SetValue(min(max(0, value), 100))  # Ensure value is 0-100

    def on_open_folder_selected(self, event):
        with wx.DirDialog(
            self,
            "Choose a directory:",
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            new_folder_path = Path(dlg.GetPath())
            if self.current_opened_folder_path == new_folder_path:
                return
            self.current_opened_folder_path = new_folder_path
            self.populate_tree_and_watch(self.current_opened_folder_path)

    def _ensure_fs_watcher(self):
        if self.fs_watcher is None:
            self.fs_watcher = wx.FileSystemWatcher()
            self.fs_watcher.SetOwner(self)
            self.Bind(wx.EVT_FSWATCHER, self.on_fs_event)

    def populate_tree_and_watch(self, root_path: Path):
        self._ensure_fs_watcher()
        if self.fs_watcher:
            self.fs_watcher.RemoveAll()
        self.tree_ctrl.DeleteAllItems()
        self.all_selected_item_paths.clear()
        self.focused_item_path = None
        print(f"DEBUG: on_tree_selection_changed triggered.")

        if not root_path or not root_path.is_dir():
            self.current_opened_folder_path = None
            self.update_button_states()
            self.clear_preview()
            return

        self.tree_root_item = self.tree_ctrl.AddRoot(root_path.name)
        self.tree_ctrl.SetItemData(self.tree_root_item, str(root_path))
        self.tree_ctrl.SetItemImage(
            self.tree_root_item, self.folder_icon_idx, wx.TreeItemIcon_Normal
        )
        self.tree_ctrl.SetItemImage(
            self.tree_root_item, self.folder_open_icon_idx, wx.TreeItemIcon_Expanded
        )
        self._add_tree_items_recursive(self.tree_root_item, root_path)
        self.tree_ctrl.Expand(self.tree_root_item)
        if self.fs_watcher and not self.fs_watcher.AddTree(str(root_path)):
            wx.LogWarning(f"Could not start watching folder: {root_path}")
        self.clear_preview()
        self.update_button_states()

    def _add_tree_items_recursive(self, parent_tree_item, parent_os_path: Path):
        try:
            entries = sorted(
                [parent_os_path / entry for entry in os.listdir(parent_os_path)],
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError:
            return
        for item_os_path in entries:
            entry_name = item_os_path.name
            if item_os_path.is_dir():
                child_item = self.tree_ctrl.AppendItem(parent_tree_item, entry_name)
                self.tree_ctrl.SetItemData(child_item, str(item_os_path))
                self.tree_ctrl.SetItemImage(
                    child_item, self.folder_icon_idx, wx.TreeItemIcon_Normal
                )
                self.tree_ctrl.SetItemImage(
                    child_item, self.folder_open_icon_idx, wx.TreeItemIcon_Expanded
                )
                self._add_tree_items_recursive(child_item, item_os_path)
            elif (
                item_os_path.is_file()
                and item_os_path.suffix.lower() in self.supported_preview_formats
            ):
                child_item = self.tree_ctrl.AppendItem(parent_tree_item, entry_name)
                self.tree_ctrl.SetItemData(child_item, str(item_os_path))
                self.tree_ctrl.SetItemImage(
                    child_item, self.file_icon_idx, wx.TreeItemIcon_Normal
                )

    def on_fs_event(self, event):
        if self.current_opened_folder_path and self.current_opened_folder_path.exists():
            wx.CallAfter(self.refresh_tree_due_to_fs_event)
        elif (
            self.current_opened_folder_path
            and not self.current_opened_folder_path.exists()
        ):
            wx.CallAfter(self.handle_root_folder_disappeared)

    def refresh_tree_due_to_fs_event(self):  # Renamed for clarity
        if self.current_opened_folder_path and self.current_opened_folder_path.exists():
            # Selections are typically lost on full repopulation, which is acceptable.
            self.populate_tree_and_watch(self.current_opened_folder_path)
        else:
            self.handle_root_folder_disappeared()

    def handle_root_folder_disappeared(self):
        if self.fs_watcher:
            self.fs_watcher.RemoveAll()
        self.tree_ctrl.DeleteAllItems()
        self.current_opened_folder_path = None
        self.all_selected_item_paths.clear()
        self.focused_item_path = None
        self.clear_preview()
        self.update_button_states()
        self.SetStatusText("Monitored folder disappeared or is inaccessible.", 0)
        wx.MessageBox(
            "The previously opened folder is no longer accessible.",
            "Folder Inaccessible",
            wx.OK | wx.ICON_WARNING,
        )

    def clear_preview(self):
        self.image_preview_ctrl.SetBitmap(wx.Bitmap(1, 1))
        self.text_preview_ctrl.SetValue("")
        self.image_preview_ctrl.Show()
        self.text_preview_ctrl.Hide()
        self.preview_panel.Layout()

    def on_tree_selection_changed(self, event):  # Renamed from on_tree_item_selected
        self.all_selected_item_paths.clear()
        self.focused_item_path = None
        print(f"DEBUG: on_tree_selection_changed triggered.")

        selected_tree_items_ids = (
            self.tree_ctrl.GetSelections()
        )  # Returns a list of item IDs
        print(
            f"DEBUG: Number of items from GetSelections(): {len(selected_tree_items_ids)}"
        )

        for item_id in selected_tree_items_ids:
            item_path_str = self.tree_ctrl.GetItemData(item_id)
            if item_path_str:
                self.all_selected_item_paths.append(Path(item_path_str))
        print(f"DEBUG: all_selected_item_paths: {self.all_selected_item_paths}")

        # Determine which item to preview
        # The event.GetItem() gives the item whose selection state *just changed*
        # This is often the best candidate for the "focused" item in a multi-select scenario.
        preview_candidate_item_id = event.GetItem()

        if preview_candidate_item_id.IsOk():
            focused_item_path_str = self.tree_ctrl.GetItemData(
                preview_candidate_item_id
            )
            print(
                f"DEBUG: Event item (preview candidate) path string: {focused_item_path_str}"
            )
            if focused_item_path_str:
                path_obj = Path(focused_item_path_str)
                if path_obj.is_file():
                    self.focused_item_path = path_obj
                    print(
                        f"DEBUG: Calling display_preview for event item: {self.focused_item_path}"
                    )
                    self.display_preview(self.focused_item_path)
                else:  # Event item is a folder
                    print(f"DEBUG: Event item is a folder: {path_obj.name}")
                    self.clear_preview()
                    self.text_preview_ctrl.SetValue(f"Folder selected: {path_obj.name}")
                    self.image_preview_ctrl.Hide()
                    self.text_preview_ctrl.Show()
                    self.preview_panel.Layout()
            else:
                print(f"DEBUG: Event item has no path data. Clearing preview.")
                self.clear_preview()  # Fallback if event item has no data
        elif (
            self.all_selected_item_paths
        ):  # Fallback if event.GetItem() is not valid, use first selected
            print(
                f"DEBUG: Event item not OK. Checking all_selected_item_paths for preview."
            )
            # Preview the first valid file from the list of all selected items
            for path_obj in self.all_selected_item_paths:
                if (
                    path_obj.is_file()
                    and path_obj.suffix.lower() in self.supported_preview_formats
                ):
                    self.focused_item_path = (
                        path_obj  # Still set focused_item_path for consistency
                    )
                    print(
                        f"DEBUG: Calling display_preview for first suitable file from multi-select: {self.focused_item_path}"
                    )
                    self.display_preview(self.focused_item_path)
                    break
            else:  # No previewable file among selected items
                print(f"DEBUG: No previewable file found in multi-selection.")
                self.clear_preview()

        self.update_button_states()

    def display_preview(self, file_path: Path):
        # ... (display_preview logic remains largely the same as in the previous full version)
        #      It takes a single file_path and updates the preview controls.
        self.image_preview_ctrl.Hide()
        self.text_preview_ctrl.Hide()
        ext = file_path.suffix.lower()
        try:
            if ext in self.supported_ocr_input_formats:
                if not PILImage:
                    wx.LogError("Pillow not loaded.")
                    return
                loaded_image = None
                if ext == ".jp2":
                    pil_img = PILImage.open(file_path)
                    pil_img.load()
                    if pil_img.mode not in ["RGB", "RGBA"]:
                        pil_img = pil_img.convert(
                            "RGBA" if "A" in pil_img.getbands() else "RGB"
                        )
                    loaded_image = wx.Image(pil_img.width, pil_img.height)
                    loaded_image.SetData(pil_img.convert("RGB").tobytes())
                    if pil_img.mode == "RGBA":
                        loaded_image.SetAlpha(pil_img.getchannel("A").tobytes())
                else:
                    loaded_image = wx.Image(str(file_path), wx.BITMAP_TYPE_ANY)
                if not loaded_image or not loaded_image.IsOk():
                    raise RuntimeError(f"Failed to load: {file_path.name}")
                img_width, img_height = (
                    loaded_image.GetWidth(),
                    loaded_image.GetHeight(),
                )
                panel_w, panel_h = self.preview_panel.GetClientSize()
                panel_w, panel_h = max(panel_w, 50), max(panel_h, 50)
                img_aspect = img_width / img_height if img_height > 0 else 1
                panel_aspect = panel_w / panel_h if panel_h > 0 else 1
                if img_aspect > panel_aspect:
                    new_width, new_height = (
                        panel_w,
                        int(panel_w / img_aspect) if img_aspect != 0 else panel_h,
                    )
                else:
                    new_height, new_width = (
                        panel_h,
                        int(panel_h * img_aspect) if panel_h != 0 else panel_w,
                    )
                new_width, new_height = max(1, new_width), max(1, new_height)
                scaled_image = loaded_image.Scale(
                    new_width, new_height, wx.IMAGE_QUALITY_HIGH
                )
                self.image_preview_ctrl.SetBitmap(wx.Bitmap(scaled_image))
                self.image_preview_ctrl.Show()
            elif ext in [".json", ".md"]:
                content = file_path.read_text(encoding="utf-8", errors="replace")
                self.text_preview_ctrl.SetValue(content)
                self.text_preview_ctrl.Show()
            elif ext == ".pdf":
                if fitz:
                    try:
                        doc = fitz.open(file_path)
                        if len(doc) > 0:
                            page = doc.load_page(0)
                            pix = page.get_pixmap()
                            img_bytes = pix.tobytes("ppm")
                            wx_image = wx.Image(io.BytesIO(img_bytes))
                            img_width, img_height = (
                                wx_image.GetWidth(),
                                wx_image.GetHeight(),
                            )
                            panel_w, panel_h = self.preview_panel.GetClientSize()
                            panel_w, panel_h = max(panel_w, 50), max(panel_h, 50)
                            img_aspect = img_width / img_height if img_height > 0 else 1
                            panel_aspect = panel_w / panel_h if panel_h > 0 else 1
                            if img_aspect > panel_aspect:
                                new_width, new_height = (
                                    panel_w,
                                    int(panel_w / img_aspect)
                                    if img_aspect != 0
                                    else panel_h,
                                )
                            else:
                                new_height, new_width = (
                                    panel_h,
                                    int(panel_h * img_aspect)
                                    if panel_h != 0
                                    else panel_w,
                                )
                            new_width, new_height = (
                                max(1, new_width),
                                max(1, new_height),
                            )
                            scaled_image = wx_image.Scale(
                                new_width, new_height, wx.IMAGE_QUALITY_HIGH
                            )
                            self.image_preview_ctrl.SetBitmap(wx.Bitmap(scaled_image))
                        else:
                            self.image_preview_ctrl.SetBitmap(wx.Bitmap(1, 1))
                        doc.close()
                        self.image_preview_ctrl.Show()
                    except Exception as e_pdf:
                        wx.LogError(f"PDF preview error {file_path.name}: {e_pdf}")
                        self.text_preview_ctrl.SetValue(
                            f"PDF: {file_path.name}\nPreview failed: {e_pdf}"
                        )
                        self.text_preview_ctrl.Show()
                else:
                    self.text_preview_ctrl.SetValue(
                        f"PDF: {file_path.name}\nPyMuPDF not installed."
                    )
                    self.text_preview_ctrl.Show()
            else:
                self.text_preview_ctrl.SetValue(
                    f"Unsupported preview: {file_path.name}"
                )
                self.text_preview_ctrl.Show()
        except Exception as e:
            wx.LogError(f"Preview error for {file_path.name}: {e}")
            self.text_preview_ctrl.SetValue(f"Error previewing {file_path.name}:\n{e}")
            self.text_preview_ctrl.Show()
        self.preview_panel.Layout()

    def on_delete_button_clicked(self, event):
        if not self.all_selected_item_paths:
            wx.MessageBox(
                "No items selected to delete.", "Delete", wx.OK | wx.ICON_INFORMATION
            )
            return

        num_items = len(self.all_selected_item_paths)
        item_names = "\n".join(
            [f"- {p.name}" for p in self.all_selected_item_paths[:5]]
        )  # Show first 5
        if num_items > 5:
            item_names += f"\n- ...and {num_items - 5} more."

        msg = f"Are you sure you want to delete the following {num_items} item(s)?\n{item_names}\n\n"
        msg += "This will move them to the trash (if send2trash is available) or delete them permanently."

        with wx.MessageDialog(
            self, msg, "Confirm Delete", wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING
        ) as dlg:
            if dlg.ShowModal() != wx.ID_YES:
                return

        deleted_count = 0
        error_count = 0
        for item_path in self.all_selected_item_paths:
            try:
                if item_path.exists():  # Check if it wasn't deleted by a previous operation in the same batch
                    if send2trash:
                        send2trash(str(item_path))
                        wx.LogStatus(f"Moved to trash: {item_path.name}")
                    elif item_path.is_file():
                        os.remove(item_path)
                        wx.LogStatus(f"Permanently deleted file: {item_path.name}")
                    elif item_path.is_dir():
                        shutil.rmtree(item_path)
                        wx.LogStatus(f"Permanently deleted folder: {item_path.name}")
                    deleted_count += 1
            except Exception as e:
                wx.LogError(f"Error deleting {item_path.name}: {e}")
                error_count += 1

        # FSW should pick up changes. Clear local selections.
        self.all_selected_item_paths.clear()
        self.focused_item_path = None
        self.clear_preview()
        # self.update_button_states() will be called by FSW event -> refresh_tree -> populate_tree_and_watch

        final_msg = f"Deletion process finished.\nSuccessfully deleted: {deleted_count}\nErrors: {error_count}"
        wx.MessageBox(final_msg, "Deletion Report", wx.OK | wx.ICON_INFORMATION)
        if error_count > 0:
            wx.LogMessage("Some items could not be deleted. Check log for details.")

    def on_run_ocr_button_clicked(self, event):
        if not self.mistral_client:
            wx.MessageBox("Mistral client not initialized.", "Error")
            return

        ocr_target_paths = [
            p
            for p in self.all_selected_item_paths
            if p.is_file() and p.suffix.lower() in self.supported_ocr_input_formats
        ]

        if not ocr_target_paths:
            wx.MessageBox(
                "No suitable image files selected for OCR.",
                "OCR",
                wx.OK | wx.ICON_INFORMATION,
            )
            return
        if not self.current_opened_folder_path:
            wx.MessageBox("Base folder not identified.", "Error")
            return

        self.run_ocr_button.Disable()
        self.stitch_pdf_button.Disable()
        self.delete_button.Disable()
        threading.Thread(
            target=self._perform_ocr_workflow_for_list,
            args=(ocr_target_paths, self.current_opened_folder_path),
        ).start()

    def _perform_ocr_workflow_for_list(
        self, image_paths: list[Path], base_output_folder: Path
    ):
        json_output_dir = base_output_folder / "json"
        json_output_dir.mkdir(parents=True, exist_ok=True)
        total_files = len(image_paths)
        wx.CallAfter(self.update_progress, 0)

        for i, image_path in enumerate(image_paths):
            current_file_progress_start = int((i / total_files) * 100)
            wx.CallAfter(
                self.update_status,
                f"Processing file {i + 1}/{total_files}: {image_path.name}...",
            )

            with tempfile.TemporaryDirectory() as tmpdir:
                temp_pdf_path = Path(tmpdir) / image_path.with_suffix(".pdf").name
                wx.CallAfter(
                    self.update_progress,
                    current_file_progress_start + int(1 * (100 / total_files) / 4),
                )  # 1/4th of this file's share

                pdf_file = _image_to_pdf(
                    image_path,
                    temp_pdf_path,
                    lambda msg: wx.CallAfter(self.update_status, msg, 1),
                )
                if not pdf_file:
                    wx.CallAfter(
                        self.update_status,
                        f"Failed PDF conversion: {image_path.name}",
                        0,
                    )
                    continue  # Skip to next file

                wx.CallAfter(
                    self.update_progress,
                    current_file_progress_start + int(2 * (100 / total_files) / 4),
                )  # 2/4th
                base64_pdf = _encode_pdf_to_base64(
                    pdf_file, lambda msg: wx.CallAfter(self.update_status, msg, 1)
                )
                if not base64_pdf:
                    wx.CallAfter(
                        self.update_status, f"Failed PDF encoding: {image_path.name}", 0
                    )
                    continue

                wx.CallAfter(
                    self.update_progress,
                    current_file_progress_start + int(3 * (100 / total_files) / 4),
                )  # 3/4th
                ocr_response_obj = _request_mistral_ocr(
                    self.mistral_client,
                    pdf_file,
                    base64_pdf,
                    lambda msg: wx.CallAfter(self.update_status, msg, 1),
                )

                if ocr_response_obj:
                    ocr_data_dict = {}
                    try:
                        if hasattr(ocr_response_obj, "model_dump"):
                            ocr_data_dict = ocr_response_obj.model_dump()
                        elif hasattr(ocr_response_obj, "dict"):
                            ocr_data_dict = ocr_response_obj.dict()
                        else:
                            ocr_data_dict = ocr_response_obj
                    except Exception as e:
                        wx.CallAfter(
                            wx.LogError, f"Could not convert OCR response to dict: {e}"
                        )
                        ocr_data_dict = {
                            "error": "Failed to serialize OCR response",
                            "details": str(e),
                        }

                    json_file_path = (
                        json_output_dir / image_path.with_suffix(".json").name
                    )
                    try:
                        with open(json_file_path, "w", encoding="utf-8") as jf:
                            json.dump(ocr_data_dict, jf, indent=2)
                        wx.CallAfter(
                            self.update_status,
                            f"OCR data saved: {json_file_path.name}",
                            1,
                        )
                    except Exception as e:
                        wx.CallAfter(
                            wx.LogError,
                            f"Failed to save OCR JSON for {image_path.name}: {e}",
                        )
                        wx.CallAfter(
                            self.update_status,
                            f"Failed save JSON: {image_path.name}",
                            0,
                        )
                else:
                    wx.CallAfter(
                        self.update_status, f"Failed OCR request: {image_path.name}", 0
                    )
            wx.CallAfter(
                self.update_progress, int(((i + 1) / total_files) * 100)
            )  # End of this file's share

        wx.CallAfter(
            self.update_status, "OCR processing complete for selected files.", 0
        )
        wx.CallAfter(self.update_progress, 100)
        if (
            self.current_opened_folder_path == base_output_folder
        ):  # Refresh tree if output is in current view
            wx.CallAfter(self.refresh_tree_due_to_fs_event)
        wx.CallAfter(self.run_ocr_button.Enable)
        wx.CallAfter(self.stitch_pdf_button.Enable)
        wx.CallAfter(self.delete_button.Enable)

    def on_stitch_button_clicked(self, event):
        # ... (on_stitch_button_clicked logic remains largely the same)
        if not self.mistral_client:
            wx.MessageBox("Mistral client not initialized.", "Error")
            return
        if not self.current_opened_folder_path:
            wx.MessageBox("No folder opened.", "Error")
            return
        self.run_ocr_button.Disable()
        self.stitch_pdf_button.Disable()
        self.delete_button.Disable()
        threading.Thread(
            target=self._perform_stitch_and_pandoc_workflow,
            args=(self.current_opened_folder_path,),
        ).start()

    def _perform_stitch_and_pandoc_workflow(self, base_folder: Path):
        # ... (_perform_stitch_and_pandoc_workflow logic remains largely the same)
        json_dir = base_folder / "json"
        markdown_dir = base_folder / "markdown"
        images_dir = base_folder / "images"
        final_pdf_dir = base_folder / "final_output"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        images_dir.mkdir(parents=True, exist_ok=True)
        final_pdf_dir.mkdir(parents=True, exist_ok=True)
        stitched_md_path = markdown_dir / "stitched_document.md"
        final_pdf_path = final_pdf_dir / "final_document.pdf"
        wx.CallAfter(self.update_progress, 5)
        wx.CallAfter(self.update_status, "Collecting JSON files...", 0)
        if not json_dir.exists() or not any(json_dir.iterdir()):
            wx.CallAfter(wx.MessageBox, "No JSON files in 'json' directory.", "Info")
            wx.CallAfter(self.update_progress, 0)
            wx.CallAfter(self.run_ocr_button.Enable)
            wx.CallAfter(self.stitch_pdf_button.Enable)
            wx.CallAfter(self.delete_button.Enable)
            return
        json_files = sorted([f for f in json_dir.glob("*.json") if f.is_file()])
        if not json_files:
            wx.CallAfter(
                wx.MessageBox, "No .json files found in 'json' directory.", "Info"
            )
            wx.CallAfter(self.update_progress, 0)
            wx.CallAfter(self.run_ocr_button.Enable)
            wx.CallAfter(self.stitch_pdf_button.Enable)
            wx.CallAfter(self.delete_button.Enable)
            return
        wx.CallAfter(self.update_progress, 20)
        _stitch_ocr_data(json_files, stitched_md_path, images_dir, self.update_status)
        wx.CallAfter(self.update_progress, 70)
        wx.CallAfter(self.update_status, "Stitching complete. Converting to PDF...", 0)
        try:
            pandoc_cmd = [
                "pandoc",
                str(stitched_md_path),
                "-o",
                str(final_pdf_path),
                "--resource-path",
                str(images_dir),
                "--pdf-engine=tectonic",
            ]
            result = subprocess.run(
                pandoc_cmd, capture_output=True, text=True, check=False, cwd=base_folder
            )
            if result.returncode == 0:
                wx.CallAfter(self.update_status, f"Final PDF: {final_pdf_path.name}", 0)
                wx.CallAfter(self.update_progress, 100)
                wx.CallAfter(
                    wx.MessageBox,
                    f"Successfully created PDF: {final_pdf_path}",
                    "Success",
                )
                if self.current_opened_folder_path == base_folder:
                    wx.CallAfter(self.refresh_tree_due_to_fs_event)
            else:
                wx.CallAfter(
                    wx.LogError,
                    f"Pandoc error (Code {result.returncode}):\n{result.stderr}",
                )
                wx.CallAfter(
                    wx.MessageBox,
                    f"Pandoc failed. Check logs.\nError: {result.stderr[:200]}...",
                    "Pandoc Error",
                )
                wx.CallAfter(self.update_status, "Pandoc failed.", 0)
                wx.CallAfter(self.update_progress, 0)
        except FileNotFoundError:
            wx.CallAfter(
                wx.LogError, "Pandoc not found. Install Pandoc and ensure it's in PATH."
            )
            wx.CallAfter(wx.MessageBox, "Pandoc not found.", "Error")
            wx.CallAfter(self.update_status, "Pandoc not found.", 0)
            wx.CallAfter(self.update_progress, 0)
        except Exception as e:
            wx.CallAfter(wx.LogError, f"Error running Pandoc: {e}")
            wx.CallAfter(self.update_status, "Pandoc error.", 0)
            wx.CallAfter(self.update_progress, 0)
        wx.CallAfter(self.run_ocr_button.Enable)
        wx.CallAfter(self.stitch_pdf_button.Enable)
        wx.CallAfter(self.delete_button.Enable)

    def on_exit(self, event):
        if self.fs_watcher:
            self.fs_watcher.RemoveAll()
        self.Close(True)

    def update_button_states(self):
        can_ocr = self.mistral_client is not None
        has_selection = bool(self.all_selected_item_paths)

        # OCR button enabled if at least one selected file is a supported image type
        ocr_target_paths = [
            p
            for p in self.all_selected_item_paths
            if p.is_file() and p.suffix.lower() in self.supported_ocr_input_formats
        ]
        can_run_ocr_on_selection = can_ocr and bool(ocr_target_paths)

        is_folder_opened = self.current_opened_folder_path is not None

        self.run_ocr_button.Enable(can_run_ocr_on_selection)
        self.stitch_pdf_button.Enable(can_ocr and is_folder_opened)
        self.delete_button.Enable(
            has_selection and is_folder_opened
        )  # Can delete if items are selected & folder is open


class ImageApp(wx.App):
    def OnInit(self):
        self.SetAppName("Pantheon")  # Set the application name
        if not PILImage:
            wx.MessageBox(
                "Pillow library is essential. Please install 'Pillow'.",
                "Critical Error",
                wx.OK | wx.ICON_ERROR,
            )
            return False
        if not Mistral:
            wx.MessageBox(
                "MistralAI library not loaded. OCR features disabled. Install 'mistralai'.",
                "Warning",
                wx.OK | wx.ICON_WARNING,
            )
        if not _get_mistral_api_key() and Mistral:
            wx.MessageBox(
                "MISTRAL_API_KEY not set. OCR features disabled.",
                "Warning",
                wx.OK | wx.ICON_WARNING,
            )
        if not fitz:
            wx.LogWarning(
                "PyMuPDF (fitz) not found. PDF preview will be limited/unavailable."
            )
        if not send2trash:
            wx.LogInfo(
                "send2trash library not found. Deletions will be permanent. Consider installing 'send2trash'."
            )

        wx.InitAllImageHandlers()
        frame = ImageFrame(None, "Pantheon")
        frame.Show()
        return True


def main():
    app = ImageApp()
    app.MainLoop()


if __name__ == "__main__":
    main()
