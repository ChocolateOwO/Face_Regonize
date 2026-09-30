"""Video-only sessions. Never change Event/kiosk models or provider settings."""
from __future__ import annotations
import threading
import time
from typing import Literal
import numpy as np
from app.services.scan_settings import ScanSettings

Device = Literal['auto', 'cpu', 'cuda', 'tensorrt']
PROVIDERS = dict(cpu='CPUExecutionProvider', cuda='CUDAExecutionProvider', tensorrt='TensorrtExecutionProvider')

class VideoSettings(ScanSettings):
    device: Device = 'auto'

class DeviceError(ValueError):
    pass

_lock = threading.RLock()
_sessions = {}
_in_flight = 0  # video inference calls currently holding the shared engine lock

def in_flight() -> int:
    return _in_flight
# One inference in flight across video sessions AND the existing shared engine.
# Extra video workers overlap download/decode/checkpoints, not GPU submissions.

def session(device: str):
    with _lock:
        if device in _sessions:
            return _sessions[device]
        if device not in PROVIDERS:
            raise DeviceError('Choose Auto, CPU, or a verified GPU provider.')
        from app.face_recognition import engine
        from app.services.hardware_info_service import verified_providers
        provider = PROVIDERS[device]
        if provider not in verified_providers():
            raise DeviceError(f'{provider} is unavailable. Choose Auto/CPU or repair its runtime; no CPU substitution was made.')
        try:
            app = engine.FaceAnalysis(name=engine.MODEL_NAME, providers=[provider] if device == 'cpu' else [provider, 'CPUExecutionProvider'],
                                     allowed_modules=['detection', 'recognition'])
            app.prepare(ctx_id=-1 if device == 'cpu' else 0, det_size=(320, 320))
            for model in (app.det_model, app.models['recognition']):
                model.session.disable_fallback()
                if model.session.get_providers()[0] != provider:
                    raise RuntimeError('Model provider initialization fell back')
            # Exercise BOTH actual models, even when the synthetic image has no face.
            with engine.inference_lock:
                app.det_model.detect(np.zeros((320, 320, 3), np.uint8), max_num=0, metric='default')
                app.models['recognition'].get_feat(np.zeros((112, 112, 3), np.uint8))
            _sessions[device] = app
            return app
        except Exception as exc:
            raise DeviceError(f'{provider} cannot run the video model. Choose Auto/CPU or repair its runtime; no CPU substitution was made.') from exc

def resolve(requested: str) -> str:
    if requested != 'auto':
        session(requested)
        return requested
    for device in ('cuda', 'cpu'):
        try:
            session(device)
            return device
        except DeviceError:
            continue
    raise DeviceError('Neither CUDA nor CPU can run the video model. Check the backend model/runtime installation.')

def capabilities() -> dict:
    options = [dict(value='auto', label='Auto (verified CUDA, otherwise CPU)')]
    errors = {}
    for device in ('cuda', 'tensorrt', 'cpu'):
        try:
            session(device)
            options.append(dict(value=device, label=PROVIDERS[device]))
        except DeviceError as exc:
            errors[device] = str(exc)
    return dict(options=options, unavailable=errors, inference_limit=1,
                fallback='Auto verifies CUDA on both models, then CPU. Explicit devices never substitute CPU.')

def detect(image, device: str, timings: dict):
    from app.face_recognition import engine
    global _in_flight
    app = session(device)
    begin = time.perf_counter()
    with engine.inference_lock:
        _in_flight += 1
        try:
            return _detect_locked(app, image, device, timings, begin)
        finally:
            _in_flight -= 1

def _detect_locked(app, image, device: str, timings: dict, begin: float):
    """Body of detect(); the caller already holds the shared engine lock."""
    from app.face_recognition import engine
    entered = time.perf_counter()
    for model in (app.det_model, app.models['recognition']):
        if model.session.get_providers()[0] != PROVIDERS[device]:
            raise DeviceError('Selected model provider changed; processing stopped instead of silently switching devices.')
    bboxes, kpss = app.det_model.detect(image, max_num=0, metric='default')
    faces = []
    for i in range(bboxes.shape[0]):
        face = engine.Face(bbox=bboxes[i, :4], kps=kpss[i] if kpss is not None else None, det_score=bboxes[i, 4])
        app.models['recognition'].get(image, face)
        faces.append(engine.DetectedFace(face.normed_embedding.astype(np.float32), tuple(face.bbox.tolist()), float(face.det_score)))
    finished = time.perf_counter()
    timings['inference_wait_seconds'] = timings.get('inference_wait_seconds', 0) + entered - begin
    timings['inference_seconds'] = timings.get('inference_seconds', 0) + finished - entered
    return faces
