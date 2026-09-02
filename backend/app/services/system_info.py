from __future__ import annotations

import platform
import subprocess

import insightface
import onnxruntime
import psutil

from app.face_recognition.engine import MODEL_NAME, get_active_provider
from app.face_recognition.index import recognition_index


def _gpu_info() -> str:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,utilization.gpu", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip().splitlines()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return "No NVIDIA GPU detected (nvidia-smi unavailable)"


def get_system_info() -> dict:
    mem = psutil.virtual_memory()
    return {
        "cpu": platform.processor() or platform.machine(),
        "cpu_cores_logical": psutil.cpu_count(logical=True),
        "cpu_cores_physical": psutil.cpu_count(logical=False),
        "cpu_usage_percent": psutil.cpu_percent(interval=0.1),
        "ram_total_gb": round(mem.total / (1024**3), 1),
        "ram_available_gb": round(mem.available / (1024**3), 1),
        "ram_usage_percent": mem.percent,
        "gpu": _gpu_info(),
        "python_version": platform.python_version(),
        "insightface_version": insightface.__version__,
        "onnxruntime_version": onnxruntime.__version__,
        "onnxruntime_available_providers": onnxruntime.get_available_providers(),
        "model_name": MODEL_NAME,
        "inference_device": get_active_provider(),
        "registered_faces_indexed": recognition_index.size(),
    }
