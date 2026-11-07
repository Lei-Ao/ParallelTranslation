"""Offline performance baseline for translation and layout paths.

Run before/after a change to compare the same machine and sample document.
This file is intentionally not named test_*.py so unittest discovery skips it.

Example:
    .\\.venv\\Scripts\\python.exe tests/perf_baseline.py --doc .data/<id>
    .\\.venv\\Scripts\\python.exe tests/perf_baseline.py --doc .data/<id> --pages 4
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

import pdf_layout  # noqa: E402
import pdf_typography  # noqa: E402


class CallCounter:
    """Count expensive PyMuPDF page calls during one layout operation."""

    METHODS = ("get_text", "get_textpage", "get_drawings", "get_image_info",
               "get_bboxlog", "get_svg_image", "insert_htmlbox", "add_redact_annot")

    def __init__(self):
        self.counts = Counter()
        self.patches = []

    def __enter__(self):
        for name in self.METHODS:
            original = getattr(pymupdf.Page, name)

            def wrapper(page, *args, __name=name, __original=original, **kwargs):
                if __name in {"get_text", "get_textpage"}:
                    option = args[0] if args and isinstance(args[0], str) else kwargs.get("option", "text")
                    self.counts[f"{__name}:{option}"] += 1
                else:
                    self.counts[__name] += 1
                return __original(page, *args, **kwargs)

            setattr(pymupdf.Page, name, wrapper)
            self.patches.append((name, original))
        return self

    def __exit__(self, *exc):
        for name, original in self.patches:
            setattr(pymupdf.Page, name, original)
        return False

    def report(self, label):
        print(f"calls {label}:")
        for key, value in sorted(self.counts.items()):
            print(f"  {key:<24} {value:5d}")
        print(f"  {'total':<24} {sum(self.counts.values()):5d}")


def timed(label, function, repeats=1):
    values = []
    result = None
    for _ in range(repeats):
        started = time.perf_counter()
        result = function()
        values.append(time.perf_counter() - started)
    best = min(values)
    print(f"{label:<44} {best:8.3f}s" + (f"  ({', '.join(f'{v:.3f}' for v in values)})" if repeats > 1 else ""))
    return best, result


def group_count(pages, size, max_characters, long_block_characters):
    """Mirror static/translation-queue.js groupByPage for request-count estimates."""
    groups = []
    for page in pages:
        group = []
        characters = 0

        def flush():
            nonlocal group, characters
            if group:
                groups.append(list(group))
            group = []
            characters = 0

        for block in page["blocks"]:
            length = len(block.get("text", ""))
            if length >= long_block_characters:
                flush()
                groups.append([block["id"]])
                continue
            if group and (len(group) >= size or characters + length > max_characters):
                flush()
            group.append(block["id"])
            characters += length
        flush()
    sizes = {}
    for group in groups:
        sizes[len(group)] = sizes.get(len(group), 0) + 1
    return len(groups), sizes


def render_page(path, number, scale):
    with pymupdf.open(path) as pdf:
        page = pdf[number - 1]
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        return pixmap.tobytes("png")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", required=True, help=".data/<document id> directory")
    parser.add_argument("--pages", type=int, default=0, help="limit to first N pages (0 = all)")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--skip-layout", action="store_true")
    parser.add_argument("--counts", action="store_true",
                        help="count PyMuPDF page calls for one page and a cold build")
    args = parser.parse_args()

    source_dir = Path(args.doc)
    document_path = source_dir / "document.json"
    source_pdf = source_dir / "source.pdf"
    if not document_path.is_file() or not source_pdf.is_file():
        raise SystemExit(f"missing document.json/source.pdf under {source_dir}")

    document = json.loads(document_path.read_text(encoding="utf-8"))
    target = "简体中文"
    if args.pages:
        document["pages"] = document["pages"][: args.pages]
        document["total_blocks"] = sum(len(page["blocks"]) for page in document["pages"])
    blocks = [block for page in document["pages"] for block in page["blocks"]]
    translations = "\n".join(block.get("translation", "") for block in blocks)
    print(f"sample: {len(document['pages'])} pages, {len(blocks)} blocks, "
          f"{sum(len(b.get('text', '')) for b in blocks)} source chars, "
          f"{len(translations)} translated chars")
    print(f"document.json: {document_path.stat().st_size} bytes, source.pdf: {source_pdf.stat().st_size} bytes\n")

    for size, chars, long_block, label in [
        (1, 24000, 18000, "current sequential"),
        (3, 6000, 1500, "current frontend default"),
        (5, 12000, 4000, "planned default"),
        (5, 24000, 18000, "backend limit"),
    ]:
        count, sizes = group_count(document["pages"], size, chars, long_block)
        print(f"requests {label:<26} {count:4d}  groups by size {dict(sorted(sizes.items()))}")
    print()

    if not args.skip_layout:
        work = Path(tempfile.mkdtemp(prefix="parallel-perf-"))
        try:
            if args.pages:
                with pymupdf.open(source_pdf) as pdf:
                    pdf.select(range(min(args.pages, pdf.page_count)))
                    pdf.save(work / "source.pdf")
            else:
                shutil.copy2(source_pdf, work / "source.pdf")
            (work / "document.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
            if args.counts:
                count_index = min(4, len(document["pages"]) - 1)
                typography = pdf_typography.font_resources(translations)
                with CallCounter() as counter:
                    pdf_layout.typeset(work / "source.pdf", {"pages": [document["pages"][count_index]]},
                                       target, page_numbers=[count_index], typography=typography)
                counter.report(f"single typeset (page index {count_index})")
                with CallCounter() as counter:
                    pdf_layout.build_layout(work, document, target)
                counter.report("cold build_layout")
                return
            timed("layout cold (all pages)", lambda: pdf_layout.build_layout(work, document, target), args.repeats)
            timed("layout warm (revision cache)", lambda: pdf_layout.build_layout(work, document, target), args.repeats)
            layout_dir = work / "layout"
            print(f"layout cache: {len(list(layout_dir.glob('*.pdf')))} revisions, "
                  f"{len(list((layout_dir / 'pages').glob('*.pdf')))} page files, "
                  f"{sum(p.stat().st_size for p in layout_dir.glob('*.pdf')) / 1e6:.1f} MB PDFs")

            editable = next((page for page in document["pages"]
                             if any(block.get("translation") for block in page["blocks"])), None)
            if editable is not None:
                block = next(block for block in editable["blocks"] if block.get("translation"))
                original = block["translation"]
                block["translation"] = original + "（基准）"

                def edited_layout():
                    return pdf_layout.build_layout(work, document, target)

                timed("layout after one-block edit", edited_layout, args.repeats)
                block["translation"] = original

            if blocks:
                typography_text = translations
                pdf_typography.subset_fonts.cache_clear()
                pdf_typography.oblique_font.cache_clear()
                timed("font_resources cold", lambda: pdf_typography.font_resources(typography_text), args.repeats)
                timed("font_resources warm", lambda: pdf_typography.font_resources(typography_text), args.repeats)
                miss_text = typography_text + "\u9fa5\u9f98\u9f9f"
                timed("font_resources one new glyph", lambda: pdf_typography.font_resources(miss_text), args.repeats)

            first_page = document["pages"][0]
            timed("typeset one page", lambda: pdf_layout.typeset(
                work / "source.pdf", {"pages": [first_page]}, target, page_numbers=[0],
                typography=pdf_typography.font_resources(translations)), args.repeats)

            for scale in (1.7, 3.0):
                timed(f"render page 1 @ {scale}x", lambda: render_page(work / "source.pdf", 1, scale), args.repeats)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def load_json():
        return json.loads(document_path.read_text(encoding="utf-8"))

    timed("load_doc JSON parse", load_json, args.repeats)

    def save_json():
        text = json.dumps(document, ensure_ascii=False)
        path = Path(tempfile.gettempdir()) / "parallel-perf-save.json"
        path.write_text(text, encoding="utf-8")
        return path

    timed("save_doc JSON serialize+write", save_json, args.repeats)


if __name__ == "__main__":
    main()