"""Local, embedded book typography; subset before instancing variable fonts."""
import hashlib
from performance import measured
import io
import json
import math
import os
import re
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

import pymupdf

FONT_CACHE_VERSION = 2
FONT_CACHE_LIMIT = 48


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


@lru_cache(maxsize=8)
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


@lru_cache(maxsize=12)
def oblique_font(data):
    """Embed real slanted outlines when the CJK family has no italic master.

    MuPDF does not reliably synthesize italic for an embedded upright face.
    Preserve its family, weight, advances and Unicode map; shear outlines only.
    """
    from fontTools.ttLib import TTFont
    from fontTools.pens.recordingPen import DecomposingRecordingPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen

    with TTFont(io.BytesIO(data)) as font:
        slant = math.tan(math.radians(12))
        glyphs = font.getGlyphSet()
        transformed = {}
        for name in font.getGlyphOrder():
            recording = DecomposingRecordingPen(glyphs)
            glyphs[name].draw(recording)
            pen = TTGlyphPen(None)
            recording.replay(TransformPen(pen, (1, 0, slant, 1, 0, 0)))
            transformed[name] = pen.glyph()
        font['glyf'].glyphs.update(transformed)
        for name, glyph in transformed.items():
            glyph.recalcBounds(font['glyf'])
            advance, _ = font['hmtx'][name]
            font['hmtx'][name] = (advance, getattr(glyph, 'xMin', 0))
        font['post'].italicAngle = -12
        font['head'].macStyle |= 2
        font['OS/2'].fsSelection = (font['OS/2'].fsSelection | 1) & ~64
        font['hhea'].caretSlopeRise = 1000
        font['hhea'].caretSlopeRun = round(slant * 1000)
        style = 'Italic' if font['OS/2'].usWeightClass == 400 else 'SemiBold Italic'
        for identifier, value in ((2, style), (4, 'ParallelBook ' + style),
                                  (6, 'ParallelBook-' + style.replace(' ', ''))):
            font['name'].setName(value, identifier, 3, 1, 0x409)
        output = io.BytesIO()
        font.save(output)
        return output.getvalue()


def _font_cache_root():
    return Path(tempfile.gettempdir()) / "parallel-translation-font-cache"


def _read_font_cache(key):
    folder = _font_cache_root() / key
    try:
        data = (
            (folder / "regular.ttf").read_bytes(),
            (folder / "semibold.ttf").read_bytes(),
            (folder / "italic.ttf").read_bytes(),
            (folder / "semibold-italic.ttf").read_bytes(),
            json.loads((folder / "meta.json").read_text(encoding="utf-8"))["fallback"],
        )
        return data if all(data[:4]) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_font_cache(key, regular, semibold, italic, semibold_italic, fallback):
    root = _font_cache_root()
    folder = root / key
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for name, data in (("regular.ttf", regular), ("semibold.ttf", semibold),
                           ("italic.ttf", italic), ("semibold-italic.ttf", semibold_italic)):
            temp = folder / (name + ".tmp")
            temp.write_bytes(data)
            temp.replace(folder / name)
        (folder / "meta.json").write_text(json.dumps({"fallback": fallback}), encoding="utf-8")
        entries = [entry for entry in root.iterdir() if entry.is_dir()]
        if len(entries) > FONT_CACHE_LIMIT:
            entries.sort(key=lambda entry: entry.stat().st_mtime_ns)
            for entry in entries[:-FONT_CACHE_LIMIT]:
                shutil.rmtree(entry, ignore_errors=True)
    except OSError:
        pass


@measured("font_prepare")
def font_resources(text):
    source = font_source()
    if source is None:
        return {"css": "", "family": "serif", "archive": None, "label": "内置宋体回退"}
    # Include the compatibility forms used by pdf_layout and ordinary labels.
    import unicodedata
    characters = "".join(sorted(set(text + unicodedata.normalize("NFKC", text)
                                    + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789 .,:;()[]-")))
    key = hashlib.sha256(
        f"{FONT_CACHE_VERSION}|{font_signature()}|{characters}".encode("utf-8")).hexdigest()[:40]
    cached = _read_font_cache(key)
    try:
        if cached is not None:
            regular, semibold, italic, semibold_italic, fallback_characters = cached
        else:
            regular, semibold = subset_fonts(characters, source[0], source[1])
            from fontTools.ttLib import TTFont
            with TTFont(io.BytesIO(regular)) as embedded:
                supported = embedded.getBestCmap()
                fallback_characters = "".join(char for char in characters
                                              if not char.isspace() and ord(char) not in supported)
            italic = oblique_font(regular)
            semibold_italic = oblique_font(semibold)
            _write_font_cache(key, regular, semibold, italic, semibold_italic, fallback_characters)
    except (ImportError, OSError, ValueError, KeyError):
        return {"css": "", "family": "serif", "archive": None, "label": "内置宋体回退"}
    archive = pymupdf.Archive()
    archive.add((regular, "parallel-regular.ttf"))
    archive.add((semibold, "parallel-semibold.ttf"))
    archive.add((italic, "parallel-italic.ttf"))
    archive.add((semibold_italic, "parallel-semibold-italic.ttf"))
    css = ("@font-face{font-family:ParallelBook;src:url(parallel-regular.ttf);font-weight:400;}"
           "@font-face{font-family:ParallelBook;src:url(parallel-semibold.ttf);font-weight:600;}"
           "@font-face{font-family:ParallelBook;src:url(parallel-italic.ttf);font-weight:400;font-style:italic;}"
           "@font-face{font-family:ParallelBook;src:url(parallel-semibold-italic.ttf);font-weight:600;font-style:italic;}")
    return {"css": css, "family": "ParallelBook,serif", "archive": archive, "label": source[2],
            "fallback_characters": fallback_characters}
