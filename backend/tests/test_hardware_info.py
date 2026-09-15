"""Phase G1 — Hardware discovery module + Setting-backed device config."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import hardware_info_service as hw
from app.services import settings_cache


class HardwareInfoTests(unittest.TestCase):
    def test_get_hardware_info_never_raises_and_has_sane_types(self):
        info = hw.get_hardware_info()
        self.assertGreaterEqual(info.cpu_cores, 1)
        self.assertGreaterEqual(info.total_ram_mb, 0)
        self.assertGreaterEqual(info.free_ram_mb, 0)
        self.assertIsInstance(info.available_providers, list)
        self.assertIsInstance(info.cuda_available, bool)
        self.assertIsInstance(info.gpus, list)

    def test_cuda_available_tracks_VERIFIED_providers_not_advertised_ones(self):
        """onnxruntime advertises every provider it was built with, even when
        that provider's runtime libraries are absent (this machine advertises
        TensorRT but cannot initialise it). cuda_available must follow what
        actually initialises, never the advertised list."""
        info = hw.get_hardware_info()
        self.assertEqual(info.cuda_available, "CUDAExecutionProvider" in info.usable_providers)

    def test_usable_providers_is_a_subset_of_advertised(self):
        info = hw.get_hardware_info()
        self.assertTrue(set(info.usable_providers).issubset(set(info.available_providers)))

    def test_advertised_but_uninitialisable_provider_is_not_reported_usable(self):
        """The exact TensorRT trap: ORT accepts the request, silently falls
        back, and get_available_providers() keeps claiming it is there."""
        hw._usable_cache = None
        self.addCleanup(setattr, hw, "_usable_cache", None)

        class FakeSession:
            def __init__(self, *a, **k):
                requested = k.get("providers") or (a[2] if len(a) > 2 else [])
                # emulate ORT: never raises, just falls back to CPU
                self._active = ["CPUExecutionProvider"] if requested and requested[0] == "GhostProvider" else list(requested)

            def get_providers(self):
                return self._active

        import onnxruntime

        with patch.object(onnxruntime, "InferenceSession", FakeSession):
            usable = hw._verified_providers(["GhostProvider", "CPUExecutionProvider"])
        self.assertNotIn("GhostProvider", usable)
        self.assertIn("CPUExecutionProvider", usable)

    def test_verification_result_is_cached(self):
        hw._usable_cache = None
        self.addCleanup(setattr, hw, "_usable_cache", None)
        calls = {"n": 0}

        class CountingSession:
            def __init__(self, *a, **k):
                calls["n"] += 1
                self._p = list(k.get("providers") or [])

            def get_providers(self):
                return self._p

        import onnxruntime

        with patch.object(onnxruntime, "InferenceSession", CountingSession):
            hw._verified_providers(["CPUExecutionProvider"])
            first = calls["n"]
            hw._verified_providers(["CPUExecutionProvider"])
        self.assertEqual(calls["n"], first, "provider probing must not re-run on every call")

    @staticmethod
    def _blocking_import(blocked_name):
        real_import = __import__

        def _fake(name, *args, **kwargs):
            if name == blocked_name:
                raise ImportError(f"no {blocked_name}")
            return real_import(name, *args, **kwargs)

        return _fake

    def test_degrades_gracefully_when_psutil_missing(self):
        with patch("builtins.__import__", side_effect=self._blocking_import("psutil")):
            cores, total, free = hw._cpu_and_ram()
            self.assertGreaterEqual(cores, 1)
            self.assertEqual(total, 0)
            self.assertEqual(free, 0)

    def test_degrades_gracefully_when_onnxruntime_missing(self):
        with patch("builtins.__import__", side_effect=self._blocking_import("onnxruntime")):
            providers = hw._available_providers()
            self.assertEqual(providers, [])

    def test_degrades_gracefully_when_nvidia_smi_missing(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("no nvidia-smi")):
            gpus = hw._gpus_via_nvidia_smi()
            self.assertEqual(gpus, [])

    def test_gpus_parsed_from_nvidia_smi_output(self):
        fake = type("R", (), {"stdout": "0, NVIDIA Test GPU, 6141, 5351\n"})()
        with patch("subprocess.run", return_value=fake):
            gpus = hw._gpus_via_nvidia_smi()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0].name, "NVIDIA Test GPU")
        self.assertEqual(gpus[0].total_vram_mb, 6141)
        self.assertEqual(gpus[0].free_vram_mb, 5351)

    def test_malformed_nvidia_smi_line_is_skipped_not_fatal(self):
        fake = type("R", (), {"stdout": "garbage,line\nnot,even,close,to,valid\n"})()
        with patch("subprocess.run", return_value=fake):
            gpus = hw._gpus_via_nvidia_smi()
        self.assertEqual(gpus, [])

    def test_clamp(self):
        self.assertEqual(hw.clamp(5, 1, 10), 5)
        self.assertEqual(hw.clamp(-5, 1, 10), 1)
        self.assertEqual(hw.clamp(50, 1, 10), 10)


class ProviderSelectionTests(unittest.TestCase):
    """onnxruntime advertises TensorRT on every machine it was built with, even
    with no TensorRT runtime installed. Asking onnxruntime to prove that loads
    onnxruntime_providers_tensorrt.dll, which fails on a missing nvinfer and
    prints a large native EP Error block before silently falling back. These
    pin the quiet path: check TensorRT's own runtime first, then probe."""

    def setUp(self):
        hw._usable_cache = None
        self.addCleanup(setattr, hw, "_usable_cache", None)

    def probe(self, *, tensorrt_runtime, usable, advertised=None):
        """Returns (providers actually probed, verified result)."""
        requested = []

        class FakeSession:
            def __init__(self, model, options=None, providers=None):
                requested.append(providers[0])
                self._active = list(providers) if providers[0] in usable else ["CPUExecutionProvider"]

            def get_providers(self):
                return self._active

        import onnxruntime

        advertised = advertised or ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
        with patch.object(hw, "_tensorrt_runtime_present", return_value=tensorrt_runtime),                 patch.object(onnxruntime, "InferenceSession", FakeSession):
            return requested, hw._verified_providers(advertised)

    def test_tensorrt_advertised_but_runtime_missing_is_skipped_before_any_session(self):
        requested, verified = self.probe(tensorrt_runtime=False,
                                         usable={"CUDAExecutionProvider", "CPUExecutionProvider"})
        self.assertNotIn("TensorrtExecutionProvider", requested, "the failing TensorRT probe must never run")
        self.assertNotIn("TensorrtExecutionProvider", verified)

    def test_cuda_is_selected_when_tensorrt_is_unusable(self):
        _requested, verified = self.probe(tensorrt_runtime=False,
                                          usable={"CUDAExecutionProvider", "CPUExecutionProvider"})
        self.assertEqual(verified[0], "CUDAExecutionProvider")

    def test_cpu_is_selected_when_neither_tensorrt_nor_cuda_is_usable(self):
        _requested, verified = self.probe(tensorrt_runtime=False, usable={"CPUExecutionProvider"})
        self.assertEqual(verified, ["CPUExecutionProvider"])

    def test_tensorrt_wins_when_its_runtime_is_really_there(self):
        requested, verified = self.probe(
            tensorrt_runtime=True,
            usable={"TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"})
        self.assertEqual(requested[0], "TensorrtExecutionProvider")
        self.assertEqual(verified[0], "TensorrtExecutionProvider", "priority is TensorRT, CUDA, CPU")

    def test_the_runtime_check_needs_every_tensorrt_library(self):
        with patch.object(hw, "_library_loads", return_value=False):
            self.assertFalse(hw._tensorrt_runtime_present())
        with patch.object(hw, "_library_loads", return_value=True):
            self.assertTrue(hw._tensorrt_runtime_present())

    def test_a_missing_library_is_reported_unloadable_not_raised(self):
        self.assertFalse(hw._library_loads("reconize-no-such-library-42.dll"))

    def test_verification_happens_once_per_process_and_later_calls_reuse_it(self):
        sessions = {"n": 0}

        class CountingSession:
            def __init__(self, model, options=None, providers=None):
                sessions["n"] += 1
                self._p = list(providers or [])

            def get_providers(self):
                return self._p

        import onnxruntime

        with patch.object(hw, "_tensorrt_runtime_present", return_value=False),                 patch.object(hw, "_available_providers", return_value=["CUDAExecutionProvider", "CPUExecutionProvider"]),                 patch.object(onnxruntime, "InferenceSession", CountingSession):
            first = hw.verified_providers()
            after_first = sessions["n"]
            # Whatever asks later — the first Event batch's worker profile
            # included — must reuse the cached answer, not probe again.
            again = hw.verified_providers()
            hw.get_hardware_info()
        self.assertEqual(first, again)
        self.assertEqual(sessions["n"], after_first, "provider probing must not run a second time")


class EngineProviderTests(unittest.TestCase):
    """The face-model session must use the SAME provider the verification
    reported — no separate CUDA attempt, and never TensorRT."""

    def build_with(self, verified):
        from app.face_recognition import engine

        requested = []

        class FakeFaceAnalysis:
            def __init__(self, **kwargs):
                requested.append(list(kwargs.get("providers") or []))
                self.models = {"recognition": type("M", (), {
                    "session": type("S", (), {"get_providers": staticmethod(lambda: list(requested[-1]))})()})()}

            def prepare(self, **kwargs):
                pass

        with patch.object(engine, "FaceAnalysis", FakeFaceAnalysis),                 patch.object(engine, "detect_faces", lambda img: []),                 patch.object(engine, "_face_app", None),                 patch("app.services.hardware_info_service.verified_providers", return_value=verified):
            engine.get_face_app()
        return requested

    def test_cuda_session_when_cuda_is_verified(self):
        requested = self.build_with(["CUDAExecutionProvider", "CPUExecutionProvider"])
        self.assertEqual(requested[0][0], "CUDAExecutionProvider")

    def test_cpu_session_without_a_failed_cuda_attempt_when_cuda_is_not_verified(self):
        requested = self.build_with(["CPUExecutionProvider"])
        self.assertEqual(requested, [["CPUExecutionProvider"]], "no doomed CUDA session is built")

    def test_tensorrt_is_never_requested_for_the_face_model(self):
        requested = self.build_with(["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"])
        self.assertNotIn("TensorrtExecutionProvider", requested[0])


class EventProcessingDeviceSettingTests(unittest.TestCase):
    def setUp(self):
        self.original = settings_cache._cache["event_processing_device"]
        self.addCleanup(lambda: settings_cache._cache.__setitem__("event_processing_device", self.original))

    def test_default_is_auto(self):
        settings_cache._cache["event_processing_device"] = "auto"
        self.assertEqual(settings_cache.get_event_processing_device(), "auto")

    def test_set_and_get(self):
        settings_cache.set_event_processing_device("gpu:0")
        self.assertEqual(settings_cache.get_event_processing_device(), "gpu:0")

    def test_load_from_db_reads_stored_value(self):
        from types import SimpleNamespace

        class FakeSession:
            def get(self, model, key):
                if key == "event_processing_device":
                    return SimpleNamespace(value="cpu")
                return None

        settings_cache.load_from_db(FakeSession())
        self.assertEqual(settings_cache.get_event_processing_device(), "cpu")

    def test_load_from_db_keeps_default_when_no_row(self):
        class FakeSession:
            def get(self, model, key):
                return None

        settings_cache._cache["event_processing_device"] = "auto"
        settings_cache.load_from_db(FakeSession())
        self.assertEqual(settings_cache.get_event_processing_device(), "auto")


class HardwareInfoRouteOrderingTests(unittest.TestCase):
    """Regression test for a real bug caught during this phase: a bare
    GET /hardware-info registered AFTER GET /{batch_id} is unreachable —
    FastAPI matches "hardware-info" as a batch_id and returns 404 instead.
    """

    def test_hardware_info_registered_before_batch_id_catchall(self):
        import app.api.photo_batches as pb

        get_paths = [r.path for r in pb.router.routes if "GET" in r.methods]
        hw_index = get_paths.index("/api/photo-batches/hardware-info")
        batch_id_index = get_paths.index("/api/photo-batches/{batch_id}")
        self.assertLess(hw_index, batch_id_index, "/hardware-info must be registered before /{batch_id}")


if __name__ == "__main__":
    unittest.main()
