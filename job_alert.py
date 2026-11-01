"""Play a five-second Windows job alarm and temporarily raise the speaker volume."""

from __future__ import annotations

import ctypes
import logging
import math
from pathlib import Path
import struct
import tempfile
import threading
from uuid import UUID
import wave

try:
    import winsound
except ImportError:
    winsound = None


ALARM_DURATION_SECONDS = 5


def _check_hresult(result: int) -> None:
    if result < 0:
        raise ctypes.WinError(result)


def _com_call(pointer, index: int, argument_types: tuple, *arguments) -> None:
    """Call a method in a native COM interface's vtable."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argument_types)(table[index])
    _check_hresult(method(pointer, *arguments))


def _guid(value: str):
    return (ctypes.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


class WindowsVolume:
    """Use Core Audio without third-party packages; all calls stay on one thread."""

    def __init__(self):
        self._interfaces = []
        self._uninitialize = False
        self._changed = False
        self._ole32 = ctypes.WinDLL("ole32")
        self._ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
        self._ole32.CoInitializeEx.restype = ctypes.c_long
        self._ole32.CoUninitialize.argtypes = ()
        self._ole32.CoUninitialize.restype = None
        self._ole32.CoCreateInstance.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
        )
        self._ole32.CoCreateInstance.restype = ctypes.c_long
        result = self._ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        if result != -2147417850:  # RPC_E_CHANGED_MODE: COM is already initialized.
            _check_hresult(result)
            self._uninitialize = True
        try:
            enumerator = ctypes.c_void_p()
            _check_hresult(self._ole32.CoCreateInstance(
                _guid("bcde0395-e52f-467c-8e3d-c4579291692e"), None, 1,
                _guid("a95664d2-9614-4f35-a746-de8db63617e6"), ctypes.byref(enumerator),
            ))
            self._interfaces.append(enumerator)
            device = ctypes.c_void_p()
            # IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia).
            _com_call(enumerator, 4, (ctypes.c_int, ctypes.c_int, ctypes.c_void_p),
                      0, 1, ctypes.byref(device))
            self._interfaces.append(device)
            self._endpoint = ctypes.c_void_p()
            # IMMDevice::Activate(IAudioEndpointVolume).
            _com_call(device, 3, (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p, ctypes.c_void_p),
                      _guid("5cdf2c82-841e-4546-9722-0cf74078229a"), 1, None,
                      ctypes.byref(self._endpoint))
            self._interfaces.append(self._endpoint)
            self._level = ctypes.c_float()
            self._muted = ctypes.c_int()
            _com_call(self._endpoint, 9, (ctypes.c_void_p,), ctypes.byref(self._level))
            _com_call(self._endpoint, 15, (ctypes.c_void_p,), ctypes.byref(self._muted))
        except BaseException:
            self.close()
            raise

    def maximize(self) -> None:
        self._changed = True  # Roll back even if only one of the setters succeeds.
        _com_call(self._endpoint, 7, (ctypes.c_float, ctypes.c_void_p), 1.0, None)
        _com_call(self._endpoint, 14, (ctypes.c_int, ctypes.c_void_p), 0, None)

    def close(self) -> None:
        try:
            if self._changed:
                self._changed = False
                try:
                    _com_call(self._endpoint, 7, (ctypes.c_float, ctypes.c_void_p),
                              self._level.value, None)
                finally:
                    _com_call(self._endpoint, 14, (ctypes.c_int, ctypes.c_void_p),
                              self._muted.value, None)
        finally:
            for pointer in reversed(self._interfaces):
                _com_call(pointer, 2, ())  # IUnknown::Release
            self._interfaces.clear()
            if self._uninitialize:
                self._uninitialize = False
                self._ole32.CoUninitialize()


def write_alarm(path: Path) -> None:
    """Generate a repeating two-tone WAV, independent of Windows sound settings."""
    sample_rate = 22050
    frames = bytearray()
    tone_frames = int(sample_rate * 0.35)
    for frequency in (880, 660):
        for index in range(tone_frames):
            fade = min(1.0, index / 220, (tone_frames - 1 - index) / 220)
            sample = int(32767 * fade * math.sin(2 * math.pi * frequency * index / sample_rate))
            frames.extend(struct.pack("<h", sample))
    frames.extend(b"\0\0" * int(sample_rate * 0.15))
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, sample_rate, 0, "NONE", "not compressed"))
        output.writeframes(frames)


class JobAlert:
    def __init__(self):
        self.playing = False
        self._folder = None
        self._volume = None
        self._thread = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._started = False

    def start(self) -> bool:
        """Play once per run for five seconds, without delaying job automation."""
        if self._thread is not None:
            return False
        if winsound is None:
            logging.error("Job sound alerts require Windows. Jobs will still be recorded.")
            return False
        self._thread = threading.Thread(target=self._run, name="job-alarm", daemon=True)
        self._thread.start()
        self._ready.wait()
        return self._started

    def _run(self) -> None:
        # Core Audio COM interfaces must be created and closed on the same thread.
        try:
            self._folder = tempfile.TemporaryDirectory(prefix="amazon-job-alert-")
            path = Path(self._folder.name) / "alarm.wav"
            write_alarm(path)
            try:
                self._volume = WindowsVolume()
                self._volume.maximize()
            except OSError as error:
                logging.error("Cannot set speaker volume to 100%%: %s. Playing at the available volume.", error)
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC
                               | winsound.SND_LOOP | winsound.SND_NODEFAULT)
            self.playing = True
            self._started = True
            self._ready.set()
            self._stop.wait(ALARM_DURATION_SECONDS)
        except (OSError, RuntimeError) as error:
            logging.error("Cannot play the job alarm: %s. Jobs will still be recorded.", error)
        finally:
            try:
                self._cleanup()
            finally:
                self._ready.set()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _cleanup(self) -> None:
        try:
            if self.playing:
                winsound.PlaySound(None, 0)
        except RuntimeError as error:
            logging.error("Cannot stop the job alarm: %s", error)
        finally:
            self.playing = False
            if self._volume is not None:
                try:
                    self._volume.close()
                except OSError as error:
                    logging.error("Cannot restore the previous speaker volume: %s", error)
                finally:
                    self._volume = None
            if self._folder is not None:
                try:
                    self._folder.cleanup()
                except OSError as error:
                    logging.error("Cannot remove the temporary alarm file: %s", error)
                finally:
                    self._folder = None
