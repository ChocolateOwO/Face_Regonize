"""Persistent, bounded video-source scheduler. No Event/Drive destination writes.

Source checkpoints replace previous totals. Recovery never replays a started
source: its partial results become interrupted, while untouched pending sources
continue. A frozen private enrollment snapshot is reused after restart.
"""
from __future__ import annotations
import copy
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import numpy as np
import psutil
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.services import local_video_experiment as video, video_devices as devices
from app.services.scan_settings import ScanSettings

logger = logging.getLogger(__name__)
MAX_PENDING = 32
DEFAULT_WORKERS = 2

def worker_ceiling() -> int:
    """Highest accepted worker limit: one logical CPU per worker. Each worker
    decodes on the CPU; model inference stays one call at a time regardless,
    and RAM/disk are checked again at every admission."""
    return max(1, os.cpu_count() or 1)

class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    max_workers: int = Field(default=DEFAULT_WORKERS, ge=1, strict=True)

    @field_validator('max_workers')
    @classmethod
    def _within_ceiling(cls, value: int) -> int:
        if value > worker_ceiling():
            raise ValueError(f'At most {worker_ceiling()} video workers on this machine (one per logical CPU).')
        return value

_thread = None
_wake = threading.Event()
_stop = threading.Event()
_workers = {}  # (job_id, source_key) -> thread
_stages = {}   # (job_id, source_key) -> 'downloading' | 'processing'
_tokens = {}
_indices = {}
# job_id -> {person_id: saved preview}. Mirrors the batch's persisted previews
# (this process is their only writer) so a weaker match costs no state I/O.
_previews = {}
_sequence = 0
_gpu_cache = (0.0, None)

def gpu_usage():
    """Machine-wide telemetry, never claimed to belong exclusively to a batch."""
    global _gpu_cache
    if time.monotonic() - _gpu_cache[0] < 5:
        return _gpu_cache[1]
    value = None
    try:
        result = subprocess.run(['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'],
                                capture_output=True, text=True, timeout=2, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        utilization, used, total = map(float, result.stdout.splitlines()[0].split(','))
        value = dict(utilization_percent=utilization, used_vram_mib=used, total_vram_mib=total)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    _gpu_cache = (time.monotonic(), value)
    return value

def config():
    path = video.ROOT / 'scheduler.json'
    if path.is_symlink():
        raise video.MediaError('Scheduler configuration is unavailable.')
    if path.exists():
        raw = json.loads(path.read_text('utf-8'))
        # A limit saved on a machine with more CPUs is clamped, never rejected.
        if isinstance(raw.get('max_workers'), int) and raw['max_workers'] > worker_ceiling():
            raw['max_workers'] = worker_ceiling()
        return WorkerSettings.model_validate(raw).model_dump()
    return WorkerSettings().model_dump()

def configure(settings: WorkerSettings):
    with video._lock:
        video.ROOT.mkdir(parents=True, exist_ok=True)
        path = video.ROOT / 'scheduler.json'
        if path.is_symlink() or video.ROOT.is_symlink():
            raise video.MediaError('Scheduler configuration is unavailable.')
        temp = path.with_suffix('.writing')
        temp.write_text(settings.model_dump_json(), 'utf-8')
        os.replace(temp, path)
        _wake.set()
        return config()

def telemetry():
    """Queued batches, per-stage workers and GPU inference are separate numbers:
    workers are CPU/IO slots (download, decode, checkpoint); inference is the
    single shared GPU call that workers take turns on."""
    gpu = gpu_usage()
    with video._lock:
        queued_batches = queued_sources = 0
        for state in video.list_jobs():
            if not state.get('scheduler_version') or state['status'] not in {'queued', 'running'}:
                continue
            waiting = sum(s['status'] in {'pending', 'queued'} for s in state.get('videos', [state]))
            queued_sources += waiting
            if waiting and not any(k[0] == state['id'] for k in _workers):
                queued_batches += 1
        stages = list(_stages.values())
        return {**config(), 'max_workers_limit': worker_ceiling(), 'default_workers': DEFAULT_WORKERS,
                'active_workers': len(_workers), 'downloading_workers': stages.count('downloading'),
                'processing_workers': stages.count('processing'), 'queued_sources': queued_sources,
                'queued_batches': queued_batches, 'inference_limit': 1, 'inference_in_flight': devices.in_flight(),
                'pending_limit': MAX_PENDING, 'reserved_download_bytes': video.claimed_disk_bytes(),
                'disk_reserve_bytes': video.DISK_RESERVE,
                'gpu': gpu, 'available_ram_bytes': psutil.virtual_memory().available,
                'free_disk_bytes': shutil.disk_usage(video.ROOT if video.ROOT.exists() else video.ROOT.parent).free}

def download_path(job_id: str, source: dict) -> Path:
    from app.services import drive_video_batch as batch
    return batch.downloads_dir(job_id) / source['source_video_key'] / ('source' + Path(source['filename']).suffix.lower())

def claim_key(job_id: str, source: dict):
    # Same key drive.download uses by default, so the admission claim and the
    # transfer's own claim are one reservation, never counted twice.
    return ('download', str(download_path(job_id, source)))

def freeze(job_id):
    index, model = video.identity_snapshot()
    # Same frozen IDs/embeddings as the live matcher; private experiment file.
    with index._lock:
        payload = dict(ids=index._ids, first=index._first_names, full=index._full_names,
                       participant=index._participant_ids, embeddings=index._matrix.tolist())
    path = video.directory(job_id) / 'enrollment.json'
    path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), 'utf-8')
    return index, model

def load_index(job_id):
    path = video.directory(job_id) / 'enrollment.json'
    if path.is_symlink():
        raise video.MediaError('Frozen enrollment snapshot is unavailable.')
    payload = json.loads(path.read_text('utf-8'))
    index = video.RecognitionIndex()
    index.rebuild([SimpleNamespace(id=key, first_name=payload['full'][i], last_name='',
        participant_id=payload['participant'][i], embedding=np.asarray(payload['embeddings'][i], np.float32).tobytes())
        for i, key in enumerate(payload['ids'])])
    return index

def enqueue(job_id, settings, requested='auto'):
    video.initialize()
    actual = devices.resolve(requested)
    with video._lock:
        state = video._read(job_id)
        if state['status'] != 'ready':
            raise video.Busy('Only a ready upload can start. This experiment already has a run.')
        if sum(s['status'] in video.ACTIVE for s in video.list_jobs()) >= MAX_PENDING:
            raise video.Busy('Video queue is full (32 batches). Wait for a batch to finish.')
        try:
            index, model = freeze(job_id)
        except Exception:
            logger.exception('Could not freeze video enrollment')
            state.update(status='failed', finished_at=video.now(), error='Could not freeze enrollment/model configuration. Check backend model and database availability; no video was processed.')
            for source in state.get('videos', []):
                source.update(status='not_processed', error='Enrollment snapshot could not be prepared.')
            return video._write(state)
        model.update(provider=devices.PROVIDERS[actual], detector_provider=devices.PROVIDERS[actual])
        state.update(status='queued', scheduler_version=1, queued_at=video.now(), last_dispatched=0,
            requested_device=requested, actual_device=actual, requested_provider='Auto' if requested == 'auto' else devices.PROVIDERS[requested],
            actual_provider=devices.PROVIDERS[actual], model=model, scan_settings=settings.model_dump(), preview_version=1,
            worker_limit_at_submission=config()['max_workers'], post_scan_delay_seconds=settings.post_scan_delay_seconds)
        _indices[job_id] = index
        _tokens[job_id] = threading.Event()
        if state.get('kind') != 'drive_batch':
            state['media'].update(expected_samples=None,
                max_samples=video.maximum_sample_count(state['media']['frame_count'], state['media']['fps'], settings),
                virtual_capture_fps_limit=settings.camera_fps,
                effective_available_fps_limit=min(settings.camera_fps, state['media']['fps']))
        video._write(state)
    launch()
    _wake.set()
    return state

def recover(state):
    from app.services import drive_video_batch as batch
    if state.get('kind') == 'drive_batch':
        try:
            batch.cleanup(state['id'])
            state['temporary_downloads_cleaned'] = True
        except Exception:
            state.update(status='failed', error='Restart cleanup failed. Check storage permissions before deleting this stopped experiment.', temporary_downloads_cleaned=False)
            video._write(state)
            return
    if state['status'] not in video.ACTIVE:
        return
    cancelling = state['status'] == 'cancelling'
    if state.get('kind') == 'drive_batch':
        for source in state['videos']:
            if source['status'] in {'downloading', 'running'}:
                source.update(status='interrupted', finished_at=video.now(), error='Backend stopped during this source. Partial results kept; this source will not be downloaded or counted again automatically.')
            elif cancelling and source['status'] == 'pending':
                source.update(status='not_processed', error='Cancelled before processing.')
        batch.refresh_totals(state)
        state['status'] = 'cancelled' if cancelling else 'queued'
        finish_if_done(state)
    elif state['status'] == 'queued' and not state.get('started_at'):
        pass
    else:
        state.update(status='cancelled' if cancelling else 'interrupted', finished_at=video.now(),
                     error='Backend stopped; partial results retained. No automatic replay of this video.')
    video._write(state)

def finish_if_done(state):
    if state.get('kind') != 'drive_batch':
        return
    sources = state['videos']
    if any(s['status'] in {'pending', 'downloading', 'running'} for s in sources):
        return
    errors = sum(s['status'] in {'failed', 'interrupted'} for s in sources)
    completed = sum(s['status'] == 'completed' for s in sources)
    state['status'] = ('cancelled' if state['status'] == 'cancelling' or state['status'] == 'cancelled' else
                       'completed_with_errors' if errors and completed else 'failed' if errors else 'completed')
    if errors:
        state['error'] = f'{errors} of {len(sources)} videos failed or were interrupted. Completed and partial results retained; no automatic retry.'
    state['finished_at'] = video.now()

def cancel(job_id):
    with video._lock:
        state = video.get(job_id)
        if state['status'] not in video.ACTIVE:
            return state
        _tokens.setdefault(job_id, threading.Event()).set()
        state['status'] = 'cancelling'
        for source in state.get('videos', []):
            if source['status'] == 'pending':
                source.update(status='not_processed', error='Cancelled before processing.')
        if not any(key[0] == job_id for key in _workers):
            state.update(status='cancelled', finished_at=video.now())
            _indices.pop(job_id, None)
            _tokens.pop(job_id, None)
        video._write(state)
        _wake.set()
        return state

def launch():
    global _thread
    with video._lock:
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=dispatch, name='video-scheduler', daemon=True)
            _thread.start()

def shutdown(timeout=15):
    _stop.set(); _wake.set()
    if _thread:
        _thread.join(timeout)
    deadline = time.monotonic() + timeout
    while _workers and time.monotonic() < deadline:
        time.sleep(.02)

def dispatch():
    while not _stop.is_set():
        try:
            tick()
        except Exception:
            logger.exception('Video queue dispatch failed; persisted jobs retained')
        _wake.wait(.25); _wake.clear()

def tick():
    global _sequence
    with video._lock:
        for _ in range(config()['max_workers'] - len(_workers)):
            jobs = [s for s in video.list_jobs() if s.get('scheduler_version') and s['status'] in {'queued', 'running'}]
            jobs.sort(key=lambda s: (s.get('last_dispatched', 0), s['queued_at'], s['id']))
            admitted = False
            for state in jobs:
                pending = [s for s in state.get('videos', [state]) if s['status'] == ('pending' if state.get('kind') == 'drive_batch' else 'queued')]
                if not pending:
                    finish_if_done(state)
                    video._write(state)
                    continue
                source = pending[0]
                key = (state['id'], source.get('source_video_key', 'local'))
                if key in _workers:
                    continue
                size = int(source.get('source_file', {}).get('bytes', 0) or 0)
                if psutil.virtual_memory().available < 1024**3:
                    state['waiting_reason'] = 'Waiting for at least 1 GiB available RAM.'
                    video._write(state); continue
                claim = claim_key(state['id'], source) if state.get('kind') == 'drive_batch' else None
                if claim is not None:
                    try:
                        video.claim_disk(claim, size, source['filename'], video.ROOT)
                    except video.MediaError as exc:
                        if not _workers and not video.claimed_disk_bytes():
                            # Nothing else will free space: fail this file clearly.
                            source.update(status='failed', error=str(exc))
                            if state.get('kind') == 'drive_batch': finish_if_done(state)
                        else:
                            state['waiting_reason'] = f'{source["filename"]}: waiting for disk space held by other active videos. {exc}'
                        video._write(state)
                        continue
                _sequence = max(_sequence, *(s.get('last_dispatched', 0) for s in jobs)) + 1
                state.update(status='running', started_at=state.get('started_at') or video.now(), last_dispatched=_sequence, waiting_reason=None)
                source.update(status='downloading' if state.get('kind') == 'drive_batch' else 'running', started_at=video.now(), model=copy.deepcopy(state['model']))
                video._write(state)
                token = _tokens.setdefault(state['id'], threading.Event())
                worker = threading.Thread(target=run_source, args=(key, copy.deepcopy(source), token), name=f'video-{state["id"][:6]}-{key[1]}', daemon=True)
                _workers[key] = worker
                _stages[key] = 'downloading' if claim is not None else 'processing'
                try: worker.start()
                except Exception:
                    _workers.pop(key); _stages.pop(key, None)
                    if claim is not None: video.release_disk(claim)
                    source.update(status='failed', error='Could not start video worker.')
                    finish_if_done(state); video._write(state)
                    raise
                admitted = True
                break
            if not admitted:
                break

def run_source(key, source, token):
    from app.services import drive_video_batch as batch, drive_video_source as drive
    job_id, source_key = key
    clock = time.perf_counter()
    parent = video.get(job_id)
    is_batch = parent.get('kind') == 'drive_batch'
    folder = batch.downloads_dir(job_id) / source_key if is_batch else None
    base_seconds = source.get('processing_seconds', 0)
    source['stage_seconds'] = {}
    stages = source['stage_seconds']
    cancelled = lambda: token.is_set() or _stop.is_set()
    def publish():
        source['processing_seconds'] = base_seconds + time.perf_counter() - clock
        with video._lock:
            state = video._read(job_id)
            if is_batch:
                state['videos'] = [copy.deepcopy(source) if s['source_video_key'] == source_key else s for s in state['videos']]
                batch.refresh_totals(state)
                state['stage_seconds'] = {name: sum(s.get('stage_seconds', {}).get(name, 0) for s in state['videos'])
                                         for name in {k for s in state['videos'] for k in s.get('stage_seconds', {})}}
                state['processing_seconds'] = max(0, (video.datetime.now(video.timezone.utc) - video.datetime.fromisoformat(state['started_at'])).total_seconds())
                state['temporary_downloads_cleaned'] = not any(batch.downloads_dir(job_id).rglob('source.*')) if batch.downloads_dir(job_id).exists() else True
                finish_if_done(state)
            else:
                cancelling = state['status'] == 'cancelling'
                state = copy.deepcopy(source)
                if cancelling and state['status'] in video.ACTIVE:
                    state['status'] = 'cancelling'
            video._write(state)
    def representative(context, row, face, match, image, frame_index, timestamp):
        if not is_batch:
            video.save_representative(context, row, face, match, image, frame_index, timestamp)
            return
        with video._lock:
            # Most matched faces are not stronger than the saved example. Test
            # against the batch's current preview first and touch no state when
            # it stands: a publish per matched face rewrote the whole batch
            # state under the global lock, stalling every other worker.
            known = _previews.get(job_id)
            if known is None:
                known = _previews[job_id] = {p['person_id']: p.get('preview') for p in video._read(job_id)['people']}
            person = row['person_id']
            first = person not in known
            if not first:
                probe = dict(person_id=person, preview=known[person])
                video.save_representative(context, probe, face, match, image, frame_index, timestamp)
                if probe['preview'] is known[person]:
                    return
            publish()
            state = video._read(job_id)
            fresh = next(p for p in state['people'] if p['person_id'] == person)
            if first:
                video.save_representative(context, fresh, face, match, image, frame_index, timestamp)
            else:
                fresh['preview'] = probe['preview']
            video._write(state)
            known[person] = fresh.get('preview')
    try:
        index = _indices.get(job_id)
        if index is None:
            index = load_index(job_id)
            _indices[job_id] = index
        # Re-verify the frozen actual provider; Auto cannot switch mid-run.
        devices.session(parent['actual_device'])
        if is_batch:
            with video._lock:
                folder.mkdir(parents=True, exist_ok=True)
            path = folder / ('source' + Path(source['filename']).suffix.lower())
            begin = time.perf_counter()
            def progress(written):
                source['downloaded_bytes'] = written
                publish()
            checksum = drive.download(source['source_file'], parent['folder_id'], parent['drive_account']['account_id'], path, cancelled, progress)
            stages['download_seconds'] = time.perf_counter() - begin
            video.release_disk(claim_key(job_id, source))  # every byte is on disk now
            with video._lock:
                _stages[key] = 'processing'
            if cancelled(): raise drive.DownloadCancelled('Cancelled after download; temporary video removed.')
            begin = time.perf_counter()
            source['media'] = {**video.probe(path, max_duration=drive.MAX_DURATION), **checksum}
            stages['probe_seconds'] = time.perf_counter() - begin
        else:
            path = video.video_path(source)
        settings = ScanSettings.model_validate(parent['scan_settings'])
        source['media'].update(virtual_capture_fps_limit=settings.camera_fps, effective_available_fps_limit=min(settings.camera_fps, source['media']['fps']),
                              max_samples=video.maximum_sample_count(source['media']['frame_count'], source['media']['fps'], settings))
        source.update(status='running', actual_provider=parent['actual_provider'], requested_provider=parent['requested_provider'], scan_settings=parent['scan_settings'])
        publish()
        video.process_video(source, path, index, settings, publish, representative,
            cancelled=cancelled, detector=lambda image: devices.detect(image, parent['actual_device'], stages))
    except Exception as exc:
        logger.exception('Video source stopped: %s/%s', job_id, source_key)
        source.update(status='cancelled' if token.is_set() else 'interrupted' if _stop.is_set() else 'failed',
            error=str(exc) if isinstance(exc, (video.MediaError, drive.DriveVideoError, devices.DeviceError)) else 'Video worker failed. Partial results retained; inspect backend log. No automatic retry.')
    finally:
        try:
            if folder and folder.exists():
                if folder.is_symlink() or folder.resolve().parent != batch.downloads_dir(job_id).resolve():
                    raise video.MediaError('Temporary source directory is unsafe.')
                shutil.rmtree(folder)
                with video._lock:
                    parent_folder = batch.downloads_dir(job_id)
                    if parent_folder.exists() and not any(parent_folder.iterdir()):
                        parent_folder.rmdir()
            if _stop.is_set() and not token.is_set() and source['status'] == 'cancelled':
                source.update(status='interrupted', error='Backend stopped at a safe checkpoint; source is not replayed automatically.')
            source.update(finished_at=video.now(), decode_phase='stopped')
            publish()
        except Exception:
            logger.exception('Video cleanup/checkpoint failed')
            with video._lock:
                state = video._read(job_id)
                token.set()
                source.update(status='failed', error='Temporary cleanup or checkpoint failed. Results retained; check storage permissions.', finished_at=video.now())
                if is_batch:
                    state['videos'] = [copy.deepcopy(source) if s['source_video_key'] == source_key else s for s in state['videos']]
                    for pending in state['videos']:
                        if pending['status'] == 'pending':
                            pending.update(status='not_processed', error='Batch stopped after temporary-storage failure.')
                    batch.refresh_totals(state)
                    state['status'] = 'cancelling'
                    finish_if_done(state)
                else:
                    state = copy.deepcopy(source)
                state.update(temporary_downloads_cleaned=False, error='Temporary cleanup or checkpoint failed. Results retained; check storage permissions.')
                video._write(state)
        finally:
            with video._lock:
                _workers.pop(key, None); _stages.pop(key, None)
                if is_batch:
                    video.release_disk(claim_key(job_id, source))
                state = video._read(job_id)
                if state['status'] not in video.ACTIVE:
                    _indices.pop(job_id, None); _tokens.pop(job_id, None); _previews.pop(job_id, None)
            _wake.set()

def decorate(result, state):
    if not state.get('scheduler_version'):
        return
    with video._lock:
        result['active_workers'] = sum(k[0] == state['id'] for k in _workers)
        queued = sorted((s for s in video.list_jobs() if s.get('scheduler_version') and s['status'] in {'queued', 'running'}),
                        key=lambda s: (s.get('last_dispatched', 0), s['queued_at'], s['id']))
        result['queue_position'] = next((i+1 for i,s in enumerate(queued) if s['id'] == state['id']), None)

def is_working(job_id):
    with video._lock:
        return any(key[0] == job_id for key in _workers)

def csv_metadata(state):
    if not state.get('scheduler_version'):
        return {}
    return dict(requested_provider=state['requested_provider'], actual_provider=state['actual_provider'],
        worker_limit_at_submission=state['worker_limit_at_submission'], inference_limit=1,
        stage_seconds=json.dumps(state.get('stage_seconds', {}), sort_keys=True),
        stage_time_meaning='sum of source worker times, may overlap; inference_wait included in virtual scan work',
        restart_policy='resume untouched pending sources; interrupted sources retain partial counts and are never automatically replayed')
