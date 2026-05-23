import cairosvg
from PIL import Image
import os


def png(src, out, w=None, h=None):
    cairosvg.svg2png(url=src, write_to=out, output_width=w, output_height=h)
    print("✓", out)


# Icons (square, from dark tile)
for s in (16, 32, 48, 180, 512):
    name = {
        16: "favicon-16.png",
        32: "favicon-32.png",
        48: "favicon-48.png",
        180: "apple-touch-icon.png",
        512: "icon-512.png",
    }[s]
    png("icon/icon.svg", f"icon/{name}", w=s, h=s)

# Bare marks (baked), 512 square
png("mark/mark-ondark.svg", "mark/mark-ondark.png", w=512, h=512)
png("mark/mark-onlight.svg", "mark/mark-onlight.png", w=512, h=512)

# Wordmarks (baked), 3x the 300x100 viewBox -> 900x300
png("wordmark/wordmark-ondark.svg", "wordmark/wordmark-ondark.png", w=900, h=300)
png("wordmark/wordmark-onlight.svg", "wordmark/wordmark-onlight.png", w=900, h=300)

# Social card 1:1
png("social/social.svg", "social/social-1280x640.png", w=1280, h=640)

# Multi-size .ico
imgs = [Image.open(f"icon/favicon-{s}.png").convert("RGBA") for s in (16, 32, 48)]
imgs[0].save("icon/favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)], append_images=imgs[1:])
print("✓ icon/favicon.ico")
