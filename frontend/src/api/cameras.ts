/**
 * Camera selection, shared by Settings (app-wide default) and the kiosk
 * (per-device override).
 *
 * Two different identifiers are involved on purpose:
 *
 *  - deviceId is issued by the browser and is scoped to one browser profile
 *    and origin. It is precise, but it means nothing on another machine or in
 *    another browser, so it must never be the app-wide setting.
 *  - label ("HD Webcam (5986:211b)") is the same string wherever that physical
 *    camera is plugged in, so it is what Settings stores as the app-wide
 *    default and what the kiosk matches against locally.
 *
 * So: Settings saves a label to the backend, the kiosk resolves that label to
 * a local deviceId at start, and a kiosk override (a deviceId) is kept in
 * localStorage for this browser only.
 */

export interface CameraDevice {
  deviceId: string;
  label: string;
}

const OVERRIDE_KEY = "reconize_kiosk_camera_device_id";

/** Browsers hide camera labels until permission has been granted once. */
export async function listCameras(): Promise<CameraDevice[]> {
  const devices = await navigator.mediaDevices.enumerateDevices();
  return devices
    .filter((d) => d.kind === "videoinput")
    .map((d, i) => ({
      deviceId: d.deviceId,
      // A camera with no label means permission has not been granted yet.
      label: d.label || `Camera ${i + 1}`,
    }));
}

/**
 * Ask for camera access purely so enumerateDevices() will return labels, then
 * release it immediately — this must never leave a camera light on.
 */
export async function listCamerasWithPermission(): Promise<CameraDevice[]> {
  let stream: MediaStream | null = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ video: true });
    return await listCameras();
  } finally {
    stream?.getTracks().forEach((t) => t.stop());
  }
}

export function getKioskOverride(): string | null {
  try {
    return localStorage.getItem(OVERRIDE_KEY);
  } catch {
    return null;
  }
}

export function setKioskOverride(deviceId: string | null): void {
  try {
    if (deviceId) localStorage.setItem(OVERRIDE_KEY, deviceId);
    else localStorage.removeItem(OVERRIDE_KEY);
  } catch {
    /* private mode / storage disabled — fall back to the app-wide default */
  }
}

/**
 * Which camera should this kiosk open?
 *   1. the override chosen on this machine, if that camera is still present
 *   2. the app-wide default label from Settings, matched to a local device
 *   3. whatever the browser picks (current behaviour)
 * Returns null for case 3.
 */
export function resolveCameraDeviceId(
  cameras: CameraDevice[],
  settingLabel: string,
): string | null {
  const override = getKioskOverride();
  if (override && cameras.some((c) => c.deviceId === override)) return override;

  if (settingLabel) {
    const match = cameras.find((c) => c.label === settingLabel);
    if (match) return match.deviceId;
  }
  return null;
}

/** Constraints for getUserMedia. `exact` so it fails loudly rather than
 *  silently opening a different camera than the one that was chosen. */
export function videoConstraints(deviceId: string | null): MediaTrackConstraints {
  return deviceId ? { deviceId: { exact: deviceId } } : { facingMode: "user" };
}
