"""Convert an untrusted raster image in a bounded subprocess without changing its original."""

import io
import json
import sys
import warnings
from pathlib import Path

MAX_PIXELS = 40_000_000
MAX_PREVIEW_BYTES = 2 * 1024 * 1024


def render(source, destination, max_edge, crop):
    from PIL import Image, ImageOps
    from pillow_heif import register_heif_opener

    register_heif_opener(decode_threads=1)
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(source) as original:
            if original.format not in {"JPEG", "PNG", "WEBP", "HEIF"}:
                raise ValueError("Unsupported image encoding.")
            if original.width * original.height > MAX_PIXELS:
                raise ValueError("Image dimensions exceed preview limits.")
            source_format = original.format
            image = ImageOps.exif_transpose(original)
            width, height = image.size
            if crop is not None:
                box = (int(crop[0] * width), int(crop[1] * height),
                       int(crop[2] * width), int(crop[3] * height))
                if box[2] <= box[0] or box[3] <= box[1]:
                    raise ValueError("Crop is smaller than one pixel.")
                image = image.crop(box)
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            rgba = image.convert("RGBA")
            preview = Image.new("RGB", rgba.size, "white")
            preview.paste(rgba, mask=rgba.getchannel("A"))
            # A new image excludes EXIF, GPS, thumbnails, and other source metadata.
            for quality in (90, 80, 65):
                buffer = io.BytesIO()
                preview.save(buffer, format="JPEG", quality=quality, optimize=True)
                if buffer.tell() <= MAX_PREVIEW_BYTES:
                    Path(destination).write_bytes(buffer.getvalue())
                    return {"source_format": source_format,
                            "oriented_original_width": width,
                            "oriented_original_height": height,
                            "preview_width": preview.width, "preview_height": preview.height}
            raise ValueError("Preview exceeds response limits. Request a smaller crop or max_edge.")


def main():
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    resource.setrlimit(resource.RLIMIT_FSIZE, (3 * 1024 * 1024, 3 * 1024 * 1024))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    source, destination, metadata, edge, crop = sys.argv[1:]
    result = render(source, destination, int(edge), json.loads(crop))
    Path(metadata).write_text(json.dumps(result))


if __name__ == "__main__":
    main()
