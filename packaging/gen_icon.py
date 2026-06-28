"""Generate Himmy's macOS app icon (a squircle with a premium indigo gradient + an AI sparkle).

Writes a 1024x1024 master PNG; the surrounding script turns it into build/icon.icns via
sips + iconutil. Re-runnable and self-contained (Pillow only).
"""

from __future__ import annotations

import math
import sys

from PIL import Image, ImageDraw, ImageFilter

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --- squircle mask (macOS-style rounded rect with generous margin) -------------------------
margin = 100
box = (margin, margin, S - margin, S - margin)
radius = 205
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius, fill=255)

# --- diagonal gradient (top-left indigo -> bottom-right blue) -------------------------------
top = (109, 94, 246)    # #6D5EF6 indigo-violet
bot = (47, 129, 247)    # #2F81F7 blue
grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gpx = grad.load()
for y in range(S):
    for x in range(S):
        t = (x + y) / (2 * S)
        r, g, b = lerp(top, bot, t)
        gpx[x, y] = (r, g, b, 255)
img.paste(grad, (0, 0), mask)

# --- subtle top highlight for depth ---------------------------------------------------------
hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
hd = ImageDraw.Draw(hl)
hd.ellipse((margin - 40, margin - 260, S - margin + 40, margin + 360), fill=(255, 255, 255, 46))
hl = hl.filter(ImageFilter.GaussianBlur(60))
img = Image.alpha_composite(img, Image.composite(hl, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))


def star(cx, cy, outer, inner, points=4, rot=0.0):
    """Return polygon vertices for a concave star (an AI 'sparkle')."""
    verts = []
    for i in range(points * 2):
        ang = rot + math.pi * i / points
        r = outer if i % 2 == 0 else inner
        verts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return verts


# --- the sparkle (soft glow under a crisp white mark) ---------------------------------------
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
gd = ImageDraw.Draw(glow)
gd.polygon(star(512, 524, 300, 70, 4, -math.pi / 2), fill=(255, 255, 255, 150))
glow = glow.filter(ImageFilter.GaussianBlur(26))
img = Image.alpha_composite(img, glow)

d = ImageDraw.Draw(img)
d.polygon(star(512, 524, 300, 70, 4, -math.pi / 2), fill=(255, 255, 255, 255))
# a small companion sparkle, upper-right
d.polygon(star(712, 348, 86, 22, 4, -math.pi / 2), fill=(255, 255, 255, 235))

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/himmy-icon-1024.png"
img.save(out)
print("wrote", out)
