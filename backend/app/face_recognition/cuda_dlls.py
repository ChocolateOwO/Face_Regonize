"""Make the CUDA execution provider's DLL dependencies findable on Windows.

Nothing here has anything to do with recognition semantics — it is purely a
loader fix, kept in its own module so engine.py stays about faces.

THE PROBLEM

onnxruntime-gpu ships `onnxruntime_providers_cuda.dll`, but not the CUDA and
cuDNN runtimes it links against. Those arrive as separate wheels that install
under `site-packages/nvidia/...`, a location no Windows DLL search path knows
about. So the provider DLL fails to load with

    Error loading "...onnxruntime_providers_cuda.dll" which depends on
    "cublasLt64_13.dll" which is missing. (Error 126)

and ONNX Runtime silently falls back to CPU, even though
`get_available_providers()` still advertises CUDAExecutionProvider — that list
reflects how the package was built, not what can actually load.

WHY os.add_dll_directory() ALONE IS NOT ENOUGH

`os.add_dll_directory()` calls `AddDllDirectory`, which only affects loads that
opt into the user-directory search. ONNX Runtime loads its provider DLL by
absolute path, and Windows then resolves *that DLL's* dependencies using the
standard search order, which does not include the directories added this way.
Verified on this machine: after add_dll_directory, `ctypes.CDLL` could load
cublasLt64_13.dll by name, yet the CUDA provider still failed with Error 126.

WHAT ACTUALLY WORKS

Load the CUDA/cuDNN DLLs into the process ourselves, by absolute path, before
ONNX Runtime needs them. Once a module is resident, Windows satisfies a
dependency of the same name from the loaded-module list instead of searching
the disk, so the provider DLL then loads. `add_dll_directory` is still done
first, because it is what lets each of these DLLs find its own siblings.

This is process-local and temporary: no change to the Windows PATH, no files
copied or moved, nothing installed.
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import sysconfig
from pathlib import Path

logger = logging.getLogger(__name__)

# add_dll_directory() returns handles that remove the directory again when they
# are garbage-collected, so they are kept for the lifetime of the process.
_dll_dir_handles: list[object] = []

_registered_dirs: list[str] = []
_preloaded: list[str] = []
_done = False


def nvidia_dll_dirs() -> list[Path]:
    """Every directory holding a DLL inside the installed `nvidia` packages.

    Discovered from the *active* interpreter's site-packages, so this follows
    the virtualenv rather than assuming any particular project location. It
    also finds cuDNN, not just the CUDA runtime — both are needed, and which
    subdirectories exist depends on which wheels are installed.
    """
    root = Path(sysconfig.get_paths()["purelib"]) / "nvidia"
    if not root.is_dir():
        return []
    return sorted({p.parent for p in root.rglob("*.dll")})


def enable() -> bool:
    """Best-effort. Returns True if the CUDA runtime DLLs are now resident.

    Never raises: a machine with no GPU, or no nvidia wheels, must still start
    the backend normally on CPU.
    """
    global _done
    if _done:
        return bool(_preloaded)
    _done = True

    if sys.platform != "win32":
        return False

    dirs = nvidia_dll_dirs()
    if not dirs:
        logger.info("No bundled NVIDIA DLL directories found — CUDA provider will be unavailable.")
        return False

    for d in dirs:
        try:
            _dll_dir_handles.append(os.add_dll_directory(str(d)))
            _registered_dirs.append(str(d))
        except OSError as e:  # noqa: PERF203 — one bad directory must not stop the rest
            logger.warning("Could not register DLL directory %s: %s", d, e)

    # Several passes: these DLLs depend on each other and the right order is
    # not documented, so anything that fails is simply retried once its
    # dependencies have been loaded by an earlier pass.
    pending = [p for d in dirs for p in sorted(d.glob("*.dll"))]
    for _ in range(4):
        if not pending:
            break
        retry = []
        for dll in pending:
            try:
                ctypes.WinDLL(str(dll))
                _preloaded.append(dll.name)
            except OSError:
                retry.append(dll)
        if len(retry) == len(pending):
            break  # no progress — the rest are genuinely unloadable
        pending = retry

    if pending:
        logger.warning("Some CUDA DLLs could not be preloaded: %s",
                       ", ".join(sorted(p.name for p in pending)))
    logger.info("CUDA runtime: registered %d DLL director%s, preloaded %d DLL(s).",
                len(_registered_dirs), "y" if len(_registered_dirs) == 1 else "ies", len(_preloaded))
    return bool(_preloaded)


def status() -> dict:
    """For diagnostics — what this module actually managed to do."""
    return {
        "registered_dirs": list(_registered_dirs),
        "preloaded_count": len(_preloaded),
        "preloaded": list(_preloaded),
    }
