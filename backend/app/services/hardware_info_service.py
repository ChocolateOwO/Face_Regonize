"""Hardware discovery (Phase G1) — CPU cores, RAM, GPU/VRAM.

Shared by GPU device selection (Phase G3) and the Continuous Processing
Pipeline's benchmark-profile sizing (Phase B) — one hardware-discovery
module, not two. Degrades gracefully: every field has a safe fallback if
detection fails (headless machine, no GPU, nvidia-smi missing), since this
must never crash startup or block a GPU-less machine from running at all.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class GpuInfo:
    index: int
    name: str
    total_vram_mb: int
    free_vram_mb: int


@dataclass
class HardwareInfo:
    cpu_cores: int
    total_ram_mb: int
    free_ram_mb: int
    available_providers: list[str]  # what onnxruntime ADVERTISES — not proof of usability
    usable_providers: list[str]     # verified: a real session actually initialised on it
    cuda_available: bool            # verified, not advertised
    gpus: list[GpuInfo] = field(default_factory=list)


def _cpu_and_ram() -> tuple[int, int, int]:
    try:
        import psutil

        cpu_cores = psutil.cpu_count(logical=True) or 1
        mem = psutil.virtual_memory()
        return cpu_cores, int(mem.total // (1024 * 1024)), int(mem.available // (1024 * 1024))
    except Exception:
        import os

        logger.warning("psutil unavailable, falling back to os.cpu_count() with no RAM figures", exc_info=True)
        return (os.cpu_count() or 1), 0, 0


def _available_providers() -> list[str]:
    try:
        import onnxruntime

        return list(onnxruntime.get_available_providers())
    except Exception:
        logger.warning("Could not query onnxruntime providers", exc_info=True)
        return []


_PROBE_MODEL: bytes | None = None
_usable_cache: list[str] | None = None

_TENSORRT_PROVIDER = "TensorrtExecutionProvider"
# TensorRT ships as its own runtime, separately from the onnxruntime wheel.
_TENSORRT_LIBS_WINDOWS = ("nvinfer_10.dll", "nvonnxparser_10.dll")
_TENSORRT_LIBS_POSIX = ("libnvinfer.so.10", "libnvonnxparser.so.10")
# Fastest first. Everything else keeps the order onnxruntime gave it.
_PROVIDER_PRIORITY = (_TENSORRT_PROVIDER, "CUDAExecutionProvider", "CPUExecutionProvider")


def _library_loads(name: str) -> bool:
    import ctypes

    try:
        ctypes.WinDLL(name) if name.endswith(".dll") else ctypes.CDLL(name)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001 — a probe must never take the process down
        logger.debug("Could not test %s", name, exc_info=True)
        return False


def _tensorrt_runtime_present() -> bool:
    """Ask TensorRT's own runtime libraries, quietly.

    onnxruntime is BUILT with TensorRT, so it advertises the provider on every
    machine. Asking onnxruntime to prove it makes it load
    onnxruntime_providers_tensorrt.dll, which — with no TensorRT installed —
    fails on a missing nvinfer, prints a large native "EP Error" block straight
    to the console, and silently falls back. Loading the dependency ourselves
    answers the same question with no output at all.
    """
    names = _TENSORRT_LIBS_WINDOWS if os.name == "nt" else _TENSORRT_LIBS_POSIX
    return all(_library_loads(name) for name in names)


def _probe_model() -> bytes | None:
    """Smallest possible ONNX graph (one Relu) used purely to ask a provider
    to prove it can initialise. Built once, in memory — never touches the
    real face-recognition models, so probing cannot disturb them."""
    global _PROBE_MODEL
    if _PROBE_MODEL is not None:
        return _PROBE_MODEL
    try:
        from onnx import TensorProto, helper

        node = helper.make_node("Relu", ["x"], ["y"])
        graph = helper.make_graph(
            [node], "probe",
            [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 1])],
            [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 1])],
        )
        model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
        model.ir_version = 10  # stay within what the installed runtime accepts
        _PROBE_MODEL = model.SerializeToString()
    except Exception:
        logger.warning("Could not build ONNX probe model; provider verification unavailable", exc_info=True)
        _PROBE_MODEL = None
    return _PROBE_MODEL


def _verified_providers(advertised: list[str]) -> list[str]:
    """A provider is 'usable' only if a real InferenceSession initialises on
    it AND onnxruntime reports it as actually active afterwards.

    onnxruntime advertises every provider it was BUILT with, whether or not
    that provider's runtime libraries exist on the machine. Asking for a
    missing one does not raise — it logs and silently falls back to CPU/CUDA,
    so `get_available_providers()` alone will happily claim TensorRT is there
    when no TensorRT runtime is installed. Only the post-construction
    get_providers() call distinguishes the two.
    """
    global _usable_cache
    if _usable_cache is not None:
        return _usable_cache

    model = _probe_model()
    if model is None:
        _usable_cache = list(advertised)  # cannot verify — do not silently claim nothing works
        return _usable_cache

    # The CUDA provider's DLL dependencies are only findable after this loader
    # fix runs (see cuda_dlls' own docstring — it documents this exact silent
    # CPU fallback). Without it the probe would report CUDA as unusable on a
    # perfectly good GPU machine, which is a worse lie than the one being fixed.
    try:
        from app.face_recognition import cuda_dlls

        cuda_dlls.enable()
    except Exception:
        logger.info("CUDA DLL bootstrap unavailable during provider probe", exc_info=True)

    import onnxruntime

    # TensorRT -> CUDA -> CPU, then anything else onnxruntime advertised.
    candidates = [p for p in _PROVIDER_PRIORITY if p in advertised]
    candidates += [p for p in advertised if p not in _PROVIDER_PRIORITY]
    tensorrt_skipped = False
    if _TENSORRT_PROVIDER in candidates and not _tensorrt_runtime_present():
        candidates.remove(_TENSORRT_PROVIDER)
        tensorrt_skipped = True

    usable: list[str] = []
    for provider in candidates:
        try:
            options = onnxruntime.SessionOptions()
            options.log_severity_level = 3  # a failing provider is expected here, not an error to shout about
            session = onnxruntime.InferenceSession(model, options, providers=[provider])
            if provider in session.get_providers():
                usable.append(provider)
            else:
                logger.info("Provider %s is advertised but fell back to %s — not usable", provider, session.get_providers())
            del session
        except Exception:
            logger.info("Provider %s advertised but failed to initialise", provider, exc_info=True)
    _usable_cache = usable
    if tensorrt_skipped:
        if "CUDAExecutionProvider" in usable:
            logger.info("TensorRT unavailable: required runtime DLL missing; using CUDAExecutionProvider")
        else:
            logger.info("TensorRT and CUDA unavailable; using CPUExecutionProvider")
    return usable


def verified_providers() -> list[str]:
    """The providers this process may actually use, best first. Verified once
    (a real session has to initialise) and cached, so the first Event batch
    neither pays for provider probing nor prints it."""
    return list(_verified_providers(_available_providers()))


def _gpus_via_nvidia_smi() -> list[GpuInfo]:
    """Best-effort only — an empty list here does not mean CUDA is
    unavailable (onnxruntime's own provider list is the authority on that),
    only that per-GPU name/VRAM detail could not be read."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,name,memory.total,memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except Exception:
        return []
    gpus: list[GpuInfo] = []
    for line in result.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            index, name, total_mb, free_mb = parts
            gpus.append(GpuInfo(index=int(index), name=name, total_vram_mb=int(total_mb), free_vram_mb=int(free_mb)))
        except ValueError:
            continue
    return gpus


def get_hardware_info() -> HardwareInfo:
    cpu_cores, total_ram_mb, free_ram_mb = _cpu_and_ram()
    available_providers = _available_providers()
    usable_providers = _verified_providers(available_providers)
    cuda_available = "CUDAExecutionProvider" in usable_providers
    gpus = _gpus_via_nvidia_smi() if cuda_available else []
    return HardwareInfo(
        cpu_cores=cpu_cores,
        total_ram_mb=total_ram_mb,
        free_ram_mb=free_ram_mb,
        available_providers=available_providers,
        usable_providers=usable_providers,
        cuda_available=cuda_available,
        gpus=gpus,
    )


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
