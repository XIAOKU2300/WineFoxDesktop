from pathlib import Path


PLUGIN_DIR = Path(__file__).resolve().parent
DEFAULT_WATERMARK_PATH = PLUGIN_DIR / "assets" / "jiuhu_watermark_huizhi.png"


def _safe_float(value, default: float, minimum: float = None, maximum: float = None) -> float:
    try:
        number = float(value)
    except Exception:
        number = float(default)
    if minimum is not None:
        number = max(float(minimum), number)
    if maximum is not None:
        number = min(float(maximum), number)
    return number


def _save_watermarked_image(image, path: Path, original_format: str = ""):
    suffix = path.suffix.lower()
    fmt = (original_format or "").upper()
    if suffix in {".jpg", ".jpeg"} or fmt == "JPEG":
        image.convert("RGB").save(path, format="JPEG", quality=95, subsampling=0, optimize=True)
        return
    if suffix == ".webp" or fmt == "WEBP":
        image.save(path, format="WEBP", quality=95, method=6)
        return
    image.save(path, format="PNG", optimize=True, compress_level=6)


def apply_watermark_to_image_file(
    image_path,
    watermark_path=None,
    width_ratio: float = 0.24,
    opacity: float = 0.76,
    margin_ratio: float = 0.035,
    position: str = "bottom_left",
) -> dict:
    """Overlay the Jiuhu watermark on an output image in-place."""
    path = Path(str(image_path or ""))
    if not path.exists() or not path.is_file():
        return {"applied": False, "reason": "image_missing"}

    wm_path = Path(str(watermark_path or DEFAULT_WATERMARK_PATH))
    if not wm_path.exists() or not wm_path.is_file():
        return {"applied": False, "reason": "watermark_missing", "watermark_path": str(wm_path)}

    try:
        from PIL import Image, ImageFilter, ImageOps
    except Exception as exc:
        return {"applied": False, "reason": f"pillow_missing: {exc}"}

    try:
        with Image.open(path) as src:
            original_format = src.format or ""
            base = ImageOps.exif_transpose(src).convert("RGBA")
        with Image.open(wm_path) as wm_src:
            watermark = wm_src.convert("RGBA")

        if base.width <= 0 or base.height <= 0 or watermark.width <= 0 or watermark.height <= 0:
            return {"applied": False, "reason": "invalid_size"}

        width_ratio = _safe_float(width_ratio, 0.24, 0.12, 0.50)
        opacity = _safe_float(opacity, 0.76, 0.05, 1.0)
        margin_ratio = _safe_float(margin_ratio, 0.035, 0.005, 0.12)

        target_width = int(base.width * width_ratio)
        target_width = max(96, min(target_width, int(base.width * 0.50)))
        scale = target_width / float(watermark.width)
        target_height = max(1, int(watermark.height * scale))
        max_height = max(1, int(base.height * min(max(width_ratio * 0.75, 0.08), 0.28)))
        if target_height > max_height:
            scale = max_height / float(watermark.height)
            target_width = max(1, int(watermark.width * scale))
            target_height = max(1, int(watermark.height * scale))
        watermark = watermark.resize((target_width, target_height), Image.Resampling.LANCZOS)

        wr, wg, wb, wa = watermark.split()
        wa = wa.point(lambda p: int(p * opacity))
        watermark = Image.merge("RGBA", (wr, wg, wb, wa))

        margin = int(min(base.width, base.height) * margin_ratio)
        margin = max(10, margin)
        pos = str(position or "bottom_left").strip().lower()
        if pos in {"bottom_right", "right", "rb"}:
            x = base.width - watermark.width - margin
            y = base.height - watermark.height - margin
        elif pos in {"top_left", "left_top", "lt"}:
            x = margin
            y = margin
        elif pos in {"top_right", "right_top", "rt"}:
            x = base.width - watermark.width - margin
            y = margin
        else:
            x = margin
            y = base.height - watermark.height - margin
        x = max(0, min(x, base.width - watermark.width))
        y = max(0, min(y, base.height - watermark.height))

        shadow_alpha = wa.filter(ImageFilter.GaussianBlur(radius=max(2, int(target_width * 0.018))))
        shadow_alpha = shadow_alpha.point(lambda p: int(p * 0.30))
        shadow = Image.new("RGBA", watermark.size, (34, 28, 20, 0))
        shadow.putalpha(shadow_alpha)
        offset = max(1, int(min(base.width, base.height) * 0.004))
        base.alpha_composite(shadow, (max(0, x + offset), max(0, y + offset)))
        base.alpha_composite(watermark, (x, y))

        _save_watermarked_image(base, path, original_format=original_format)
        return {
            "applied": True,
            "watermark": "jiuhu_huizhi",
            "watermark_path": str(wm_path),
            "position": pos or "bottom_left",
            "width": base.width,
            "height": base.height,
            "bytes": path.stat().st_size,
        }
    except Exception as exc:
        return {"applied": False, "reason": str(exc)[:200], "watermark_path": str(wm_path)}
