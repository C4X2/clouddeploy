import io
from pathlib import Path
from PIL import Image, ImageOps


def optimize_images(
    directory: str | Path,
    max_dimension: int = 1200,
    jpeg_quality: int = 80,
    quantize_png: bool = True,
    min_savings_ratio: float = 0.05,
    min_savings_bytes: int = 2048,
) -> tuple[int, int]:
    """
    Optimizes all .jpg/.jpeg/.png files under `directory` in place.
    Uses `min_savings_ratio` and `min_savings_bytes` so already-optimized
    images are not repeatedly re-encoded on subsequent runs.
    Returns (files_minified, total_bytes_saved).
    """
    root = Path(directory)
    if not root.exists():
        return 0, 0

    files_minified = 0
    bytes_saved = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        ext = path.suffix.lower()
        if ext not in {".jpg", ".jpeg", ".png"}:
            continue

        original_size = path.stat().st_size
        if original_size < min_savings_bytes:
            continue

        with Image.open(path) as raw_img:
            # Apply phone EXIF rotation before stripping EXIF metadata
            img = ImageOps.exif_transpose(raw_img)
            resized = False

            # Downscale images larger than max_dimension (keeps aspect ratio)
            if max(img.size) > max_dimension:
                img.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                resized = True

            buf = io.BytesIO()
            if ext in {".jpg", ".jpeg"}:
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(
                    buf,
                    format="JPEG",
                    quality=jpeg_quality,
                    optimize=True,
                    progressive=True,
                )
            elif ext == ".png":
                if quantize_png and img.mode not in ("P", "1", "L"):
                    img = img.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
                img.save(buf, format="PNG", optimize=True)

            new_bytes = buf.getvalue()

        saved = original_size - len(new_bytes)
        savings_ratio = saved / original_size if original_size > 0 else 0.0

        # Only overwrite if resized or if savings exceed the threshold (idempotent)
        if saved > 0 and (resized or (saved >= min_savings_bytes and savings_ratio >= min_savings_ratio)):
            path.write_bytes(new_bytes)
            files_minified += 1
            bytes_saved += saved

    return files_minified, bytes_saved
