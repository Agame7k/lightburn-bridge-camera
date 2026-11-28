#!/usr/bin/env python3
"""
Rebuild placeholder.jpg so the Bridge can animate it without an image library.

  python3 make_placeholder.py [source.jpg]      (default: the current placeholder.jpg)

Needs Pillow (developer machine only; the Bridge itself never runs this).

Adds a status bar under the "Camera not connected" text with a pulsing dot and a
"waiting" timer, then saves the image with a restart marker after every MCU. That lets
lbcam_stream.py swap individual 16x16 blocks of the compressed image (the dot and the
timer digits) by re-encoding only those blocks in pure Python. Everything it needs - where
the live cells are, the colours and the glyph bitmaps - travels in a JSON COM segment.
"""
import base64
import json
import os
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "placeholder.jpg")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
MCU = 16                  # 4:2:0 -> 16x16 pixel MCUs
CELL_W, CELL_H = 16, 32   # one live cell = 1 x 2 MCUs
CLOCK_CELLS = 6           # the timer strip: 96 x 32 pixels
ROW_Y = 608               # top of the live cells (multiple of MCU)
PANEL = (38, 40, 47)      # status bar background (uniform, so cells can be redrawn on it)
TEXT = (154, 160, 170)    # matches the grey sub-text above
DOT_ON, DOT_OFF = (74, 222, 128), (40, 84, 58)
LABEL = "Bridge online · waiting for a camera"
CLOCK = "00:00:00"
BASELINE = 23            # text baseline inside a cell
GLYPHS = "0123456789:"


def glyph(ch, font):
    """Antialiased coverage of one character, as wide as its advance and CELL_H tall (0..255)."""
    w = max(1, round(font.getlength(ch)))
    im = Image.new("L", (w, CELL_H), 0)
    ImageDraw.Draw(im).text((0, BASELINE), ch, fill=255, font=font, anchor="ls")
    return im


def strip(text, font):
    """The timer text laid out left-aligned in its strip, the same way lbcam_stream.py does it."""
    im = Image.new("L", (CLOCK_CELLS * CELL_W, CELL_H), 0)
    x = 0
    for ch in text:
        g = glyph(ch, font)
        im.paste(g, (x, 0))
        x += g.width
    return im


def dot():
    big = Image.new("L", (CELL_W * 4, CELL_H * 4), 0)
    r = 5 * 4
    cx, cy = big.width // 2, big.height // 2
    ImageDraw.Draw(big).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
    return big.resize((CELL_W, CELL_H), Image.LANCZOS)


def paste_alpha(img, alpha, x, y, color):
    img.paste(Image.new("RGB", alpha.size, color), (x, y), alpha)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else OUT
    img = Image.open(src).convert("RGB")
    assert img.size == (1280, 720), img.size
    font = ImageFont.truetype(FONT, 20)

    label_w = int(ImageDraw.Draw(img).textlength(LABEL, font=font))
    gap = MCU
    content = CELL_W + MCU // 2 + label_w + gap + CLOCK_CELLS * CELL_W
    x0 = (img.width - content) // 2 // MCU * MCU       # dot cell, MCU aligned
    label_x = x0 + CELL_W + MCU // 2
    clock_x = -(-(label_x + label_w + gap) // MCU) * MCU  # round up to MCU
    x1 = clock_x + int(font.getlength(CLOCK))

    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x0 - 16, ROW_Y - 6, x1 + 16, ROW_Y + CELL_H + 6), radius=19, fill=PANEL)
    d.text((label_x, ROW_Y + CELL_H / 2), LABEL, fill=TEXT, font=font, anchor="lm")

    # draw the initial state too, so the image still looks right where it is shown as-is
    paste_alpha(img, dot(), x0, ROW_Y, DOT_ON)
    paste_alpha(img, strip(CLOCK, font), clock_x, ROW_Y, TEXT)

    spec = {
        "lbcam_live": 1,
        "cell": [CELL_W, CELL_H],
        "bg": PANEL, "fg": TEXT, "dot_on": DOT_ON, "dot_off": DOT_OFF,
        "dot": [x0, ROW_Y],
        "clock": [clock_x, ROW_Y, CLOCK_CELLS],
        "glyphs": {ch: [glyph(ch, font).width, base64.b64encode(glyph(ch, font).tobytes()).decode()]
                   for ch in GLYPHS},
        "dot_mask": base64.b64encode(dot().tobytes()).decode(),
    }
    comment = json.dumps(spec, separators=(",", ":")).encode()
    img.save(OUT, "JPEG", quality=88, subsampling=2, optimize=False, progressive=False,
             restart_marker_blocks=1, comment=comment)
    print("wrote", OUT, os.path.getsize(OUT), "bytes; live cells at x=%d..%d y=%d" % (x0, x1, ROW_Y))


if __name__ == "__main__":
    main()
