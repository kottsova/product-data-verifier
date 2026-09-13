import unittest
from unittest.mock import patch

import requests

from core.fetch import _html_content_complete, fetch_source


class FakeResponse:
    def __init__(self, body=b"", status=200, url="https://example.com/final", content_type="text/html; charset=utf-8"):
        self.content = body
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    @property
    def text(self):
        return self.content.decode(self.encoding or "utf-8", errors="replace")


class FakeSession:
    def __init__(self, response):
        self.response = response

    def get(self, *_args, **_kwargs):
        return self.response


def html_response(body, status=200):
    return FakeResponse(body.encode(), status=status)


class FetchSourceTests(unittest.TestCase):
    def test_normal_html_returns_meaningful_text_without_playwright(self):
        body = "<html><head><title>Model X100</title><meta name='description' content='Product description'></head><body><script>bad()</script><h1>Model X100</h1><table><tr><td>Weight</td><td>10 kg</td></tr></table><p>" + "Useful product information. " * 5 + "</p></body></html>"
        with patch("core.fetch._fetch_with_playwright") as browser:
            result = fetch_source("https://example.com/product", session=FakeSession(html_response(body)))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["fetch_method"], "requests")
        self.assertIn("Weight 10 kg", result["text"])
        self.assertNotIn("bad()", result["text"])
        browser.assert_not_called()

    def test_js_shell_uses_playwright(self):
        shell = "<html><body><div id='root'></div><script>render()</script></body></html>"
        rendered = "<html><head><title>X100</title></head><body>" + "Rendered product content. " * 10 + "</body></html>"
        with patch("core.fetch._fetch_with_playwright", return_value=("https://example.com/product", 200, rendered)) as browser:
            result = fetch_source("https://example.com/product", session=FakeSession(html_response(shell)))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["fetch_method"], "playwright")
        self.assertIn("Rendered product content", result["text"])
        browser.assert_called_once()

    def test_empty_spec_values_require_playwright(self):
        shell = """<html><body><section class='product-specifications'>
          <div class='spec-row'><h3 class='spec-label'>Power</h3><div class='spec-value' data-value='4600 W'></div></div>
          <div class='spec-row'><h3 class='spec-label'>Width</h3><div class='spec-value'></div></div>
          <div class='spec-row'><h3 class='spec-label'>Weight</h3><div class='spec-value'></div></div>
        </section></body></html>"""
        rendered = """<html><body><section class='product-specifications'>
          <div class='spec-row'><h3>Power</h3><div>4600 W</div></div>
          <div class='spec-row'><h3>Width</h3><div>592 mm</div></div>
          <div class='spec-row'><h3>Weight</h3><div>10 kg</div></div>
        </section></body></html>"""
        text = "Power Width Weight " + "product specifications " * 20
        self.assertFalse(_html_content_complete(shell, text))
        with patch("core.fetch._fetch_with_playwright", return_value=("https://example.com/p", 200, rendered)) as browser:
            result = fetch_source("https://example.com/p", session=FakeSession(html_response(shell)))
        self.assertEqual(result["fetch_method"], "playwright")
        browser.assert_called_once()

    def test_meaningful_product_json_ld_does_not_require_playwright(self):
        body = """<html><body><section class='product-specifications'><h3>Power</h3></section>
        <script type='application/ld+json'>{"@type":"Product","mpn":"X100",
          "additionalProperty":[{"@type":"PropertyValue","name":"Power","value":"4600 W"}]}</script>
        </body></html>"""
        with patch("core.fetch._fetch_with_playwright") as browser:
            result = fetch_source("https://example.com/p", session=FakeSession(html_response(body)))
        self.assertEqual(result["fetch_method"], "requests")
        browser.assert_not_called()

    def test_broad_product_detail_container_with_incidental_empty_ui_is_complete(self):
        body = """<html><body><main class='product-detail'>
          <div><h2>Technical data</h2><p>Power: 4600 W</p><p>Width: 592 mm</p>
          <p>Weight: 10 kg</p><div class='content'></div><div class='value'></div>
          <div class='description'></div><p>Useful complete product information is available in this section.</p>
          </div></main></body></html>"""
        with patch("core.fetch._fetch_with_playwright") as browser:
            result = fetch_source("https://example.com/p", session=FakeSession(html_response(body)))
        self.assertEqual(result["fetch_method"], "requests")
        browser.assert_not_called()

    def test_block_pages_are_structured(self):
        cases = {
            "Complete the CAPTCHA": "captcha",
            "Access Denied": "access_denied",
            "Sign in to continue": "login_required",
            "Subscribe to continue": "paywall",
            "Verify you are human": "bot_challenge",
        }
        for body, reason in cases.items():
            with self.subTest(reason=reason), patch("core.fetch._fetch_with_playwright") as browser:
                result = fetch_source("https://example.com/product", session=FakeSession(html_response(f"<html><body>{body}</body></html>")))
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["blocked_reason"], reason)
                browser.assert_not_called()

    def test_http_statuses_are_structured(self):
        for status, expected, reason in ((404, "not_found", None), (403, "blocked", "access_denied"), (500, "error", None)):
            with self.subTest(status=status):
                result = fetch_source("https://example.com/x", session=FakeSession(html_response("error", status)))
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["http_status"], status)
                self.assertEqual(result["blocked_reason"], reason)

    def test_binary_image_is_unsupported(self):
        response = FakeResponse(b"image", content_type="image/png")
        result = fetch_source("https://example.com/image.png", session=FakeSession(response))
        self.assertEqual(result["status"], "unsupported")
        self.assertEqual(result["document_type"], "binary")

    def test_invalid_scheme_is_rejected(self):
        result = fetch_source("file:///etc/passwd")
        self.assertEqual(result["status"], "unsupported")
        self.assertIsNotNone(result["error"])

    def test_request_exception_is_structured(self):
        session = FakeSession(None)
        session.get = lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.ConnectionError("offline"))
        result = fetch_source("https://example.com", session=session)
        self.assertEqual(result["status"], "error")

    def test_pdf_preserves_urls_type_and_text(self):
        response = FakeResponse(b"%PDF fake", url="https://cdn.example/manual.pdf", content_type="application/pdf")
        with patch("core.fetch._extract_pdf_text", return_value="Model X100 manual"):
            result = fetch_source("https://example.com/download?id=1", session=FakeSession(response))
        self.assertEqual(result["source_url"], "https://example.com/download?id=1")
        self.assertEqual(result["final_url"], "https://cdn.example/manual.pdf")
        self.assertEqual(result["fetch_method"], "pdf")
        self.assertEqual(result["pdf_text"], "Model X100 manual")
        self.assertEqual(result["text_status"], "available")

    def test_image_only_pdf_is_not_a_fetch_error(self):
        response = FakeResponse(b"%PDF scanned", content_type="application/pdf")
        with patch("core.fetch._extract_pdf_text", return_value=""):
            result = fetch_source("https://example.com/scan.pdf", session=FakeSession(response))
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["text_status"], "text_not_available")
        self.assertIsNone(result["error"])


if __name__ == "__main__":
    unittest.main()
