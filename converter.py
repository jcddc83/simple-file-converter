#!/usr/bin/env python3
"""
File Converter - Convert images (WEBP, PNG, AVIF, BMP, TIFF, JPG) and PDFs to JPG or PNG
"""
import sys
import os
import json
import datetime
import faulthandler
import platform
import traceback
import threading
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QPushButton, QLabel, QFileDialog,
                             QSpinBox, QComboBox, QCheckBox, QSlider, QLineEdit,
                             QMessageBox, QProgressBar, QGroupBox, QListWidget,
                             QAbstractItemView, QInputDialog, QScrollArea)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, qInstallMessageHandler, QtMsgType
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PIL import Image, ImageOps
import pypdfium2 as pdfium


# ---------------------------------------------------------------------------
# Logging. A windowed .exe has no console, so unhandled errors, Qt warnings
# and native crashes are written to a log file the user can find:
#   Windows:   %LOCALAPPDATA%\FileConverter\crash.log
#   Mac/Linux: ~/.local/share/FileConverter/crash.log
# ---------------------------------------------------------------------------
if sys.platform == 'win32':
    LOG_DIR = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'FileConverter'
else:
    LOG_DIR = Path.home() / '.local' / 'share' / 'FileConverter'
LOG_FILE = LOG_DIR / 'crash.log'
_log_handle = None


def log_line(message):
    """Append a timestamped line to the log; never raises."""
    if _log_handle is None:
        return
    try:
        stamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        _log_handle.write(f"[{stamp}] {message}\n")
    except (OSError, ValueError):
        pass


def log_exception(context, exc_info=None):
    """Log a traceback (defaults to the exception currently being handled)."""
    exc_type, exc, tb = exc_info or sys.exc_info()
    text = ''.join(traceback.format_exception(exc_type, exc, tb))
    log_line(f"{context}\n{text}")


def _qt_message_handler(mode, context, message):
    # Skip debug/info chatter; keep warnings, criticals and fatals
    if mode in (QtMsgType.QtDebugMsg, QtMsgType.QtInfoMsg):
        return
    log_line(f"Qt {mode.name}: {message}")


def _excepthook(exc_type, exc, tb):
    """Log unhandled exceptions. Installing a hook also stops PyQt6 from
    aborting the whole process when a slot raises."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc, tb)
        return
    log_exception("Unhandled exception", (exc_type, exc, tb))
    try:
        QMessageBox.critical(
            None,
            "Unexpected Error",
            f"Something went wrong: {exc}\n\nDetails were saved to:\n{LOG_FILE}"
        )
    except Exception:
        pass


def _thread_excepthook(args):
    log_exception(f"Unhandled exception in thread {args.thread.name if args.thread else '?'}",
                  (args.exc_type, args.exc_value, args.exc_traceback))


def init_logging():
    global _log_handle
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE.with_name('crash.log.old'))
        _log_handle = open(LOG_FILE, 'a', buffering=1, encoding='utf-8')
        faulthandler.enable(_log_handle)  # native crashes (segfaults) too
    except OSError:
        _log_handle = None
    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    qInstallMessageHandler(_qt_message_handler)
    try:
        from PyQt6.QtCore import PYQT_VERSION_STR
        log_line(f"Session start | Python {platform.python_version()} | {platform.platform()} | "
                 f"PyQt {PYQT_VERSION_STR} | Pillow {Image.__version__} | "
                 f"pypdfium2 {pdfium.version.PYPDFIUM_INFO}")
    except Exception:
        log_line("Session start")


class ConversionCancelledError(Exception):
    """Raised internally when a conversion is cancelled by the user"""


# Image modes wider than 8 bits per channel that Pillow loads as one channel
HIGH_BIT_MODES = ('I', 'F', 'I;16', 'I;16L', 'I;16B', 'I;16N')


def reduce_to_8bit(img):
    """Scale 16/32-bit integer or float grayscale down to 8-bit 'L'.
    Plain convert('L') clips everything above 255, turning a 16-bit
    image into near-solid white."""
    if img.mode == 'F':
        lo, hi = img.getextrema()
        if lo >= 0 and hi <= 1.0:      # normalized 0..1 floats
            scale, offset = 255.0, 0.0
        elif hi > lo:                  # arbitrary range: stretch to fit
            scale = 255.0 / (hi - lo)
            offset = -lo * scale
        else:
            scale, offset = 1.0, 0.0
        return img.point(lambda v: v * scale + offset).convert('L')

    if img.mode != 'I':
        img = img.convert('I')
    lo, hi = img.getextrema()
    if lo < 0 or hi > 65535:           # true 32-bit / signed data: stretch
        scale = 255.0 / max(hi - lo, 1)
        offset = -lo * scale
    elif hi > 255:                     # 16-bit data: keep tonality, /256
        scale, offset = 1 / 256, 0.0
    else:                              # already 8-bit range
        scale, offset = 1.0, 0.0
    return img.point(lambda v: v * scale + offset).convert('L')


def normalize_mode(img, output_format):
    """Bring any Pillow image mode into one that saves cleanly as JPG/PNG."""
    mode = img.mode
    has_transparency = 'transparency' in img.info

    if mode in HIGH_BIT_MODES:
        # PNG can store 16-bit grayscale natively; keep it for PNG output
        if output_format == 'png' and mode.startswith('I;16'):
            return img if mode == 'I;16' else img.convert('I;16')
        return reduce_to_8bit(img)

    if output_format == 'png':
        if mode in ('1', 'L', 'LA', 'RGB', 'RGBA') and not (has_transparency and mode != 'RGBA'):
            return img
        if mode in ('LA', 'RGBA', 'RGBa', 'PA') or has_transparency:
            return img.convert('RGBA')
        return img.convert('RGB')      # P, CMYK, YCbCr, LAB, HSV, ...

    # JPEG has no alpha: flatten anything transparent onto white
    if mode in ('LA', 'RGBA', 'RGBa', 'PA') or has_transparency:
        rgba = img.convert('RGBA')
        background = Image.new('RGB', rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel('A'))
        return background
    return img if mode == 'RGB' else img.convert('RGB')


class FocusSlider(QSlider):
    """Slider that only responds to wheel events when it has keyboard focus."""
    def wheelEvent(self, event):
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class ConversionWorker(QThread):
    """Background worker for file conversion"""
    progress = pyqtSignal(int)
    finished = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, input_file, output_file, settings):
        super().__init__()
        self.input_file = input_file
        self.output_file = output_file
        self.settings = settings
        self._cancelled = False
        self.written_files = []  # every file this run produced
        self.notes = []          # non-fatal remarks for the completion report

    def cancel(self):
        """Request cancellation; the worker stops at the next checkpoint"""
        self._cancelled = True

    def _check_cancelled(self):
        if self._cancelled:
            raise ConversionCancelledError()

    def run(self):
        try:
            file_ext = Path(self.input_file).suffix.lower()

            if file_ext == '.pdf':
                self.convert_pdf()
            elif file_ext in ['.webp', '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif', '.avif']:
                self.convert_image()
            else:
                self.error.emit(f"Unsupported file format: {file_ext}")
                return

            self.finished.emit(f"Successfully converted to {self.output_file}")
        except ConversionCancelledError:
            self.error.emit("Cancelled")
        except Exception as e:
            log_exception(f"Conversion failed: {self.input_file}")
            self.error.emit(f"Conversion failed: {str(e)}")

    def _output_path(self, index, count, label):
        """Single result -> the chosen output file; several -> stem_<label>N"""
        out = Path(self.output_file)
        if count == 1:
            return out
        return out.parent / f"{out.stem}_{label}{index}.{self.ext}"

    @property
    def ext(self):
        return 'png' if self.settings.get('output_format', 'jpg').lower() == 'png' else 'jpg'

    def convert_pdf(self):
        """Render PDF pages to JPG or PNG. Pages are rendered one at a time
        straight to RGB pixels and encoded once, so JPG output is compressed
        only once (no intermediate lossy step) and memory stays bounded."""
        output_format = self.settings.get('output_format', 'jpg').lower()
        quality = self.settings.get('quality', 85)
        pixel_density = self.settings.get('pixel_density', 300)
        width = self.settings.get('width')
        height = self.settings.get('height')
        pages = self.settings.get('pages', 'all').strip()

        # Parse and validate page specification before rendering anything
        page_list = None
        if pages.lower() != 'all':
            page_list = self.parse_pages(pages)

        try:
            pdf = pdfium.PdfDocument(self.input_file)
        except pdfium.PdfiumError as e:
            raise ValueError(f"Could not open PDF: {e}")

        try:
            total_pages = len(pdf)
            wanted = page_list if page_list else list(range(1, total_pages + 1))
            selected = [p for p in wanted if p <= total_pages]
            if not selected:
                raise ValueError(f"Page(s) {pages} not found (this PDF has {total_pages} page{'s' if total_pages != 1 else ''})")
            missing = [p for p in wanted if p > total_pages]
            if missing:
                self.notes.append(f"pages not in document were skipped: {', '.join(map(str, missing))}")

            self.progress.emit(5)
            scale = pixel_density / 72  # PDF points are 1/72 inch
            for i, page_no in enumerate(selected):
                self._check_cancelled()
                page = pdf[page_no - 1]
                try:
                    img = page.render(scale=scale).to_pil().convert('RGB')
                finally:
                    page.close()

                if width or height:
                    img = self.resize_image(img, width, height, 'max')

                out = self._output_path(page_no, len(selected), 'page')
                if output_format == 'png':
                    img.save(str(out), 'PNG')
                else:
                    img.save(str(out), 'JPEG', quality=quality)
                self.written_files.append(str(out))
                self.progress.emit(5 + int(95 * (i + 1) / len(selected)))
        finally:
            pdf.close()

    def convert_image(self):
        """Convert an image to JPG or PNG with specified settings.
        Multi-page TIFFs export every page (stem_pageN); animated
        WebP/PNG/AVIF export the first frame and say so."""
        quality = self.settings.get('quality', 85)
        width = self.settings.get('width')
        height = self.settings.get('height')
        fit = self.settings.get('fit', 'max')
        strip = self.settings.get('strip', False)
        output_format = self.settings.get('output_format', 'jpg').lower()

        self.progress.emit(10)

        with Image.open(self.input_file) as src:
            n_frames = getattr(src, 'n_frames', 1)
            if src.format == 'TIFF':
                count = n_frames
            else:
                count = 1
                if n_frames > 1 and src.format != 'MPO':  # MPO = normal camera JPEG
                    self.notes.append("animated/multi-frame image: only the first frame was converted")

            for idx in range(count):
                self._check_cancelled()
                src.seek(idx)

                # Bake the EXIF orientation into the pixels. Without this,
                # phone photos come out sideways when metadata is stripped
                # (the rotation tag is lost). exif_transpose also removes the
                # orientation tag from the EXIF it keeps, so preserved
                # metadata doesn't rotate twice. It returns a copy.
                img = ImageOps.exif_transpose(src)

                # Capture EXIF before any compositing: converting/flattening
                # drops .info (and with it the EXIF bytes).
                exif_data = img.info.get('exif', b'')

                # WEBP (and some TIFF) sources store EXIF as raw TIFF bytes
                # without the b'Exif\x00\x00' APP1 header; Pillow's JPEG saver
                # silently drops the metadata without it, so normalize.
                if exif_data and not exif_data.startswith(b'Exif\x00\x00'):
                    exif_data = b'Exif\x00\x00' + exif_data

                img = normalize_mode(img, output_format)

                if width or height:
                    img = self.resize_image(img, width, height, fit)

                self._check_cancelled()

                if output_format == 'png':
                    save_kwargs = {'format': 'PNG'}
                else:
                    save_kwargs = {'format': 'JPEG', 'quality': quality}
                if not strip and exif_data:
                    save_kwargs['exif'] = exif_data

                out = self._output_path(idx + 1, count, 'page')
                img.save(str(out), **save_kwargs)
                self.written_files.append(str(out))
                self.progress.emit(10 + int(90 * (idx + 1) / count))

    def resize_image(self, img, width, height, fit):
        """Resize image based on fit mode"""
        original_width, original_height = img.size

        # Calculate target dimensions
        if width and height:
            target_width = width
            target_height = height
        elif width:
            target_width = width
            target_height = int(original_height * (width / original_width))
        elif height:
            target_height = height
            target_width = int(original_width * (height / original_height))
        else:
            return img

        if fit == 'max':
            # Fit within bounds, maintaining aspect ratio
            img.thumbnail((target_width, target_height), Image.Resampling.LANCZOS)
        elif fit == 'scale':
            # Stretch to exact dimensions
            img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
        elif fit == 'crop':
            # Crop to exact dimensions
            img_ratio = original_width / original_height
            target_ratio = target_width / target_height

            if img_ratio > target_ratio:
                # Image is wider
                new_width = int(original_height * target_ratio)
                left = (original_width - new_width) // 2
                img = img.crop((left, 0, left + new_width, original_height))
            else:
                # Image is taller
                new_height = int(original_width / target_ratio)
                top = (original_height - new_height) // 2
                img = img.crop((0, top, original_width, top + new_height))

            img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)

        return img

    def parse_pages(self, pages_str):
        """Parse page specification like '1-3,5,7-9' into list of page numbers"""
        pages = []
        parts = pages_str.split(',')
        for part in parts:
            part = part.strip()
            if '-' in part:
                try:
                    start, end = (int(x) for x in part.split('-'))
                except ValueError:
                    raise ValueError(f"Invalid page range '{part}'")
                if start < 1 or end < 1:
                    raise ValueError(f"Invalid page range '{part}': pages start at 1")
                if start > end:
                    raise ValueError(f"Invalid page range '{part}': start is after end")
                pages.extend(range(start, end + 1))
            else:
                try:
                    page = int(part)
                except ValueError:
                    raise ValueError(f"Invalid page number '{part}' (use e.g. all, 1-3, or 1,3,5)")
                if page < 1:
                    raise ValueError(f"Invalid page number '{part}': pages start at 1")
                pages.append(page)
        return sorted(set(pages))


class DropZone(QLabel):
    """Drag and drop zone for files"""
    filesDropped = pyqtSignal(list)  # Changed to emit list of files

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("""
            QLabel {
                border: 3px dashed #aaa;
                border-radius: 10px;
                padding: 40px;
                background-color: #f8f9fa;
                color: #6c757d;
                font-size: 16px;
            }
            QLabel:hover {
                border-color: #007bff;
                background-color: #e7f3ff;
            }
        """)
        self.setText("Drop file(s) here\nor click Browse button\n\nSupported: PDF, WEBP, PNG, AVIF, JPG, BMP, TIFF\n\nMultiple files supported!")

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        files = [u.toLocalFile() for u in event.mimeData().urls()]
        if files:
            self.filesDropped.emit(files)  # Emit all files


class PresetManager:
    """Manage conversion presets"""
    if sys.platform == 'win32':
        _app_dir = Path(os.environ.get('APPDATA', Path.home())) / 'FileConverter'
    else:
        _app_dir = Path.home() / '.config' / 'FileConverter'
    try:
        _app_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # save_presets reports the failure when it matters
    PRESETS_FILE = str(_app_dir / "presets.json")

    # Output format (JPG/PNG) is deliberately NOT part of presets: the
    # dropdown next to Convert is the single source of truth for format,
    # and presets control everything else (quality, size, DPI, metadata).
    DEFAULT_PRESETS = {
        "Web Optimized": {
            "quality": 85,
            "width": None,
            "height": None,
            "fit": "max",
            "strip": True
        },
        "High Quality": {
            "quality": 95,
            "width": None,
            "height": None,
            "fit": "max",
            "strip": False
        },
        "Thumbnail": {
            "quality": 80,
            "width": 400,
            "height": 400,
            "fit": "max",
            "strip": True
        },
        "PDF Standard (300 DPI)": {
            "pixel_density": 300,
            "width": None,
            "height": None,
            "pages": "all"
        },
        "PDF High Quality (600 DPI)": {
            "pixel_density": 600,
            "width": None,
            "height": None,
            "pages": "all"
        },
        "PDF Web (150 DPI)": {
            "pixel_density": 150,
            "width": None,
            "height": None,
            "pages": "all"
        }
    }

    # Exact snapshots of built-in presets shipped in earlier versions,
    # kept so load_presets can retire them from a user's presets.json.
    # A retired preset is removed only if it still matches one of its
    # shipped snapshots exactly; user-edited copies are always kept.
    RETIRED_PRESETS = {
        "Web Optimized (WEBP/PNG → JPG)": [
            {"quality": 85, "width": None, "height": None, "fit": "max", "strip": True},
            {"quality": 85, "width": None, "height": None, "fit": "max", "strip": True, "output_format": "jpg"},
        ],
        "High Quality (WEBP/PNG → JPG)": [
            {"quality": 95, "width": None, "height": None, "fit": "max", "strip": False},
            {"quality": 95, "width": None, "height": None, "fit": "max", "strip": False, "output_format": "jpg"},
        ],
        "Thumbnail (WEBP/PNG → JPG)": [
            {"quality": 80, "width": 400, "height": 400, "fit": "max", "strip": True},
            {"quality": 80, "width": 400, "height": 400, "fit": "max", "strip": True, "output_format": "jpg"},
        ],
        "PNG Output (WEBP/JPG → PNG)": [
            {"quality": 95, "width": None, "height": None, "fit": "max", "strip": False, "output_format": "png"},
        ],
        "PNG Web Optimized (→ PNG)": [
            {"quality": 95, "width": None, "height": None, "fit": "max", "strip": True, "output_format": "png"},
        ],
        "PDF Standard (300 DPI)": [
            {"pixel_density": 300, "width": None, "height": None, "pages": "all"},
            {"pixel_density": 300, "width": None, "height": None, "pages": "all", "output_format": "jpg"},
        ],
        "PDF High Quality (600 DPI)": [
            {"pixel_density": 600, "width": None, "height": None, "pages": "all"},
            {"pixel_density": 600, "width": None, "height": None, "pages": "all", "output_format": "jpg"},
        ],
        "PDF Web (150 DPI)": [
            {"pixel_density": 150, "width": None, "height": None, "pages": "all"},
            {"pixel_density": 150, "width": None, "height": None, "pages": "all", "output_format": "jpg"},
        ],
        "PDF → PNG (300 DPI)": [
            {"pixel_density": 300, "width": None, "height": None, "pages": "all", "output_format": "png"},
        ],
    }

    # Built-in presets the user deleted. Remembered in presets.json so a
    # deleted built-in stays deleted across launches and updates.
    deleted_defaults = set()

    @classmethod
    def load_presets(cls):
        """Load presets from file. Retired built-ins are dropped only if
        they still match a shipped snapshot exactly; anything the user
        edited or saved themselves is always kept. Current built-ins are
        merged in if absent, unless the user deleted them.

        File format: {"presets": {...}, "deleted_defaults": [...]}.
        The older format (a bare {name: settings} dict) is still read."""
        presets = {}
        cls.deleted_defaults = set()
        if os.path.exists(cls.PRESETS_FILE):
            try:
                with open(cls.PRESETS_FILE, 'r') as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = {}
            if isinstance(data, dict) and isinstance(data.get("presets"), dict):
                presets = data["presets"]
                deleted = data.get("deleted_defaults", [])
                if isinstance(deleted, list):
                    cls.deleted_defaults = {d for d in deleted if isinstance(d, str)}
            elif isinstance(data, dict):
                presets = data  # legacy flat format
        for name, snapshots in cls.RETIRED_PRESETS.items():
            if name in presets and any(presets[name] == snapshot for snapshot in snapshots):
                del presets[name]
        for name, settings in cls.DEFAULT_PRESETS.items():
            if name not in presets and name not in cls.deleted_defaults:
                presets[name] = settings
        return presets

    @classmethod
    def save_presets(cls, presets):
        """Save presets to file; returns False if the file can't be written"""
        try:
            with open(cls.PRESETS_FILE, 'w') as f:
                json.dump({"presets": presets,
                           "deleted_defaults": sorted(cls.deleted_defaults)}, f, indent=2)
            return True
        except OSError:
            return False

    @classmethod
    def mark_deleted(cls, name):
        """Remember that the user deleted a built-in preset"""
        if name in cls.DEFAULT_PRESETS:
            cls.deleted_defaults.add(name)

    @classmethod
    def restore_defaults(cls, presets):
        """Re-add any missing built-ins (existing/edited presets are kept).
        Returns the names that were restored."""
        restored = [n for n in cls.DEFAULT_PRESETS if n not in presets]
        for name in restored:
            presets[name] = cls.DEFAULT_PRESETS[name]
        cls.deleted_defaults.clear()
        return restored


class FileConverter(QMainWindow):
    """Main application window"""

    def __init__(self):
        super().__init__()
        self.input_files = []  # Changed to list for batch processing
        self.presets = PresetManager.load_presets()
        self.current_batch_index = 0
        self.batch_running = False
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("Simple File Converter - PDF/Images to JPG or PNG")
        self.setMinimumSize(850, 600)
        self.resize(850, 800)  # Start with comfortable default size

        # Central widget with scroll area
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Create scroll area for entire content
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        # Scrollable content widget
        scroll_content = QWidget()
        layout = QVBoxLayout(scroll_content)
        layout.setSpacing(15)
        layout.setContentsMargins(15, 15, 15, 15)

        # Drop zone
        self.drop_zone = DropZone()
        self.drop_zone.filesDropped.connect(self.on_files_dropped)
        layout.addWidget(self.drop_zone)

        # File list section
        file_list_label = QLabel("Files to Convert:")
        file_list_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        layout.addWidget(file_list_label)

        # File list widget
        self.file_list = QListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setMinimumHeight(100)
        self.file_list.setMaximumHeight(150)
        layout.addWidget(self.file_list)

        # File management buttons
        file_buttons_layout = QHBoxLayout()
        browse_btn = QPushButton("Add Files...")
        browse_btn.setMinimumHeight(38)
        browse_btn.setStyleSheet("font-size: 13px;")
        browse_btn.clicked.connect(self.browse_files)
        file_buttons_layout.addWidget(browse_btn)

        remove_btn = QPushButton("Remove Selected")
        remove_btn.setMinimumHeight(38)
        remove_btn.setStyleSheet("font-size: 13px;")
        remove_btn.clicked.connect(self.remove_selected_files)
        file_buttons_layout.addWidget(remove_btn)

        clear_btn = QPushButton("Clear All")
        clear_btn.setMinimumHeight(38)
        clear_btn.setStyleSheet("font-size: 13px;")
        clear_btn.clicked.connect(self.clear_all_files)
        file_buttons_layout.addWidget(clear_btn)

        file_buttons_layout.addStretch()
        layout.addLayout(file_buttons_layout)

        # Preset selection
        preset_layout = QHBoxLayout()
        preset_layout.addWidget(QLabel("Preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumHeight(38)
        self.preset_combo.setStyleSheet("font-size: 13px;")
        self.preset_combo.addItem("Custom")
        self.preset_combo.addItems(sorted(self.presets.keys()))
        self.preset_combo.currentTextChanged.connect(self.on_preset_changed)
        preset_layout.addWidget(self.preset_combo, 1)

        # Preset management buttons
        save_preset_btn = QPushButton("Save as Preset")
        save_preset_btn.setMinimumHeight(38)
        save_preset_btn.setStyleSheet("font-size: 13px;")
        save_preset_btn.clicked.connect(self.save_current_as_preset)
        preset_layout.addWidget(save_preset_btn)

        delete_preset_btn = QPushButton("Delete Preset")
        delete_preset_btn.setMinimumHeight(38)
        delete_preset_btn.setStyleSheet("font-size: 13px;")
        delete_preset_btn.clicked.connect(self.delete_current_preset)
        preset_layout.addWidget(delete_preset_btn)

        restore_preset_btn = QPushButton("Restore Defaults")
        restore_preset_btn.setMinimumHeight(38)
        restore_preset_btn.setStyleSheet("font-size: 13px;")
        restore_preset_btn.setToolTip("Bring back any built-in presets you deleted (your own presets are untouched)")
        restore_preset_btn.clicked.connect(self.restore_default_presets)
        preset_layout.addWidget(restore_preset_btn)

        layout.addLayout(preset_layout)

        # Settings panel for images
        self.image_settings = QGroupBox("Image Settings")
        image_layout = QVBoxLayout()
        image_layout.setSpacing(12)
        image_layout.setContentsMargins(15, 15, 15, 15)

        # Width
        width_layout = QHBoxLayout()
        width_label = QLabel("Width:")
        width_label.setMinimumWidth(120)
        width_layout.addWidget(width_label)
        self.width_input = QSpinBox()
        self.width_input.setRange(0, 10000)
        self.width_input.setValue(0)
        self.width_input.setSpecialValueText("Original")
        self.width_input.setMinimumWidth(120)
        self.width_input.setMinimumHeight(35)
        self.width_input.setStyleSheet("font-size: 13px;")
        width_layout.addWidget(self.width_input)
        width_layout.addStretch()
        image_layout.addLayout(width_layout)

        # Height
        height_layout = QHBoxLayout()
        height_label = QLabel("Height:")
        height_label.setMinimumWidth(120)
        height_layout.addWidget(height_label)
        self.height_input = QSpinBox()
        self.height_input.setRange(0, 10000)
        self.height_input.setValue(0)
        self.height_input.setSpecialValueText("Original")
        self.height_input.setMinimumWidth(120)
        self.height_input.setMinimumHeight(35)
        self.height_input.setStyleSheet("font-size: 13px;")
        height_layout.addWidget(self.height_input)
        height_layout.addStretch()
        image_layout.addLayout(height_layout)

        # Fit mode
        fit_layout = QHBoxLayout()
        fit_label = QLabel("Fit:")
        fit_label.setMinimumWidth(120)
        fit_layout.addWidget(fit_label)
        self.fit_combo = QComboBox()
        self.fit_combo.addItems(["max", "crop", "scale"])
        self.fit_combo.setMinimumWidth(120)
        self.fit_combo.setMinimumHeight(35)
        self.fit_combo.setStyleSheet("font-size: 13px;")
        fit_layout.addWidget(self.fit_combo)
        fit_layout.addStretch()
        image_layout.addLayout(fit_layout)

        # Output format
        output_format_layout = QHBoxLayout()
        output_format_label = QLabel("Output Format:")
        output_format_label.setMinimumWidth(120)
        output_format_layout.addWidget(output_format_label)
        self.output_format_combo = QComboBox()
        self.output_format_combo.addItems(["JPG", "PNG"])
        self.output_format_combo.setMinimumWidth(120)
        self.output_format_combo.setMinimumHeight(35)
        self.output_format_combo.setStyleSheet("font-size: 13px;")
        self.output_format_combo.currentTextChanged.connect(self.update_convert_button)
        self.output_format_combo.currentTextChanged.connect(self.update_pdf_settings_title)
        self.output_format_combo.currentTextChanged.connect(self.update_quality_enabled)
        output_format_layout.addWidget(self.output_format_combo)
        output_format_layout.addStretch()
        image_layout.addLayout(output_format_layout)

        # Strip metadata
        self.strip_checkbox = QCheckBox("Strip metadata")
        self.strip_checkbox.setMinimumHeight(35)
        self.strip_checkbox.setStyleSheet("font-size: 13px;")
        image_layout.addWidget(self.strip_checkbox)

        # Quality
        quality_layout = QVBoxLayout()
        quality_layout.setSpacing(8)
        quality_label = QLabel("Quality:")
        quality_label.setMinimumWidth(120)
        quality_layout.addWidget(quality_label)
        quality_slider_layout = QHBoxLayout()
        self.quality_slider = FocusSlider(Qt.Orientation.Horizontal)
        self.quality_slider.setRange(1, 100)
        self.quality_slider.setValue(85)
        self.quality_slider.setMinimumHeight(40)
        self.quality_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                height: 8px;
                background: #ddd;
                border-radius: 4px;
            }
            QSlider::handle:horizontal {
                background: #007bff;
                width: 20px;
                margin: -8px 0;
                border-radius: 10px;
            }
            QSlider::groove:horizontal:disabled {
                background: #ebebeb;
            }
            QSlider::handle:horizontal:disabled {
                background: #b8b8b8;
            }
        """)
        self.quality_slider.valueChanged.connect(self.update_quality_label)
        self._quality_touched = False
        self.quality_slider.valueChanged.connect(lambda _v: setattr(self, '_quality_touched', True))
        self.quality_label = QLabel("85")
        self.quality_label.setMinimumWidth(50)
        self.quality_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        quality_slider_layout.addWidget(self.quality_slider)
        quality_slider_layout.addWidget(self.quality_label)
        quality_layout.addLayout(quality_slider_layout)
        image_layout.addLayout(quality_layout)

        self.image_settings.setLayout(image_layout)
        layout.addWidget(self.image_settings)

        # Settings panel for PDF
        self.pdf_settings = QGroupBox("PDF Settings (PDF → JPG or PNG)")
        pdf_layout = QVBoxLayout()
        pdf_layout.setSpacing(12)
        pdf_layout.setContentsMargins(15, 15, 15, 15)

        # Pixel density
        density_layout = QHBoxLayout()
        density_label = QLabel("Pixel Density (DPI):")
        density_label.setMinimumWidth(120)
        density_layout.addWidget(density_label)
        self.density_input = QSpinBox()
        self.density_input.setRange(72, 1200)
        self.density_input.setValue(300)
        self.density_input.setSingleStep(50)
        self.density_input.setMinimumWidth(120)
        self.density_input.setMinimumHeight(35)
        self.density_input.setStyleSheet("font-size: 13px;")
        density_layout.addWidget(self.density_input)
        density_layout.addStretch()
        pdf_layout.addLayout(density_layout)

        # PDF Width
        pdf_width_layout = QHBoxLayout()
        pdf_width_label = QLabel("Width:")
        pdf_width_label.setMinimumWidth(120)
        pdf_width_layout.addWidget(pdf_width_label)
        self.pdf_width_input = QSpinBox()
        self.pdf_width_input.setRange(0, 10000)
        self.pdf_width_input.setValue(0)
        self.pdf_width_input.setSpecialValueText("Original")
        self.pdf_width_input.setMinimumWidth(120)
        self.pdf_width_input.setMinimumHeight(35)
        self.pdf_width_input.setStyleSheet("font-size: 13px;")
        pdf_width_layout.addWidget(self.pdf_width_input)
        pdf_width_layout.addStretch()
        pdf_layout.addLayout(pdf_width_layout)

        # PDF Height
        pdf_height_layout = QHBoxLayout()
        pdf_height_label = QLabel("Height:")
        pdf_height_label.setMinimumWidth(120)
        pdf_height_layout.addWidget(pdf_height_label)
        self.pdf_height_input = QSpinBox()
        self.pdf_height_input.setRange(0, 10000)
        self.pdf_height_input.setValue(0)
        self.pdf_height_input.setSpecialValueText("Original")
        self.pdf_height_input.setMinimumWidth(120)
        self.pdf_height_input.setMinimumHeight(35)
        self.pdf_height_input.setStyleSheet("font-size: 13px;")
        pdf_height_layout.addWidget(self.pdf_height_input)
        pdf_height_layout.addStretch()
        pdf_layout.addLayout(pdf_height_layout)

        # Pages
        pages_layout = QHBoxLayout()
        pages_label = QLabel("Pages:")
        pages_label.setMinimumWidth(120)
        pages_layout.addWidget(pages_label)
        self.pages_input = QLineEdit()
        self.pages_input.setPlaceholderText("all, or 1-3, or 1,3,5")
        self.pages_input.setText("all")
        self.pages_input.setMinimumHeight(35)
        self.pages_input.setStyleSheet("font-size: 13px;")
        pages_layout.addWidget(self.pages_input)
        pdf_layout.addLayout(pages_layout)

        self.pdf_settings.setLayout(pdf_layout)
        layout.addWidget(self.pdf_settings)

        # Status label for batch processing
        self.status_label = QLabel("")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet("font-weight: bold; color: #007bff; font-size: 13px;")
        layout.addWidget(self.status_label)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumHeight(25)
        self.progress_bar.setStyleSheet("font-size: 12px;")
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # Convert button
        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setEnabled(False)
        self.convert_btn.setStyleSheet("""
            QPushButton {
                background-color: #007bff;
                color: white;
                padding: 10px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #0056b3;
            }
            QPushButton:disabled {
                background-color: #ccc;
            }
        """)
        self.convert_btn.clicked.connect(self.convert_files)
        layout.addWidget(self.convert_btn)

        # Cancel button - only visible while a batch is running
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: white;
                padding: 10px;
                font-size: 14px;
                font-weight: bold;
                border-radius: 5px;
            }
            QPushButton:hover {
                background-color: #c82333;
            }
        """)
        self.cancel_btn.clicked.connect(self.cancel_batch)
        self.cancel_btn.setVisible(False)
        layout.addWidget(self.cancel_btn)

        # Finalize scroll area
        scroll_area.setWidget(scroll_content)
        main_layout.addWidget(scroll_area)

        # Initially show/hide settings based on file type
        self.update_settings_visibility()
        # Set initial quality-slider state for the default output format
        self.update_quality_enabled(self.output_format_combo.currentText())

    def update_quality_label(self, value):
        self.quality_label.setText(str(value))

    def browse_files(self):
        """Browse for multiple files"""
        filenames, _ = QFileDialog.getOpenFileNames(
            self,
            "Select File(s)",
            "",
            "Supported Files (*.pdf *.webp *.png *.avif *.jpg *.jpeg *.bmp *.tiff *.tif);;All Files (*)"
        )
        if filenames:
            self.on_files_dropped(filenames)

    def on_files_dropped(self, filepaths):
        """Handle multiple files being dropped or selected"""
        if self.batch_running:
            return  # the queue is frozen while a batch runs
        skipped = []
        for filepath in filepaths:
            if filepath not in self.input_files:
                # Validate file extension
                ext = Path(filepath).suffix.lower()
                if ext in ['.pdf', '.webp', '.png', '.avif', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif']:
                    self.input_files.append(filepath)
                    self.file_list.addItem(Path(filepath).name)
                else:
                    skipped.append(Path(filepath).name)

        if skipped:
            self.status_label.setText(f"Skipped {len(skipped)} file{'s' if len(skipped) > 1 else ''} — unsupported format")
        elif self.input_files:
            self.status_label.setText("")

        self.update_convert_button()
        self.update_settings_visibility()

    def remove_selected_files(self):
        """Remove selected files from the list"""
        if self.batch_running:
            return
        selected_items = self.file_list.selectedItems()
        if not selected_items:
            return

        for item in selected_items:
            index = self.file_list.row(item)
            self.file_list.takeItem(index)
            if index < len(self.input_files):
                self.input_files.pop(index)

        self.update_convert_button()
        self.update_settings_visibility()

    def clear_all_files(self):
        """Clear all files from the list"""
        if self.batch_running:
            return
        self.file_list.clear()
        self.input_files.clear()
        self.update_convert_button()
        self.update_settings_visibility()

    def update_convert_button(self):
        """Enable/disable convert button based on file list"""
        has_files = len(self.input_files) > 0
        self.convert_btn.setEnabled(has_files and not self.batch_running)
        fmt = self.output_format_combo.currentText() if hasattr(self, 'output_format_combo') else 'JPG'
        if has_files:
            count = len(self.input_files)
            self.convert_btn.setText(f"Convert {count} File{'s' if count > 1 else ''} to {fmt}")
        else:
            self.convert_btn.setText(f"Convert to {fmt}")

    def update_pdf_settings_title(self, fmt):
        """Keep the PDF settings title in sync with the selected output format"""
        if hasattr(self, 'pdf_settings'):
            self.pdf_settings.setTitle(f"PDF Settings (PDF → {fmt})")

    def update_quality_enabled(self, fmt):
        """Quality only affects JPG compression; grey it out for PNG"""
        enabled = fmt != 'PNG'
        self.quality_slider.setEnabled(enabled)
        self.quality_label.setEnabled(enabled)
        self.quality_slider.setToolTip(
            f"JPG quality ({self.quality_slider.value()})" if enabled
            else "Quality has no effect on PNG output")

    def update_settings_visibility(self):
        """Show/hide settings based on file type"""
        # If all files are PDFs, show only PDF settings
        # If all files are images, show only image settings
        # If mixed, show both
        if not self.input_files:
            self.image_settings.setVisible(True)
            self.pdf_settings.setVisible(True)
            return

        has_pdf = any(Path(f).suffix.lower() == '.pdf' for f in self.input_files)
        has_image = any(Path(f).suffix.lower() in ['.webp', '.png', '.avif', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif']
                       for f in self.input_files)

        # The output format dropdown lives in Image Settings but applies to
        # PDFs too, so keep the group visible for PDF-only queues.

        self.pdf_settings.setVisible(has_pdf)
        self.image_settings.setVisible(has_image or has_pdf)

    def on_preset_changed(self, preset_name):
        """Load preset settings"""
        if preset_name == "Custom" or preset_name not in self.presets:
            return

        settings = self.presets[preset_name]

        # Apply settings
        if "quality" in settings:
            self._quality_touched = True
            self.quality_slider.setValue(settings["quality"])
        if "width" in settings:
            self.width_input.setValue(settings["width"] or 0)
        if "height" in settings:
            self.height_input.setValue(settings["height"] or 0)
        if "fit" in settings:
            index = self.fit_combo.findText(settings["fit"])
            if index >= 0:
                self.fit_combo.setCurrentIndex(index)
        if "strip" in settings:
            self.strip_checkbox.setChecked(settings["strip"])
        if "pixel_density" in settings:
            self.density_input.setValue(settings["pixel_density"])
        if "pages" in settings:
            self.pages_input.setText(settings["pages"])
        if "width" in settings:
            self.pdf_width_input.setValue(settings["width"] or 0)
        if "height" in settings:
            self.pdf_height_input.setValue(settings["height"] or 0)

    def get_batch_output_name(self, input_path, fmt, claimed):
        """Output filename for a batch item. When several queued files share
        the same stem (e.g. photos/a.jpg and photos/b/a.jpg), disambiguate
        with a numeric suffix so one conversion can't overwrite or clobber
        another (or trigger a false overwrite prompt)."""
        # Compare case-insensitively: Windows/macOS filesystems treat
        # A.jpg and a.jpg as the same file.
        candidate = f"{input_path.stem}.{fmt}"
        if candidate.lower() in claimed:
            n = 2
            while f"{input_path.stem}_{n}.{fmt}".lower() in claimed:
                n += 1
            candidate = f"{input_path.stem}_{n}.{fmt}"
        claimed.add(candidate.lower())
        return str(self.output_dir / candidate)

    def convert_files(self):
        """Start batch file conversion"""
        if not self.input_files:
            return

        self.batch_output_names = {}

        # Ask for output directory for batch conversion
        if len(self.input_files) > 1:
            output_dir = QFileDialog.getExistingDirectory(
                self,
                "Select Output Directory",
                str(Path(self.input_files[0]).parent)
            )
            if not output_dir:
                return
            self.output_dir = Path(output_dir)

            # Pre-compute output names for the whole batch so same-stem files
            # are disambiguated instead of silently colliding. Needs
            # output_dir, so it must run after the directory is chosen.
            fmt = self.output_format_combo.currentText().lower()
            claimed = set()
            for f in self.input_files:
                self.batch_output_names[f] = self.get_batch_output_name(Path(f), fmt, claimed)
        else:
            # Single file - ask for specific filename
            input_path = Path(self.input_files[0])
            fmt = self.output_format_combo.currentText().lower()
            default_output = input_path.parent / f"{input_path.stem}.{fmt}"
            filter_str = "JPEG Image (*.jpg)" if fmt == 'jpg' else "PNG Image (*.png)"

            output_file, _ = QFileDialog.getSaveFileName(
                self,
                "Save As",
                str(default_output),
                filter_str
            )

            if not output_file:
                return

            self.output_dir = Path(output_file).parent
            self.single_output_file = output_file

        # Start batch conversion
        self.current_batch_index = 0
        self.batch_errors = []
        self.batch_successes = []
        self.batch_notes = []
        self.overwrite_all = False
        self.batch_cancelled = False
        self.batch_running = True
        self.convert_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.progress_bar.setVisible(True)
        self.convert_next_file()

    def _release_worker(self):
        """Drop the finished worker only after its thread has fully stopped.
        The worker emits finished/error from inside run(), so this slot can
        run while the QThread is still winding down; releasing the last
        reference at that point makes Qt abort ("Destroyed while thread is
        still running")."""
        worker = getattr(self, 'worker', None)
        if worker is not None:
            worker.wait()
        self.worker = None

    def cancel_batch(self):
        """User requested cancellation of the running batch"""
        self.batch_cancelled = True
        if hasattr(self, 'worker') and self.worker is not None:
            self.worker.cancel()

    def convert_next_file(self):
        """Convert the next file in the batch"""
        if self.batch_cancelled:
            self.on_batch_complete()
            return
        if self.current_batch_index >= len(self.input_files):
            # Batch complete
            self.on_batch_complete()
            return

        input_file = self.input_files[self.current_batch_index]
        input_path = Path(input_file)

        # Determine output file
        is_paged = input_path.suffix.lower() in ('.pdf', '.tif', '.tiff')  # may write stem_pageN files
        if len(self.input_files) == 1 and hasattr(self, 'single_output_file'):
            output_file = self.single_output_file
        elif getattr(self, 'batch_output_names', None) and input_file in self.batch_output_names:
            output_file = self.batch_output_names[input_file]
        else:
            fmt = self.output_format_combo.currentText().lower()
            output_file = str(self.output_dir / f"{input_path.stem}.{fmt}")

        # In batch mode, confirm before overwriting existing files. For PDFs
        # the output may be several page files (stem_pageN.ext), so check for
        # any existing collision, not just the base name (which is never
        # written for multi-page PDFs).
        base_out = Path(output_file)
        if is_paged:
            existing = sorted(base_out.parent.glob(f"{base_out.stem}_page*.{base_out.suffix.lstrip('.')}"))
            collision = existing or ([base_out] if base_out.exists() else [])
            collision_desc = ", ".join(p.name for p in existing[:3]) if existing else base_out.name
        else:
            collision = [base_out] if base_out.exists() else []
            collision_desc = base_out.name
        if len(self.input_files) > 1 and collision and not self.overwrite_all:
            reply = QMessageBox.question(
                self,
                "File Already Exists",
                f"{collision_desc} already exists. Overwrite?",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.YesToAll |
                QMessageBox.StandardButton.No |
                QMessageBox.StandardButton.Cancel
            )
            if reply == QMessageBox.StandardButton.Cancel:
                self.batch_cancelled = True
                self.on_batch_complete()
                return
            elif reply == QMessageBox.StandardButton.No:
                self.batch_errors.append((input_path.name, "Skipped — file already exists"))
                self.current_batch_index += 1
                self.convert_next_file()
                return
            elif reply == QMessageBox.StandardButton.YesToAll:
                self.overwrite_all = True

        # Update status
        current = self.current_batch_index + 1
        total = len(self.input_files)
        self.status_label.setText(f"Converting file {current} of {total}: {input_path.name}")
        self.progress_bar.setValue(0)

        # Get settings for this file type
        if input_path.suffix.lower() == '.pdf':
            settings = {
                'pixel_density': self.density_input.value(),
                'width': self.pdf_width_input.value() or None,
                'height': self.pdf_height_input.value() or None,
                'pages': self.pages_input.text().strip() or 'all',
                'quality': self.quality_slider.value() if self._quality_touched else 95,
                'output_format': self.output_format_combo.currentText().lower()
            }
        else:
            settings = {
                'quality': self.quality_slider.value(),
                'width': self.width_input.value() or None,
                'height': self.height_input.value() or None,
                'fit': self.fit_combo.currentText(),
                'strip': self.strip_checkbox.isChecked(),
                'output_format': self.output_format_combo.currentText().lower()
            }

        # Start conversion
        self._release_worker()
        self.worker = ConversionWorker(input_file, output_file, settings)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self.on_file_conversion_finished)
        self.worker.error.connect(self.on_file_conversion_error)
        self.worker.start()

    def on_file_conversion_finished(self, message):
        """Handle successful conversion of a single file in batch"""
        # Report exactly the file(s) this run wrote (a multi-page PDF/TIFF
        # produces stem_pageN.ext files rather than the base output path)
        name = Path(self.worker.input_file).name
        written = [Path(p).name for p in self.worker.written_files] or [Path(self.worker.output_file).name]
        self.batch_successes.append(", ".join(written))
        for note in self.worker.notes:
            self.batch_notes.append(f"{name}: {note}")
        self.current_batch_index += 1
        self.convert_next_file()

    def on_file_conversion_error(self, error_message):
        """Handle conversion error for a single file in batch"""
        if self.batch_cancelled:
            self.on_batch_complete()
            return
        failed_file = self.input_files[self.current_batch_index]
        self.batch_errors.append((Path(failed_file).name, error_message))
        self.current_batch_index += 1
        self.convert_next_file()

    def on_batch_complete(self):
        """Handle batch conversion completion"""
        self.batch_running = False
        self.progress_bar.setVisible(False)
        self.cancel_btn.setVisible(False)
        self.status_label.setText("")
        self.convert_btn.setEnabled(bool(self.input_files))
        self._release_worker()

        if self.batch_cancelled:
            QMessageBox.information(
                self,
                "Cancelled",
                "Conversion cancelled."
            )
            return

        # Show results
        total = len(self.input_files)
        success_count = len(self.batch_successes)
        error_count = len(self.batch_errors)

        notes_text = ("\n\nNote:\n" + "\n".join(f"- {n}" for n in self.batch_notes)) if self.batch_notes else ""

        if error_count == 0:
            if success_count == 1 and len(self.batch_successes[0].split(', ')) == 1:
                QMessageBox.information(
                    self,
                    "Success",
                    f"Successfully converted to {self.batch_successes[0]}!{notes_text}"
                )
            else:
                details = "\n".join(f"- {name}" for name in self.batch_successes)
                QMessageBox.information(
                    self,
                    "Success",
                    f"Successfully converted {success_count} file{'s' if success_count > 1 else ''}:\n{details}{notes_text}"
                )
        else:
            error_details = "\n".join([f"- {name}: {err}" for name, err in self.batch_errors])
            QMessageBox.warning(
                self,
                "Batch Conversion Complete",
                f"Converted {success_count} of {total} files.\n\n"
                f"Errors ({error_count}):\n{error_details}{notes_text}"
            )


    def save_current_as_preset(self):
        """Save current settings as a new preset"""
        # Ask for preset name
        preset_name, ok = QInputDialog.getText(
            self,
            "Save Preset",
            "Enter a name for this preset:"
        )

        if not ok or not preset_name.strip():
            return

        preset_name = preset_name.strip()

        if preset_name == "Custom":
            QMessageBox.warning(self, "Invalid Name", "'Custom' is reserved. Please choose another name.")
            return

        # Check if preset already exists
        if preset_name in self.presets:
            reply = QMessageBox.question(
                self,
                "Overwrite Preset",
                f"A preset named '{preset_name}' already exists. Overwrite it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # Get current settings (all settings, not just for current file type)
        preset_settings = {}

        # Image settings
        preset_settings['quality'] = self.quality_slider.value()
        preset_settings['width'] = self.width_input.value() or None
        preset_settings['height'] = self.height_input.value() or None
        preset_settings['fit'] = self.fit_combo.currentText()
        preset_settings['strip'] = self.strip_checkbox.isChecked()

        # PDF settings
        preset_settings['pixel_density'] = self.density_input.value()
        preset_settings['pages'] = self.pages_input.text().strip() or 'all'

        # Save preset
        self.presets[preset_name] = preset_settings
        PresetManager.deleted_defaults.discard(preset_name)
        if not PresetManager.save_presets(self.presets):
            QMessageBox.warning(self, "Save Failed",
                                f"Could not write presets to {PresetManager.PRESETS_FILE}")

        # Update combo box
        self.preset_combo.blockSignals(True)
        current_index = self.preset_combo.currentIndex()
        self.preset_combo.clear()
        self.preset_combo.addItem("Custom")
        self.preset_combo.addItems(sorted(self.presets.keys()))

        # Select the newly saved preset
        index = self.preset_combo.findText(preset_name)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        else:
            self.preset_combo.setCurrentIndex(current_index)

        self.preset_combo.blockSignals(False)

        QMessageBox.information(self, "Success", f"Preset '{preset_name}' saved successfully!")

    def restore_default_presets(self):
        """Bring back deleted built-in presets without touching existing ones"""
        restored = PresetManager.restore_defaults(self.presets)
        if not restored:
            QMessageBox.information(self, "Restore Defaults", "All built-in presets are already present.")
            return
        if not PresetManager.save_presets(self.presets):
            QMessageBox.warning(self, "Save Failed",
                                f"Could not write presets to {PresetManager.PRESETS_FILE}")
        current = self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Custom")
        self.preset_combo.addItems(sorted(self.presets.keys()))
        index = self.preset_combo.findText(current)
        self.preset_combo.setCurrentIndex(index if index >= 0 else 0)
        self.preset_combo.blockSignals(False)
        QMessageBox.information(self, "Restore Defaults", "Restored: " + ", ".join(restored))

    def delete_current_preset(self):
        """Delete the currently selected preset"""
        preset_name = self.preset_combo.currentText()

        if preset_name == "Custom":
            QMessageBox.warning(self, "Cannot Delete", "Cannot delete the 'Custom' preset.")
            return

        if preset_name not in self.presets:
            QMessageBox.warning(self, "No Preset", "Please select a preset to delete.")
            return

        # Confirm deletion
        reply = QMessageBox.question(
            self,
            "Delete Preset",
            f"Are you sure you want to delete the preset '{preset_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        # Delete preset
        del self.presets[preset_name]
        PresetManager.mark_deleted(preset_name)
        if not PresetManager.save_presets(self.presets):
            QMessageBox.warning(self, "Save Failed",
                                f"Could not write presets to {PresetManager.PRESETS_FILE}")

        # Update combo box
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Custom")
        self.preset_combo.addItems(sorted(self.presets.keys()))
        self.preset_combo.setCurrentIndex(0)  # Select "Custom"
        self.preset_combo.blockSignals(False)

        QMessageBox.information(self, "Success", f"Preset '{preset_name}' deleted successfully!")


    def closeEvent(self, event):
        """Wait for a running conversion before closing, so the worker
        thread never gets destroyed mid-run (which crashes the app)."""
        if hasattr(self, 'worker') and self.worker is not None and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "Conversion in Progress",
                "A conversion is still running. Cancel it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.batch_cancelled = True
            self.worker.cancel()
            self.worker.wait(10000)
        event.accept()


def main():
    init_logging()
    # Enable high DPI scaling for better display on high-resolution screens
    # Note: In PyQt6, high DPI scaling is enabled by default
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Modern cross-platform style
    converter = FileConverter()
    converter.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
