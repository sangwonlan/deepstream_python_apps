

import json, os, time
from typing import Optional

def ensure_dir(path: str):
    d = os.path.dirname(path)
    if d and not os.path.exists(d):
        os.makedirs(d, exist_ok=True)

def write_status(path: str, camera_id: str, track_id: int, prefall: bool, dwell: float, note: Optional[str]=None):
    ensure_dir(path)
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    key = f"{camera_id}:{track_id}"
    data[key] = {
        "timestamp": now,
        "camera": camera_id,
        "track_id": track_id,
        "prefall": prefall,
        "dwell_sec": round(dwell, 2),
        "color": "red" if prefall else "green",
        "note": note or ""
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
