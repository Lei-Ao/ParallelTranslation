import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import pymupdf
from fastapi.testclient import TestClient

import app as service


def sample_pdf(rotation=0):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=595, height=842)
        page.insert_text((60, 80), "Reading with context", fontsize=22)
        page.insert_textbox((60, 120, 530, 220), "An agent translates a document using surrounding paragraphs.\nConsistent terminology helps readers understand technical ideas.", fontsize=12)
        page.insert_textbox((60, 250, 530, 350), "A second paragraph contains a quote: \"attention\".\nValues such as 42 and citations [1] should remain unchanged.", fontsize=12)
        page.set_rotation(rotation)
        pdf.new_page()
        return pdf.tobytes()


class TranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data_patch = patch.object(service, "DATA", Path(self.temp.name))
        self.data_patch.start()
        self.client = TestClient(service.app)
        self.client.headers["X-Local-Token"] = self.client.get("/api/session").json()["token"]
        self.agent = {"base_url": "https://example.com/v1", "model": "my-agent", "api_key": "test-secret"}

    def tearDown(self):
        self.client.close()
        self.data_patch.stop()
        self.temp.cleanup()

    def upload(self, rotation=0):
        result = self.client.post("/api/documents", files={"file": ("sample.pdf", sample_pdf(rotation), "application/pdf")})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def mock_agent(self, handler):
        real_client = httpx.AsyncClient
        return patch.object(service.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))

    def test_upload_extraction_image_and_reload(self):
        doc = self.upload()
        self.assertEqual(len(doc["pages"]), 2)
        self.assertEqual(doc["total_blocks"], 3)
        self.assertTrue(doc["pages"][1]["needs_ocr"])
        self.assertIn("Reading with context", doc["pages"][0]["blocks"][0]["text"])
        image = self.client.get(f"/api/documents/{doc['id']}/pages/1.png")
        self.assertTrue(image.content.startswith(b"\x89PNG"))
        self.assertEqual(self.client.get(f"/api/documents/{doc['id']}").json(), doc)
        self.assertEqual(self.client.get("/api/documents").json()[0]["pages"], 2)

    def test_rotated_page_coordinates(self):
        doc = self.upload(90)
        page = doc["pages"][0]
        self.assertEqual(page["width"], 842)
        x0, y0, x1, y1 = page["blocks"][0]["bbox"]
        self.assertTrue(0 <= x0 < x1 <= 1)
        self.assertTrue(0 <= y0 < y1 <= 1)
        self.assertGreater(x0, .8)

    def test_openai_translation_context_and_save(self):
        doc = self.upload()
        block_id = doc["pages"][0]["blocks"][1]["id"]
        def handler(request):
            self.assertEqual(str(request.url), "https://example.com/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-secret")
            payload = json.loads(request.content)
            self.assertFalse(payload["stream"])
            self.assertEqual(payload["model"], "my-agent")
            prompt = payload["messages"][1]["content"]
            self.assertIn("agent = 智能体", prompt)
            self.assertIn("Reading with context", prompt)
            self.assertIn("A second paragraph", prompt)
            return httpx.Response(200, json={"choices": [{"message": {"content": "智能体结合上下文翻译文档。"}, "finish_reason": "stop"}]})
        with self.mock_agent(handler):
            response = self.client.post(f"/api/documents/{doc['id']}/blocks/{block_id}/translate", json={"agent": self.agent, "glossary": "agent = 智能体"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["translation"], "智能体结合上下文翻译文档。")
        saved = self.client.get(f"/api/documents/{doc['id']}").json()
        self.assertEqual(saved["pages"][0]["blocks"][1]["target"], "简体中文")
        self.assertNotIn("test-secret", json.dumps(saved))
        self.assertEqual(self.client.get("/api/documents").json()[0]["translated"], 1)

    def test_custom_json_handles_quotes_newlines_and_nested_response(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["data"]["input"], 'text "quoted"\nnext')
            self.assertIn("专业文档翻译", body["prompt"])
            return httpx.Response(200, json={"data": [{"translation": "自定义译文"}]})
        config = service.AgentConfig(mode="custom", base_url="https://example.com/agent", template='{"data":{"input":"{{text}}"},"prompt":"{{prompt}}"}', response_path="data.0.translation")
        import asyncio
        with self.mock_agent(handler):
            result = asyncio.run(service.invoke_agent(config, "要求", 'text "quoted"\nnext'))
        self.assertEqual(result, "自定义译文")

    def test_agent_client_is_reused_inside_one_event_loop(self):
        import asyncio
        service._AGENT_CLIENT = service._AGENT_CLIENT_KEY = None
        calls = []
        real_client = httpx.AsyncClient

        def factory(**kwargs):
            calls.append(kwargs)
            return real_client(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}]})), **kwargs)

        with patch.object(service.httpx, "AsyncClient", side_effect=factory):
            async def run():
                config = service.AgentConfig(**self.agent)
                await service.invoke_agent(config, "first", "first")
                await service.invoke_agent(config, "second", "second")
            asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertIn("limits", calls[0])

    def test_concurrent_translation_saves_every_block_without_lost_updates(self):
        import asyncio
        doc = self.upload()
        active = 0
        peak = 0

        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            prompt = json.loads(request.content)["messages"][1]["content"]
            context = json.loads(prompt.split("以下 JSON 为待翻译内容与参考资料：\n", 1)[1])
            await asyncio.sleep(.01)
            active -= 1
            return httpx.Response(200, json={"choices": [{"message": {"content": "译文 " + context["block_id"]}, "finish_reason": "stop"}]})

        async def translate_all():
            request = service.TranslateRequest(agent=service.AgentConfig(**self.agent))
            return await asyncio.gather(*(service.translate(doc["id"], block["id"], request)
                                          for block in service.all_blocks(doc)))

        with self.mock_agent(handler):
            asyncio.run(translate_all())
        self.assertEqual(peak, 3)
        saved = service.load_doc(doc["id"])
        for block in service.all_blocks(saved):
            self.assertEqual(block["translation"], "译文 " + block["id"])
            self.assertEqual(block["target"], "简体中文")

    def test_failed_translation_preserves_previous_edit(self):
        doc = self.upload()
        block = doc["pages"][0]["blocks"][0]
        route = f"/api/documents/{doc['id']}/blocks/{block['id']}"
        self.client.patch(route, json={"translation": "已校对的译文"})
        with self.mock_agent(lambda req: httpx.Response(401, text="secret provider details")):
            response = self.client.post(route + "/translate", json={"agent": self.agent})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("secret", response.text)
        saved = self.client.get(f"/api/documents/{doc['id']}").json()
        self.assertEqual(saved["pages"][0]["blocks"][0]["translation"], "已校对的译文")

    def test_batch_translation_maps_ids_and_saves_atomically(self):
        doc = self.upload()
        ids = [b["id"] for b in service.all_blocks(doc)]
        def handler(request):
            payload = json.loads(request.content)
            self.assertIn("current_blocks", payload["messages"][1]["content"])
            output = {"blocks": [{"id": block_id, "translation": "译文 " + block_id} for block_id in reversed(ids)]}
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}]})
        with self.mock_agent(handler):
            response = self.client.post(f"/api/documents/{doc['id']}/blocks/translate-batch", json={"agent": self.agent, "block_ids": ids})
        self.assertEqual(response.status_code, 200, response.text)
        for block in service.all_blocks(service.load_doc(doc["id"])):
            self.assertEqual(block["translation"], "译文 " + block["id"])

    def test_invalid_batch_does_not_overwrite_existing_translation(self):
        doc = self.upload()
        ids = [b["id"] for b in service.all_blocks(doc)]
        self.client.patch(f"/api/documents/{doc['id']}/blocks/{ids[0]}", json={"translation": "已校对"})
        payloads = [{"blocks": [{"id": ids[0], "translation": "部分结果"}]},
                    {"blocks": [{"id": ids[0], "translation": "重复"}] * 3},
                    {"blocks": [{"id": block_id, "translation": ""} for block_id in ids]}]
        for output in payloads:
            with self.mock_agent(lambda req: httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(output)}}]})):
                response = self.client.post(f"/api/documents/{doc['id']}/blocks/translate-batch", json={"agent": self.agent, "block_ids": ids})
            self.assertEqual(response.status_code, 502)
            self.assertEqual(response.headers.get("X-Translation-Fallback"), "single")
            self.assertEqual(service.all_blocks(service.load_doc(doc["id"]))[0]["translation"], "已校对")

    def test_agent_errors_distinguish_transient_from_authentication_failures(self):
        for status in [401, 403, 404, 408, 429, 500, 502, 503, 504]:
            with self.subTest(status=status), self.mock_agent(lambda req: httpx.Response(status)):
                response = self.client.post("/api/agent/test", json=self.agent)
            self.assertEqual(response.headers.get("X-Agent-Status"), str(status))
            self.assertEqual(response.headers.get("X-Translation-Retryable") == "true", status in {408, 429, 500, 502, 503, 504})
            self.assertNotIn("X-Translation-Fallback", response.headers)

    def test_truncated_batch_can_fallback_without_saving_partial_text(self):
        doc = self.upload()
        ids = [b["id"] for b in service.all_blocks(doc)]
        with self.mock_agent(lambda req: httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]})):
            response = self.client.post(f"/api/documents/{doc['id']}/blocks/translate-batch", json={"agent": self.agent, "block_ids": ids})
        self.assertEqual(response.headers.get("X-Translation-Fallback"), "single")
        self.assertTrue(all(not b["translation"] for b in service.all_blocks(service.load_doc(doc["id"]))))

    def test_truncated_and_invalid_output_rejected(self):
        for payload in [{"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}, {"choices": []}, {"choices": [{"message": {"content": ""}}]}]:
            with self.subTest(payload=payload), self.mock_agent(lambda req: httpx.Response(200, json=payload)):
                response = self.client.post("/api/agent/test", json=self.agent)
                self.assertEqual(response.status_code, 502)

    def test_timeout_reported(self):
        def handler(request):
            raise httpx.ReadTimeout("timed out")
        with self.mock_agent(handler):
            self.assertEqual(self.client.post("/api/agent/test", json=self.agent).status_code, 504)

    def test_edit_and_export(self):
        doc = self.upload()
        block = doc["pages"][0]["blocks"][0]
        response = self.client.patch(f"/api/documents/{doc['id']}/blocks/{block['id']}", json={"translation": "结合上下文阅读"})
        self.assertTrue(response.json()["edited"])
        export = self.client.get(f"/api/documents/{doc['id']}/export")
        self.assertIn("结合上下文阅读", export.text)
        self.assertIn("Reading with context", export.text)
        self.assertIn("尚未翻译", export.text)

    def test_upload_validation(self):
        response = self.client.post("/api/documents", files={"file": ("bad.pdf", b"not a pdf")})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/documents", files={"file": ("bad.pdf", b"%PDF-broken")})
        self.assertEqual(response.status_code, 400)
        with pymupdf.open(stream=sample_pdf(), filetype="pdf") as pdf:
            encrypted = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
        response = self.client.post("/api/documents", files={"file": ("locked.pdf", encrypted)})
        self.assertEqual(response.status_code, 400)

    def test_endpoint_normalization_and_security(self):
        self.assertEqual(service.endpoint(service.AgentConfig(base_url="https://example.com/v1/")), "https://example.com/v1/chat/completions")
        url = "https://example.com/v1/chat/completions"
        self.assertEqual(service.endpoint(service.AgentConfig(base_url=url)), url)
        for url in ["file:///etc/passwd", "http://remote.example/v1", "https://key@example.com", "https://example.com/?key=secret"]:
            with self.assertRaises(service.HTTPException):
                service.endpoint(service.AgentConfig(base_url=url))

    def test_non_translatable_fragments_skip_agent(self):
        for value in ["0.4", "2000", "(5)", "(m)", "⎧\n⎪⎪⎨", ""]:
            self.assertTrue(service.non_translatable_text(value), value)
        for value in ["Reading with context", "(deg)", "sin θ", "Figure 1"]:
            self.assertFalse(service.non_translatable_text(value), value)
        doc = self.upload()
        block_id = doc["pages"][0]["blocks"][0]["id"]
        saved = service.load_doc(doc["id"])
        service.find_block(saved, block_id)[2]["text"] = "0.4"
        service.save_doc(service.doc_dir(doc["id"]), saved)
        response = self.client.post(f"/api/documents/{doc['id']}/blocks/{block_id}/translate",
                                    json={"agent": {"base_url": "file:///etc/passwd", "model": "x"}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["translation"], "0.4")

    def test_csrf_and_host_protection(self):
        self.assertEqual(self.client.post("/api/agent/test", json=self.agent, headers={"X-Local-Token": ""}).status_code, 403)
        self.assertEqual(self.client.get("/api/session", headers={"Origin": "https://foreign.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/session", headers={"Host": "foreign.example"}).status_code, 403)

    def test_invalid_document_and_page(self):
        self.assertEqual(self.client.get("/api/documents/not-a-document").status_code, 404)
        doc = self.upload()
        self.assertEqual(self.client.get(f"/api/documents/{doc['id']}/pages/0.png").status_code, 404)


if __name__ == "__main__":
    unittest.main()
