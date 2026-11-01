"""Open the first matching Amazon job in the user's existing Chrome profile."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.parse import urlencode


SEARCH_URL = "https://www.jobsatamazon.co.uk/app#/jobSearch"
JOB_URL = "https://www.jobsatamazon.co.uk/app#/jobDetail?"


class BrowserError(Exception):
    """Browser setup failed; the recorder can keep running."""


def job_url(job_id: str) -> str:
    if not isinstance(job_id, str) or not job_id.strip():
        raise BrowserError("The first job has no valid jobId.")
    return JOB_URL + urlencode({"jobId": job_id})


def default_user_data_dir() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if not local:
            raise BrowserError("LOCALAPPDATA is unavailable; set user_data_dir in browser_settings.json.")
        return Path(local) / "Google" / "Chrome" / "User Data"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "google-chrome"


def find_chrome() -> Path:
    for name in ("chrome", "google-chrome", "google-chrome-stable"):
        executable = shutil.which(name)
        if executable:
            return Path(executable)
    candidates = [
        Path(os.environ[root]) / suffix
        for root, suffix in (
            ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
            ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
            ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
        )
        if os.environ.get(root)
    ]
    candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BrowserError("Google Chrome was not found. Set chrome_path in browser_settings.json.")


def _configured_path(value: str, folder: Path) -> Path:
    path = Path(os.path.expandvars(value)).expanduser()
    return (path if path.is_absolute() else folder / path).resolve()


def browser_settings(settings_path: Path) -> dict:
    settings = {}
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if not isinstance(settings, dict):
        raise BrowserError("browser_settings.json must contain a JSON object.")
    for key in ("enabled", "auto_apply", "auto_next", "auto_start_application", "auto_withdraw_previous_application",
                "auto_agree_integrity_notice", "auto_select_shift"):
        if type(settings.get(key, True)) is not bool:
            raise BrowserError(f"{key} must be true or false in browser_settings.json.")
    timeout = settings.get("automation_timeout_seconds", 90)
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise BrowserError("automation_timeout_seconds must be an integer between 1 and 300.")
    return settings


def chrome_command(settings_path: Path, url: str, profile_override: str | None = None) -> list[str] | None:
    """Build an argument list, preserving Chrome's own cookies and login storage."""
    settings = browser_settings(settings_path)
    if not settings.get("enabled", True):
        return None
    for key in ("chrome_path", "user_data_dir", "profile_directory"):
        value = settings.get(key)
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise BrowserError(f"{key} must be a nonempty string or null in browser_settings.json.")
    executable = (_configured_path(settings["chrome_path"], settings_path.parent)
                  if settings.get("chrome_path") else find_chrome())
    if not executable.is_file():
        raise BrowserError(f"Chrome executable does not exist: {executable}")
    user_data = (_configured_path(settings["user_data_dir"], settings_path.parent)
                 if settings.get("user_data_dir") else default_user_data_dir())
    profile = profile_override or settings.get("profile_directory")
    if profile is None:
        state = json.loads((user_data / "Local State").read_text(encoding="utf-8-sig"))
        metadata = state.get("profile", {}) if isinstance(state, dict) else None
        if not isinstance(metadata, dict):
            raise BrowserError("Chrome's Local State has invalid profile metadata; set profile_directory explicitly.")
        profile = metadata.get("last_used", "Default")
    if (not isinstance(profile, str) or not profile.strip() or profile in (".", "..")
            or any(character in profile for character in ("/", "\\", "\r", "\n", "\0"))):
        raise BrowserError("profile_directory must be a Chrome folder name such as Default or Profile 7.")
    if not (user_data / profile).is_dir():
        raise BrowserError(
            f"Chrome profile does not exist: {user_data / profile}. "
            "Use the profile folder shown under Profile Path at chrome://version."
        )
    # Chrome forwards this navigation to the running instance using this profile.
    # No debugging port, cookie extraction, or Selenium profile copy is needed.
    return [str(executable), f"--user-data-dir={user_data}",
            f"--profile-directory={profile}", "--new-window", url]


class JobBrowser:
    def __init__(self, settings_path: Path, profile: str | None = None):
        self.settings_path = settings_path
        self.profile = profile
        self.opened_job_id = None
        self._retry_at = 0.0
        self._process = None
        self._automation = None
        self._automation_deadline = 0.0
        self._automation_result = True

    def _open(self, url: str) -> bool:
        if time.monotonic() < self._retry_at:
            return False
        try:
            command = chrome_command(self.settings_path, url, self.profile)
            if command is None:
                return False
            self._process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            exit_code = self._process.poll()
            if exit_code not in (None, 0):
                raise BrowserError(f"Chrome exited with code {exit_code}.")
            return True
        except (OSError, ValueError, BrowserError) as error:
            self._retry_at = time.monotonic() + 60
            logging.error("Cannot open Chrome: %s. Job recording continues; retrying browser opening in 60 seconds.", error)
            return False

    def open_first(self, cards: list[dict]) -> bool:
        """Open the first supplied job, once per recorder run, after it is saved."""
        if not cards or self.opened_job_id is not None:
            return False
        url = job_url(cards[0]["jobId"])
        if not self._open(url):
            return False
        self.opened_job_id = cards[0]["jobId"]
        print(f"Opening first matching job in your saved Chrome profile: {url}", flush=True)
        self._start_automation()
        return True

    def _start_automation(self) -> None:
        try:
            settings = browser_settings(self.settings_path)
            if not settings.get("auto_apply", True):
                return
            if sys.platform != "win32":
                raise BrowserError("The browser application steps use Windows accessibility controls.")
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if not powershell:
                raise BrowserError("Windows PowerShell was not found; continue the application manually.")
            script = Path(__file__).resolve().with_name("browser_steps.ps1")
            if not script.is_file():
                raise BrowserError(f"Browser automation helper is missing: {script}")
            timeout = settings.get("automation_timeout_seconds", 90)
            command = [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-File", str(script), "-JobId", self.opened_job_id,
                       "-TimeoutSeconds", str(timeout)]
            if not settings.get("auto_next", True):
                command.append("-SkipNext")
            if settings.get("auto_start_application", False):
                command.append("-StartApplication")
                if settings.get("auto_withdraw_previous_application", False):
                    command.append("-WithdrawPreviousApplication")
            if settings.get("auto_agree_integrity_notice", False):
                command.append("-AgreeIntegrityNotice")
            if settings.get("auto_select_shift", False):
                command.append("-SelectShift")
            self._automation = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._automation_deadline = time.monotonic() + timeout + 10
            self._automation_result = None
        except (OSError, ValueError, BrowserError) as error:
            self._automation_result = False
            logging.error("Cannot start the browser application steps: %s. Saved records are kept; continue in Chrome or restart to retry.", error)

    def poll_automation(self) -> None:
        """Check the application helper without opening more windows."""
        if self._automation is None:
            return
        result = self._automation.poll()
        if result is not None:
            self._automation_result = result == 0
            self._automation = None
            if result:
                logging.error("Browser automation stopped before completing the configured steps. Check Chrome and the browser automation messages above.")
        elif time.monotonic() >= self._automation_deadline:
            logging.error("Browser automation exceeded its timeout. Stopping the helper; saved records are kept.")
            self.close()

    def wait_for_automation(self) -> bool:
        """Let --test-browser finish the same steps used by continuous polling."""
        if self._automation is not None:
            try:
                self._automation.wait(timeout=max(0, self._automation_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                logging.error("Browser automation exceeded its timeout.")
                self.close()
            else:
                self.poll_automation()
        return self._automation_result is True

    def close(self) -> None:
        """Stop only our automation helper; leave the user's Chrome open."""
        if self._automation is None:
            return
        process, self._automation = self._automation, None
        self._automation_result = False
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def open_search(self) -> bool:
        """Let the user check their profile and Amazon login without polling."""
        return self._open(SEARCH_URL)
