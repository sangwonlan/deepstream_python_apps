
import json, time
from urllib import request
def console_alert(cam_id: str, track_id: int, level: str, detail: str):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] [{cam_id}] track={track_id} >> {level} :: {detail}", flush=True)
def http_alert(endpoint: str, cam_id: str, track_id: int, level: str, detail: str, timeout: float=2.0):
    payload = {"timestamp": time.time(),"camera_id": cam_id,"track_id": track_id,"level": level,"detail": detail}
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(endpoint, data=data, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except Exception as e:
        print(f"[WARN] http_alert failed: {e}")
        return None
