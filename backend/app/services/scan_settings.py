"""Validated per-run scan controls. No persistence or recognition configuration changes."""
from __future__ import annotations
from pydantic import BaseModel, ConfigDict, Field

RANGES = {
    "camera_fps": {"min": 1.0, "max": 120.0},
    "post_scan_delay_seconds": {"min": 0.0, "max": 30.0},
    "target_detections_per_second": {"min": 0.1, "max": 30.0},
}

class ScanSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    camera_fps: float = Field(default=30.0, ge=1.0, le=120.0, strict=True)
    post_scan_delay_seconds: float = Field(default=0.4, ge=0.0, le=30.0, strict=True)
    target_detections_per_second: float = Field(default=2.5, ge=0.1, le=30.0, strict=True)

def next_scan_start(start: float, finish: float, settings: ScanSettings) -> float:
    return max(finish + settings.post_scan_delay_seconds,
               start + 1 / settings.target_detections_per_second)
