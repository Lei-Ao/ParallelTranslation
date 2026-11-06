"""Read-only character geometry for the browser's selectable PDF text layer."""
import math

import pymupdf


def page_selection(page, *, native=None, citation_runs=None):
    """Use the actual source/output glyph positions, including crop and rotation.

    Geometry is in displayed page points, independent of preview resolution or
    reader zoom. No extraction blocks, saved translations or layout caches change.
    """
    lines = []
    bounds = page.rect
    matrix = page.rotation_matrix
    if native is None:
        native = page.get_text("rawdict", flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    # Match PyMuPDF rawdict sort=True while retaining the same span objects as the citation extractor.
    for block in sorted(native["blocks"], key=lambda item: (item["bbox"][3], item["bbox"][0])):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            characters = []
            for span in line.get("spans", []):
                offset = 0
                for char in span.get("chars", []):
                    text = char.get("c", "")
                    start = offset
                    offset += len(text)
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
                    glyph = [text, *[round(value, 5) for value in (ul.x, ul.y, width, height, a, b, c, d)]]
                    if citation_runs is not None and id(span) in citation_runs:
                        glyph.extend([citation_runs[id(span)], start])
                    characters.append(glyph)
            if characters:
                lines.append(characters)
    return {"width": bounds.width, "height": bounds.height, "lines": lines}
