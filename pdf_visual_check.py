"""Post-layout visual acceptance through the same translation API.

Checks are page-cached, separate from translation and never rewrite wording.
Failure/unsupported vision is explicitly incomplete, never a successful audit.
"""
import hashlib
import json
from pathlib import Path
import threading

import pdf_document
import pdf_model
from performance import count

CHECK_VERSION = 1
_LOCK = threading.RLock()


def _validate_result(data, block_ids):
    if data.get("status") not in {"passed", "issues"} or not isinstance(data.get("issues"), list) or len(data["issues"]) > 100:
        raise pdf_model.VisionError("视觉验收响应格式不完整")
    if (data["status"] == "passed") != (len(data["issues"]) == 0):
        raise pdf_model.VisionError("视觉验收状态与问题列表矛盾")
    problems = []
    kinds = {"missing_text", "overlap", "clipping", "formula_changed", "figure_obscured", "spacing", "style", "uncertain"}
    for item in data["issues"]:
        if not isinstance(item, dict) or item.get("kind") not in kinds or item.get("severity") not in {"warning", "error"}:
            raise pdf_model.VisionError("视觉验收问题类型无效")
        description = item.get("message")
        block_id = item.get("block_id")
        if not isinstance(description, str) or not description.strip() or len(description) > 1000 or block_id is not None and block_id not in block_ids:
            raise pdf_model.VisionError("视觉验收问题描述或段落对应无效")
        coords = pdf_model.box(item["bbox"]) if item.get("bbox") is not None else None
        problems.append({"kind": item["kind"], "severity": item["severity"], "message": description.strip(), "block_id": block_id, "bbox": coords})
    return {"status": data["status"], "issues": problems}


def validate(directory, document, report):
    directory = Path(directory)
    result = dict(report)
    result["issues"] = list(report["issues"])
    pages = []
    # No credentials/configuration is a truthful incomplete result for old API
    # clients and offline tools. The current UI always supplies its agent.
    if not pdf_model.configured():
        result["visual_check"] = {"status": "unavailable", "pages": [], "message": "未提供翻译 API 配置，未执行视觉验收"}
        result["issues"].append({"page": 1, "block_id": None, "kind": "visual_check", "message": result["visual_check"]["message"]})
        return result
    selected = [page for page in document["pages"] if any(b.get("translation") and b.get("target") == report["target"] for b in page["blocks"])]
    source_hash = pdf_document.file_hash(directory / "source.pdf")
    with _LOCK:
        for index, saved in enumerate(selected):
            number = saved["number"]
            pdf_document._PROGRESS[str(directory.resolve())] = {"state": "checking", "message": f"视觉 API 正在验收页面 {index + 1}/{len(selected)}", "current": index + 1, "total": len(selected)}
            # Page content, including native glyph/annotation changes, determines
            # the check identity. Unchanged pages survive another page's edit.
            source_image, _, _ = pdf_model.page_image(directory / "source.pdf", number)
            output_image, _, _ = pdf_model.page_image(directory / "layout" / (report["revision"] + ".pdf"), number)
            kept = [issue for issue in report["issues"] if issue.get("page") == number and issue.get("kind") in {"preserved", "reference", "untranslated"}]
            identity = {"version": CHECK_VERSION, "api": pdf_model.fingerprint(), "source": source_hash, "page": number,
                        "output_pixels": hashlib.sha256(output_image.encode()).hexdigest(), "target": report["target"],
                        "blocks": saved["blocks"], "preserved": kept}
            key = pdf_document.text_hash(json.dumps(identity, sort_keys=True))
            path = directory / "structure" / "visual-checks" / (key + ".json")
            block_ids = {b["id"] for b in saved["blocks"]}
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                checked = _validate_result(cached, block_ids)
                count("visual_check_cache_hits")
            except (OSError, ValueError, TypeError, pdf_model.VisionError):
                try:
                    # Full expected text can be large; no silent truncation that
                    # could hide omissions. Bound unusually dense pages visibly.
                    context = json.dumps({"expected_blocks": [{"id": b["id"], "source": b["text"], "translation": b.get("translation", "")} for b in saved["blocks"]],
                                          "intentionally_preserved": kept}, ensure_ascii=False)
                    if len(context) > 100000:
                        raise pdf_model.VisionError("本页文字量超出视觉验收预算，请手动检查；未标记为通过")
                    prompt = ("对照两张图片进行排版视觉验收：第一张原文页，第二张译文页。"
                              "检查漏字、文字重叠/截断、公式字形或符号变化、图表遮挡、异常新增留白、正文意外变粗或换字体。"
                              "只检查可见版式和内容完整性，不评审译文措辞，不要求等长翻译；原文已有留白及明确保留的区域不要误报为漏译。"
                              "无法判断请返回 uncertain，不能猜测通过。bbox 使用第二张图左上角归一化 [x0,y0,x1,y1]，无法定位可为 null。"
                              '返回 {"status":"passed"或"issues","issues":[{"kind":"missing_text|overlap|clipping|formula_changed|figure_obscured|spacing|style|uncertain",'
                              '"severity":"warning"或"error","message":"具体问题","block_id":"已提供ID或null","bbox":null}]}。'
                              "通过时 issues 必须为空。参考文字与保留说明：\n" + context)
                    checked = _validate_result(pdf_model.request_json("vision_acceptance", prompt, [source_image, output_image]), block_ids)
                    pdf_document.atomic_json(path, checked)
                except pdf_model.VisionError as error:
                    checked = {"status": "unavailable", "issues": [], "message": str(error)}
            pages.append({"page": number, **checked})
            for issue in checked["issues"]:
                result["issues"].append({"page": number, "block_id": issue["block_id"], "kind": "visual_check", "message": "视觉验收：" + issue["message"]})
            if checked["status"] == "unavailable":
                result["issues"].append({"page": number, "block_id": None, "kind": "visual_check", "message": "视觉验收未完成：" + checked["message"]})
        overall = "unavailable" if any(p["status"] == "unavailable" for p in pages) else "issues" if any(p["status"] == "issues" for p in pages) else "passed"
        result["visual_check"] = {"status": overall, "pages": pages, "provider": "translation_api", "version": CHECK_VERSION}
        pdf_document._PROGRESS[str(directory.resolve())] = {"state": overall, "message": {"passed": "视觉验收通过", "issues": "视觉验收发现问题，请查看排版报告", "unavailable": "视觉验收未完成，请检查翻译 API 后重试"}[overall]}
    return result
