"""Turn an image into a header-logo extension (see :meth:`minimal.extensions.registry.Registry.register_logo`).

The image is cropped to its opaque area, shrunk to the requested width and
reduced to a few colours; pixel art small enough keeps its exact colours.
Needs Pillow, which minimal does not depend on::

    uv run --with pillow python -m minimal.extensions.logo IMAGE [-w 24] [-c 16] [--nearest] [-o FILE]

Without ``-o`` the extension source is printed; save it in an extensions
folder (e.g. ``~/.minimal/extensions/widgets/logo.py``) and ``/reload``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

KEYS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
"""Pixel characters, one per colour (none is transparent)."""


def convert(
    path: str | Path, width: int = 24, max_colors: int = 16, nearest: bool = False
) -> tuple[list[str], dict[str, str]]:
    """The ``art`` and ``colors`` of :meth:`~minimal.extensions.registry.Registry.register_logo` for the image at ``path``.

    Pixels with alpha below 128 are transparent. The image only shrinks:
    one narrower than ``width`` keeps its size. ``nearest`` resizes without
    smoothing, which keeps pixel art crisp.

    Raises:
        ValueError: if ``width`` or ``max_colors`` is out of range or the image is fully transparent.
        OSError: if the image cannot be read.
        ImportError: if Pillow is not installed.
    """
    if width < 1:
        raise ValueError("width must be at least 1")
    if not 1 <= max_colors <= len(KEYS):
        raise ValueError(f"colors must be between 1 and {len(KEYS)}")
    from PIL import Image

    img = Image.open(path).convert("RGBA")
    bbox = img.getchannel("A").point(lambda a: 255 if a >= 128 else 0).getbbox()
    if bbox is None:
        raise ValueError(f"{path}: the image is fully transparent")
    img = img.crop(bbox)
    if img.width > width:
        resample = Image.Resampling.NEAREST if nearest else Image.Resampling.LANCZOS
        img = img.resize((width, max(1, round(img.height * width / img.width))), resample)

    w, h = img.size
    coords = [(x, y) for y in range(h) for x in range(w)]
    access = img.load()
    pixels = [access[xy] for xy in coords]
    opaque = [a >= 128 for _, _, _, a in pixels]
    rgb = [p[:3] for p in pixels]
    if len({c for c, o in zip(rgb, opaque) if o}) > max_colors:
        quantized = img.convert("RGB").quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
        palette, indices = quantized.getpalette(), quantized.load()
        rgb = [tuple(palette[3 * indices[xy] : 3 * indices[xy] + 3]) for xy in coords]
    keys: dict[tuple[int, ...], str] = {}
    for color, is_opaque in zip(rgb, opaque):
        if is_opaque and color not in keys:
            keys[color] = KEYS[len(keys)]
    art = [
        "".join(keys[rgb[i]] if opaque[i] else "." for i in range(y * w, (y + 1) * w))
        for y in range(h)
    ]
    colors = {key: "#{:02x}{:02x}{:02x}".format(*color) for color, key in keys.items()}
    return art, colors


def extension_source(art: list[str], colors: dict[str, str], description: str) -> str:
    """Python source of an extension that registers ``art`` and ``colors`` as the header logo."""
    rows = "".join(f"    {row!r},\n" for row in art)
    entries = "".join(f"    {key!r}: {value!r},\n" for key, value in colors.items())
    return (
        f"{description!r}\n\n"
        '__version__ = "1.0"\n\n'
        f"ART = [\n{rows}]\n\n"
        f"COLORS = {{\n{entries}}}\n\n\n"
        "def register(minimal):\n"
        "    minimal.register_logo(ART, COLORS)\n"
    )


def main(argv: list[str] | None = None) -> None:
    """Command line: convert an image and print or write the extension."""
    parser = argparse.ArgumentParser(
        prog="python -m minimal.extensions.logo", description="Turn an image into a minimal header-logo extension."
    )
    parser.add_argument("image")
    parser.add_argument(
        "-w",
        "--width",
        type=int,
        default=24,
        help="width in pixels, one terminal column each; the height keeps the aspect ratio, "
        "two pixel rows per terminal row",
    )
    parser.add_argument("-c", "--colors", type=int, default=16, help="most colours to keep (1-62)")
    parser.add_argument("--nearest", action="store_true", help="resize without smoothing, for pixel art")
    parser.add_argument("-o", "--output", type=Path, help="extension file to write (default: print it)")
    args = parser.parse_args(argv)
    try:
        art, colors = convert(args.image, args.width, args.colors, args.nearest)
    except ImportError:
        print("minimal.extensions.logo needs Pillow: uv run --with pillow python -m minimal.extensions.logo IMAGE", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError) as e:
        print(f"minimal.extensions.logo: {e}", file=sys.stderr)
        sys.exit(1)
    source = extension_source(art, colors, f"Header logo converted from {Path(args.image).name}.")
    if args.output is None:
        print(source, end="")
        return
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(source, encoding="utf-8")
    print(f"Wrote {args.output}; run /reload in minimal to show it.")


if __name__ == "__main__":
    main()
