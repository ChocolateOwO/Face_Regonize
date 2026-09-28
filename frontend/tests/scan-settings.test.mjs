import assert from "node:assert/strict";
import test from "node:test";
import { DEFAULT_SCAN_SETTINGS, scanSettingsError, freezeScanSettings, nextScanStart, runSerialScans } from "../src/api/scanSettings.ts";
import { videoConstraints } from "../src/api/cameras.ts";

test("defaults, finite ranges and frozen snapshot", () => {
  assert.deepEqual(DEFAULT_SCAN_SETTINGS, { camera_fps:30, post_scan_delay_seconds:0.4, target_detections_per_second:2.5 });
  for (const [key, values] of Object.entries({
    camera_fps: [0,121,NaN,Infinity,"30",true],
    post_scan_delay_seconds: [-1,31,NaN],
    target_detections_per_second: [0,0.09,31,Infinity],
  })) for (const value of values) assert.ok(scanSettingsError({ ...DEFAULT_SCAN_SETTINGS, [key]:value }));
  const draft={ ...DEFAULT_SCAN_SETTINGS };
  const frozen=freezeScanSettings(draft);draft.camera_fps=12;
  assert.equal(frozen.camera_fps,30);
  assert.throws(() => { frozen.camera_fps=12; },TypeError);
});
test("target and wait combine with max, defaults add no second delay", () => {
  assert.equal(nextScanStart(0,0.2,DEFAULT_SCAN_SETTINGS),0.6000000000000001);
  assert.equal(nextScanStart(0,0,DEFAULT_SCAN_SETTINGS),0.4);
  assert.equal(nextScanStart(0,0.2,{ ...DEFAULT_SCAN_SETTINGS, post_scan_delay_seconds:0.1,target_detections_per_second:1 }),1);
  assert.equal(nextScanStart(0,2,{ ...DEFAULT_SCAN_SETTINGS, post_scan_delay_seconds:1,target_detections_per_second:30 }),3);
});
async function replay({ work, settings={...DEFAULT_SCAN_SETTINGS}, timerOvershoot=0 }) {
  let clock=0, active=true, calls=0, inflight=0, maxInflight=0;
  const starts=[];
  await runSerialScans({
    settings,isActive:()=>active,now:()=>clock,
    wait:async duration=>{clock+=duration+timerOvershoot;},
    scan:async()=>{ starts.push(clock);inflight++;maxInflight=Math.max(maxInflight,inflight);
      await Promise.resolve();clock+=work[calls++];inflight--;return calls; },
    onResult:()=>{settings.post_scan_delay_seconds=0;settings.target_detections_per_second=30;if(calls===work.length)active=false;},
  });
  return {starts,maxInflight};
}
test("slow inference awaits completion; one scan in flight; draft changes ignored",async()=>{
  const {starts,maxInflight}=await replay({work:[2,2,2]});
  assert.deepEqual(starts,[0,2.4,4.800000000000001]);assert.equal(maxInflight,1);
});
test("custom target caps scan starts without stacking a second wait",async()=>{
  const {starts}=await replay({work:[0.2,0.2,0.2],settings:{...DEFAULT_SCAN_SETTINGS,post_scan_delay_seconds:0.1,target_detections_per_second:1}});
  assert.deepEqual(starts,[0,1,2]);
});
test("late timers skip missed opportunities; never catch up in a burst",async()=>{
  const {starts}=await replay({work:[0,0,0],timerOvershoot:5});
  assert.deepEqual(starts,[0,5.1,10.2]);
});
test("stop during inference drops stale callback and does not start another scan",async()=>{
  let active=true,resolveScan,calls=0,callbacks=0;
  const loop=runSerialScans({settings:DEFAULT_SCAN_SETTINGS,isActive:()=>active,
    scan:()=>{calls++;return new Promise(resolve=>{resolveScan=resolve;});},onResult:()=>callbacks++});
  active=false;resolveScan([]);await loop;
  assert.equal(calls,1);assert.equal(callbacks,0);
});
test("camera FPS requested through existing constraints; tap constraints unchanged",()=>{
  assert.deepEqual(videoConstraints("chosen",12),{deviceId:{exact:"chosen"},frameRate:{ideal:12,max:12}});
  assert.deepEqual(videoConstraints(null,30),{facingMode:"user",frameRate:{ideal:30,max:30}});
  assert.deepEqual(videoConstraints("chosen"),{deviceId:{exact:"chosen"}});
});
