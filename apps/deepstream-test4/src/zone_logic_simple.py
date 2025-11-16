# src/zone_logic_simple.py
from dataclasses import dataclass
from typing import List, Tuple, Dict, Any
import math
import yaml

Point = Tuple[float, float]


# -----------------------------
# 1) 설정 구조체
# -----------------------------
@dataclass
class ZoneThresholds:
    d2_edge: float      # 엣지 근접 거리(픽셀 기준)
    T_alert: float      # Zone1 안에서 Alert로 볼 체류시간(초)
    cooldown_sec: float = 30.0


@dataclass
class ZoneConfig:
    camera_id: str
    fps: float
    bed_polygon: List[Point]
    thresholds: ZoneThresholds


def load_zone_config(path: str) -> ZoneConfig:
    """YAML 설정 파일 로드"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    thr = data.get("thresholds", {})
    bed_poly = [tuple(p) for p in data["bed_polygon"]]

    return ZoneConfig(
        camera_id=data.get("camera_id", "cam01"),
        fps=float(data.get("fps", 30.0)),
        bed_polygon=bed_poly,
        thresholds=ZoneThresholds(
            d2_edge=float(thr.get("d2_edge", 40.0)),
            T_alert=float(thr.get("T_alert", 10.0)),
            cooldown_sec=float(thr.get("cooldown_sec", 30.0)),
        ),
    )


# -----------------------------
# 2) 기하 도우미 함수들
# -----------------------------
def point_in_polygon(pt: Point, poly: List[Point]) -> bool:
    """Ray casting으로 point가 polygon 안에 있는지 여부"""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        intersects = ((y1 > y) != (y2 > y)) and \
                     (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-9) + x1)
        if intersects:
            inside = not inside
    return inside


def distance_point_to_segment(p: Point, a: Point, b: Point) -> float:
    """점 p와 선분 ab 사이의 최소 거리"""
    (px, py) = p
    (ax, ay) = a
    (bx, by) = b

    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    seg_len2 = vx * vx + vy * vy

    if seg_len2 == 0.0:
        # a와 b가 같은 점인 경우
        dx, dy = px - ax, py - ay
        return math.hypot(dx, dy)

    t = (wx * vx + wy * vy) / seg_len2
    t = max(0.0, min(1.0, t))

    projx = ax + t * vx
    projy = ay + t * vy

    dx, dy = px - projx, py - projy
    return math.hypot(dx, dy)


def edge_distance(pt: Point, poly: List[Point]) -> float:
    """점에서 다각형 경계까지의 최소 거리"""
    n = len(poly)
    dmin = float("inf")
    for i in range(n):
        a = poly[i]
        b = poly[(i + 1) % n]
        d = distance_point_to_segment(pt, a, b)
        if d < dmin:
            dmin = d
    return dmin


# -----------------------------
# 3) 단일 위험구역 모니터 (Zone1만 사용)
# -----------------------------
class SimpleZoneMonitor:
    """
    단일 위험구역(Zone1)만 사용하는 모니터.

    - Zone0: 침대 안 + 엣지에서 충분히 떨어진 안전 상태
    - Zone1: 침대 안 + 엣지에서 d2_edge 이내로 가까운 위험구역(낙상 전 구역)

    침대 밖(폴리곤 밖)은 여기서는 별도 Zone2로 쓰지 않고,
    단순히 SAFE로 간주하며 체류시간(dwell)을 0으로 리셋합니다.
    """

    def __init__(self, cfg: ZoneConfig):
        self.cfg = cfg
        self.bed_polygon = cfg.bed_polygon
        self.d2_edge = cfg.thresholds.d2_edge
        self.T_alert = cfg.thresholds.T_alert

        self.dwell = 0.0          # Zone1 안에서 머문 시간(초)
        self.prev_in_zone1 = False

    def update(
        self,
        bbox: Tuple[float, float, float, float],
        dt: float = None
    ) -> Dict[str, Any]:
        """
        bbox: (x, y, w, h) - 사람 바운딩박스 (픽셀)
              x,y: 좌상단, w,h: 폭/높이
        dt  : 프레임 간 시간 간격(초). None이면 fps 기준 자동 계산.

        반환 딕셔너리:
            {
              "in_zone1": bool,          # 현재 프레임에서 Zone1인지
              "dwell": float,            # Zone1에서 누적 체류시간(초)
              "alert": bool,             # dwell >= T_alert 여부
              "level": "SAFE" |
                       "PREFALL_SHORT" |
                       "PREFALL_ALERT",
              "edge_dist": float | None  # 침대 경계까지 거리(픽셀), 침대 밖이면 None
            }
        """
        if dt is None:
            dt = 1.0 / max(self.cfg.fps, 1e-6)

        x, y, w, h = bbox
        # 사람 박스의 "발쪽" 중심점 기준으로 Zone을 판정
        bottom_center: Point = (x + w / 2.0, y + h)

        # 1) 침대 안/밖 판정
        inside = point_in_polygon(bottom_center, self.bed_polygon)

        in_zone1 = False
        dist = None

        if inside:
            dist = edge_distance(bottom_center, self.bed_polygon)
            if dist <= self.d2_edge:
                in_zone1 = True

        # 2) Zone1 체류시간 업데이트
        if in_zone1:
            self.dwell += dt
        else:
            self.dwell = 0.0

        # 3) Alert / level 판정
        alert = in_zone1 and (self.dwell >= self.T_alert)

        if not in_zone1:
            level = "SAFE"
        elif alert:
            level = "PREFALL_ALERT"   # 기준시간 이상 Zone1
        else:
            level = "PREFALL_SHORT"   # 기준시간 미만 Zone1

        self.prev_in_zone1 = in_zone1

        return {
            "in_zone1": in_zone1,
            "dwell": round(self.dwell, 3),
            "alert": alert,
            "level": level,
            "edge_dist": dist,
        }
