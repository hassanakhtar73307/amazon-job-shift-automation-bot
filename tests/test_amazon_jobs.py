import contextlib
import io
import json
import multiprocessing
import subprocess
from datetime import datetime, timezone
from http.client import IncompleteRead
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import amazon_jobs as app


def job(job_id="JOB-UK-0000000660", **changes):
    card = {
        "jobId": job_id,
        "jobUuid": None,
        "jobTitle": "Delivery Station Warehouse Associate",
        "jobType": "Full-time",
        "employmentType": "Seasonal",
        "city": "Garden City",
        "state": "Wales",
        "postalCode": "CH5 2JF",
        "locationName": "Garden City Area",
        "totalPayRateMin": 15.1,
        "totalPayRateMax": 15.1,
        "distance": 39.97481,
        "geoClusterDescription": "Garden City Area",
        "language": "English",
        "scheduleCount": 8,
    }
    card.update(changes)
    return card


def response(cards):
    return {"data": {"searchJobCardsByLocation": {"nextToken": None, "jobCards": cards}}}


class RecorderTests(unittest.TestCase):
    def setUp(self):
        # Never play audio or change the real speaker volume during tests.
        alert_patch = patch("amazon_jobs.JobAlert")
        self.alert_factory = alert_patch.start()
        self.alert = self.alert_factory.return_value
        self.addCleanup(alert_patch.stop)
        browser_patch = patch("amazon_jobs.JobBrowser")
        self.browser_factory = browser_patch.start()
        self.browser = self.browser_factory.return_value
        self.addCleanup(browser_patch.stop)

    def test_commented_distance_choices_use_only_the_active_radius(self):
        payload = app.read_json(app.BASE_DIR / "payload.json")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            content = '''{
                // "distance_miles": 20,
                "distance_miles": 30,
                // "distance_miles": 40,
                // "distance_miles": 50,
                "latitude": 53.5,
                "longitude": -2.2,
                "reference": "https://example.com//keep-this"
            }'''
            path.write_text(content, encoding="utf-8-sig")
            self.assertEqual(app.search_payload(payload, path)["variables"]["searchJobRequest"]["geoQueryClause"]["distance"], 30)
            self.assertEqual(app.read_settings(path)["reference"], "https://example.com//keep-this")
            content = content.replace('"distance_miles": 30,', '// "distance_miles": 30,').replace('// "distance_miles": 40,', '"distance_miles": 40,')
            path.write_text(content, encoding="utf-8")
            self.assertEqual(app.search_payload(payload, path)["variables"]["searchJobRequest"]["geoQueryClause"]["distance"], 40)

    def test_multiple_active_distances_are_rejected(self):
        payload = app.read_json(app.BASE_DIR / "payload.json")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            path.write_text('{"distance_miles": 30, "distance_miles": 40, "latitude": 53.5, "longitude": -2.2}', encoding="utf-8")
            with self.assertRaisesRegex(app.PollError, "Keep only one line active"):
                app.search_payload(payload, path)

    def test_miles_filter_preserves_other_search_filters(self):
        payload = app.read_json(app.BASE_DIR / "payload.json")
        original = json.loads(json.dumps(payload))
        with tempfile.TemporaryDirectory() as folder:
            settings_path = Path(folder) / "settings.json"
            for radius in (30, 40, 50, 12.5):
                settings_path.write_text(json.dumps({"distance_miles": radius, "latitude": 53.5, "longitude": -2.2}), encoding="utf-8")
                result = app.search_payload(payload, settings_path)
                request = result["variables"]["searchJobRequest"]
                self.assertEqual(request["geoQueryClause"], {"lat": 53.5, "lng": -2.2, "unit": "mi", "distance": radius})
                self.assertEqual(request["sorters"], original["variables"]["searchJobRequest"]["sorters"])
                self.assertEqual(result["query"], original["query"])
            self.assertEqual(app.search_payload(payload, settings_path, 60)["variables"]["searchJobRequest"]["geoQueryClause"]["distance"], 60)
        self.assertEqual(payload, original)

    def test_invalid_radius_or_coordinates_do_not_send_a_search(self):
        payload = app.read_json(app.BASE_DIR / "payload.json")
        valid = {"distance_miles": 30, "latitude": 53.5, "longitude": -2.2}
        invalid = [
            [], {**valid, "distance_miles": 0}, {**valid, "distance_miles": -10},
            {**valid, "distance_miles": True}, {**valid, "distance_miles": "30"},
            {**valid, "distance_miles": float("nan")}, {**valid, "latitude": 91},
            {**valid, "longitude": -181}, {"distance_miles": 30},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            for settings in invalid:
                with self.subTest(settings=settings):
                    path.write_text(json.dumps(settings), encoding="utf-8")
                    with self.assertRaises(app.PollError):
                        app.search_payload(payload, path)

    def test_changing_settings_changes_the_next_poll_without_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings_path = root / "settings.json"
            settings = {"distance_miles": 30, "latitude": 53.5, "longitude": -2.2}
            settings_path.write_text(json.dumps(settings), encoding="utf-8")
            args = ["amazon_jobs.py", "--no-auto-session", "--records", str(root / "record.json"), "--settings", str(settings_path)]
            def change_radius_or_stop(delay):
                if fetch.call_count == 1:
                    settings_path.write_text(json.dumps({**settings, "distance_miles": 40}), encoding="utf-8")
                else:
                    raise KeyboardInterrupt
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=[]) as fetch,
                patch("amazon_jobs.time.sleep", side_effect=change_radius_or_stop),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            self.assertEqual([
                call.args[0]["variables"]["searchJobRequest"]["geoQueryClause"]["distance"]
                for call in fetch.call_args_list
            ], [30, 40])

    def test_headers_next_to_script_are_loaded_without_an_extra_argument(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "headers.json").write_text(json.dumps({"authorization": "test-session"}), encoding="utf-8")
            with patch("amazon_jobs.BASE_DIR", root):
                headers = app.load_headers()
            self.assertEqual(headers["authorization"], "test-session")
            self.assertEqual(headers["Content-Type"], "application/json")

    def test_default_headers_work_without_a_local_session_file(self):
        with tempfile.TemporaryDirectory() as folder, patch("amazon_jobs.BASE_DIR", Path(folder)):
            self.assertNotIn("authorization", app.load_headers())

    def test_explicit_header_file_overrides_automatic_header_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "headers.json").write_text(json.dumps({"authorization": "automatic"}), encoding="utf-8")
            explicit = root / "custom.json"
            explicit.write_text(json.dumps({"authorization": "explicit"}), encoding="utf-8")
            with patch("amazon_jobs.BASE_DIR", root):
                self.assertEqual(app.load_headers(explicit)["authorization"], "explicit")

    def test_timestamp_and_request_id_are_fresh_for_each_request(self):
        http_response = MagicMock()
        http_response.__enter__.return_value.read.return_value = json.dumps(response([])).encode()
        headers = {"x-hvh-time": "old-time", "x-amzn-requestld": "old-id", "authorization": "test-session"}
        with (
            patch("amazon_jobs.urlopen", return_value=http_response) as send,
            patch("amazon_jobs.time.time", return_value=1234.5),
        ):
            app._request_jobs({}, headers, 15)
            app._request_jobs({}, headers, 15)
        first, second = (call.args[0] for call in send.call_args_list)
        self.assertEqual(first.get_header("X-hvh-time"), "1234500")
        self.assertNotEqual(first.get_header("X-amzn-requestld"), second.get_header("X-amzn-requestld"))
        self.assertEqual(first.get_header("Authorization"), "test-session")
        self.assertEqual(headers["x-hvh-time"], "old-time")

    def test_unique_jobs_have_independent_counters_and_latest_details(self):
        records = app.merge_jobs({}, [job(), job("SECOND"), job()])
        self.assertEqual(len(records), 2)
        self.assertEqual(records["JOB-UK-0000000660"]["Counter"], 1)
        records = app.merge_jobs(records, [job(totalPayRateMin=16), job("THIRD")])
        self.assertEqual(records["JOB-UK-0000000660"]["Counter"], 2)
        self.assertEqual(records["JOB-UK-0000000660"]["totalPayRateMin"], 16)
        self.assertEqual(records["SECOND"]["Counter"], 1)
        self.assertEqual(records["THIRD"]["Counter"], 1)
        self.assertEqual(
            set(records["JOB-UK-0000000660"]),
            {"jobId", "jobUuid", "jobTitle", "jobType", "employmentType", "city",
             "state", "postalCode", "locationName", "totalPayRateMin", "totalPayRateMax",
             "distance", "geoClusterDescription", "Counter", "JobPostedTime"},
        )

    def test_sighting_times_append_once_per_job_and_do_not_mutate_previous_records(self):
        first_time = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
        second_time = datetime(2026, 10, 20, 12, 0, 3, tzinfo=timezone.utc)
        with patch("amazon_jobs.datetime", wraps=datetime) as clock:
            clock.now.side_effect = [first_time, second_time]
            original = app.merge_jobs({}, [job("FIRST"), job("SECOND"), job("FIRST")])
            updated = app.merge_jobs(original, [job("FIRST"), job("THIRD"), job("FIRST")])
        self.assertEqual(original["FIRST"]["JobPostedTime"], [first_time.isoformat()])
        self.assertEqual(original["FIRST"]["Counter"], 1)
        self.assertEqual(updated["FIRST"]["JobPostedTime"], [first_time.isoformat(), second_time.isoformat()])
        self.assertEqual(updated["FIRST"]["Counter"], 2)
        self.assertEqual(updated["SECOND"]["JobPostedTime"], [first_time.isoformat()])
        self.assertEqual(updated["THIRD"]["JobPostedTime"], [second_time.isoformat()])

    def test_legacy_records_keep_counters_without_fabricating_old_sighting_times(self):
        row = {field: job().get(field) for field in app.FIELDS}
        row["Counter"] = 100
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            path.write_text(json.dumps([row]), encoding="utf-8")
            content = path.read_bytes()
            loaded = app.load_records(path)
            self.assertEqual(loaded[row["jobId"]]["Counter"], 100)
            self.assertEqual(loaded[row["jobId"]]["JobPostedTime"], [])
            self.assertEqual(path.read_bytes(), content)
            updated = app.merge_jobs(loaded, [job()])
            app.save_records(path, updated)
            saved = app.load_records(path)[row["jobId"]]
        self.assertEqual(saved["Counter"], 101)
        self.assertEqual(len(saved["JobPostedTime"]), 1)
        self.assertEqual(datetime.fromisoformat(saved["JobPostedTime"][0]).utcoffset().total_seconds(), 0)

    def test_saved_sighting_times_preserve_their_timezone_offsets(self):
        row = app.merge_jobs({}, [job()])[job()["jobId"]]
        row["Counter"] = 2
        row["JobPostedTime"] = ["2026-10-20T13:00:00+01:00", "2026-10-20T12:00:03Z"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            path.write_text(json.dumps([row]), encoding="utf-8")
            loaded = app.load_records(path)
            updated = app.merge_jobs(loaded, [job()])
            self.assertEqual(updated[row["jobId"]]["JobPostedTime"][:2], row["JobPostedTime"])
            self.assertEqual(len(updated[row["jobId"]]["JobPostedTime"]), 3)

    def test_empty_response_keeps_historical_records(self):
        records = app.merge_jobs({}, [job()])
        self.assertEqual(app.merge_jobs(records, app.parse_response(response([]))), records)

    def test_counters_continue_after_saving_and_restarting(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            app.save_records(path, app.merge_jobs({}, [job(), job("SECOND")]))
            reloaded = app.load_records(path)
            first_history = list(reloaded[job()["jobId"]]["JobPostedTime"])
            app.save_records(path, app.merge_jobs(reloaded, [job()]))
            rows = json.loads(path.read_text(encoding="utf-8"))
            self.assertIsInstance(rows, list)
            self.assertEqual([row["Counter"] for row in rows], [2, 1])
            self.assertEqual([len(row["JobPostedTime"]) for row in rows], [2, 1])
            self.assertEqual(rows[0]["JobPostedTime"][:1], first_history)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_partial_or_malformed_responses_do_not_count(self):
        malformed = [
            None, [], {}, {"data": None},
            {"data": {"searchJobCardsByLocation": None}},
            response(None), response([job(), None]), response([job(), {}]),
            response([job(job_id=" ")]),
            {**response([job()]), "errors": [{"message": "Partial failure"}]},
        ]
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(app.PollError):
                app.parse_response(value)

    def test_bad_saved_records_are_not_overwritten(self):
        row = app.merge_jobs({}, [job()])["JOB-UK-0000000660"]
        invalid_files = ["{broken", "{}", "[null]", "[{}]", json.dumps([row, row])]
        invalid_files.extend(json.dumps([{**row, "Counter": value}]) for value in [True, -1, "3", None])
        invalid_files.extend(
            json.dumps([{**row, "JobPostedTime": value}])
            for value in [None, {}, "2026-10-20T12:00:00Z", [None], [42], ["invalid"], ["2026-10-20T12:00:00"]]
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            for content in invalid_files:
                with self.subTest(content=content):
                    path.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError):
                        app.load_records(path)
                    self.assertEqual(path.read_text(encoding="utf-8"), content)

    def test_failed_atomic_replace_preserves_original_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            path.write_text("[]\n", encoding="utf-8")
            with patch("amazon_jobs.os.replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    app.save_records(path, app.merge_jobs({}, [job()]))
            self.assertEqual(path.read_text(encoding="utf-8"), "[]\n")
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_fetch_posts_the_provided_payload(self):
        payload = app.read_json(app.BASE_DIR / "payload.json")
        http_response = MagicMock()
        http_response.__enter__.return_value.read.return_value = json.dumps(response([job()])).encode()
        with patch("amazon_jobs.urlopen", return_value=http_response) as request:
            cards = app._request_jobs(payload, {"Content-Type": "application/json"}, 15)
        sent = request.call_args.args[0]
        self.assertEqual(sent.full_url, "https://www.jobsatamazon.co.uk/graphql")
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(json.loads(sent.data), payload)
        self.assertEqual(cards[0]["jobId"], "JOB-UK-0000000660")

    def test_http_rate_limit_exposes_retry_after(self):
        error = HTTPError(app.API_URL, 429, "Too Many Requests", {"Retry-After": "120"}, io.BytesIO())
        with patch("amazon_jobs.urlopen", side_effect=error):
            with self.assertRaises(app.PollError) as caught:
                app._request_jobs({}, {}, 15)
        self.assertEqual(caught.exception.retry_after, 120)

    def test_network_and_invalid_json_fail_as_poll_errors(self):
        for error in [URLError("offline"), TimeoutError("timed out"), IncompleteRead(b"partial")]:
            with self.subTest(error=error), patch("amazon_jobs.urlopen", side_effect=error):
                with self.assertRaises(app.PollError):
                    app._request_jobs({}, {}, 15)
        http_response = MagicMock()
        http_response.__enter__.return_value.read.return_value = b"<html>Unavailable</html>"
        with patch("amazon_jobs.urlopen", return_value=http_response):
            with self.assertRaises(app.PollError):
                app._request_jobs({}, {}, 15)

    def test_access_errors_explain_how_to_load_request_headers(self):
        error = HTTPError(app.API_URL, 403, "Forbidden", {}, io.BytesIO())
        with patch("amazon_jobs.urlopen", side_effect=error):
            result = app._request_result({}, {}, 15)
        self.assertIn("HTTP 403", result["error"])
        self.assertIn("--headers-file headers.json", result["error"])

    def test_worker_result_transmits_jobs_and_rate_limit(self):
        with patch("amazon_jobs._request_jobs", return_value=[job()]):
            self.assertEqual(app._request_result({}, {}, 15), {"cards": [job()]})
        with patch("amazon_jobs._request_jobs", side_effect=app.PollError("limited", 120)):
            self.assertEqual(app._request_result({}, {}, 15), {"error": "limited", "retry_after": 120})

    def test_overall_timeout_stops_an_unresponsive_worker(self):
        real_context = multiprocessing.get_context("spawn")
        context = MagicMock()
        # Keep the actual child busy before it can make any network request.
        context.Pool.side_effect = lambda **kwargs: real_context.Pool(
            **kwargs, initializer=time.sleep, initargs=(30,)
        )
        children_before = {child.pid for child in multiprocessing.active_children()}
        started = time.monotonic()
        with patch("amazon_jobs.multiprocessing.get_context", return_value=context):
            with self.assertRaisesRegex(app.PollError, "timed out after 0.2 seconds"):
                app.fetch_jobs({}, {}, 0.2, "urllib")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual({child.pid for child in multiprocessing.active_children()}, children_before)

    def test_bounded_request_returns_results_and_preserves_retry_after(self):
        context = MagicMock()
        pool = context.Pool.return_value.__enter__.return_value
        pending = pool.apply_async.return_value
        with patch("amazon_jobs.multiprocessing.get_context", return_value=context):
            pending.get.return_value = {"cards": [job()]}
            self.assertEqual(app.fetch_jobs({}, {}, 15, "urllib"), [job()])
            pending.get.return_value = {"error": "limited", "retry_after": 120}
            with self.assertRaises(app.PollError) as caught:
                app.fetch_jobs({}, {}, 15, "urllib")
            self.assertEqual(caught.exception.retry_after, 120)

    def test_curl_transmits_credentials_via_stdin_and_parses_jobs(self):
        def execute(command, **kwargs):
            self.assertNotIn("test-secret", " ".join(command))
            self.assertIn("authorization: test-secret", kwargs["input"])
            self.assertEqual(kwargs["input"].count("user-agent:"), 1)
            payload_path = command[command.index("--data-binary") + 1][1:]
            self.assertEqual(app.read_json(Path(payload_path)), {"query": "test"})
            Path(command[command.index("--dump-header") + 1]).write_text("HTTP/1.1 200 OK\r\n\r\n", encoding="utf-8")
            Path(command[command.index("--output") + 1]).write_text(json.dumps(response([job()])), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "200", "")
        headers = {"authorization": "test-secret", "User-Agent": "default", "user-agent": "browser"}
        with patch("amazon_jobs.subprocess.run", side_effect=execute):
            self.assertEqual(app._curl_request_jobs({"query": "test"}, headers, 15, "curl"), [job()])

    def test_curl_respects_retry_after_from_the_final_response(self):
        def execute(command, **kwargs):
            Path(command[command.index("--dump-header") + 1]).write_text(
                "HTTP/1.1 200 Connection established\r\nRetry-After: 1\r\n\r\nHTTP/2 429\r\nRetry-After: 120\r\n\r\n", encoding="utf-8"
            )
            return subprocess.CompletedProcess(command, 0, "429", "")
        with patch("amazon_jobs.subprocess.run", side_effect=execute):
            with self.assertRaises(app.PollError) as caught:
                app._curl_request_jobs({}, {}, 15, "curl")
        self.assertEqual(caught.exception.retry_after, 120)

    def test_curl_timeout_becomes_a_poll_error(self):
        with patch("amazon_jobs.subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 3)):
            with self.assertRaisesRegex(app.PollError, "timed out after 3 seconds"):
                app._curl_request_jobs({}, {}, 3, "curl")

    def test_available_curl_is_selected_automatically(self):
        with (
            patch("amazon_jobs.shutil.which", return_value="curl.exe"),
            patch("amazon_jobs._curl_request_jobs", return_value=[job()]) as request,
        ):
            self.assertEqual(app.fetch_jobs({}, {}, 15), [job()])
            request.assert_called_once_with({}, {}, 15, "curl.exe")

    def test_once_mode_persists_counts_and_failure_preserves_them(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            args = ["amazon_jobs.py", "--no-auto-session", "--once", "--records", str(path)]
            with patch("sys.argv", args), patch("amazon_jobs.fetch_jobs", return_value=[job(), job("SECOND")]):
                self.assertEqual(app.main(), 0)
                self.assertEqual(app.main(), 0)
            content = path.read_bytes()
            self.assertEqual([row["Counter"] for row in json.loads(content)], [2, 2])
            with patch("sys.argv", args), patch("amazon_jobs.fetch_jobs", side_effect=app.PollError("offline")):
                self.assertEqual(app.main(), 1)
            self.assertEqual(path.read_bytes(), content)

    def test_default_poll_cadence_accounts_for_request_duration(self):
        with tempfile.TemporaryDirectory() as folder:
            args = ["amazon_jobs.py", "--no-auto-session", "--records", str(Path(folder) / "record.json")]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=[]),
                patch("amazon_jobs.time.monotonic", side_effect=[100, 100.5]),
                patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt) as sleep,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            sleep.assert_called_once_with(2.5)

    def test_error_backoff_respects_server_and_recovers_to_three_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            args = ["amazon_jobs.py", "--no-auto-session", "--records", str(Path(folder) / "record.json")]
            results = [app.PollError("limited", 120), app.PollError("offline"), []]
            terminal = io.StringIO()
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", side_effect=results),
                patch("amazon_jobs.time.monotonic", return_value=0),
                patch("amazon_jobs.time.sleep", side_effect=[None, None, KeyboardInterrupt]) as sleep,
                contextlib.redirect_stderr(terminal),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [120, 6, 3])
            self.assertIn("ERROR: limited", terminal.getvalue())
            self.assertIn("ERROR: offline", terminal.getvalue())
            self.assertNotIn("INFO:", terminal.getvalue())

    def test_verbose_option_shows_successful_poll_for_troubleshooting(self):
        with tempfile.TemporaryDirectory() as folder:
            args = ["amazon_jobs.py", "--no-auto-session", "--once", "--verbose", "--records", str(Path(folder) / "record.json")]
            terminal = io.StringIO()
            with patch("sys.argv", args), patch("amazon_jobs.fetch_jobs", return_value=[job()]), contextlib.redirect_stderr(terminal):
                self.assertEqual(app.main(), 0)
            self.assertIn("INFO: Saw 1 unique jobs", terminal.getvalue())

    def test_invalid_intervals_are_rejected(self):
        for value in ["0", "-3", "nan", "inf"]:
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with patch("sys.argv", ["amazon_jobs.py", "--interval", value]):
                    with self.assertRaises(SystemExit) as caught:
                        app.main()
                self.assertEqual(caught.exception.code, 2)

    def test_default_alarm_runs_with_browser_and_stops_further_fetches_and_record_updates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            # A matching historical job still launches the restored automation.
            app.save_records(path, app.merge_jobs({}, [job()]))
            args = ["amazon_jobs.py", "--records", str(path)]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.load_headers", return_value={}),
                patch("amazon_jobs.SessionManager") as session,
                patch("amazon_jobs.fetch_jobs", side_effect=[[], [job()], AssertionError("Fetching continued after browser opened")]) as fetch,
                patch("amazon_jobs.time.monotonic", return_value=0),
                patch("amazon_jobs.time.sleep", side_effect=[None, None, KeyboardInterrupt]) as sleep,
                contextlib.redirect_stdout(io.StringIO()) as output,
                contextlib.redirect_stderr(io.StringIO()),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            row = app.load_records(path)[job()["jobId"]]
            self.assertEqual(row["Counter"], 2)
            self.assertEqual(len(row["JobPostedTime"]), 2)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(session.return_value.ensure.call_count, 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 1, 1])
            self.alert_factory.assert_called_once()
            self.alert.start.assert_called_once()
            self.alert.close.assert_called_once()
            self.browser_factory.assert_called_once_with(app.BASE_DIR / "browser_settings.json", None)
            self.browser.open_first.assert_called_once_with([job()])
            self.assertEqual(self.browser.poll_automation.call_count, 3)
            self.browser.close.assert_called_once()
            self.assertIn("Job fetching stopped for this run", output.getvalue())
            self.assertIn("Alarm will stop automatically after 5 seconds", output.getvalue())

    def test_restarting_searches_again_and_updates_the_existing_job_once_per_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            args = ["amazon_jobs.py", "--no-auto-session", "--records", str(path)]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=[job()]) as fetch,
                patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                for count in (1, 2):
                    with self.assertRaises(KeyboardInterrupt):
                        app.main()
                    row = app.load_records(path)[job()["jobId"]]
                    self.assertEqual(row["Counter"], count)
                    self.assertEqual(len(row["JobPostedTime"]), count)
                    self.assertEqual(fetch.call_count, count)
            self.assertEqual(self.browser_factory.call_count, 2)
            self.assertEqual(self.browser.open_first.call_count, 2)
            self.assertEqual(self.browser.close.call_count, 2)
            self.assertEqual(self.alert.start.call_count, 2)
            self.assertEqual(self.alert.close.call_count, 2)

    def test_jobs_outside_trigger_rules_are_recorded_without_alarm_or_automation(self):
        cards = [
            job("REDUCED", jobType="Reduced", city="Rochdale Area"),
            job("PART_TIME", jobType="Part-time", employmentType="Full-time", city="Near Bolton"),
            job("NULL", jobType=None, city=None),
            job("WRONG_CASE", jobType="full-time"),
            job("MISSING"),
        ]
        del cards[-1]["jobType"]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            app.save_records(path, app.merge_jobs({}, [job("HISTORICAL_FULL_TIME")]))
            with (
                patch("sys.argv", ["amazon_jobs.py", "--no-auto-session", "--records", str(path)]),
                patch("amazon_jobs.fetch_jobs", return_value=cards) as fetch,
                patch("amazon_jobs.time.sleep", side_effect=[None, KeyboardInterrupt]),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            records = app.load_records(path)
            self.assertEqual(fetch.call_count, 2)
            for card in cards:
                self.assertEqual(records[card["jobId"]]["Counter"], 2)
            self.assertEqual(records["HISTORICAL_FULL_TIME"]["Counter"], 1)
            self.alert.start.assert_not_called()
            self.browser.open_first.assert_not_called()
            self.assertNotIn("JOBS FOUND", output.getvalue())

    def test_waits_for_full_time_then_alarms_and_opens_first_eligible_job(self):
        reduced = job("REDUCED", jobType="Reduced")
        first = job("FIRST_FULL_TIME", employmentType="Seasonal")
        second = job("SECOND_FULL_TIME")
        cards = [reduced, first, job("PART_TIME", jobType="Part-time"), second]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"

            def alert_after_save():
                self.assertEqual(set(app.load_records(path)), {card["jobId"] for card in cards})
                return True

            self.alert.start.side_effect = alert_after_save
            with (
                patch("sys.argv", ["amazon_jobs.py", "--no-auto-session", "--records", str(path)]),
                patch("amazon_jobs.fetch_jobs", side_effect=[[reduced], cards, AssertionError("Fetching continued after launch")]) as fetch,
                patch("amazon_jobs.time.monotonic", return_value=0),
                patch("amazon_jobs.time.sleep", side_effect=[None, None, KeyboardInterrupt]) as sleep,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [3, 1, 1])
            self.alert.start.assert_called_once()
            self.browser.open_first.assert_called_once_with([first, second])
            self.assertEqual(app.load_records(path)["REDUCED"]["Counter"], 2)
            self.assertIn("2 eligible jobs", output.getvalue())

    def test_rochdale_and_bolton_trigger_without_full_time_job_type(self):
        for city in ("Rochdale", "Bolton", " rochdale ", "BOLTON"):
            with self.subTest(city=city), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "record.json"
                other = job("OTHER", jobType="Reduced")
                eligible = job("CITY_MATCH", city=city, jobType="Part-time")
                self.alert.reset_mock()
                self.browser.reset_mock()
                with (
                    patch("sys.argv", ["amazon_jobs.py", "--no-auto-session", "--records", str(path)]),
                    patch("amazon_jobs.fetch_jobs", return_value=[other, eligible]),
                    patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        app.main()
                self.assertEqual(set(app.load_records(path)), {"OTHER", "CITY_MATCH"})
                self.alert.start.assert_called_once()
                self.browser.open_first.assert_called_once_with([eligible])

    def test_fetching_stops_only_after_the_browser_opens_successfully(self):
        self.browser.open_first.side_effect = [False, True]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            args = ["amazon_jobs.py", "--no-auto-session", "--no-alert", "--records", str(path)]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", side_effect=[[job()], [job()], AssertionError("Unexpected third request")]) as fetch,
                patch("amazon_jobs.time.sleep", side_effect=[None, None, KeyboardInterrupt]),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.browser.open_first.call_count, 2)
            self.assertEqual(app.load_records(path)[job()["jobId"]]["Counter"], 2)
            self.alert.start.assert_not_called()

    def test_no_browser_mode_keeps_polling_and_recording_sightings(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            args = ["amazon_jobs.py", "--no-auto-session", "--no-browser", "--no-alert", "--records", str(path)]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=[job()]) as fetch,
                patch("amazon_jobs.time.sleep", side_effect=[None, KeyboardInterrupt]),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            row = app.load_records(path)[job()["jobId"]]
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(row["Counter"], 2)
            self.assertEqual(len(row["JobPostedTime"]), 2)
            self.browser.open_first.assert_not_called()

    def test_historical_jobs_do_not_alert_when_current_results_are_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            app.save_records(path, app.merge_jobs({}, [job()]))
            args = ["amazon_jobs.py", "--no-auto-session", "--alert", "--records", str(path)]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=[]),
                patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            self.alert.start.assert_not_called()
            self.alert.close.assert_called_once()

    def test_failed_save_does_not_start_alert(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            app.save_records(path, {})
            args = ["amazon_jobs.py", "--no-auto-session", "--alert", "--records", str(path)]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=[job()]),
                patch("amazon_jobs.save_records", side_effect=PermissionError("locked")),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(app.main(), 1)
            self.assertEqual(app.load_records(path), {})
            self.alert.start.assert_not_called()
            self.alert.close.assert_called_once()

    def test_once_and_no_alert_modes_record_jobs_without_audio(self):
        for options in (["--once"], ["--once", "--alert"], ["--no-alert"]):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "record.json"
                args = ["amazon_jobs.py", "--no-auto-session", *options, "--records", str(path)]
                self.alert.reset_mock()
                with (
                    patch("sys.argv", args),
                    patch("amazon_jobs.fetch_jobs", return_value=[job()]),
                    patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
                ):
                    if "--once" in options:
                        self.assertEqual(app.main(), 0)
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            app.main()
                self.assertEqual(app.load_records(path)[job()["jobId"]]["Counter"], 1)
                self.alert.start.assert_not_called()
                self.alert.close.assert_not_called()

    def test_test_alert_does_not_read_credentials_or_contact_amazon(self):
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-alert"]),
            patch("amazon_jobs.read_json") as read,
            patch("amazon_jobs.fetch_jobs") as fetch,
            patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                app.main()
        read.assert_not_called()
        fetch.assert_not_called()
        self.alert.start.assert_called_once()
        self.alert.close.assert_called_once()

    def test_test_alert_reports_playback_failure_and_cleans_up(self):
        self.alert.start.return_value = False
        with patch("sys.argv", ["amazon_jobs.py", "--test-alert"]):
            self.assertEqual(app.main(), 1)
        self.alert.close.assert_called_once()

    def test_test_alert_finishes_after_five_seconds_without_contacting_amazon(self):
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-alert"]),
            patch("amazon_jobs.fetch_jobs") as fetch,
            patch("amazon_jobs.time.sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(app.main(), 0)
        sleep.assert_called_once_with(5)
        fetch.assert_not_called()
        self.alert.close.assert_called_once()

    def test_browser_uses_current_api_order_after_successful_save(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "record.json"
            # Historical JSON order must not determine which current job opens.
            app.save_records(path, app.merge_jobs({}, [job("HISTORICAL")]))
            args = ["amazon_jobs.py", "--no-auto-session", "--records", str(path)]
            cards = [job("FIRST"), job("SECOND")]

            def open_after_save(current_cards):
                self.assertEqual(current_cards, cards)
                self.assertEqual(app.load_records(path)["FIRST"]["Counter"], 1)
                return True

            self.browser.open_first.side_effect = open_after_save
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=cards),
                patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            rows = app.load_records(path)
            self.assertEqual(set(rows), {"HISTORICAL", "FIRST", "SECOND"})
            self.assertEqual(rows["FIRST"]["Counter"], 1)
            self.assertEqual(rows["SECOND"]["Counter"], 1)
            self.browser.open_first.assert_called_once_with(cards)

    def test_browser_does_not_open_for_empty_results_failed_request_or_failed_save(self):
        for result in ([], app.PollError("offline"), [job()]):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "record.json"
                app.save_records(path, app.merge_jobs({}, [job("HISTORICAL")]))
                args = ["amazon_jobs.py", "--no-auto-session", "--records", str(path)]
                self.browser.reset_mock()
                with (
                    patch("sys.argv", args),
                    patch("amazon_jobs.fetch_jobs", side_effect=[result]),
                    patch("amazon_jobs.save_records", side_effect=PermissionError("locked") if result else None),
                    patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    if isinstance(result, list) and result:
                        self.assertEqual(app.main(), 1)
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            app.main()
                self.browser.open_first.assert_not_called()

    def test_no_browser_and_once_modes_keep_recording_without_opening_chrome(self):
        for option in ("--no-browser", "--once"):
            with self.subTest(option=option), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "record.json"
                args = ["amazon_jobs.py", "--no-auto-session", option, "--records", str(path)]
                self.browser.reset_mock()
                with (
                    patch("sys.argv", args),
                    patch("amazon_jobs.fetch_jobs", return_value=[job()]),
                    patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
                ):
                    if option == "--once":
                        self.assertEqual(app.main(), 0)
                    else:
                        with self.assertRaises(KeyboardInterrupt):
                            app.main()
                self.browser.open_first.assert_not_called()
                self.assertEqual(app.load_records(path)[job()["jobId"]]["Counter"], 1)

    def test_test_browser_opens_first_current_result_without_recording_or_audio(self):
        eligible_cards = [
            job("FIRST", city="Rochdale", jobType="Reduced"),
            job("SECOND", city="Bolton", jobType="Part-time"),
            job("THIRD_FULL_TIME"),
        ]
        cards = [job("REDUCED", jobType="Reduced"), *eligible_cards]
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-browser", "--no-auto-session", "--distance", "50"]),
            patch("amazon_jobs.load_headers", return_value={}),
            patch("amazon_jobs.fetch_jobs", return_value=cards) as fetch,
            patch("amazon_jobs.save_records") as save,
            patch("amazon_jobs.load_records") as load,
            patch("amazon_jobs.time.sleep") as sleep,
        ):
            self.assertEqual(app.main(), 0)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0]["variables"]["searchJobRequest"]["geoQueryClause"]["distance"], 50)
        self.browser.open_first.assert_called_once_with(eligible_cards)
        self.browser.open_search.assert_not_called()
        self.browser.wait_for_automation.assert_called_once()
        self.browser.close.assert_called_once()
        save.assert_not_called()
        load.assert_not_called()
        sleep.assert_not_called()
        self.alert.start.assert_not_called()

    def test_test_browser_reports_empty_results_without_opening_history(self):
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-browser", "--no-auto-session"]),
            patch("amazon_jobs.load_headers", return_value={}),
            patch("amazon_jobs.fetch_jobs", return_value=[]),
            patch("amazon_jobs.save_records") as save,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(app.main(), 0)
        self.assertIn("No jobs currently match", output.getvalue())
        self.browser.open_first.assert_not_called()
        self.browser.open_search.assert_not_called()
        save.assert_not_called()

    def test_test_browser_does_not_open_non_full_time_results(self):
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-browser", "--no-auto-session"]),
            patch("amazon_jobs.load_headers", return_value={}),
            patch("amazon_jobs.fetch_jobs", return_value=[job(jobType="Reduced")]),
            patch("amazon_jobs.save_records") as save,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(app.main(), 0)
        self.assertIn("jobType Full-time", output.getvalue())
        self.browser.open_first.assert_not_called()
        self.alert.start.assert_not_called()
        save.assert_not_called()

    def test_test_browser_renews_session_before_searching(self):
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-browser"]),
            patch("amazon_jobs.load_headers", return_value={}),
            patch("amazon_jobs.SessionManager") as manager,
            patch("amazon_jobs.fetch_jobs", return_value=[job()]) as fetch,
        ):
            manager.return_value.ensure.return_value = {"authorization": "test-session"}
            self.assertEqual(app.main(), 0)
        manager.return_value.ensure.assert_called_once_with(15)
        self.assertEqual(fetch.call_args.args[1], {"authorization": "test-session"})

    def test_test_browser_request_failure_does_not_open_or_save(self):
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-browser", "--no-auto-session"]),
            patch("amazon_jobs.load_headers", return_value={}),
            patch("amazon_jobs.fetch_jobs", side_effect=app.PollError("offline")),
            patch("amazon_jobs.save_records") as save,
            contextlib.redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(app.main(), 1)
        self.assertIn("Cannot open the first matching job: offline", output.getvalue())
        self.browser.open_first.assert_not_called()
        save.assert_not_called()

    def test_test_browser_reports_launch_failure(self):
        self.browser.open_first.return_value = False
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-browser", "--no-auto-session"]),
            patch("amazon_jobs.load_headers", return_value={}),
            patch("amazon_jobs.fetch_jobs", return_value=[job()]),
        ):
            self.assertEqual(app.main(), 1)
        self.browser.wait_for_automation.assert_not_called()
        self.browser.close.assert_called_once()

    def test_browser_worker_is_checked_and_stopped_during_continuous_polling(self):
        with tempfile.TemporaryDirectory() as folder:
            args = ["amazon_jobs.py", "--no-auto-session", "--records", str(Path(folder) / "record.json")]
            with (
                patch("sys.argv", args),
                patch("amazon_jobs.fetch_jobs", return_value=[job()]),
                patch("amazon_jobs.time.sleep", side_effect=KeyboardInterrupt),
            ):
                with self.assertRaises(KeyboardInterrupt):
                    app.main()
            self.browser_factory.assert_called_once()
            self.browser.open_first.assert_called_once()
            self.browser.poll_automation.assert_called_once()
            self.browser.close.assert_called_once()

    def test_test_browser_reports_workflow_failure_after_successful_open(self):
        self.browser.wait_for_automation.return_value = False
        with (
            patch("sys.argv", ["amazon_jobs.py", "--test-browser", "--no-auto-session"]),
            patch("amazon_jobs.load_headers", return_value={}),
            patch("amazon_jobs.fetch_jobs", return_value=[job()]),
        ):
            self.assertEqual(app.main(), 1)
        self.browser.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
