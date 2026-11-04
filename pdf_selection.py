"""Read-only character geometry for the browser's selectable PDF text layer."""
import math

import pymupdf


def page_selection(page):
    """Use the actual source/output glyph positions, including crop and rotation.

    Geometry is in displayed page points, independent of preview resolution or
    reader zoom. No extraction blocks, saved translations or layout caches change.
    """
    lines = []
    bounds = page.rect
    matrix = page.rotation_matrix
    native = page.get_text("rawdict", sort=True,
                           flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for block in native["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            characters = []
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    text = char.get("c", "")
                    if not text:
                        continue
                    quad = pymupdf.recover_char_quad(line["dir"], span, char)
                    ul, ur, ll = (point * matrix for point in (quad.ul, quad.ur, quad.ll))
                    horizontal, vertical = ur - ul, ll - ul
                    width, height = abs(horizontal), abs(vertical)
                    if not all(math.isfinite(value) for value in (*ul, width, height)) or height <= 0:
                        continue
                    # A zero-advance character still belongs in copied text.
                    a, b = (horizontal.x / width, horizontal.y / width) if width else (1, 0)
                    c, d = vertical.x / height, vertical.y / height
                    if not (quad.rect * matrix).intersects(bounds) and not (width == 0 and bounds.contains(ul)):
                        continue
                    characters.append([text, *[round(value, 5) for value in
                                               (ul.x, ul.y, width, height, a, b, c, d)]])
            if characters:
                lines.append(characters)
    return {"width": bounds.width, "height": bounds.height, "lines": lines}
