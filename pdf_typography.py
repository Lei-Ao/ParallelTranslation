"""Local, embedded book typography; subset before instancing variable fonts."""
import io
import os
import re
from functools import lru_cache
from pathlib import Path

import pymupdf


def space_cjk_latin(text):
    """Display Latin terms next to Han text with one space; keep stored text intact.

    Only touching boundaries change. Existing whitespace, punctuation, numeric
    labels, URLs, email addresses and inline code retain their original spelling.
    """
    han = r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U000323af]"
    latin = r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]"
    term = rf"[0-9]*{latin}[A-Za-z0-9\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]*(?:[-_][A-Za-z0-9]+)*"
    protected = re.compile(r"(`[^`\n]*`|https?://[^\s<>]+|www\.[^\s<>]+|[\w.+-]+@[\w.-]+)")
    parts = protected.split(text)
    for index in range(0, len(parts), 2):
        value = re.sub(rf"({han})(?={term})", r"\1 ", parts[index])
        parts[index] = re.sub(rf"({term})(?={han})", r"\1 ", value)
    return "".join(parts)


@lru_cache(maxsize=1)
def font_source():
    fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for regular, bold, label in [
        (fonts / "NotoSerifSC-VF.ttf", fonts / "NotoSerifSC-VF.ttf", "思源宋体（Noto Serif SC）"),
        (fonts / "Deng.ttf", fonts / "Dengb.ttf", "等线"),
    ]:
        if regular.is_file() and bold.is_file():
            return str(regular), str(bold), label
    return None


def font_signature():
    source = font_source()
    if source is None:
        return "builtin-serif"
    return [(path, Path(path).stat().st_size, Path(path).stat().st_mtime_ns) for path in source[:2]]


@lru_cache(maxsize=4)
def subset_fonts(characters, regular, bold):
    from fontTools import subset
    from fontTools.ttLib import TTFont
    from fontTools.varLib.instancer import instantiateVariableFont

    results = []
    for path, weight in ((regular, 400), (bold, 600)):
        font = TTFont(path, fontNumber=0)
        options = subset.Options()
        options.recalc_timestamp = False
        # Contextual digit/dash ligatures in this font lose their original
        # Unicode mapping in MuPDF. Keep glyphs individually searchable.
        options.layout_features = []
        worker = subset.Subsetter(options=options)
        worker.populate(text=characters)
        worker.subset(font)
        if "fvar" in font:
            instantiateVariableFont(font, {"wght": weight}, inplace=True)
        # Give the embedded derivative its own family; the glyphs use real
        # regular/semibold masters rather than the variable font's thin default.
        for identifier, value in ((1, "ParallelBook"), (2, "Regular" if weight == 400 else "SemiBold"),
                                  (4, "ParallelBook Regular" if weight == 400 else "ParallelBook SemiBold"),
                                  (6, "ParallelBook-Regular" if weight == 400 else "ParallelBook-SemiBold")):
            font["name"].setName(value, identifier, 3, 1, 0x409)
        font["OS/2"].usWeightClass = weight
        output = io.BytesIO()
        font.save(output)
        font.close()
        results.append(output.getvalue())
    return tuple(results)


def font_resources(text):
    source = font_source()
    if source is None:
        return {"css": "", "family": "serif", "archive": None, "label": "内置宋体回退"}
    # Include the compatibility forms used by pdf_layout and ordinary labels.
    import unicodedata
    characters = "".join(sorted(set(text + unicodedata.normalize("NFKC", text)
                                    + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:;()[]-")))
    try:
        regular, semibold = subset_fonts(characters, source[0], source[1])
        from fontTools.ttLib import TTFont
        with TTFont(io.BytesIO(regular)) as embedded:
            supported = embedded.getBestCmap()
            fallback_characters = "".join(char for char in characters if not char.isspace() and ord(char) not in supported)
    except (ImportError, OSError, ValueError, KeyError):
        return {"css": "", "family": "serif", "archive": None, "label": "内置宋体回退"}
    archive = pymupdf.Archive()
    archive.add((regular, "parallel-regular.ttf"))
    archive.add((semibold, "parallel-semibold.ttf"))
    css = ("@font-face{font-family:ParallelBook;src:url(parallel-regular.ttf);font-weight:400;}"
           "@font-face{font-family:ParallelBook;src:url(parallel-semibold.ttf);font-weight:600;}")
    return {"css": css, "family": "ParallelBook,serif", "archive": archive, "label": source[2],
            "fallback_characters": fallback_characters}
