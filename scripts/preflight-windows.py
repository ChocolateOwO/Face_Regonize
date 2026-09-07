"""Read-only environment check. Never opens project DB or storage."""
from __future__ import annotations
import argparse, subprocess, sys
from pathlib import Path
parser = argparse.ArgumentParser(); parser.add_argument('--require-gpu', action='store_true'); args = parser.parse_args()
print('Python:', sys.version.split()[0], '64-bit' if sys.maxsize > 2**32 else '32-bit')
try: print('NVIDIA:', subprocess.check_output(['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'], text=True).strip())
except Exception: print('NVIDIA: not detected')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'backend'))
from app.face_recognition import cuda_dlls
cuda_dlls.enable()
import onnxruntime as ort
from insightface.app import FaceAnalysis
print('ONNX Runtime:', ort.__version__); print('Available providers:', ort.get_available_providers())
print('CUDA DLLs preloaded:', cuda_dlls.status()['preloaded_count'])
model_dir = Path.home() / '.insightface' / 'models' / 'buffalo_l'
print('buffalo_l cache:', model_dir if model_dir.is_dir() else 'not downloaded; first startup needs internet')
try:
    app = FaceAnalysis(name='buffalo_l', providers=['CUDAExecutionProvider','CPUExecutionProvider'], allowed_modules=['detection','recognition'])
    app.prepare(ctx_id=0, det_size=(320, 320)); active = app.models['recognition'].session.get_providers()
    print('Actual InsightFace provider:', active[0] if active else 'unknown'); print('CPU fallback available:', 'CPUExecutionProvider' in active)
    if args.require_gpu and active[0] != 'CUDAExecutionProvider': raise SystemExit('GPU requested but CUDA session did not start.')
except Exception:
    if args.require_gpu: raise
    print('CPU path: CUDA unavailable or model download failed; recognition remains CPU when model is available.')
