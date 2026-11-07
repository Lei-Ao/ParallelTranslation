"""Detect one paragraph whose extraction was split across a column or page break.

The saved document stores blocks per page, so a paragraph crossing a page or
column boundary reaches the translator as two unrelated fragments.  Both the
translation prompt and the typesetter need to know that the second fragment
continues the first.  Detection uses only page geometry, reading order and the
text itself; it never keys on a paper name, page number or block id.
"""
import re

# A sentence that is actually finished.  Closing quotes/brackets may follow the
# punctuation.  Citation brackets do not count, so a mid-sentence "[13]" tail is
# still a continuation.
_TERMINAL = re.compile(r"[.!?:;。！？：；][\"'”’）)\]】»]?\s*$")
# English prose continues with a lower-case letter.  A new paragraph normally
# starts with a capital, so requiring lower case keeps the detector
# conservative; unknown cases simply keep the current per-fragment behaviour.
# An opening quote or bracket may precede the first word.
_LOWER_START = re.compile(r"^\s*[\"'“‘（(]?[a-z]")
# Extraction sometimes leaves a lone symbol or a short formula tail at the end
# of a column.  Those are not paragraphs and must not start a pair.
_CAPTION = re.compile(r"^\s*(?:fig(?:ure)?|table|algorithm|图|表)\s*\.?\s*(?:\d|[IVXLC]+\b)", re.I)
_REFERENCE = re.compile(r"^\s*(?:\[\d{1,4}\]|\d{1,4}\.\s)")
_AFFILIATION = re.compile(r"@|universit|institut|department|school|academy|college|大学|学院|研究所", re.I)

MIN_PROSE_WORDS = 10
MIN_PROSE_CHARS = 70
GEOMETRY_TOLERANCE = 0.02    # normalized page units
WIDTH_TOLERANCE = 0.04
EDGE_TOLERANCE = 0.035


def _words(text):
    return len(re.findall(r"\b[A-Za-z][A-Za-z'-]{1,}\b", text))


def _prose_block(block):
    """A running-text block, not a title, author line, caption or table."""
    text = (block.get("text") or "").strip()
    if not text or _CAPTION.match(text) or _REFERENCE.match(text) or _AFFILIATION.search(text):
        return False
    box = block.get("bbox") or ()
    if len(box) != 4:
        return False
    width = box[2] - box[0]
    height = box[3] - box[1]
    if width < 0.12 or height < 0.02:
        return False
    words = _words(text)
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    return (words >= MIN_PROSE_WORDS or cjk >= 20
            or (words >= 8 and len(text) >= MIN_PROSE_CHARS))


def _columns(blocks):
    """Cluster running-text blocks into vertical columns by left/right edges."""
    lanes = []
    for block in blocks:
        box = block["bbox"]
        width = box[2] - box[0]
        lane = next((item for item in lanes
                     if abs(item["x0"] - box[0]) <= GEOMETRY_TOLERANCE
                     and abs((item["x1"] - item["x0"]) - width) <= WIDTH_TOLERANCE), None)
        if lane is None:
            lanes.append({"x0": box[0], "x1": box[2], "width": width,
                          "blocks": [block], "top": box[1], "bottom": box[3]})
        else:
            lane["blocks"].append(block)
            lane["x0"] = min(lane["x0"], box[0])
            lane["x1"] = max(lane["x1"], box[2])
            lane["top"] = min(lane["top"], box[1])
            lane["bottom"] = max(lane["bottom"], box[3])
    return sorted(lanes, key=lambda item: (item["x0"], item["top"]))


def _first_last(blocks):
    """Most likely first and last running-text block of one column."""
    if not blocks:
        return None, None
    ordered = sorted(blocks, key=lambda block: (block["bbox"][1], block["bbox"][0]))
    return ordered[0], ordered[-1]


def _continues(first, second, first_lane, second_lane, second_page_top, cross_page):
    """Strong geometric and textual evidence that `second` continues `first`.

    `first` must be the bottom of one column and `second` the top of the next
    column/page in reading order, so unrelated paragraphs elsewhere cannot
    pair.  The source text must end mid-sentence and the continuation must
    start with a lower-case word, which rules out ordinary paragraph
    boundaries far more reliably than either signal alone.
    """
    if not first or not second:
        return False
    one, two = first["bbox"], second["bbox"]
    text_one = (first.get("text") or "").strip()
    text_two = (second.get("text") or "").strip()
    if not text_one or not text_two:
        return False
    if _TERMINAL.search(text_one):
        return False
    if not _LOWER_START.match(text_two):
        return False
    # The pair must sit at the bottom/top of their lanes.  `first_lane` is the
    # lane that ends and `second_lane` the next lane in reading order.
    if one[3] < first_lane["bottom"] - EDGE_TOLERANCE:
        return False
    if two[1] > second_lane["top"] + EDGE_TOLERANCE:
        return False
    # A page-break continuation resumes where the next page's text area
    # begins.  If a figure, table or caption occupies its top, the next
    # lower-case prose is not the rest of the previous sentence.  Same-page
    # column changes sit under a title/abstract, so this test is page-only.
    if cross_page and two[1] > second_page_top + EDGE_TOLERANCE * 1.5:
        return False
    # Compare the block's offset from its own column edge, not its absolute x:
    # a column break moves the continuation to the next column, a page break
    # moves it back to the first column.  Width must still match.
    offset_one = one[0] - first_lane["x0"]
    offset_two = two[0] - second_lane["x0"]
    if abs(offset_one - offset_two) > GEOMETRY_TOLERANCE * 1.6:
        return False
    if abs((one[2] - one[0]) - (two[2] - two[0])) > WIDTH_TOLERANCE:
        return False
    return True


def continuation_pairs(document):
    """Return `{previous_id, next_id, page, next_page}` for split paragraphs.

    Blocks are consumed in reading order: page by page, columns left to right,
    top to bottom inside each column.  Only adjacent column tails/heads pair.
    """
    pages = document.get("pages") or []
    lanes_by_page, tops = [], []
    for page in pages:
        saved = page.get("blocks") or []
        boxes = [block.get("bbox") for block in saved
                 if isinstance(block.get("bbox"), (list, tuple)) and len(block["bbox"]) == 4]
        tops.append(min((box[1] for box in boxes), default=0.0))
        lanes_by_page.append(_columns([block for block in saved if _prose_block(block)]))

    pairs = []
    sequence = []
    for index, lanes in enumerate(lanes_by_page):
        for lane in lanes:
            first, last = _first_last(lane["blocks"])
            sequence.append((index, lane, first, last))

    for (page_a, lane_a, _, last), (page_b, lane_b, first, _) in zip(sequence, sequence[1:]):
        if last is None or first is None:
            continue
        if page_b > page_a + 1:
            continue
        if not _continues(last, first, lane_a, lane_b, tops[page_b], page_b > page_a):
            continue
        pairs.append({"page": page_a + 1, "block": last.get("id"),
                      "next_page": page_b + 1, "next_block": first.get("id")})
    return pairs


def continuation_map(document):
    """Per-block hints: the head gets ``continues``, the tail ``continues_from``.

    The head of a split paragraph is marked because the typesetter uses the
    flag to reclaim the page-bottom gap; the tail carries the source id so the
    translation prompt can continue the previous sentence.
    """
    hints = {}
    for pair in continuation_pairs(document):
        hints.setdefault(pair["block"], {})["continues"] = True
        hints.setdefault(pair["next_block"], {})["continues_from"] = pair["block"]
    return hints
