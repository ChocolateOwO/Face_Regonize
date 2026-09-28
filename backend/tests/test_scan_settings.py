"""Isolated validation/API and scheduler properties; no camera, DB or model."""
import asyncio,json,math,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI
from pydantic import ValidationError
from app.api import scan_settings as api
from app.auth.deps import get_current_user
from app.models.models import User
from app.services.scan_settings import ScanSettings, next_scan_start
from app.services.local_video_experiment import next_capture
from tests.test_local_video_experiment import request

class ScanSettingsTests(unittest.TestCase):
    def test_defaults_custom_ranges_finite_and_frozen(self):
        self.assertEqual(ScanSettings().model_dump(),dict(camera_fps=30.0,post_scan_delay_seconds=0.4,target_detections_per_second=2.5))
        settings=ScanSettings(camera_fps=1,post_scan_delay_seconds=0,target_detections_per_second=30)
        with self.assertRaises(ValidationError):settings.camera_fps=60
        for key,values in {"camera_fps":[0,121,True,"30",float("nan"),float("inf")],
                           "post_scan_delay_seconds":[-1,31,False,"0.4",float("-inf")],
                           "target_detections_per_second":[0,0.09,31,None,float("nan")]}.items():
            for value in values:
                with self.subTest(key=key,value=value),self.assertRaises(ValidationError):
                    ScanSettings.model_validate({key:value})
        with self.assertRaises(ValidationError):ScanSettings.model_validate({"other":1})

    def test_combined_rule_target_wait_and_slow_work(self):
        self.assertAlmostEqual(next_scan_start(0,0.2,ScanSettings()),0.6)
        self.assertEqual(next_scan_start(0,0.2,ScanSettings(post_scan_delay_seconds=0.1,target_detections_per_second=1)),1)
        self.assertEqual(next_scan_start(0,2,ScanSettings(post_scan_delay_seconds=1,target_detections_per_second=30)),3)

    def test_virtual_capture_properties_latest_distinct_and_deadline_no_backlog(self):
        for fps in (1,1.9,5,29.97,30,120):
            for camera in (1,2.5,30,120):
                for delay,target,work in ((0,30,0),(0.4,2.5,0.07),(3,30,1.8),(0,0.1,0.2)):
                    settings=ScanSettings(camera_fps=camera,post_scan_delay_seconds=delay,target_detections_per_second=target)
                    frame,capture=0,0.0
                    for _ in range(20):
                        new_frame,new_capture=next_capture(frame,capture,work,fps,settings)
                        self.assertGreater(new_frame,frame)
                        self.assertGreaterEqual(new_capture+1e-9,next_scan_start(capture,capture+work,settings))
                        tick=math.floor(new_capture*camera+1e-8)
                        self.assertEqual(new_frame,math.floor(tick*fps/camera+1e-8))
                        self.assertLessEqual(new_frame/fps,new_capture+1e-8)
                        frame,capture=new_frame,new_capture

    def test_api_defaults_admin_permission_and_validation(self):
        app=FastAPI();app.include_router(api.router)
        def call(path,method="GET",data=None):
            return asyncio.run(request(app,"/api/scan-settings"+path,method,
                json.dumps(data).encode() if data is not None else b"",
                {"Content-Type":"application/json"}))
        self.assertEqual(call("")[0],401)
        self.assertEqual(call("/validate","POST",{})[0],401)
        app.dependency_overrides[get_current_user]=lambda:User(username="viewer",password_hash="x",role="viewer")
        status,raw,_=call("")
        self.assertEqual(status,200);self.assertFalse(json.loads(raw)["can_configure"])
        self.assertEqual(call("/validate","POST",{})[0],403)
        app.dependency_overrides[get_current_user]=lambda:User(username="admin",password_hash="x",role="admin")
        status,raw,_=call("")
        self.assertTrue(json.loads(raw)["can_configure"])
        self.assertEqual(call("/validate","POST",{})[0],200)
        self.assertEqual(call("/validate","POST",{"camera_fps":0})[0],422)
        self.assertEqual(call("/validate","POST",{"camera_fps":"30"})[0],422)
        self.assertEqual(call("/validate","POST",{"camera_fps":True})[0],422)
        self.assertEqual(call("/validate","POST",{"camera_fps":float("nan")})[0],422)
        self.assertEqual(call("/validate","POST",{"post_scan_delay_seconds":float("inf")})[0],422)
        status,raw,_=call("/validate","POST",{"camera_fps":12,"post_scan_delay_seconds":0.25,"target_detections_per_second":1.5})
        self.assertEqual(status,200)
        self.assertEqual(json.loads(raw)["camera_fps"],12)
