import io
import threading
import unittest
from unittest.mock import MagicMock, patch

import launch


def response(body, content_type="application/json"):
    result = io.BytesIO(body)
    result.status = 200
    result.headers = {"Content-Type": content_type}
    return result


class LauncherTests(unittest.TestCase):
    def test_ready_requires_backend_and_both_assets(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b'{"token":"test"}'), response(b"body{}", "text/css"), response(b"queue();", "text/javascript"), response(b"init();", "text/javascript")]
        with patch.object(launch.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(launch.server_ready())
        self.assertEqual(opener.open.call_count, 4)

    def test_missing_queue_does_not_open_broken_page(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b'{"token":"test"}'), response(b"body{}", "text/css"), response(b"missing", "text/html")]
        with patch.object(launch.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(launch.server_ready())

    def test_missing_stylesheet_does_not_open_broken_page(self):
        opener = MagicMock()
        opener.open.side_effect = [response(b'{"token":"test"}'), response(b"not css", "text/html")]
        with patch.object(launch.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(launch.server_ready())

    def test_existing_server_is_reused(self):
        with patch.object(launch, "server_ready", return_value=True), patch.object(launch.webbrowser, "open") as browser, patch.object(launch.uvicorn, "run") as server:
            launch.main()
            browser.assert_called_once_with(launch.URL)
            server.assert_not_called()

    def test_browser_opens_only_once_after_ready(self):
        with patch.object(launch, "server_ready", return_value=True), patch.object(launch.webbrowser, "open") as browser:
            launch.open_when_ready(threading.Event())
            browser.assert_called_once_with(launch.URL)


if __name__ == "__main__":
    unittest.main()
