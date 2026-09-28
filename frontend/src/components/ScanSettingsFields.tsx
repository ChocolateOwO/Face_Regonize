import { SCAN_RANGES, scanSettingsError, type ScanSettings } from "../api/scanSettings";

export default function ScanSettingsFields({ value, onChange, disabled = false, dark = false, video = false }: {
  value: ScanSettings; onChange: (value: ScanSettings) => void;
  disabled?: boolean; dark?: boolean; video?: boolean;
}) {
  const fields: { key: keyof ScanSettings; label: string; step: string }[] = [
    { key: "camera_fps", label: "Camera FPS", step: "any" },
    { key: "post_scan_delay_seconds", label: "Wait after completed scan (seconds)", step: "any" },
    { key: "target_detections_per_second", label: "Target detections per second", step: "any" },
  ];
  const error = scanSettingsError(value);
  return <fieldset disabled={disabled} className="my-3 rounded-lg border border-gray-400/40 p-3">
    <legend className="px-1 text-sm font-semibold">Scanning settings (frozen on Start)</legend>
    <div className="grid gap-3 sm:grid-cols-3">
      {fields.map(field => <label key={field.key} className="block text-xs">
        {field.label}
        <input type="number" min={SCAN_RANGES[field.key].min} max={SCAN_RANGES[field.key].max}
          step={field.step} value={Number.isNaN(value[field.key]) ? "" : value[field.key]}
          onChange={event => onChange({ ...value, [field.key]: event.target.value === "" ? NaN : Number(event.target.value) })}
          className={"mt-1 block w-full rounded border px-2 py-2 disabled:opacity-60 " + (dark ? "border-white/30 bg-white/10 text-white" : "border-gray-300 bg-white text-gray-900")} />
      </label>)}
    </div>
    <p className="mt-2 text-xs">
      {video ? "Camera FPS limits virtual frame availability; uploaded source FPS is unchanged."
        : "Camera FPS is requested from the camera; negotiated actual FPS may be lower."}
      {" "}Target detections/s is a scan-start ceiling, not guaranteed throughput.
      Wait begins after the completed scan. Restart the run to change values.
      Ranges: camera 1-120 FPS; wait 0-30 s; target 0.1-30/s.
    </p>
    {error && <p role="alert" className={dark ? "mt-2 text-xs text-red-300" : "mt-2 text-xs text-red-600"}>{error}</p>}
  </fieldset>;
}
