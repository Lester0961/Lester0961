#!/usr/bin/env python3
"""Turn the supplied headshot into terminal-friendly ASCII for the SVG templates."""

from __future__ import annotations

import argparse
import html
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

RAMP = " .,:;irsXA253hMHGS#9B&@"


def portrait_to_ascii(image_path: Path, width: int) -> list[str]:
    """Crop the head-and-shoulders area and return proportional ASCII lines."""
    image = Image.open(image_path).convert("RGB")
    # The supplied photo is square; this removes the bright empty edge area while
    # retaining the hair, face, shoulders, and tie.
    left, top, right, bottom = (105, 25, 920, 1024)
    image = image.crop((left, top, right, bottom))
    image = ImageOps.grayscale(image)
    image = ImageOps.autocontrast(image, cutoff=1)
    image = image.filter(ImageFilter.MedianFilter(size=3))
    image = ImageEnhance.Contrast(image).enhance(1.35)
    height = max(1, round(image.height / image.width * width * 0.50))
    image = image.resize((width, height), Image.Resampling.LANCZOS)

    pixels = list(image.getdata())
    return [
        "".join(RAMP[pixel * (len(RAMP) - 1) // 255] for pixel in pixels[row * width : (row + 1) * width])
        for row in range(height)
    ]


def svg_tspans(lines: list[str]) -> str:
    output: list[str] = []
    for index, line in enumerate(lines):
        dy = "0" if index == 0 else "15"
        output.append(f'<tspan x="40" dy="{dy}">{html.escape(line)}</tspan>')
    return "\n        ".join(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("templates", type=Path, nargs="*")
    parser.add_argument("--width", type=int, default=44)
    parser.add_argument("--print", action="store_true", dest="print_only")
    args = parser.parse_args()

    art = portrait_to_ascii(args.image, args.width)
    if args.print_only or not args.templates:
        print("\n".join(art))
        return

    replacement = svg_tspans(art)
    for template in args.templates:
        content = template.read_text(encoding="utf-8")
        if "{{ASCII_PORTRAIT}}" not in content:
            raise SystemExit(f"{template} does not contain the ASCII placeholder")
        template.write_text(content.replace("{{ASCII_PORTRAIT}}", replacement), encoding="utf-8")


if __name__ == "__main__":
    main()
