import { type MatchedPerson } from "./VideoMatchPreview";
export interface VideoSourceResult {
  source_video_key: string; filename: string; status: string; error: string | null;
  media: { fps: number; frame_count: number; duration_seconds: number; bytes?: number } | null;
  downloaded_bytes: number;
  // Older responses included this; current API intentionally omits private
  // Drive selection/version/resource-key metadata while a download runs.
  source_file?: { bytes: number };
  decoded_frames: number; sampled_frames: number; detected_face_detections: number;
  matched_face_detections: number; unknown_detections: number; frames_with_unknown: number;
  verified_total_frames: number | null; progress_percent: number;
  processing_seconds: number; recognition_processing_seconds?: number; actual_video_detection_hz: number | null;
  people?: MatchedPerson[];
  samples?: { frame_index: number; timestamp_seconds: number; faces: number; matched_people: number; unknown_detections: number }[];
}
function downloadProgress(source: VideoSourceResult): string {
  const amount = (source.downloaded_bytes / 1024 ** 2).toFixed(1) + " MiB downloaded";
  const total = source.source_file?.bytes ?? source.media?.bytes;
  return total != null && Number.isFinite(total) && total > 0
    ? amount + " (" + Math.min(100, source.downloaded_bytes / total * 100).toFixed(1) + "%)"
    : amount + " (total unavailable until validation)";
}
export default function VideoBatchSources({ sources }: { sources: VideoSourceResult[] }) {
  return <section className="my-4">
    <h3 className="mb-2 font-semibold">Results by source video / camera</h3>
    <p className="mb-2 text-xs text-gray-600">Filename identifies the source; no camera identity or shared recording clock is inferred. A failed source retains completed sampled frames. Processing time below includes its download, validation and EOF check.</p>
    <div className="overflow-x-auto"><table className="w-full text-left text-sm">
      <thead><tr><th className="p-2">Video</th><th className="p-2">Status / progress</th><th className="p-2">Source FPS / duration</th><th className="p-2">Decoded / sampled</th><th className="p-2">Faces / matched / unknown</th><th className="p-2">Achieved scans/s / elapsed</th></tr></thead>
      <tbody>{sources.map(source => <tr key={source.source_video_key} className="border-t">
        <td className="p-2 break-words">{source.filename}</td>
        <td className="p-2">{source.status === "running" ? "processing" : source.status}
          {source.status === "downloading" && <span className="block text-xs">{downloadProgress(source)}</span>}
          {source.status === "running" && <span className="block text-xs">{source.progress_percent.toFixed(1)}% estimated decode progress</span>}
          {source.error && <p role="alert" className="text-xs text-red-700">{source.error}</p>}</td>
        <td className="p-2">{source.media ? source.media.fps.toFixed(3) + " FPS / " + source.media.duration_seconds.toFixed(3) + " s (nominal)" : "Available after validation"}</td>
        <td className="p-2">{source.decoded_frames} / {source.sampled_frames}
          <span className="block text-xs">{source.verified_total_frames == null ? "Readable total not verified" : source.verified_total_frames + " verified readable frames"}</span>
          {source.media && <span className="block text-xs">{source.media.frame_count} reported frames (estimate)</span>}</td>
        <td className="p-2">{source.detected_face_detections} / {source.matched_face_detections} / {source.unknown_detections}</td>
        <td className="p-2">{source.actual_video_detection_hz?.toFixed(3) ?? "—"} / {source.processing_seconds.toFixed(3)} s</td>
      </tr>)}</tbody>
    </table></div>
    {sources.map(source => <details key={source.source_video_key} className="mt-3 rounded border p-2">
      <summary className="cursor-pointer text-sm font-medium">{source.filename}: person counts and sampled timestamps</summary>
      {!source.people?.length ? <p className="my-2 text-sm">{source.sampled_frames === 0 ? "No sampled frame results for this source." : source.detected_face_detections === 0 ? "No faces detected in sampled frames." : "Faces detected; no enrolled identity matched."}</p> :
        <table className="my-2 w-full text-left text-xs"><thead><tr><th>Name</th><th>Sampled frames detected</th><th>First / last timestamp in this clip</th></tr></thead>
          <tbody>{source.people.map(person => <tr key={person.identity_key}><td>{person.name}</td><td>{person.detection_count}</td><td>{person.first_timestamp_seconds.toFixed(3)} / {person.last_timestamp_seconds.toFixed(3)} s</td></tr>)}</tbody></table>}
      <div className="max-h-48 overflow-auto"><table className="w-full text-left text-xs"><thead><tr><th>Frame index (zero-based)</th><th>Timestamp in this clip</th><th>Faces</th><th>Matched identities</th><th>Unknown faces</th></tr></thead>
        <tbody>{source.samples?.map(sample => <tr key={sample.frame_index}><td>{sample.frame_index}</td><td>{sample.timestamp_seconds.toFixed(3)} s</td><td>{sample.faces}</td><td>{sample.matched_people}</td><td>{sample.unknown_detections}</td></tr>)}</tbody></table></div>
    </details>)}
  </section>;
}
