from pathlib import Path
import shutil
import subprocess
import sys
import unittest


@unittest.skipUnless(sys.platform == "win32", "Windows accessibility selector tests")
class BrowserStepSelectorTests(unittest.TestCase):
    def test_modal_shift_selection_and_saved_application_workflow(self):
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if not powershell:
            self.skipTest("Windows PowerShell is unavailable")
        root = Path(__file__).resolve().parent.parent
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(root / "tests" / "browser_steps_fixture.ps1"),
             "-SourcePath", str(root / "browser_steps.ps1")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Passed modal selection", result.stdout)
        self.assertIn("Passed maximum-hour selection", result.stdout)
        self.assertIn("Passed clicking a shift while Chrome still reports", result.stdout)
        self.assertIn("Passed desktop accessibility document preservation", result.stdout)
        self.assertIn("Passed the saved Apply", result.stdout)
        self.assertIn("Passed restarting directly at shift selection", result.stdout)


if __name__ == "__main__":
    unittest.main()
