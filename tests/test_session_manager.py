import base64
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import amazon_jobs as app
from session_manager import SessionError, SessionManager, merge_cookies, token_expiry


def token(expires):
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def headers(expires):
    return {"authorization": "Bearer Status|UNAUTHENTICATED|Session|" + token(expires), "cookie": 'aws-waf-token=keep-existing; session="old"'}


class SessionManagerTests(unittest.TestCase):
    def setUp(self):
        alert_patch = patch("amazon_jobs.JobAlert")
        alert_patch.start()
        self.addCleanup(alert_patch.stop)
        browser_patch = patch("amazon_jobs.JobBrowser")
        browser_patch.start()
        self.addCleanup(browser_patch.stop)

    def test_valid_session_does_not_make_a_renewal_request(self):
        fetch = Mock()
        with tempfile.TemporaryDirectory() as folder:
            manager = SessionManager(Path(folder) / "headers.json", headers(4600), fetch, clock=lambda: 1000)
            self.assertEqual(manager.ensure(15), headers(4600))
        fetch.assert_not_called()

    def test_near_expiry_renews_token_and_saves_cookie_updates(self):
        fetch = Mock(return_value=({"token": token(4600)}, [("set-cookie", "session=new; Path=/; Secure; HttpOnly"), ("set-cookie", "another=value; Path=/")]))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "headers.json"
            manager = SessionManager(path, headers(1200), fetch, clock=lambda: 1000)
            renewed = manager.ensure(15)
            self.assertEqual(token_expiry(renewed), 4600)
            self.assertIn("aws-waf-token=keep-existing", renewed["cookie"])
            self.assertIn("session=new", renewed["cookie"])
            self.assertIn("another=value", renewed["cookie"])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), renewed)
            manager.ensure(15)
        self.assertEqual(fetch.call_count, 1)

    def test_expired_or_missing_token_triggers_renewal(self):
        for previous in (headers(900), {}):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as folder:
                fetch = Mock(return_value=({"token": token(4600)}, []))
                manager = SessionManager(Path(folder) / "headers.json", previous, fetch, clock=lambda: 1000)
                self.assertEqual(token_expiry(manager.ensure(15)), 4600)
                fetch.assert_called_once()

    def test_repeated_renewal_across_more_than_eight_hours(self):
        clock = [1000]
        fetch = Mock(side_effect=lambda current, timeout: ({"token": token(clock[0] + 3600)}, []))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "headers.json"
            manager = SessionManager(path, headers(4600), fetch, clock=lambda: clock[0])
            for _ in range(9):
                clock[0] += 3300  # Advance 55 minutes without sleeping.
                self.assertEqual(token_expiry(manager.ensure(15)), clock[0] + 3600)
            self.assertEqual(token_expiry(json.loads(path.read_text())), clock[0] + 3600)
        self.assertEqual(fetch.call_count, 9)

    def test_failed_renewal_preserves_old_file_and_waits_before_retry(self):
        clock = [1000]
        fetch = Mock(side_effect=app.PollError("offline"))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "headers.json"
            path.write_text(json.dumps(headers(1200)), encoding="utf-8")
            original = path.read_bytes()
            manager = SessionManager(path, headers(1200), fetch, clock=lambda: clock[0])
            with self.assertRaises(app.PollError):
                manager.ensure(15)
            with self.assertRaises(SessionError) as caught:
                manager.ensure(15)
            self.assertEqual(caught.exception.retry_after, 60)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(path.read_bytes(), original)
            clock[0] += 60
            fetch.side_effect = None
            fetch.return_value = ({"token": token(4700)}, [])
            self.assertEqual(token_expiry(manager.ensure(15)), 4700)

    def test_invalid_session_responses_do_not_overwrite_saved_headers(self):
        for body in ({}, {"token": ""}, {"token": "bad\nheader"}, {"token": token(999)}):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "headers.json"
                path.write_text(json.dumps(headers(1200)), encoding="utf-8")
                original = path.read_bytes()
                manager = SessionManager(path, headers(1200), Mock(return_value=(body, [])), clock=lambda: 1000)
                with self.assertRaises(SessionError):
                    manager.ensure(15)
                self.assertEqual(path.read_bytes(), original)

    def test_forced_renewal_replaces_a_still_valid_token(self):
        fetch = Mock(return_value=({"token": token(5000)}, []))
        with tempfile.TemporaryDirectory() as folder:
            manager = SessionManager(Path(folder) / "headers.json", headers(4600), fetch, clock=lambda: 1000)
            self.assertEqual(token_expiry(manager.ensure(15, force=True)), 5000)
        fetch.assert_called_once()

    def test_deleted_cookie_is_removed_without_discarding_other_cookies(self):
        cookie = merge_cookies("keep=value; remove=value", [("Set-Cookie", "remove=; Max-Age=0; Path=/")])
        self.assertEqual(cookie, "keep=value")

    def test_session_request_excludes_old_authorization_but_keeps_cookies(self):
        with patch("amazon_jobs.shutil.which", return_value="curl.exe"), patch("amazon_jobs._curl_json_request", return_value=({}, [])) as request:
            app.fetch_session({"Authorization": "expired-secret", "Cookie": "session=existing", "Content-Length": "999"}, 15)
        url, sent, timeout, executable = request.call_args.args
        self.assertEqual(url, app.SESSION_URL)
        self.assertNotIn("authorization", sent)
        self.assertNotIn("content-length", sent)
        self.assertEqual(sent["cookie"], "session=existing")

    def test_curl_session_get_keeps_multiple_set_cookie_headers(self):
        def execute(command, **kwargs):
            self.assertEqual(command[command.index("--request") + 1], "GET")
            self.assertNotIn("--data-binary", command)
            Path(command[command.index("--dump-header") + 1]).write_text("HTTP/2 200\r\nSet-Cookie: first=one; Path=/\r\nSet-Cookie: second=two; Path=/\r\n\r\n", encoding="utf-8")
            Path(command[command.index("--output") + 1]).write_text(json.dumps({"token": "fresh"}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "200", "")
        with patch("amazon_jobs.subprocess.run", side_effect=execute):
            body, response_headers = app._curl_json_request(app.SESSION_URL, {}, 15, "curl")
        self.assertEqual(body, {"token": "fresh"})
        self.assertEqual(len([pair for pair in response_headers if pair[0] == "set-cookie"]), 2)

    def test_default_recorder_renews_expired_session_before_fetching_jobs(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session_path, records_path = root / "headers.json", root / "records.json"
            session_path.write_text(json.dumps(headers(time.time() - 1)), encoding="utf-8")
            fresh = token(time.time() + 3600)
            args = ["amazon_jobs.py", "--once", "--headers-file", str(session_path), "--records", str(records_path)]
            def search(payload, sent_headers, timeout, transport):
                self.assertTrue(sent_headers["authorization"].endswith(fresh))
                return [{"jobId": "JOB-TEST"}]
            terminal_out, terminal_error = io.StringIO(), io.StringIO()
            with patch("sys.argv", args), patch("amazon_jobs.fetch_session", return_value=({"token": fresh}, [])) as renew, patch("amazon_jobs.fetch_jobs", side_effect=search), contextlib.redirect_stdout(terminal_out), contextlib.redirect_stderr(terminal_error):
                self.assertEqual(app.main(), 0)
            self.assertEqual(terminal_out.getvalue(), "")
            self.assertEqual(terminal_error.getvalue(), "")
            renew.assert_called_once()
            self.assertEqual(json.loads(records_path.read_text())[0]["Counter"], 1)
            self.assertTrue(json.loads(session_path.read_text())["authorization"].endswith(fresh))

    def test_access_refusal_renews_session_and_only_counts_successful_response(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            session_path, records_path = root / "headers.json", root / "records.json"
            session_path.write_text(json.dumps(headers(time.time() + 3600)), encoding="utf-8")
            fresh = token(time.time() + 3700)
            args = ["amazon_jobs.py", "--headers-file", str(session_path), "--records", str(records_path)]
            responses = [app.PollError("refused", status_code=403), [{"jobId": "JOB-TEST"}]]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_session", return_value=({"token": fresh}, [])) as renew,
                patch("amazon_jobs.fetch_jobs", side_effect=responses) as search,
                patch("amazon_jobs.time.sleep", side_effect=[None, KeyboardInterrupt]),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            renew.assert_called_once()
            self.assertTrue(search.call_args_list[1].args[1]["authorization"].endswith(fresh))
            self.assertEqual(json.loads(records_path.read_text())[0]["Counter"], 1)


if __name__ == "__main__":
    unittest.main()
