import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import job_browser


class JobBrowserTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.executable = self.root / "Google Chrome" / "chrome.exe"
        self.executable.parent.mkdir()
        self.executable.touch()
        self.user_data = self.root / "User Data"
        (self.user_data / "Profile 7").mkdir(parents=True)
        (self.user_data / "Profile 2").mkdir()
        (self.user_data / "Local State").write_text(
            json.dumps({"profile": {"last_used": "Profile 7"}}), encoding="utf-8",
        )
        self.settings = self.root / "browser_settings.json"
        self.config = {"enabled": True, "chrome_path": str(self.executable),
                       "user_data_dir": str(self.user_data), "profile_directory": "Profile 7",
                       "auto_apply": False}
        self.write_config()
        self.browser = job_browser.JobBrowser(self.settings)
        self.addCleanup(self.browser.close)
        launcher_patch = patch("job_browser.subprocess.Popen")
        self.launch = launcher_patch.start()
        self.launch.return_value.poll.return_value = None
        self.addCleanup(launcher_patch.stop)
        output_patch = contextlib.redirect_stdout(io.StringIO())
        output_patch.__enter__()
        self.addCleanup(output_patch.__exit__, None, None, None)

    def write_config(self):
        self.settings.write_text(json.dumps(self.config), encoding="utf-8")

    def test_opens_first_api_job_once_even_if_later_results_change(self):
        cards = [{"jobId": "FIRST"}, {"jobId": "SECOND"}]
        self.assertTrue(self.browser.open_first(cards))
        self.assertFalse(self.browser.open_first(cards))
        self.assertFalse(self.browser.open_first([{"jobId": "THIRD"}]))
        self.launch.assert_called_once()
        command = self.launch.call_args.args[0]
        self.assertEqual(command[-1], "https://www.jobsatamazon.co.uk/app#/jobDetail?jobId=FIRST")
        self.assertEqual(self.browser.opened_job_id, "FIRST")

    def test_regular_profile_login_is_reused_without_incognito_or_debugging(self):
        self.browser.open_first([{"jobId": "FIRST"}])
        command = self.launch.call_args.args[0]
        self.assertEqual(command[0], str(self.executable))
        self.assertIn(f"--user-data-dir={self.user_data}", command)
        self.assertIn("--profile-directory=Profile 7", command)
        self.assertIn("--new-window", command)
        self.assertFalse(any("incognito" in part or "remote-debugging" in part for part in command))
        self.assertFalse(self.launch.call_args.kwargs.get("shell", False))
        self.assertEqual(self.launch.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_empty_results_and_disabled_browser_never_launch(self):
        self.assertFalse(self.browser.open_first([]))
        self.config["enabled"] = False
        self.write_config()
        self.assertFalse(self.browser.open_first([{"jobId": "FIRST"}]))
        self.launch.assert_not_called()

    def test_missing_or_incorrect_profile_does_not_create_a_logged_out_profile(self):
        self.config["profile_directory"] = "Profile 100"
        self.write_config()
        with self.assertLogs(level="ERROR") as logs:
            self.assertFalse(self.browser.open_first([{"jobId": "FIRST"}]))
        self.assertIn("profile does not exist", " ".join(logs.output))
        self.launch.assert_not_called()
        self.assertFalse((self.user_data / "Profile 100").exists())

    def test_profile_override_and_auto_detection(self):
        command = job_browser.chrome_command(self.settings, job_browser.SEARCH_URL, "Profile 2")
        self.assertIn("--profile-directory=Profile 2", command)
        self.config["profile_directory"] = None
        self.write_config()
        command = job_browser.chrome_command(self.settings, job_browser.SEARCH_URL)
        self.assertIn("--profile-directory=Profile 7", command)

    def test_invalid_settings_are_reported_without_launching(self):
        invalid = [[], {"enabled": "false"}, {"chrome_path": False},
                   {"user_data_dir": ""}, {"profile_directory": "../Profile 7"}]
        for config in invalid:
            with self.subTest(config=config):
                self.settings.write_text(json.dumps({**self.config, **config} if isinstance(config, dict) else config),
                                         encoding="utf-8")
                with self.assertRaises(job_browser.BrowserError):
                    job_browser.chrome_command(self.settings, job_browser.SEARCH_URL)
        self.launch.assert_not_called()

    def test_job_id_is_encoded_as_one_query_parameter(self):
        identifier = "JOB-UK-123&other=value/#?"
        fragment = urlsplit(job_browser.job_url(identifier)).fragment
        self.assertEqual(parse_qs(fragment.split("?", 1)[1]), {"jobId": [identifier]})

    def test_launch_failure_retries_after_cooldown_and_can_recover(self):
        clock = [100]
        with patch("job_browser.time.monotonic", side_effect=lambda: clock[0]):
            self.launch.side_effect = OSError("Chrome could not start")
            with self.assertLogs(level="ERROR"):
                self.assertFalse(self.browser.open_first([{"jobId": "FIRST"}]))
            self.assertIsNone(self.browser.opened_job_id)
            clock[0] = 120
            self.assertFalse(self.browser.open_first([{"jobId": "FIRST"}]))
            self.launch.assert_called_once()
            clock[0] = 161
            self.launch.side_effect = None
            self.assertTrue(self.browser.open_first([{"jobId": "FIRST"}]))
        self.assertEqual(self.launch.call_count, 2)

    def test_immediate_chrome_exit_is_reported_as_failure(self):
        self.launch.return_value.poll.return_value = 1
        with self.assertLogs(level="ERROR"):
            self.assertFalse(self.browser.open_first([{"jobId": "FIRST"}]))
        self.assertIsNone(self.browser.opened_job_id)

    def test_test_browser_opens_search_using_the_same_profile(self):
        self.assertTrue(self.browser.open_search())
        self.assertEqual(self.launch.call_args.args[0][-1], job_browser.SEARCH_URL)
        self.assertIsNone(self.browser.opened_job_id)

    def test_relative_configuration_paths_resolve_next_to_settings_file(self):
        self.config["chrome_path"] = "Google Chrome/chrome.exe"
        self.config["user_data_dir"] = "User Data"
        self.write_config()
        command = job_browser.chrome_command(self.settings, job_browser.SEARCH_URL)
        self.assertEqual(command[0], str(self.executable))
        self.assertIn(f"--user-data-dir={self.user_data}", command)

    def enable_automation(self, **settings):
        self.config.update({"auto_apply": True, "auto_next": True, "automation_timeout_seconds": 20,
                            **settings})
        self.write_config()
        platform_patch = patch("job_browser.sys.platform", "win32")
        platform_patch.start()
        self.addCleanup(platform_patch.stop)
        powershell_patch = patch("job_browser.shutil.which", return_value="powershell.exe")
        powershell_patch.start()
        self.addCleanup(powershell_patch.stop)
        chrome = MagicMock()
        chrome.poll.return_value = None
        worker = MagicMock()
        worker.poll.return_value = None
        self.launch.side_effect = [chrome, worker]
        return chrome, worker

    def test_opening_first_job_starts_persistent_apply_next_workflow_once(self):
        chrome, worker = self.enable_automation()
        self.assertTrue(self.browser.open_first([{"jobId": "FIRST"}]))
        self.assertEqual(self.launch.call_count, 2)
        command = self.launch.call_args.args[0]
        self.assertIn("browser_steps.ps1", Path(command[command.index("-File") + 1]).name)
        self.assertEqual(command[command.index("-JobId") + 1], "FIRST")
        self.assertEqual(command[command.index("-TimeoutSeconds") + 1], "20")
        self.assertNotIn("-SkipNext", command)
        self.assertNotIn("-StartApplication", command)
        self.assertNotIn("-WithdrawPreviousApplication", command)
        self.assertNotIn("-AgreeIntegrityNotice", command)
        self.assertNotIn("-SelectShift", command)
        self.assertFalse(self.browser.open_first([{"jobId": "SECOND"}]))
        self.assertEqual(self.launch.call_count, 2)
        worker.wait.assert_not_called()
        chrome.terminate.assert_not_called()

    def test_next_can_be_disabled_without_disabling_apply(self):
        self.enable_automation(auto_next=False)
        self.browser.open_first([{"jobId": "FIRST"}])
        self.assertIn("-SkipNext", self.launch.call_args.args[0])

    def test_authorized_start_and_previous_application_replacement_are_passed_to_helper(self):
        self.enable_automation(auto_start_application=True, auto_withdraw_previous_application=True)
        self.browser.open_first([{"jobId": "FIRST"}])
        command = self.launch.call_args.args[0]
        self.assertIn("-StartApplication", command)
        self.assertIn("-WithdrawPreviousApplication", command)

    def test_start_can_be_enabled_without_authorizing_previous_application_replacement(self):
        self.enable_automation(auto_start_application=True, auto_withdraw_previous_application=False)
        self.browser.open_first([{"jobId": "FIRST"}])
        command = self.launch.call_args.args[0]
        self.assertIn("-StartApplication", command)
        self.assertNotIn("-WithdrawPreviousApplication", command)

    def test_integrity_agreement_and_shift_selection_are_passed_to_helper(self):
        self.enable_automation(auto_agree_integrity_notice=True, auto_select_shift=True)
        self.browser.open_first([{"jobId": "FIRST"}])
        command = self.launch.call_args.args[0]
        self.assertIn("-AgreeIntegrityNotice", command)
        self.assertIn("-SelectShift", command)

    def test_integrity_agreement_and_shift_selection_can_be_disabled(self):
        self.enable_automation(auto_agree_integrity_notice=False, auto_select_shift=False)
        self.browser.open_first([{"jobId": "FIRST"}])
        command = self.launch.call_args.args[0]
        self.assertNotIn("-AgreeIntegrityNotice", command)
        self.assertNotIn("-SelectShift", command)

    def test_disabling_start_also_prevents_previous_application_replacement(self):
        self.enable_automation(auto_start_application=False, auto_withdraw_previous_application=True)
        self.browser.open_first([{"jobId": "FIRST"}])
        command = self.launch.call_args.args[0]
        self.assertNotIn("-StartApplication", command)
        self.assertNotIn("-WithdrawPreviousApplication", command)

    def test_automation_completion_does_not_reopen_browser(self):
        chrome, worker = self.enable_automation()
        self.browser.open_first([{"jobId": "FIRST"}])
        worker.poll.return_value = 0
        self.browser.poll_automation()
        self.assertTrue(self.browser.wait_for_automation())
        self.assertFalse(self.browser.open_first([{"jobId": "FIRST"}]))
        self.assertEqual(self.launch.call_count, 2)
        chrome.terminate.assert_not_called()
        worker.terminate.assert_not_called()

    def test_automation_failure_is_reported_without_reopening_the_job(self):
        chrome, worker = self.enable_automation()
        self.browser.open_first([{"jobId": "FIRST"}])
        worker.poll.return_value = 1
        with self.assertLogs(level="ERROR"):
            self.browser.poll_automation()
        self.assertFalse(self.browser.wait_for_automation())
        self.assertFalse(self.browser.open_first([{"jobId": "FIRST"}]))
        chrome.terminate.assert_not_called()

    def test_stop_terminates_only_the_helper_and_leaves_chrome_open(self):
        chrome, worker = self.enable_automation()
        self.browser.open_first([{"jobId": "FIRST"}])
        self.browser.close()
        worker.terminate.assert_called_once()
        chrome.terminate.assert_not_called()
        self.browser.close()
        worker.terminate.assert_called_once()

    def test_unresponsive_automation_is_stopped_at_its_deadline(self):
        chrome, worker = self.enable_automation()
        self.browser.open_first([{"jobId": "FIRST"}])
        with patch("job_browser.time.monotonic", return_value=self.browser._automation_deadline + 1):
            with self.assertLogs(level="ERROR"):
                self.browser.poll_automation()
        worker.terminate.assert_called_once()
        chrome.terminate.assert_not_called()
        self.assertFalse(self.browser.wait_for_automation())

    def test_test_browser_waits_for_workflow_completion(self):
        chrome, worker = self.enable_automation()
        self.browser.open_first([{"jobId": "FIRST"}])
        worker.wait.return_value = 0
        worker.poll.return_value = 0
        self.assertTrue(self.browser.wait_for_automation())
        worker.wait.assert_called_once()
        chrome.wait.assert_not_called()

    def test_automation_start_failure_keeps_the_job_open(self):
        chrome, worker = self.enable_automation()
        self.launch.side_effect = [chrome, OSError("helper could not start")]
        with self.assertLogs(level="ERROR"):
            self.assertTrue(self.browser.open_first([{"jobId": "FIRST"}]))
        self.assertEqual(self.browser.opened_job_id, "FIRST")
        self.assertFalse(self.browser.wait_for_automation())

    def test_invalid_automation_settings_do_not_launch_browser(self):
        for changes in ({"auto_apply": "true"}, {"auto_next": 1},
                        {"auto_start_application": "true"}, {"auto_withdraw_previous_application": 1},
                        {"auto_agree_integrity_notice": "true"}, {"auto_select_shift": 1},
                        {"automation_timeout_seconds": 0}, {"automation_timeout_seconds": True},
                        {"automation_timeout_seconds": 301}):
            with self.subTest(changes=changes):
                self.config.update({"auto_apply": False, "auto_next": True,
                                    "automation_timeout_seconds": 90, **changes})
                self.write_config()
                with self.assertRaises(job_browser.BrowserError):
                    job_browser.chrome_command(self.settings, job_browser.SEARCH_URL)
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
