#!/usr/bin/env python3
"""
File Converter - Convert images (WEBP, PNG, AVIF, BMP, TIFF, JPG) and PDFs to JPG or PNG
"""
import sys
import os
import json
from pathlib import Path
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QPushButton, QComboBox,
                             QSpinBox, QCheckBox, QSlider, QLineEdit, QGroupBox,
                             QFileDialog, QProgressBar, QMessageBox, QInputDialog,
                             QListWidget, QAbstractItemView, QScrollArea)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent
from PIL import Image
import pdf2image
from pdf2image import pdf2image as _pdf2image_impl

# Prevent console windows from flashing when poppler subprocesses run on Windows.
# pdf2image binds Popen into its own namespace at import time
# (from subprocess import Popen), so subprocess.Popen alone is not enough;
# the module attribute must be patched as well.
if sys.platform == 'win32':
    import subprocess as _subprocess
    _OrigPopen = _subprocess.Popen
    class _PopenNoWindow(_OrigPopen):
        def __init__(self, *args, **kwargs):
            if 'creationflags' not in kwargs:
                kwargs['creationflags'] = _subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    _subprocess.Popen = _PopenNoWindow
    _pdf2image_impl.Popen = _PopenNoWindow


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

    def run(self):
        try:
            file_ext = Path(self.input_file).suffix.lower()

            if file_ext == '.pdf':
                self.convert_pdf_to_jpg()
            elif file_ext in ['.webp', '.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif', '.avif']:
                self.convert_image()
            else:
                self.error.emit(f"Unsupported file format: {file_ext}")
                return

            self.finished.emit(f"Successfully converted to {self.output_file}")
        except Exception as e:
            self.error.emit(f"Conversion failed: {str(e)}")

    def convert_pdf_to_jpg(self):
        """Convert PDF to JPG with specified settings"""
        pixel_density = self.settings.get('pixel_density', 300)
        width = self.settings.get('width')
        height = self.settings.get('height')
        pages = self.settings.get('pages', 'all')

        # Parse and validate page specification before rendering anything
        page_list = None
        if pages != 'all':
            page_list = self.parse_pages(pages)

        # Only render the pages we need; pdf2image renders every page
        # between first_page and last_page, so narrow the range for
        # selections like "1,3" instead of loading the whole document.
        convert_kwargs = {'dpi': pixel_density, 'fmt': 'jpg'}
        if page_list:
            convert_kwargs['first_page'] = min(page_list)
            convert_kwargs['last_page'] = max(page_list)

        images = pdf2image.convert_from_path(self.input_file, **convert_kwargs)

        total_pages = len(images)
        self.progress.emit(10)

        # Filter pages if specified
        if page_list:
            first = convert_kwargs['first_page']
            images = [images[p - first] for p in page_list if p - first < total_pages]

        # Resize if width/height specified
        if width or height:
            images = [self.resize_image(img, width, height, 'max') for img in images]

        self.progress.emit(50)

        # Save images
        output_path = Path(self.output_file)
        if len(images) == 1:
            images[0].save(self.output_file, 'JPEG', quality=95)
        else:
            # Multiple pages - save with page numbers
            for i, img in enumerate(images, 1):
                page_output = output_path.parent / f"{output_path.stem}_page{i}.jpg"
                img.save(str(page_output), 'JPEG', quality=95)

        self.progress.emit(100)

    def convert_image(self):
        """Convert image to JPG or PNG with specified settings"""
        quality = self.settings.get('quality', 85)
        width = self.settings.get('width')
        height = self.settings.get('height')
        fit = self.settings.get('fit', 'max')
        strip = self.settings.get('strip', False)
        output_format = self.settings.get('output_format', 'jpg').lower()

        self.progress.emit(20)

        img = Image.open(self.input_file)

        # Capture EXIF from the original file before any compositing:
        # flattening onto a new background image drops .info (and with it
        # the EXIF bytes), which would silently disable "preserve metadata".
        exif_data = img.info.get('exif', b'')

        if output_format == 'png':
            # Preserve alpha for PNG; only normalize palette mode
            if img.mode == 'P':
                img = img.convert('RGBA')
            elif img.mode not in ('RGB', 'RGBA', 'L', 'LA'):
                img = img.convert('RGBA')
        else:
            # Flatten alpha to white background for JPEG
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')

        self.progress.emit(50)

        if width or height:
            img = self.resize_image(img, width, height, fit)

        self.progress.emit(80)

        if output_format == 'png':
            save_kwargs = {'format': 'PNG'}
            if not strip and exif_data:
                save_kwargs['exif'] = exif_data
        else:
            save_kwargs = {'format': 'JPEG', 'quality': quality}
            if not strip and exif_data:
                save_kwargs['exif'] = exif_data

        img.save(self.output_file, **save_kwargs)

        self.progress.emit(100)

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
                start, end = part.split('-')
                start, end = int(start), int(end)
                if start < 1 or end < 1:
                    raise ValueError(f"Invalid page range '{part}': pages start at 1")
                if start > end:
                    raise ValueError(f"Invalid page range '{part}': start is after end")
                pages.extend(range(start, end + 1))
            else:
                page = int(part)
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
    _app_dir.mkdir(parents=True, exist_ok=True)
    PRESETS_FILE = str(_app_dir / "presets.json")

    DEFAULT_PRESETS = {
        "Web Optimized (WEBP/PNG → JPG)": {
            "quality": 85,
            "width": None,
            "height": None,
            "fit": "max",
            "strip": True
        },
        "High Quality (WEBP/PNG → JPG)": {
            "quality": 95,
            "width": None,
            "height": None,
            "fit": "max",
            "strip": False
        },
        "Thumbnail (WEBP/PNG → JPG)": {
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

    @classmethod
    def load_presets(cls):
        """Load presets from file or use defaults"""
        if os.path.exists(cls.PRESETS_FILE):
            try:
                with open(cls.PRESETS_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return cls.DEFAULT_PRESETS.copy()

    @classmethod
    def save_presets(cls, presets):
        """Save presets to file"""
        with open(cls.PRESETS_FILE, 'w') as f:
            json.dump(presets, f, indent=2)


class FileConverter(QMainWindow):
    """Main application window"""

    def __init__(self):
        super().__init__()
        self.input_files = []  # Changed to list for batch processing
        self.presets = PresetManager.load_presets()
        self.current_batch_index = 0
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("File Converter - PDF/Images to JPG or PNG")
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
        """)
        self.quality_slider.valueChanged.connect(self.update_quality_label)
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
        self.pdf_settings = QGroupBox("PDF Settings (PDF → JPG)")
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

        # Finalize scroll area
        scroll_area.setWidget(scroll_content)
        main_layout.addWidget(scroll_area)

        # Initially show/hide settings based on file type
        self.update_settings_visibility()

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

        self.update_convert_button()
        self.update_settings_visibility()

    def remove_selected_files(self):
        """Remove selected files from the list"""
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
        self.file_list.clear()
        self.input_files.clear()
        self.update_convert_button()
        self.update_settings_visibility()

    def update_convert_button(self):
        """Enable/disable convert button based on file list"""
        has_files = len(self.input_files) > 0
        self.convert_btn.setEnabled(has_files)
        fmt = self.output_format_combo.currentText() if hasattr(self, 'output_format_combo') else 'JPG'
        if has_files:
            count = len(self.input_files)
            self.convert_btn.setText(f"Convert {count} File{'s' if count > 1 else ''} to {fmt}")
        else:
            self.convert_btn.setText(f"Convert to {fmt}")

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

        self.pdf_settings.setVisible(has_pdf)
        self.image_settings.setVisible(has_image)

    def on_preset_changed(self, preset_name):
        """Load preset settings"""
        if preset_name == "Custom" or preset_name not in self.presets:
            return

        settings = self.presets[preset_name]

        # Apply settings
        if "quality" in settings:
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

    def convert_files(self):
        """Start batch file conversion"""
        if not self.input_files:
            return

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
        else:
            # Single file - ask for specific filename
            input_path = Path(self.input_files[0])
            is_pdf = input_path.suffix.lower() == '.pdf'
            fmt = 'jpg' if is_pdf else self.output_format_combo.currentText().lower()
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
        self.overwrite_all = False
        self.convert_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.convert_next_file()

    def convert_next_file(self):
        """Convert the next file in the batch"""
        if self.current_batch_index >= len(self.input_files):
            # Batch complete
            self.on_batch_complete()
            return

        input_file = self.input_files[self.current_batch_index]
        input_path = Path(input_file)

        # Determine output file
        is_pdf = input_path.suffix.lower() == '.pdf'
        if len(self.input_files) == 1 and hasattr(self, 'single_output_file'):
            output_file = self.single_output_file
        else:
            fmt = 'jpg' if is_pdf else self.output_format_combo.currentText().lower()
            output_file = str(self.output_dir / f"{input_path.stem}.{fmt}")

        # In batch mode, confirm before overwriting existing files
        if len(self.input_files) > 1 and Path(output_file).exists() and not self.overwrite_all:
            reply = QMessageBox.question(
                self,
                "File Already Exists",
                f"{Path(output_file).name} already exists. Overwrite?",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.YesToAll |
                QMessageBox.StandardButton.No |
                QMessageBox.StandardButton.Cancel
            )
            if reply == QMessageBox.StandardButton.Cancel:
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
        if is_pdf:
            settings = {
                'pixel_density': self.density_input.value(),
                'width': self.pdf_width_input.value() or None,
                'height': self.pdf_height_input.value() or None,
                'pages': self.pages_input.text().strip() or 'all'
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
        self.worker = ConversionWorker(input_file, output_file, settings)
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self.on_file_conversion_finished)
        self.worker.error.connect(self.on_file_conversion_error)
        self.worker.start()

    def on_file_conversion_finished(self, message):
        """Handle successful conversion of a single file in batch"""
        self.batch_successes.append(self.input_files[self.current_batch_index])
        self.current_batch_index += 1
        self.convert_next_file()

    def on_file_conversion_error(self, error_message):
        """Handle conversion error for a single file in batch"""
        failed_file = self.input_files[self.current_batch_index]
        self.batch_errors.append((Path(failed_file).name, error_message))
        self.current_batch_index += 1
        self.convert_next_file()

    def on_batch_complete(self):
        """Handle batch conversion completion"""
        self.progress_bar.setVisible(False)
        self.status_label.setText("")
        self.convert_btn.setEnabled(True)

        # Show results
        total = len(self.input_files)
        success_count = len(self.batch_successes)
        error_count = len(self.batch_errors)

        if error_count == 0:
            QMessageBox.information(
                self,
                "Success",
                f"Successfully converted {success_count} file{'s' if success_count > 1 else ''}!"
            )
        else:
            error_details = "\n".join([f"- {name}: {err}" for name, err in self.batch_errors])
            QMessageBox.warning(
                self,
                "Batch Conversion Complete",
                f"Converted {success_count} of {total} files.\n\n"
                f"Errors ({error_count}):\n{error_details}"
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
        PresetManager.save_presets(self.presets)

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
        PresetManager.save_presets(self.presets)

        # Update combo box
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("Custom")
        self.preset_combo.addItems(sorted(self.presets.keys()))
        self.preset_combo.setCurrentIndex(0)  # Select "Custom"
        self.preset_combo.blockSignals(False)

        QMessageBox.information(self, "Success", f"Preset '{preset_name}' deleted successfully!")


def main():
    # Enable high DPI scaling for better display on high-resolution screens
    # Note: In PyQt6, high DPI scaling is enabled by default
    app = QApplication(sys.argv)
    app.setStyle('Fusion')  # Modern cross-platform style
    converter = FileConverter()
    converter.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
