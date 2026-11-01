import base64
import contextlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import import_session as session


def request(authorization="Bearer example-session", url=session.API_URL):
    return f'''curl --url '{url}' \\
  -H 'accept: */*' \\
  -H 'authorization: {authorization}' \\
  -H 'sec-ch-ua: "Chromium";v="155"' \\
  -b 'locale=en-GB; session="example-cookie"' \\
  --data-raw $'{{"query":"example query"}}' '''


def jwt(expiry):
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": expiry}).encode()).decode().rstrip("=")
    return f"Bearer Status|unauthenticated|Session|header.{encoded}.signature"


class SessionImportTests(unittest.TestCase):
    def test_browser_request_preserves_cookie_quotes_and_header_values(self):
        headers = session.parse_request(request().replace("\n", "\r\n"))
        self.assertEqual(headers["authorization"], "Bearer example-session")
        self.assertEqual(headers["cookie"], 'locale=en-GB; session="example-cookie"')
        self.assertEqual(headers["sec-ch-ua"], '"Chromium";v="155"')

    def test_chrome_bare_url_and_cookie_header_are_supported(self):
        content = request().replace("--url ", "").replace("-b 'locale=", "-H 'cookie: locale=")
        self.assertIn("cookie", session.parse_request(content))

    def test_other_endpoints_and_missing_session_headers_are_rejected(self):
        for content in [request(url="https://example.com/graphql"), request().replace("-H 'authorization: Bearer example-session'", "")]:
            with self.subTest(content=content), self.assertRaises(ValueError):
                session.parse_request(content)

    def test_import_writes_valid_json_without_printing_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "request.curl"
            output = root / "headers.json"
            source.write_text(request(jwt(time.time() + 3600)), encoding="utf-8-sig")
            terminal = io.StringIO()
            with patch("sys.argv", ["import_session.py", str(source), "--output", str(output)]), contextlib.redirect_stdout(terminal):
                self.assertEqual(session.main(), 0)
            headers = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(headers["cookie"], 'locale=en-GB; session="example-cookie"')
            self.assertNotIn(headers["authorization"], terminal.getvalue())
            self.assertNotIn("example-cookie", terminal.getvalue())
            self.assertEqual({path.name for path in root.iterdir()}, {"request.curl", "headers.json"})

    def test_expired_or_malformed_requests_preserve_existing_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output = root / "request.curl", root / "headers.json"
            output.write_text('{"authorization": "keep-current-session"}', encoding="utf-8")
            original = output.read_bytes()
            for content in [request(jwt(time.time() - 60)), "curl 'unfinished"]:
                source.write_text(content, encoding="utf-8")
                with self.subTest(content=content), self.assertRaises(ValueError):
                    session.import_session(source, output)
                self.assertEqual(output.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
