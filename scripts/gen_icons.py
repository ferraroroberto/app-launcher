"""Generate PWA icons: solid white rocket silhouette on pure-black.

Matches photo-ocr / voice-transcriber's solid-white-on-black, flat,
no-outlines style. The rocket nose points up and the exhaust plume
trails below — keeps the silhouette recognisable at favicon size.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = (10, 10, 10)
FG = (240, 240, 240)

OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "webapp" / "static"


def draw_rocket(size: int, inset: float) -> Image.Image:
    """Render a rocket silhouette centred on a black square.

    ``inset`` is the fraction of the canvas reserved as padding (used
    to produce a maskable variant with safe margins).
    """
    img = Image.new("RGB", (size, size), BG)
    d = ImageDraw.Draw(img)

    pad = int(size * inset)
    content = size - 2 * pad
    cx = size // 2

    # ------------------------------ body (capsule)
    body_w = int(content * 0.28)
    body_h = int(content * 0.52)
    body_x = cx - body_w // 2
    body_y = pad + int(content * 0.18)
    body_radius = int(body_w * 0.5)
    d.rounded_rectangle(
        [body_x, body_y, body_x + body_w, body_y + body_h],
        radius=body_radius,
        fill=FG,
    )

    # Nose cone — semicircle on top of the capsule.
    nose_top = body_y - int(body_w * 0.4)
    d.pieslice(
        [body_x, nose_top, body_x + body_w, nose_top + body_w],
        start=180,
        end=360,
        fill=FG,
    )

    # ------------------------------ porthole (negative space)
    porthole_r = int(body_w * 0.22)
    porthole_cx = cx
    porthole_cy = body_y + int(body_h * 0.30)
    d.ellipse(
        [
            porthole_cx - porthole_r,
            porthole_cy - porthole_r,
            porthole_cx + porthole_r,
            porthole_cy + porthole_r,
        ],
        fill=BG,
    )

    # ------------------------------ side fins (triangles)
    fin_w = int(body_w * 0.55)
    fin_h = int(body_h * 0.35)
    fin_y_top = body_y + int(body_h * 0.55)
    fin_y_bottom = body_y + body_h + int(fin_h * 0.18)
    # Left fin
    d.polygon(
        [
            (body_x, fin_y_top),
            (body_x - fin_w, fin_y_bottom),
            (body_x + int(body_w * 0.15), fin_y_bottom),
        ],
        fill=FG,
    )
    # Right fin
    d.polygon(
        [
            (body_x + body_w, fin_y_top),
            (body_x + body_w + fin_w, fin_y_bottom),
            (body_x + body_w - int(body_w * 0.15), fin_y_bottom),
        ],
        fill=FG,
    )

    # ------------------------------ exhaust plume (three drops, descending)
    plume_top = body_y + body_h + int(fin_h * 0.1)
    plume_w = int(body_w * 0.7)
    plume_h = int(content * 0.16)
    d.ellipse(
        [cx - plume_w // 2, plume_top, cx + plume_w // 2, plume_top + plume_h],
        fill=FG,
    )

    return img


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    draw_rocket(512, inset=0.06).save(OUT_DIR / "icon-512.png", "PNG")
    draw_rocket(512, inset=0.20).save(OUT_DIR / "icon-512-maskable.png", "PNG")
    draw_rocket(180, inset=0.06).save(OUT_DIR / "icon-180.png", "PNG")

    print(f"wrote icons to {OUT_DIR}")


if __name__ == "__main__":
    main()
