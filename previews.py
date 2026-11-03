"""Preview content for one file.

Returns (kind, payload): ("image", png_bytes), ("text", str) or ("info", str).
PDF, video and audio previews need optional tools; without them an info message says so.
"""
import io
import os
import shutil
import subprocess

from PIL import Image
from PIL.ExifTags import TAGS

from engine import IMAGE_EXTS, VIDEO_EXTS

TEXT_BYTES = 4000
HEX_BYTES = 256
PDF_EXTS = {".pdf"}
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus"}


def _png(img):
    img = img.convert("RGBA")
    img.thumbnail((600, 600))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _pdf(path):
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ("info", "Install pypdfium2 for PDF previews.")
    doc = pdfium.PdfDocument(path)
    try:
        return ("image", _png(doc[0].render(scale=1.5).to_pil()))
    finally:
        doc.close()


def _frame(path, seek):
    args = ["-ss", "1"] if seek else []
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", *args, "-i", path, "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, timeout=30,
    )
    return proc.stdout if proc.returncode == 0 else b""


def _video(path):
    if not shutil.which("ffmpeg"):
        return ("info", "Install ffmpeg for video thumbnails.")
    # Clips shorter than the 1-second seek have no frame there, so retry from the start.
    data = _frame(path, seek=True) or _frame(path, seek=False)
    if not data:
        return ("info", "No video frame could be read.")
    return ("image", _png(Image.open(io.BytesIO(data))))


def _audio(path):
    try:
        import mutagen
    except ImportError:
        return ("info", "Install mutagen for audio tags.")
    f = mutagen.File(path, easy=True)
    if f is None:
        return ("info", "Not a readable audio file.")
    lines = [f"{k}: {', '.join(v)}" for k, v in (f.tags or {}).items()]
    if f.info:
        lines.insert(0, f"Length: {f.info.length:.1f} s")
    return ("text", "\n".join(lines) or "No tags.")


def hexdump(data):
    rows = []
    for off in range(0, len(data), 16):
        chunk = data[off:off + 16]
        hexes = " ".join(f"{b:02x}" for b in chunk).ljust(47)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append(f"{off:08x}  {hexes}  {text}")
    return "\n".join(rows)


def _text(path):
    with open(path, "rb") as f:
        data = f.read(TEXT_BYTES)
    if b"\0" in data:
        with open(path, "rb") as f:
            return ("text", "Binary file, hex view:\n\n" + hexdump(f.read(HEX_BYTES)))
    return ("text", data.decode("utf-8", errors="replace"))


def image_info(path):
    """Photo details shown above the preview: size, and camera or date if the file has them."""
    with Image.open(path) as im:
        lines = [f"Dimensions: {im.width} x {im.height}"]
        exif = im.getexif()
        for tag_id, value in exif.items():
            name = TAGS.get(tag_id)
            if name in ("Make", "Model", "DateTime"):
                lines.append(f"{name}: {value}")
    return "\n".join(lines)


def exif_summary(path):
    """(date taken, camera) for a photo, or ("", "") when the file has no such details."""
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            taken = ex.get_ifd(0x8769).get(0x9003) or ex.get(0x0132) or ""  # DateTimeOriginal, else DateTime
            camera = " ".join(str(ex.get(t, "")).strip() for t in (0x010F, 0x0110)).strip()  # Make, Model
            return str(taken), camera
    except Exception:
        return "", ""


def preview(path):
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in IMAGE_EXTS:
            return ("image", _png(Image.open(path)))
        if ext in PDF_EXTS:
            return _pdf(path)
        if ext in VIDEO_EXTS:
            return _video(path)
        if ext in AUDIO_EXTS:
            return _audio(path)
        return _text(path)
    except Exception as e:  # a preview must never crash the window; corrupt files raise many types
        return ("info", f"Preview unavailable: {e}")
