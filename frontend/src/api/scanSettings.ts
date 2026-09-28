/** Per-run controls. Camera requests and scan-start ceilings are not achieved rates. */
export interface ScanSettings {
  camera_fps: number;
  post_scan_delay_seconds: number;
  target_detections_per_second: number;
}
export const DEFAULT_SCAN_SETTINGS: Readonly<ScanSettings> = Object.freeze({
  camera_fps: 30, post_scan_delay_seconds: 0.4, target_detections_per_second: 2.5,
});
export const SCAN_RANGES = {
  camera_fps: { min: 1, max: 120 },
  post_scan_delay_seconds: { min: 0, max: 30 },
  target_detections_per_second: { min: 0.1, max: 30 },
};
export function scanSettingsError(settings: ScanSettings): string | null {
  for (const key of Object.keys(SCAN_RANGES) as (keyof ScanSettings)[]) {
    const value = settings[key], range = SCAN_RANGES[key];
    if (typeof value !== "number" || !Number.isFinite(value) || value < range.min || value > range.max)
      return key + " must be a finite number from " + range.min + " to " + range.max + ".";
  }
  return null;
}
export function freezeScanSettings(settings: ScanSettings): Readonly<ScanSettings> {
  const error = scanSettingsError(settings);
  if (error) throw new Error(error);
  return Object.freeze({ ...settings });
}
export function nextScanStart(start: number, finish: number, settings: Readonly<ScanSettings>): number {
  return Math.max(finish + settings.post_scan_delay_seconds, start + 1 / settings.target_detections_per_second);
}
/** Await one scan, then its deadline. Late timers never queue catch-up scans.
 * onResult runs only while this run is still active; settings are copied/frozen.
 * Seconds throughout, including injected test clock and wait.
 */
export async function runSerialScans<T>(options: {
  settings: Readonly<ScanSettings>; isActive: () => boolean;
  scan: () => Promise<T>; onResult: (result: T, start: number, finish: number) => void;
  now?: () => number; wait?: (seconds: number) => Promise<void>;
}): Promise<void> {
  const settings = freezeScanSettings(options.settings);
  const now = options.now ?? (() => performance.now() / 1000);
  const wait = options.wait ?? (seconds => new Promise(resolve => setTimeout(resolve, seconds * 1000)));
  while (options.isActive()) {
    const start = now();
    const result = await options.scan();
    const finish = now();
    if (!options.isActive()) return;
    options.onResult(result, start, finish);
    if (!options.isActive()) return;
    const deadline = nextScanStart(start, finish, settings);
    while (options.isActive() && now() < deadline)
      await wait(Math.min(0.1, deadline - now()));
  }
}
