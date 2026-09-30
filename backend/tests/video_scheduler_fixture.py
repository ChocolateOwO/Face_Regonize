"""Keep video regressions isolated: real scheduler, synthetic matcher/devices."""
from app.services import local_video_experiment as video, video_scheduler as scheduler, video_devices as devices

def _clear():
    scheduler._indices.clear(); scheduler._tokens.clear(); scheduler._stages.clear(); scheduler._previews.clear()
    video._disk_claims.clear()

def install(case, *, fake_index=False):
    scheduler.shutdown()
    _clear()
    case.patch(scheduler, 'config', lambda: {'max_workers': 1})
    case.patch(devices, 'resolve', lambda requested: 'cpu')
    case.patch(devices, 'session', lambda device: None)
    case.patch(devices, 'PROVIDERS', {'cpu': 'FakeProvider'})
    case.patch(devices, 'detect', lambda image, device, timings: video.detect(image))
    if fake_index:
        case.patch(scheduler, 'freeze', lambda job_id: video.identity_snapshot())

def drain():
    for token in list(scheduler._tokens.values()): token.set()
    scheduler.shutdown()
    assert not scheduler._workers
    _clear()
