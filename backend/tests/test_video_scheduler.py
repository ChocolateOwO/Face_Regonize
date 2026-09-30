"""Concurrent real scheduler with mocked Drive and synthetic video only."""
import copy
import csv
import io
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import test_drive_video_batch as fixture
from test_drive_video_batch import PARENT, ACCOUNT
from app.services import video_scheduler as scheduler, video_devices as devices
from app.services import local_video_experiment as video, drive_video_batch as batch, drive_video_source as drive
from app.services.scan_settings import ScanSettings

REAL_RESOLVE = devices.resolve
REAL_SESSION = devices.session
REAL_CONFIG = scheduler.config

class SchedulerTests(unittest.TestCase):
    # Reuse fixture helpers, not inherited test cases.
    for _name, _method in fixture.DriveBatchTests.__dict__.items():
        if callable(_method) and not _name.startswith('test_'):
            locals()[_name] = _method

    def submit(self, name='Synthetic queue', keys=None):
        return batch.create(name, PARENT, keys or list(self.items), ACCOUNT['account_id'], ScanSettings())

    def wait_jobs(self, *jobs):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            states = [video.get(s['id']) for s in jobs]
            if all(s['status'] not in video.ACTIVE for s in states) and not scheduler._workers:
                return states
            time.sleep(.01)
        self.fail('Synthetic queue did not drain')

    def simple_download(self, item, parent, account, path, cancelled, progress):
        self.downloads.append(item['file_id'])
        data = self.payloads[item['file_id']]
        path.write_bytes(data); progress(len(data))
        return {'bytes': len(data), 'sha256': 'synthetic'}

    def test_two_batches_seven_sources_bound_and_order_independent_preview(self):
        maximum = 0
        entered, release = threading.Event(), threading.Event()
        def download(*args):
            nonlocal maximum
            maximum = max(maximum, len(scheduler._workers))
            if maximum == 2: entered.set()
            self.assertTrue(release.wait(5))
            # Reverse completion order: cam02 finishes before cam01.
            if args[0]['filename'] == 'cam01.avi': time.sleep(.06)
            return self.simple_download(*args)
        with patch.object(scheduler, 'config', lambda: {'max_workers': 2}), patch.object(drive, 'download', download):
            first = self.submit('First seven')
            second = self.submit('Second seven')
            self.assertTrue(entered.wait(5))
            self.assertGreaterEqual(video.public(video.get(second['id']))['queue_position'], 1)
            release.set()
            states = self.wait_jobs(first, second)
        self.assertEqual(maximum, 2)
        self.assertEqual(len(self.downloads), 14)
        for state in states:
            self.assertEqual(state['status'], 'completed')
            self.assertEqual(state['sampled_frames'], 21)
            self.assertEqual([p['detection_count'] for p in state['people']], [21,21])
            beta = next(p for p in state['people'] if p['person_id'] == 'person-b')
            self.assertEqual((beta['preview']['source_filename'], beta['preview']['frame_number']), ('cam01.avi',1))
            self.assertEqual(len(list((video.directory(state['id'])/'previews').iterdir())), 2)
            self.clean(state)
            rows = list(csv.DictReader(io.StringIO(video.to_csv(state))))
            metadata = {r['key']:r['value'] for r in rows if r['record_type']=='metadata'}
            self.assertEqual(metadata['requested_provider'], 'Auto')
            self.assertEqual(metadata['actual_provider'], 'FakeProvider')
            self.assertEqual(len([r for r in rows if r['record_type']=='video']), 7)

    def test_cancel_one_batch_does_not_cancel_other_and_no_duplicate_retry(self):
        entered, release = threading.Event(), threading.Event()
        def download(*args):
            entered.set(); self.assertTrue(release.wait(5))
            if args[4](): raise drive.DownloadCancelled('Synthetic cancellation')
            return self.simple_download(*args)
        with patch.object(drive, 'download', download):
            first = self.submit('Cancel this')
            self.assertTrue(entered.wait(5))
            second = self.submit('Keep this')
            video.cancel(first['id']); release.set()
            cancelled, complete = self.wait_jobs(first, second)
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertEqual(complete['status'], 'completed')
        self.assertEqual(complete['sampled_frames'], 21)
        with self.assertRaises(video.Busy): video.start(complete['id'])
        self.clean(cancelled); self.clean(complete)

    def test_restart_pending_survives_started_source_not_replayed(self):
        with patch.object(scheduler, 'launch', lambda: None): state = self.submit()
        state['status'] = 'running'
        state['videos'][0].update(status='completed', sampled_frames=3)
        state['videos'][1].update(status='running', sampled_frames=1)
        video._write(state)
        folder = batch.downloads_dir(state['id']); folder.mkdir(); (folder/'partial').write_bytes(b'partial')
        scheduler._indices.clear()
        with patch.object(scheduler, 'load_index', lambda job_id: self.matcher()), patch.object(drive, 'download', self.simple_download):
            video._initialized = False; video.initialize()
            recovered = video.get(state['id'])
            self.assertEqual(recovered['status'], 'queued')
            self.assertEqual(recovered['videos'][1]['status'], 'interrupted')
            self.assertFalse(folder.exists())
            scheduler.launch()
            finished, = self.wait_jobs(state)
        self.assertEqual(self.downloads, [f'video_{i:05d}' for i in range(3,8)])
        self.assertEqual(finished['sampled_frames'],19)
        self.assertEqual(finished['status'],'completed_with_errors')
        before = copy.deepcopy(finished)
        scheduler.shutdown(); video._initialized=False; video.initialize()
        self.assertEqual(video.get(state['id']),before)
        self.clean(finished)

    def test_cancel_queued_batch_releases_cached_snapshot_without_download(self):
        with patch.object(scheduler, 'launch', lambda: None): state = self.submit()
        self.assertIn(state['id'], scheduler._indices)
        cancelled = video.cancel(state['id'])
        self.assertEqual(cancelled['status'], 'cancelled')
        self.assertNotIn(state['id'], scheduler._indices)
        self.assertNotIn(state['id'], scheduler._tokens)
        self.assertEqual(self.downloads, [])
        self.clean(cancelled)

    def test_cleanup_failure_stops_own_pending_sources_and_retains_partial_counts(self):
        actual = scheduler.shutil.rmtree
        failed = False
        def cleanup(path, *args, **kwargs):
            nonlocal failed
            if str(path).endswith('video-001') and not failed:
                failed = True
                raise PermissionError('Synthetic locked source')
            return actual(path, *args, **kwargs)
        with patch.object(scheduler.shutil, 'rmtree', cleanup), patch.object(drive,'download',self.simple_download):
            state, = self.wait_jobs(self.submit())
        self.assertTrue(failed)
        self.assertEqual(state['videos'][0]['status'],'failed')
        self.assertTrue(all(s['status']=='not_processed' for s in state['videos'][1:]))
        self.assertEqual(state['sampled_frames'],3)
        self.assertFalse(state['temporary_downloads_cleaned'])
        self.assertIn('permissions',state['error'])
        batch.cleanup(state['id'])

    def test_device_failure_auto_fallback_and_worker_validation_admin_only(self):
        def available(device):
            if device != 'cpu': raise devices.DeviceError('Synthetic GPU runtime missing')
        with patch.object(devices, 'session', available):
            self.assertEqual(REAL_RESOLVE('auto'), 'cpu')
            with self.assertRaisesRegex(devices.DeviceError, 'missing'): REAL_RESOLVE('cuda')
        with patch.object(devices, 'resolve', side_effect=devices.DeviceError('CUDA unavailable; choose CPU')):
            status, raw, _ = self.call('/drive/batches','POST',dict(name='Unavailable device', folder_link=PARENT, file_ids=['video_00001'], account_id=ACCOUNT['account_id'], device='cuda'))
            self.assertEqual(status,400)
            self.assertIn(b'choose CPU',raw)
            self.assertEqual(self.downloads,[])
        ceiling = scheduler.worker_ceiling()
        for value in (0,ceiling + 1,True,1.5,'2'):
            self.assertEqual(self.call('/execution','POST',{'max_workers':value})[0],422)
        from app.auth.deps import get_current_user
        from types import SimpleNamespace
        self.app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(role='user')
        self.assertEqual(self.call('/execution')[0],403)
        self.assertEqual(self.call('/execution','POST',{'max_workers':2})[0],403)

    def test_fair_queue_rotates_batches_at_source_boundaries(self):
        order=[]
        def download(item,parent,account,path,cancelled,progress):
            order.append(path.parent.parent.parent.name)
            return self.simple_download(item,parent,account,path,cancelled,progress)
        with patch.object(scheduler, 'launch', lambda: None):
            a=self.submit('A'); b=self.submit('B')
        with patch.object(drive,'download',download):
            scheduler.launch(); self.wait_jobs(a,b)
        self.assertNotEqual(order[0],order[1])
        self.assertEqual(order[::2],[order[0]]*7)
        self.assertEqual(order[1::2],[order[1]]*7)

    def test_worker_limit_above_four_is_one_global_bound_across_batches(self):
        limit = min(6, scheduler.worker_ceiling())
        maximum, gate = 0, threading.Lock()
        full, release = threading.Event(), threading.Event()
        def download(*args):
            nonlocal maximum
            with gate:
                maximum = max(maximum, len(scheduler._workers))
                if len(scheduler._workers) >= limit: full.set()
            self.assertTrue(release.wait(10))
            return self.simple_download(*args)
        with patch.object(scheduler, 'config', lambda: {'max_workers': limit}), patch.object(drive, 'download', download):
            jobs = [self.submit(f'Batch {i}') for i in range(3)]
            self.assertTrue(full.wait(10))
            seen = scheduler.telemetry()
            release.set()
            states = self.wait_jobs(*jobs)
        self.assertEqual(maximum, limit, 'never more workers than the limit, even with 21 queued sources')
        self.assertEqual((seen['active_workers'], seen['downloading_workers'], seen['processing_workers']), (limit, limit, 0))
        self.assertEqual(seen['queued_sources'], 21 - limit)
        self.assertEqual((seen['inference_in_flight'], seen['inference_limit']), (0, 1))
        self.assertEqual(seen['max_workers_limit'], scheduler.worker_ceiling())
        for state in states:
            self.assertEqual(state['status'], 'completed')
            self.assertEqual([p['detection_count'] for p in state['people']], [21, 21])
            beta = next(p for p in state['people'] if p['person_id'] == 'person-b')
            self.assertEqual((beta['preview']['source_filename'], beta['preview']['frame_number']), ('cam01.avi', 1))
            rows = list(csv.DictReader(io.StringIO(video.to_csv(state))))
            self.assertEqual(len([r for r in rows if r['record_type'] == 'video']), 7)
            self.clean(state)
        self.assertEqual(video.claimed_disk_bytes(), 0)

    def test_saved_worker_limit_above_this_machine_is_clamped(self):
        video.ROOT.mkdir(parents=True, exist_ok=True)
        (video.ROOT / 'scheduler.json').write_text('{"max_workers": 999}', 'utf-8')
        self.assertEqual(REAL_CONFIG()['max_workers'], scheduler.worker_ceiling())
        self.assertEqual(scheduler.WorkerSettings(max_workers=scheduler.worker_ceiling()).max_workers, scheduler.worker_ceiling())

    def test_files_over_16_gib_reserve_disk_and_wait_for_space_held_by_others(self):
        big = 20 * 1024**3  # metadata only: the synthetic payload stays tiny
        for key in ('video_00001', 'video_00002'):
            self.items[key]['size'] = str(big)
        free = video.DISK_RESERVE + big + big // 2  # room for one, not both
        entered, release, order = threading.Event(), threading.Event(), []
        def download(item, parent, account, path, cancelled, progress):
            order.append(item['filename']); entered.set()
            self.assertTrue(release.wait(10))
            return self.simple_download(item, parent, account, path, cancelled, progress)
        with patch.object(video.shutil, 'disk_usage', lambda path: SimpleNamespace(free=free)), \
                patch.object(scheduler, 'config', lambda: {'max_workers': 2}), patch.object(drive, 'download', download):
            job = self.submit('Two large cameras', ['video_00001', 'video_00002'])
            self.assertTrue(entered.wait(10))
            time.sleep(.8)  # several dispatch ticks
            waiting = video.get(job['id'])
            self.assertEqual(len(scheduler._workers), 1, 'second 20 GiB file must wait for disk')
            self.assertIn('cam02.avi: waiting for disk space', waiting['waiting_reason'])
            self.assertEqual(video.claimed_disk_bytes(), big)
            release.set()
            state, = self.wait_jobs(job)
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(order, ['cam01.avi', 'cam02.avi'])
        self.assertEqual([v['source_file']['bytes'] for v in state['videos']], [big, big])
        self.assertEqual(video.claimed_disk_bytes(), 0)
        self.clean(state)

    def test_download_rechecks_budget_during_transfer_and_releases_its_claim(self):
        item = drive.validate_video(self.items['video_00001'], PARENT)
        path = video.ROOT / ('0' * 32) / 'downloads' / 'video-001' / 'source.avi'
        path.parent.mkdir(parents=True)
        calls = []
        def usage(where):
            calls.append(where)  # first check passes; space then drops mid-transfer
            return SimpleNamespace(free=video.DISK_RESERVE + item['bytes'] + (2**40 if len(calls) == 1 else -1))
        with patch.object(video.shutil, 'disk_usage', usage):
            with self.assertRaisesRegex(drive.DriveVideoError, 'Not enough free disk space for cam01.avi'):
                drive.download(item, PARENT, ACCOUNT['account_id'], path, lambda: False, lambda n: None)
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(path.stat().st_size, 0, 'nothing written once the budget failed')
        self.assertEqual(video.claimed_disk_bytes(), 0)

    def test_restart_removes_partial_local_upload(self):
        state = video.allocate('partial.avi')
        video.video_path(state).write_bytes(b'partial bytes')
        with patch.object(video, '_initialized', False):
            video.initialize()
        self.assertFalse(video.video_path(state).exists())
        self.assertEqual(video.get(state['id'])['status'], 'interrupted')


class VideoDeviceTests(unittest.TestCase):
    def test_actual_detector_and_embedding_must_run_before_gpu_is_usable(self):
        from app.face_recognition import engine
        from app.services import hardware_info_service
        from types import SimpleNamespace
        from unittest.mock import Mock
        session = SimpleNamespace(get_providers=lambda: ['CUDAExecutionProvider', 'CPUExecutionProvider'], disable_fallback=Mock())
        detector = SimpleNamespace(session=session, detect=Mock())
        recognizer = SimpleNamespace(session=session, get_feat=Mock())
        app = SimpleNamespace(det_model=detector, models={'recognition':recognizer}, prepare=Mock())
        with patch.object(devices,'_sessions',{}), patch.object(engine,'FaceAnalysis',return_value=app), patch.object(hardware_info_service,'verified_providers',return_value=['CUDAExecutionProvider']):
            self.assertIs(REAL_SESSION('cuda'),app)
            detector.detect.assert_called_once(); recognizer.get_feat.assert_called_once()
            self.assertEqual(session.disable_fallback.call_count,2)
        recognizer.get_feat.side_effect = RuntimeError('Missing runtime kernel')
        with patch.object(devices,'_sessions',{}), patch.object(engine,'FaceAnalysis',return_value=app), patch.object(hardware_info_service,'verified_providers',return_value=['CUDAExecutionProvider']):
            with self.assertRaises(devices.DeviceError): REAL_SESSION('cuda')
            self.assertEqual(devices._sessions,{})

    def test_silent_cpu_session_fallback_is_rejected(self):
        from app.face_recognition import engine
        from app.services import hardware_info_service
        from types import SimpleNamespace
        session=SimpleNamespace(get_providers=lambda:['CPUExecutionProvider'],disable_fallback=lambda:None)
        app=SimpleNamespace(det_model=SimpleNamespace(session=session),models={'recognition':SimpleNamespace(session=session)},prepare=lambda **kw:None)
        with patch.object(devices,'_sessions',{}),patch.object(engine,'FaceAnalysis',return_value=app),patch.object(hardware_info_service,'verified_providers',return_value=['CUDAExecutionProvider']):
            with self.assertRaisesRegex(devices.DeviceError,'no CPU substitution'): REAL_SESSION('cuda')
