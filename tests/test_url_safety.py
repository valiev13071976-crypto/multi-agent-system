import unittest

from tools.url_safety import (
    UnsafeUrlError,
    is_safe_http_url,
    validate_http_url,
    validate_redirect,
)


class UrlSafetyTests(unittest.TestCase):

    def test_g_localhost_rejected(self):
        self.assertFalse(is_safe_http_url("http://localhost/secret"))
        with self.assertRaises(UnsafeUrlError):
            validate_http_url("https://localhost/x")

    def test_h_loopback_rejected(self):
        self.assertFalse(is_safe_http_url("http://127.0.0.1/x"))
        self.assertFalse(is_safe_http_url("http://[::1]/x"))

    def test_i_private_ranges_rejected(self):
        self.assertFalse(is_safe_http_url("http://10.1.2.3/x"))
        self.assertFalse(is_safe_http_url("http://172.16.4.5/x"))
        self.assertFalse(is_safe_http_url("http://172.31.9.9/x"))
        self.assertFalse(is_safe_http_url("http://192.168.1.10/x"))

    def test_j_file_scheme_rejected(self):
        self.assertFalse(is_safe_http_url("file:///etc/passwd"))

    def test_k_public_https_allowed(self):
        validated = validate_http_url("https://en.wikipedia.org/wiki/Test")
        self.assertTrue(validated.startswith("https://"))
        self.assertTrue(is_safe_http_url("https://www.oecd.org/data"))

    def test_l_redirect_to_private_rejected(self):
        with self.assertRaises(UnsafeUrlError):
            validate_redirect(
                "https://en.wikipedia.org/wiki/Test",
                "http://127.0.0.1/internal",
            )
        with self.assertRaises(UnsafeUrlError):
            validate_redirect(
                "https://example.com/out",
                "http://192.168.0.20/admin",
            )
        self.assertTrue(
            validate_redirect(
                "https://example.com/a",
                "https://example.com/b",
            )
        )


if __name__ == "__main__":
    unittest.main()
