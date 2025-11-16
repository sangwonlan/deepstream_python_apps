
from typing import List, Tuple
import math
Point = Tuple[float, float]
def point_in_polygon(p: Point, poly: List[Point]) -> bool:
    x, y = p
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        dy = (y2 - y1) if (y2 - y1) != 0 else 1e-12
        if ((y1 > y) != (y2 > y)) and (x < (x2 - x1) * (y - y1) / dy + x1):
            inside = not inside
    return inside
def dist_point_to_segment(p: Point, a: Point, b: Point) -> float:
    px, py = p; ax, ay = a; bx, by = b
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    c1 = vx * wx + vy * wy
    if c1 <= 0: return math.hypot(px - ax, py - ay)
    c2 = vx * vx + vy * vy
    if c2 <= c1: return math.hypot(px - bx, py - by)
    t = c1 / c2
    projx, projy = ax + t * vx, ay + t * vy
    return math.hypot(px - projx, py - projy)
def dist_point_to_polygon(p: Point, poly: List[Point]) -> float:
    return min(dist_point_to_segment(p, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))
