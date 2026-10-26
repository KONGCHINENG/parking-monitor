"""Generate printable ArUco markers: one PNG per marker + a PDF at true size.

Layout of each printed marker (not to scale):

    +---------------------------+
    |       white quiet zone    |   <- 1 module wide all round
    |   +-------------------+   |
    |   | black border      |   |   <- the "marker size" you set = this square
    |   |   4x4 data bits   |   |
    |   +-------------------+   |
    |                           |
    |   ID 1  -  Spot A         |   <- label strip (outside the quiet zone)
    +---------------------------+

A DICT_4X4 marker is 6x6 "modules" (4 data + 1 black border each side).
The quiet zone is one more module of white so the detector can find the
black border edge even on dark asphalt.
"""

import os

import cv2
import numpy as np
from PIL import Image

from .detector import get_dictionary

MODULES = 6  # 4 data bits + 2 border bits, for DICT_4X4_*

PAPER_CM = {"a4": (21.0, 29.7), "letter": (21.59, 27.94), "a3": (29.7, 42.0)}


def marker_image(marker_id: int, px: int, label: str = "", dictionary="DICT_4X4_50"):
    """Return a grayscale image: marker of `px` pixels + quiet zone + label."""
    module = max(1, round(px / MODULES))
    px = module * MODULES                          # keep modules whole pixels
    m = cv2.aruco.generateImageMarker(get_dictionary(dictionary), marker_id, px, borderBits=1)
    quiet = module
    img = cv2.copyMakeBorder(m, quiet, quiet, quiet, quiet, cv2.BORDER_CONSTANT, value=255)
    if label:
        strip = np.full((module, img.shape[1]), 255, np.uint8)
        scale = module / 60.0
        cv2.putText(strip, label, (quiet, int(module * 0.7)), cv2.FONT_HERSHEY_SIMPLEX,
                    scale, 0, max(1, int(scale * 2)), cv2.LINE_AA)
        img = np.vstack([img, strip])
    return img


def generate(ids_labels: dict, out_dir: str, size_cm: float = 30.0, dpi: int = 150,
             paper: str = "fit", dictionary: str = "DICT_4X4_50") -> list:
    """Write PNGs and markers.pdf. Returns list of files written.

    size_cm - physical edge length of the black square (excl. quiet zone)
    paper   - "fit": one page per marker, page exactly the marker size (print shop / plotter)
              "a4"/"letter"/"a3": small markers are packed several per sheet;
              markers bigger than the sheet are split into tiles you tape together
    """
    os.makedirs(out_dir, exist_ok=True)
    px = int(round(size_cm / 2.54 * dpi))
    written, pages, imgs = [], [], []

    for mid, label in ids_labels.items():
        text = f"ID {mid}  {label}  ({size_cm:g} cm)"
        img = marker_image(mid, px, text, dictionary)
        png = os.path.join(out_dir, f"marker_{mid:02d}.png")
        # Store DPI in the PNG so image viewers print it at the right size.
        Image.fromarray(img).save(png, dpi=(dpi, dpi))
        written.append(png)
        imgs.append((mid, img))

    if paper == "fit":
        pages = [Image.fromarray(img) for _, img in imgs]
    elif _fits(imgs[0][1], dpi, paper):
        pages = _pack([img for _, img in imgs], dpi, paper)
    else:
        for mid, img in imgs:
            pages.extend(_tile(img, dpi, paper, f"ID {mid}"))

    pdf = os.path.join(out_dir, "markers.pdf")
    # resolution= sets the physical page size: page_inches = pixels / dpi.
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=dpi)
    written.append(pdf)
    return written


def _usable_area(dpi, paper, margin_cm=1.0):
    pw, ph = (int(round(c / 2.54 * dpi)) for c in PAPER_CM[paper])
    margin = int(round(margin_cm / 2.54 * dpi))
    return pw, ph, margin


def _fits(img, dpi, paper):
    pw, ph, margin = _usable_area(dpi, paper)
    return img.shape[1] <= pw - 2 * margin and img.shape[0] <= ph - 2 * margin


def _pack(imgs, dpi, paper, gap_cm=1.0):
    """Lay several small markers on each sheet, left to right, top to bottom,
    with a thin grey cut line around each (outside its white quiet zone)."""
    pw, ph, margin = _usable_area(dpi, paper)
    gap = int(round(gap_cm / 2.54 * dpi))
    pages, page, x, y, row_h = [], None, margin, margin, 0
    for img in imgs:
        h, w = img.shape
        if page is not None and x + w > pw - margin:          # next row
            x, y, row_h = margin, y + row_h + gap, 0
        if page is None or y + h > ph - margin:               # next page
            if page is not None:
                pages.append(Image.fromarray(page))
            page = np.full((ph, pw), 255, np.uint8)
            cv2.putText(page, "Print at 100% / Actual size - do not 'fit to page'",
                        (margin, margin // 2), cv2.FONT_HERSHEY_SIMPLEX, dpi / 300, 0, 1, cv2.LINE_AA)
            x, y, row_h = margin, margin, 0
        page[y:y + h, x:x + w] = img
        cv2.rectangle(page, (x - 1, y - 1), (x + w, y + h), 180, 1)
        x, row_h = x + w + gap, max(row_h, h)
    pages.append(Image.fromarray(page))
    return pages


def _tile(img, dpi, paper, name, margin_cm=1.0):
    """Cut a big marker image into paper-sized pieces with a white margin.
    Each tile is printed at 100 % and taped edge to edge (trim the margin)."""
    pw, ph = (int(round(c / 2.54 * dpi)) for c in PAPER_CM[paper])
    margin = int(round(margin_cm / 2.54 * dpi))
    tw, th = pw - 2 * margin, ph - 2 * margin            # usable area per page
    rows = -(-img.shape[0] // th)                        # ceil division
    cols = -(-img.shape[1] // tw)
    pages = []
    for r in range(rows):
        for c in range(cols):
            piece = img[r * th:(r + 1) * th, c * tw:(c + 1) * tw]
            page = np.full((ph, pw), 255, np.uint8)
            page[margin:margin + piece.shape[0], margin:margin + piece.shape[1]] = piece
            # thin grey cut line around the piece
            cv2.rectangle(page, (margin - 1, margin - 1),
                          (margin + piece.shape[1], margin + piece.shape[0]), 180, 1)
            cv2.putText(page, f"{name}  row {r + 1}/{rows}  col {c + 1}/{cols}  - print at 100%",
                        (margin, margin // 2), cv2.FONT_HERSHEY_SIMPLEX, dpi / 300, 0, 1, cv2.LINE_AA)
            pages.append(Image.fromarray(page))
    return pages
