"""Poll Amazon's job search and record a counter and sighting times for each job ID."""

from __future__ import annotations

import argparse
import json
import logging
import math
import multiprocessing
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4
from job_alert import JobAlert
from job_browser import JobBrowser
from session_manager import SessionError, SessionManager


BASE_DIR = Path(__file__).resolve().parent
API_URL = "https://www.jobsatamazon.co.uk/graphql"
SESSION_URL = "https://www.jobsatamazon.co.uk/authorize/api/csrf?countryCode=UK"
FIELDS = (
    "jobId",
    "jobUuid",
    "jobTitle",
    "jobType",
    "employmentType",
    "city",
    "state",
    "postalCode",
    "locationName",
    "totalPayRateMin",
    "totalPayRateMax",
    "distance",
    "geoClusterDescription",
)


class PollError(Exception):
    """A failed request that must not change the saved counters."""

    def __init__(self, message: str, retry_after: float = 0, status_code: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after
        self.status_code = status_code


def read_json(path: Path):
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_settings(path: Path):
    """Allow full-line // comments for inactive distance choices."""
    def unique_settings(pairs):
        settings = {}
        for key, value in pairs:
            if key in settings:
                raise ValueError(f"Duplicate setting '{key}'. Keep only one line active for each setting; comment out the others with //.")
            settings[key] = value
        return settings

    content = path.read_text(encoding="utf-8-sig")
    content = "\n".join(line for line in content.splitlines() if not line.lstrip().startswith("//"))
    return json.loads(content, object_pairs_hook=unique_settings)


def search_payload(payload: dict, settings_path: Path, distance_override: float | None = None) -> dict:
    """Read the current radius and add Amazon's geographic search clause."""
    try:
        settings = read_settings(settings_path)
    except (OSError, ValueError) as error:
        raise PollError(f"Cannot read {settings_path}: {error}") from error
    if not isinstance(settings, dict):
        raise PollError("settings.json must contain a JSON object.")
    distance = distance_override if distance_override is not None else settings.get("distance_miles")
    latitude = settings.get("latitude")
    longitude = settings.get("longitude")
    for name, value in (("distance_miles", distance), ("latitude", latitude), ("longitude", longitude)):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise PollError(f"{name} must be a finite number in settings.json.")
    if distance <= 0:
        raise PollError("distance_miles must be greater than zero in settings.json.")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise PollError("latitude must be between -90 and 90; longitude must be between -180 and 180.")
    variables = payload.get("variables")
    request = variables.get("searchJobRequest") if isinstance(variables, dict) else None
    if not isinstance(request, dict):
        raise PollError("The payload must contain variables.searchJobRequest.")
    geo = {"lat": latitude, "lng": longitude, "unit": "mi", "distance": distance}
    return {
        **payload,
        "variables": {**variables, "searchJobRequest": {**request, "geoQueryClause": geo}},
    }


def load_headers(path: Path | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "AmazonJobRecorder/1.0",
    }
    if path is None:
        default_path = BASE_DIR / "headers.json"
        if default_path.exists():
            path = default_path
    if path is not None:
        extra_headers = read_json(path)
        if not isinstance(extra_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in extra_headers.items()
        ):
            raise ValueError("The headers file must be a JSON object with string values.")
        headers.update(extra_headers)
        logging.info("Loaded request headers from %s.", path.resolve())
    return headers


def load_records(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = read_json(path)
    if not isinstance(rows, list):
        raise ValueError(f"{path} must contain a JSON array; file left untouched.")
    records = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError(f"Invalid record in {path}; file left untouched.")
        job_id = row.get("jobId")
        counter = row.get("Counter")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError(f"Missing jobId in {path}; file left untouched.")
        if type(counter) is not int or counter < 0:
            raise ValueError(f"Invalid Counter for {job_id}; file left untouched.")
        if job_id in records:
            raise ValueError(f"Duplicate jobId {job_id} in {path}; file left untouched.")
        posted_times = row.get("JobPostedTime", [])
        try:
            if not isinstance(posted_times, list):
                raise ValueError("must be an array")
            for posted_time in posted_times:
                if (not isinstance(posted_time, str)
                        or datetime.fromisoformat(posted_time.replace("Z", "+00:00")).utcoffset() is None):
                    raise ValueError("must contain ISO 8601 timestamps with timezone offsets")
        except ValueError as error:
            raise ValueError(f"Invalid JobPostedTime for {job_id} in {path}; file left untouched.") from error
        records[job_id] = {field: row.get(field) for field in FIELDS}
        records[job_id]["Counter"] = counter
        records[job_id]["JobPostedTime"] = list(posted_times)
    return records


def save_records(path: Path, records: dict[str, dict]) -> None:
    """Replace the JSON atomically so an interrupted write keeps the old file."""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(list(records.values()), handle, ensure_ascii=False, indent=4)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def parse_response(response: object) -> list[dict]:
    if not isinstance(response, dict):
        raise PollError("The API response is not a JSON object.")
    if response.get("errors"):
        raise PollError(f"GraphQL error: {json.dumps(response['errors'], ensure_ascii=False)}")
    data = response.get("data")
    result = data.get("searchJobCardsByLocation") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("jobCards"), list):
        raise PollError("The API response is missing data.searchJobCardsByLocation.jobCards.")
    cards = result["jobCards"]
    for card in cards:
        if not isinstance(card, dict):
            raise PollError("The API returned an invalid job card.")
        job_id = card.get("jobId")
        if not isinstance(job_id, str) or not job_id.strip():
            raise PollError("The API returned a job card without a valid jobId.")
    if result.get("nextToken"):
        logging.warning("More results are available; this request records the returned page only.")
    return cards


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            return max(0, (deadline - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0


def prepare_headers(headers: dict[str, str]) -> dict[str, str]:
    request_headers = {key.lower(): value for key, value in headers.items()}
    for key in request_headers:
        if any(char in key or char in request_headers[key] for char in ("\r", "\n")):
            raise PollError("Request headers cannot contain newline characters.")
        if key.lower() == "x-hvh-time":
            request_headers[key] = str(int(time.time() * 1000))
        elif key.lower() in ("x-amzn-requestid", "x-amzn-requestld"):
            request_headers[key] = str(uuid4())
    return request_headers


def http_error_message(status: int) -> str:
    message = f"HTTP {status} from Amazon's API."
    if status in (401, 403):
        message += " Access was refused. Automatic session renewal will be attempted when enabled. If refusals persist, load fresh browser headers with --headers-file headers.json."
    return message


def _request_jobs(payload: dict, headers: dict[str, str], timeout: float) -> list[dict]:
    request = Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=prepare_headers(headers),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read()
    except HTTPError as error:
        retry_after = retry_after_seconds(error.headers.get("Retry-After"))
        status = error.code
        error.close()
        raise PollError(http_error_message(status), retry_after, status) from error
    except (URLError, TimeoutError, OSError, HTTPException) as error:
        raise PollError(f"Request failed: {error}") from error
    try:
        decoded = json.loads(body)
    except (ValueError, UnicodeError) as error:
        raise PollError("The API returned an invalid JSON response.") from error
    return parse_response(decoded)


def _curl_json_request(url: str, headers: dict[str, str], timeout: float, executable: str, payload: dict | None = None) -> tuple[object, list[tuple[str, str]]]:
    # Credentials go through stdin, rather than command-line arguments.
    header_input = "\n".join(f"{key}: {value}" for key, value in prepare_headers(headers).items()) + "\n"
    with tempfile.TemporaryDirectory(prefix="amazon-jobs-") as folder:
        root = Path(folder)
        payload_path = root / "payload.json"
        body_path = root / "response.json"
        headers_path = root / "response-headers.txt"
        if payload is not None:
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
        command = [
            executable, "--disable", "--silent", "--show-error", "--compressed",
            "--max-time", str(timeout), "--request", "GET" if payload is None else "POST",
            "--header", "@-",
        ]
        if payload is not None:
            command += ["--data-binary", f"@{payload_path}"]
        command += [
            "--dump-header", str(headers_path), "--output", str(body_path),
            "--write-out", "%{http_code}", url,
        ]
        try:
            completed = subprocess.run(
                command, input=header_input, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as error:
            raise PollError(f"Request timed out after {timeout:g} seconds; no complete response received.") from error
        except OSError as error:
            raise PollError(f"Could not start curl: {error}") from error
        if completed.returncode:
            raise PollError(f"Request failed: {completed.stderr.strip() or 'curl could not complete the request.'}")
        response_headers = []
        for line in headers_path.read_text(encoding="iso-8859-1").splitlines():
            if line.startswith("HTTP/"):
                response_headers = []
            elif ":" in line:
                key, value = line.split(":", 1)
                response_headers.append((key.lower(), value.strip()))
        try:
            status = int(completed.stdout.strip())
        except ValueError as error:
            raise PollError("curl did not return a valid HTTP status.") from error
        if not 200 <= status < 300:
            raise PollError(http_error_message(status), retry_after_seconds(dict(response_headers).get("retry-after")), status)
        try:
            decoded = read_json(body_path)
        except (ValueError, UnicodeError) as error:
            raise PollError("The API returned an invalid JSON response.") from error
        return decoded, response_headers


def _curl_request_jobs(payload: dict, headers: dict[str, str], timeout: float, executable: str) -> list[dict]:
    body, _ = _curl_json_request(API_URL, headers, timeout, executable, payload)
    return parse_response(body)


def fetch_session(headers: dict[str, str], timeout: float) -> tuple[object, list[tuple[str, str]]]:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if curl is None:
        raise PollError("Automatic session renewal requires curl. Install curl, or use --no-auto-session with browser session headers.")
    request_headers = {key.lower(): value for key, value in headers.items()}
    request_headers.pop("authorization", None)
    request_headers.pop("content-length", None)
    request_headers.update({"accept": "application/json", "content-type": "application/json", "access-control-allow-origin": "*"})
    return _curl_json_request(SESSION_URL, request_headers, timeout, curl)


def _request_result(payload: dict, headers: dict[str, str], timeout: float, log_level: int = logging.ERROR) -> dict:
    # Return errors as data so Retry-After survives the process boundary.
    logging.getLogger().setLevel(log_level)
    try:
        return {"cards": _request_jobs(payload, headers, timeout)}
    except PollError as error:
        result = {"error": str(error), "retry_after": error.retry_after}
        if error.status_code is not None:
            result["status_code"] = error.status_code
        return result


def fetch_jobs(payload: dict, headers: dict[str, str], timeout: float, transport: str = "auto") -> list[dict]:
    """Bound the entire request, including DNS and connection attempts."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if transport == "curl" or (transport == "auto" and curl):
        if curl is None:
            raise PollError("curl is not installed. Use --transport urllib or install curl.")
        return _curl_request_jobs(payload, headers, timeout, curl)
    # A disposable worker can be stopped even if DNS or a socket hangs. It never
    # writes records. The pool is closed before another request can start.
    with multiprocessing.get_context("spawn").Pool(processes=1) as pool:
        pending = pool.apply_async(_request_result, (payload, headers, timeout, logging.getLogger().getEffectiveLevel()))
        try:
            result = pending.get(timeout=timeout)
        except multiprocessing.TimeoutError as error:
            raise PollError(f"Request timed out after {timeout:g} seconds; no complete response received.") from error
    if "error" in result:
        raise PollError(result["error"], result["retry_after"], result.get("status_code"))
    return result["cards"]


def merge_jobs(records: dict[str, dict], cards: list[dict]) -> dict[str, dict]:
    """Count and timestamp each distinct job ID once per successful response."""
    updated = dict(records)
    seen = set()
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for card in cards:
        job_id = card["jobId"]
        if job_id in seen:
            continue
        seen.add(job_id)
        row = {field: card.get(field) for field in FIELDS}
        previous = records.get(job_id, {})
        row["Counter"] = previous.get("Counter", 0) + 1
        # Copy the history so a failed save cannot mutate the previous records.
        row["JobPostedTime"] = [*previous.get("JobPostedTime", []), observed_at]
        updated[job_id] = row
    return updated


def eligible_jobs(cards: list[dict]) -> list[dict]:
    """Keep Full-time jobs or Rochdale/Bolton jobs in geographic response order."""
    return [
        card for card in cards
        if card.get("jobType") == "Full-time"
        or (isinstance(card.get("city"), str)
            and card["city"].strip().casefold() in ("rochdale", "bolton"))
    ]


def positive_number(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a positive, finite number")
    return number


def open_first_matching_job(args) -> int:
    """Test navigation using a fresh filtered response without changing records."""
    try:
        payload = read_json(args.payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
            raise ValueError("The payload must be a JSON object containing a GraphQL query.")
        current_payload = search_payload(payload, args.settings, args.distance)
        headers = load_headers(args.headers_file)
        if not args.no_auto_session:
            session = SessionManager(args.headers_file or BASE_DIR / "headers.json", headers, fetch_session)
            headers = session.ensure(args.timeout)
        cards = eligible_jobs(fetch_jobs(current_payload, headers, args.timeout, args.transport))
        if not cards:
            print("No jobs currently match your search settings with jobType Full-time or city Rochdale/Bolton. No job page was opened.", flush=True)
            return 0
        browser = JobBrowser(args.browser_settings, args.chrome_profile)
        try:
            if not browser.open_first(cards):
                return 1
            return 0 if browser.wait_for_automation() else 1
        finally:
            browser.close()
    except (OSError, ValueError, PollError, SessionError) as error:
        logging.error("Cannot open the first matching job: %s", error)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=positive_number, default=3, help="seconds between polls (default: 3)")
    parser.add_argument("--timeout", type=positive_number, default=15, help="whole-request timeout in seconds (default: 15)")
    parser.add_argument("--records", type=Path, default=BASE_DIR / "record.json", help="output JSON file")
    parser.add_argument("--payload", type=Path, default=BASE_DIR / "payload.json", help="GraphQL request JSON file")
    parser.add_argument("--settings", type=Path, default=BASE_DIR / "settings.json", help="distance and coordinates JSON file (reloaded before each poll)")
    parser.add_argument("--distance", type=positive_number, help="override the radius in miles, e.g. --distance 30")
    parser.add_argument("--headers-file", type=Path, help="request headers JSON (automatically loads headers.json next to the script if present)")
    parser.add_argument("--transport", choices=("auto", "curl", "urllib"), default="auto", help="HTTP client (default: curl when available, otherwise urllib)")
    parser.add_argument("--no-auto-session", action="store_true", help="disable automatic anonymous session renewal")
    sound_options = parser.add_mutually_exclusive_group()
    sound_options.add_argument("--alert", dest="sound_alert", action="store_true", help="enable the five-second job sound alert (default)")
    sound_options.add_argument("--no-alert", dest="sound_alert", action="store_false", help="disable the job sound alert")
    parser.set_defaults(sound_alert=True)
    parser.add_argument("--test-alert", action="store_true", help="play the full-volume alarm for five seconds, without contacting Amazon")
    parser.add_argument("--no-browser", action="store_true", help="disable opening the first matching job in Chrome")
    parser.add_argument("--browser-settings", type=Path, default=BASE_DIR / "browser_settings.json", help="Chrome profile configuration JSON file")
    parser.add_argument("--chrome-profile", help="override the Chrome profile folder, e.g. \"Profile 7\"")
    parser.add_argument("--test-browser", action="store_true", help="search once, open the first eligible Full-time or Rochdale/Bolton job, and run the configured application steps without recording or audio")
    parser.add_argument("--once", action="store_true", help="make one request and exit")
    parser.add_argument("--verbose", action="store_true", help="show routine polling and session messages (default: errors and browser notices)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.ERROR, format="%(asctime)s %(levelname)s: %(message)s", force=True)

    if args.test_browser:
        return open_first_matching_job(args)

    if args.test_alert:
        alert = JobAlert()
        try:
            if not alert.start():
                return 1
            print("Testing job alarm for 2 seconds. It will stop and restore your previous volume automatically.", flush=True)
            time.sleep(2)
            return 0
        finally:
            alert.close()

    try:
        payload = read_json(args.payload)
        if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
            raise ValueError("The payload must be a JSON object containing a GraphQL query.")
        search_payload(payload, args.settings, args.distance)
        headers = load_headers(args.headers_file)
        session = None if args.no_auto_session else SessionManager(args.headers_file or BASE_DIR / "headers.json", headers, fetch_session)
        records = load_records(args.records)
        args.records.parent.mkdir(parents=True, exist_ok=True)
        if not args.records.exists():
            save_records(args.records, records)
    except (OSError, ValueError, PollError) as error:
        logging.error("Cannot start: %s", error)
        return 1

    if args.once:
        logging.info("Making one request; saving to %s.", args.records.resolve())
    else:
        logging.info("Polling every %g seconds; saving to %s. Press Ctrl+C to stop.", args.interval, args.records.resolve())
    failures = 0
    force_session_refresh = False
    request_count = 0
    automation_started = False
    alert = JobAlert() if args.sound_alert and not args.once else None
    browser = None if args.no_browser or args.once else JobBrowser(args.browser_settings, args.chrome_profile)
    try:
        while True:
            if browser is not None:
                browser.poll_automation()
            if automation_started:
                # Keep the helper alive without more API calls,
                # session renewals, or record updates for the rest of this run.
                time.sleep(1)
                continue
            started = time.monotonic()
            request_count += 1
            try:
                current_payload = search_payload(payload, args.settings, args.distance)
                if session is not None:
                    headers = session.ensure(args.timeout, force=force_session_refresh)
                    force_session_refresh = False
                geo = current_payload["variables"]["searchJobRequest"]["geoQueryClause"]
                logging.info(
                    "Request %d: searching within %g miles of (%g, %g); timeout: %g seconds...",
                    request_count, geo["distance"], geo["lat"], geo["lng"], args.timeout,
                )
                cards = fetch_jobs(current_payload, headers, args.timeout, args.transport)
                updated = merge_jobs(records, cards)
                save_records(args.records, updated)
                records = updated
                failures = 0
                logging.info("Saw %d unique jobs; saved %d total unique jobs.", len({card["jobId"] for card in cards}), len(records))
                if not cards:
                    logging.info("Request succeeded, but no jobs matched this search. Existing counters are unchanged.")
                if args.once:
                    return 0
                eligible_cards = eligible_jobs(cards)
                if cards and not eligible_cards:
                    logging.info("No Full-time or Rochdale/Bolton jobs in the current results; continuing to search without alarm or browser automation.")
                if eligible_cards and args.sound_alert and alert is not None and alert.start():
                    print(
                        f"JOBS FOUND: {len({card['jobId'] for card in eligible_cards})} eligible jobs saved to {args.records.resolve()}. "
                        "Alarm will stop automatically after 5 seconds.",
                        flush=True,
                    )
                if eligible_cards and browser is not None and browser.open_first(eligible_cards):
                    automation_started = True
                    print(
                        "Job fetching stopped for this run. Continuing the browser application steps. "
                        "The command stays open until Ctrl+C; restart it to search again.",
                        flush=True,
                    )
                delay = 1 if automation_started else max(0, args.interval - (time.monotonic() - started))
            except (PollError, SessionError) as error:
                if session is not None and getattr(error, "status_code", None) in (401, 403):
                    force_session_refresh = True
                failures += 1
                if args.once:
                    logging.error("%s Records unchanged.", error)
                    return 1
                delay = max(args.interval, min(60, args.interval * 2 ** min(failures - 1, 10)), error.retry_after)
                logging.error("%s Records unchanged; retrying in %g seconds.", error, delay)
            except OSError as error:
                logging.error("Local file operation failed: %s. Stopping to protect saved data.", error)
                return 1
            time.sleep(delay)
    finally:
        if alert is not None:
            alert.close()
        if browser is not None:
            browser.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logging.info("Stopped.")
