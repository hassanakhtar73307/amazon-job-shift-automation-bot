import ctypes
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import wave

import job_alert


class JobAlertTests(unittest.TestCase):
    def setUp(self):
        sound_patch = patch("job_alert.winsound")
        self.sound = sound_patch.start()
        self.addCleanup(sound_patch.stop)
        self.sound.SND_FILENAME = 0x20000
        self.sound.SND_ASYNC = 1
        self.sound.SND_LOOP = 8
        self.sound.SND_NODEFAULT = 2
        volume_patch = patch("job_alert.WindowsVolume")
        self.volume = volume_patch.start().return_value
        self.addCleanup(volume_patch.stop)
        self.alert = job_alert.JobAlert()
        self.addCleanup(self.alert.close)

    def test_generated_alarm_is_an_audible_pcm_wave(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "alarm.wav"
            job_alert.write_alarm(path)
            with wave.open(str(path), "rb") as sound:
                self.assertEqual((sound.getnchannels(), sound.getsampwidth(), sound.getframerate()),
                                 (1, 2, 22050))
                self.assertGreater(sound.getnframes() / sound.getframerate(), 0.8)
                frames = sound.readframes(sound.getnframes())
            samples = struct.unpack(f"<{len(frames) // 2}h", frames)
            self.assertGreater(max(samples), 32000)
            self.assertLess(min(samples), -32000)

    def test_alarm_loops_without_blocking_and_repeated_jobs_do_not_restart_it(self):
        self.assertTrue(self.alert.start())
        path, flags = self.sound.PlaySound.call_args.args
        self.assertTrue(Path(path).exists())
        self.assertTrue(flags & self.sound.SND_LOOP)
        self.assertTrue(flags & self.sound.SND_ASYNC)
        self.volume.maximize.assert_called_once()
        self.assertFalse(self.alert.start())
        self.sound.PlaySound.assert_called_once()
        self.alert.close()
        self.sound.PlaySound.assert_called_with(None, 0)
        self.volume.close.assert_called_once()
        self.assertFalse(Path(path).exists())
        self.assertFalse(self.alert.playing)
        self.alert.close()
        self.volume.close.assert_called_once()

    def test_alarm_stops_automatically_and_does_not_restart_for_repeated_jobs(self):
        with patch("job_alert.ALARM_DURATION_SECONDS", 0.01):
            self.assertTrue(self.alert.start())
            path = Path(self.sound.PlaySound.call_args.args[0])
            self.alert._thread.join(timeout=1)
        self.assertFalse(self.alert.playing)
        self.sound.PlaySound.assert_called_with(None, 0)
        self.volume.close.assert_called_once()
        self.assertFalse(path.exists())
        self.assertFalse(self.alert.start())
        self.assertEqual(self.sound.PlaySound.call_count, 2)

    def test_volume_is_created_and_restored_on_the_alarm_thread(self):
        threads = []

        def record_thread():
            threads.append(threading.get_ident())

        self.volume.maximize.side_effect = record_thread
        self.volume.close.side_effect = record_thread
        self.alert.start()
        self.alert.close()
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0], threads[1])
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_volume_failure_still_plays_alarm_and_rolls_back_on_exit(self):
        self.volume.maximize.side_effect = OSError("volume unavailable")
        with self.assertLogs(level="ERROR") as logs:
            self.assertTrue(self.alert.start())
        self.assertIn("available volume", " ".join(logs.output))
        self.assertTrue(self.alert.playing)
        self.alert.close()
        self.volume.close.assert_called_once()

    def test_playback_failure_restores_volume_and_removes_temporary_files(self):
        paths = []

        def fail(path, flags):
            paths.append(Path(path))
            raise RuntimeError("no speaker")

        self.sound.PlaySound.side_effect = fail
        with self.assertLogs(level="ERROR"):
            self.assertFalse(self.alert.start())
        self.volume.close.assert_called_once()
        self.assertFalse(paths[0].exists())
        self.assertFalse(self.alert.playing)

    def test_shutdown_restores_volume_even_if_stopping_playback_fails(self):
        self.alert.start()
        self.sound.PlaySound.side_effect = RuntimeError("speaker disconnected")
        with self.assertLogs(level="ERROR"):
            self.alert.close()
        self.volume.close.assert_called_once()
        self.assertIsNone(self.alert._folder)

    def test_shutdown_removes_temporary_files_even_if_restoring_volume_fails(self):
        self.alert.start()
        path = Path(self.sound.PlaySound.call_args.args[0])
        self.volume.close.side_effect = OSError("speaker disconnected")
        with self.assertLogs(level="ERROR"):
            self.alert.close()
        self.assertFalse(path.exists())

    def test_unsupported_platform_is_reported_without_losing_job_recording(self):
        with patch("job_alert.winsound", None), self.assertLogs(level="ERROR") as logs:
            self.assertFalse(self.alert.start())
        self.assertIn("Jobs will still be recorded", " ".join(logs.output))


class WindowsVolumeTests(unittest.TestCase):
    def test_original_volume_and_mute_are_restored_after_partial_failure(self):
        # Model a muted speaker whose unmute call fails after volume has changed.
        speaker = {"level": 0.25, "muted": 1}
        volume = job_alert.WindowsVolume.__new__(job_alert.WindowsVolume)
        volume._endpoint = object()
        volume._interfaces = []
        volume._changed = False
        volume._uninitialize = True
        volume._ole32 = MagicMock()
        volume._level = ctypes.c_float(speaker["level"])
        volume._muted = ctypes.c_int(speaker["muted"])

        def call(pointer, index, argument_types, *arguments):
            if index == 7:
                speaker["level"] = arguments[0]
            elif index == 14:
                if arguments[0] == 0:
                    raise OSError("cannot unmute")
                speaker["muted"] = arguments[0]

        with patch("job_alert._com_call", side_effect=call):
            with self.assertRaises(OSError):
                volume.maximize()
            self.assertEqual(speaker["level"], 1.0)
            volume.close()
            self.assertEqual(speaker, {"level": 0.25, "muted": 1})
            volume.close()
        volume._ole32.CoUninitialize.assert_called_once()


if __name__ == "__main__":
    unittest.main()
