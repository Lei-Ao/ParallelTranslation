import unittest

import app as service
from pdf_continuation import continuation_map, continuation_pairs

LONG = ("Learning based controllers are compared with analytical controllers on standard "
        "aerial vehicle tasks and the empirical protocol is described in detail here")
CONTINUATION = ("and analytical controllers like geometric controllers for trajectory "
                "tracking tasks across several aerial vehicle platforms")
TAIL = ("The desired position represents the end effector waypoint in the world frame and "
        "the desired velocity is obtained from the derivative of the trajectory the tuning")


def block(block_id, text, box):
    return {"id": block_id, "text": text, "bbox": box,
            "translation": "译文", "target": "简体中文"}


def page(number, blocks):
    return {"number": number, "width": 612.0, "height": 792.0, "blocks": blocks}


def two_column_document():
    left = [0.08, 0.0, 0.49, 1.0]
    right = [0.51, 0.0, 0.92, 1.0]
    return {"name": "synthetic.pdf", "pages": [
        page(1, [
            block("p1-l1", LONG, [left[0], 0.07, left[2], 0.30]),
            block("p1-l2", TAIL, [left[0], 0.70, left[2], 0.916]),
            block("p1-r1", CONTINUATION, [right[0], 0.074, right[2], 0.105]),
            block("p1-r2", LONG, [right[0], 0.11, right[2], 0.60]),
        ]),
        page(2, [
            block("p2-l1", "of a model-based controller is often a much more heuristic manual "
                           "procedure to achieve a good enough configuration", [left[0], 0.074, left[2], 0.12]),
            block("p2-l2", LONG, [left[0], 0.13, left[2], 0.55]),
        ]),
    ]}


class ContinuationDetectionTests(unittest.TestCase):
    def test_detects_column_and_page_continuations(self):
        pairs = continuation_pairs(two_column_document())
        found = {(item["block"], item["next_block"]) for item in pairs}
        self.assertIn(("p1-l2", "p1-r1"), found)
        self.assertIn(("p1-r2", "p2-l1"), found)

    def test_terminal_punctuation_or_capital_start_is_not_a_continuation(self):
        document = two_column_document()
        document["pages"][0]["blocks"][1]["text"] = TAIL + "."
        document["pages"][0]["blocks"][2]["text"] = CONTINUATION[0].upper() + CONTINUATION[1:]
        document["pages"][0]["blocks"][3]["text"] = LONG + "."
        pairs = continuation_pairs(document)
        found = {(item["block"], item["next_block"]) for item in pairs}
        self.assertNotIn(("p1-l2", "p1-r1"), found)
        self.assertNotIn(("p1-r2", "p2-l1"), found)

    def test_caption_and_author_blocks_do_not_start_a_pair(self):
        document = two_column_document()
        # A caption at the top of the right column must not become its first
        # running-text block, so the left column tail cannot pair with it.
        document["pages"][0]["blocks"][2] = block(
            "p1-caption", "Fig. 1: Trajectory tracking results for the controllers",
            [0.51, 0.074, 0.92, 0.16])
        document["pages"][0]["blocks"].append(block(
            "p1-authors", "Jane Doe\nUniversity of Example\njane@example.org",
            [0.08, 0.93, 0.49, 0.99]))
        pairs = continuation_pairs(document)
        found = {(item["block"], item["next_block"]) for item in pairs}
        self.assertNotIn(("p1-l2", "p1-caption"), found)
        self.assertFalse(any(item["block"] == "p1-authors" for item in pairs))
        from pdf_continuation import _prose_block
        self.assertFalse(_prose_block(document["pages"][0]["blocks"][2]))
        self.assertFalse(_prose_block(document["pages"][0]["blocks"][-1]))

    def test_page_without_prose_breaks_continuation(self):
        document = two_column_document()
        document["pages"].insert(1, page(2, []))
        self.assertFalse(any(pair["page"] != pair["next_page"]
                             for pair in continuation_pairs(document)))

    def test_hints_mark_head_and_tail(self):
        hints = continuation_map(two_column_document())
        self.assertTrue(hints["p1-l2"]["continues"])
        self.assertEqual(hints["p1-r1"]["continues_from"], "p1-l2")

    def test_prompt_context_carries_previous_source_and_translation(self):
        document = two_column_document()
        document["pages"][0]["blocks"][1]["translation"] = "涉及调参的结尾"
        request = service.TranslateRequest(agent=service.AgentConfig(), target="简体中文")
        blocks = service.all_blocks(document)
        index = next(i for i, item in enumerate(blocks) if item["id"] == "p1-r1")
        context = service.continuation_context(document, index, request)
        self.assertTrue(context["continuation"])
        self.assertTrue(context["previous_source_tail"].endswith("the tuning"))
        self.assertTrue(context["previous_translation_tail"].endswith("涉及调参的结尾"))
        self.assertNotIn("continuation", service.context_for(
            document, next(i for i, item in enumerate(blocks) if item["id"] == "p1-l1"), request))

    def test_ellipsis_added_only_when_source_has_none(self):
        self.assertTrue(service.introduces_ellipsis("sampled across", "在……中被随机采样"))
        self.assertTrue(service.introduces_ellipsis("sampled across", "在...中被随机采样"))
        self.assertFalse(service.introduces_ellipsis("sampled across …", "在……中被随机采样"))
        self.assertFalse(service.introduces_ellipsis("sampled across", "被随机采样"))


if __name__ == "__main__":
    unittest.main()
