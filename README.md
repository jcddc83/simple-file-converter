<img width="946" height="491" alt="simple file converter screenshot 9-2026" src="https://github.com/user-attachments/assets/90ad8594-dab8-48b8-9e6a-597d1216321d" />
# Simple File Converter

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform: Windows](https://img.shields.io/badge/Platform-Windows-blue.svg)](https://github.com/jcddc83/simple-file-converter/releases)
[![Release](https://img.shields.io/github/v/release/jcddc83/simple-file-converter.svg)](https://github.com/jcddc83/simple-file-converter/releases)

A standalone desktop app for converting images and PDFs — drag, drop, convert. No internet connection required, no file size limits, no subscriptions.

> **Status: Personal project / work in progress.** Shared as-is for anyone who finds it useful. Issues and PRs may not be addressed. Fork freely.

---

## Features

- **Input formats:** AVIF, WEBP, PNG, JPG, BMP, TIFF, PDF
- **Output formats:** JPG or PNG (selectable per conversion, for images and PDFs)
- **Batch conversion:** queue multiple files and convert in one go
- **PDF support:** choose DPI, page range, and output dimensions
- **Resize options:** fit within bounds, crop to exact dimensions, or stretch
- **Presets:** save and reload your favourite settings
- **Metadata control:** strip or preserve EXIF data
- **Fully offline:** files never leave your computer

---

## Download & Run (Windows)

1. Go to the **Releases** page of this repo and download `FileConverter.exe`
2. Double-click to run — no installation needed

> **Windows SmartScreen warning:** Because the exe is not code-signed, Windows may show a
> "Windows protected your PC" message. Click **More info → Run anyway** to proceed.
> The source code is fully visible in this repo.

---

## Mac / Linux

No pre-built binary is provided. Mac unsigned binaries trigger a security block that requires non-trivial workarounds, so the cleanest option is to run from source:

```bash
git clone https://github.com/jcddc83/simple-file-converter.git
cd simple-file-converter
pip install -r requirements.txt
# Mac/Linux: install poppler via Homebrew or apt
brew install poppler          # macOS
sudo apt install poppler-utils  # Ubuntu/Debian
python converter.py
```

---

## Usage

1. **Add files** — drag and drop onto the drop zone, or click **Add Files...**
2. **Choose settings** — pick a preset or configure manually
3. **Click Convert** — for a single file you'll be prompted for a save location; for multiple files you'll choose an output folder

---

## Settings Reference

### Image settings (applies to AVIF, WEBP, PNG, JPG, BMP, TIFF)

| Setting | Description |
|---|---|
| Width / Height | Target dimensions in pixels. `0` = keep original. |
| Fit | `max` — fit within bounds, keep aspect ratio. `crop` — crop to exact size. `scale` — stretch to exact size. |
| Output Format | `JPG` or `PNG`. PNG preserves transparency; JPG composites on white. |
| Strip metadata | Remove EXIF data from the output file. |
| Quality | JPG compression quality, 1–100. Has no effect when output is PNG. |

### PDF settings

| Setting | Description |
|---|---|
| Pixel Density (DPI) | Resolution of the output image. Default 300. Higher = larger file, more detail. |
| Width / Height | Optional output dimensions. `0` = determined by DPI. |
| Pages | `all`, a range (`1-3`), or specific pages (`1,3,5`). |

**Output format (JPG/PNG)** is chosen in Image Settings and applies to images and PDFs alike. The Quality slider (and its effect) applies to JPG output; it is greyed out for PNG.

---

## Presets

Presets are saved automatically to `%APPDATA%\FileConverter\presets.json` on Windows (or `~/.config/FileConverter/presets.json` on Mac/Linux). They survive app updates and exe rebuilds — you won't lose them when you download a new version.

**Built-in presets:**

Presets control quality, size, DPI and metadata. The output format (JPG/PNG) is chosen separately with the Output Format dropdown — combine freely (e.g. **High Quality** + **PNG** for a lossless conversion).

| Preset | Settings |
|---|---|
| Web Optimized | Quality 85, strip metadata |
| High Quality | Quality 95, preserve metadata |
| Thumbnail | 400×400 max, quality 80, strip metadata |
| PDF Standard (300 DPI) | 300 DPI, all pages |
| PDF High Quality (600 DPI) | 600 DPI, all pages |
| PDF Web (150 DPI) | 150 DPI, all pages |

---

## Building from Source (Windows exe)

```powershell
pip install -r requirements.txt
pip install pyinstaller
python -m PyInstaller --onefile --windowed --collect-all Pillow --collect-all pdf2image --name FileConverter converter.py
```

The exe will be in the `dist/` folder. Use `python -m PyInstaller` (not bare `pyinstaller`) to ensure PyInstaller runs in the same Python environment where your dependencies are installed.

To rebuild cleanly:

```powershell
Remove-Item -Recurse -Force dist, build, FileConverter.spec
python -m PyInstaller --onefile --windowed --collect-all Pillow --collect-all pdf2image --name FileConverter converter.py
```

### Dependencies

- **Python 3.8+**
- **PyQt6** — GUI
- **Pillow** — image processing (AVIF support included in modern pip wheels)
- **pdf2image** — PDF conversion
- **poppler** — required by pdf2image; must be installed separately

  - Windows: download from [oschwartz10612/poppler-windows](https://github.com/oschwartz10612/poppler-windows/releases/), extract, add `bin/` to PATH
  - macOS: `brew install poppler`
  - Linux: `sudo apt install poppler-utils`

---

## Troubleshooting

**"No module named 'PyQt6'" when running the exe**
Use `python -m PyInstaller` instead of bare `pyinstaller` — this ensures the build uses the same environment where PyQt6 is installed.

**PDF conversion fails**
Poppler is not installed or not in PATH. Test with `pdftoppm -h` in a terminal — it should print help text.

**AVIF files don't convert**
Your Pillow installation may not include AVIF support. Try `pip install pillow-avif-plugin` and rebuild.

---

## License

MIT — see [LICENSE](LICENSE)
