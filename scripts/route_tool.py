#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""取真实路网线：腾讯坐标(GCJ-02) → OSRM(WGS-84) 求路 → 结果再纠偏回 GCJ-02。

为什么绕这一圈：腾讯地图 POI 是 GCJ-02，OSRM 只认 WGS-84。直接拿 GCJ-02 去问，
300–500m 的系统偏移会让路线压在田里；求回来不转回 GCJ-02，又会整条线飘走。

单段：
    python scripts/route_tool.py --key d1_czn_wd --from 107.395026,22.357675 --to 107.358885,22.418731 \
        --out nav.json

批量（legs.json 里是 [{"key":..,"from":[lng,lat],"to":[lng,lat]}]）：
    python scripts/route_tool.py --legs legs.json --out nav.json

输出是可直接塞进 trip-data.json 的 navData 片段：{"d1_czn_wd": [[lat,lng], ...]}
"""
import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request

OSRM = 'https://router.project-osrm.org/route/v1/driving/{coords}?overview=full&geometries=geojson&steps=true'
UA = {'User-Agent': 'travel-itinerary-map/1.0 (skill route helper)'}

PI = 3.1415926535897932384626
A = 6378245.0
EE = 0.00669342162296594323


# ---------- 坐标系 ----------
def _tl(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * PI) + 40.0 * math.sin(y / 3.0 * PI)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * PI) + 320 * math.sin(y * PI / 30.0)) * 2.0 / 3.0
    return r


def _tn(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * PI) + 20.0 * math.sin(2.0 * x * PI)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * PI) + 40.0 * math.sin(x / 3.0 * PI)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * PI) + 300.0 * math.sin(x / 30.0 * PI)) * 2.0 / 3.0
    return r


def wgs2gcj(lng, lat):
    dlat, dlng = _tl(lng - 105.0, lat - 35.0), _tn(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * PI
    m = math.sin(rad)
    m = 1 - EE * m * m
    sm = math.sqrt(m)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (m * sm) * PI)
    dlng = (dlng * 180.0) / (A / sm * math.cos(rad) * PI)
    return lng + dlng, lat + dlat


def gcj2wgs(lng, lat, iters=4):
    """GCJ-02 → WGS-84，迭代逼近（一次近似误差 ~1m 级别足够画线）。"""
    wlng, wlat = lng, lat
    for _ in range(iters):
        glng, glat = wgs2gcj(wlng, wlat)
        wlng += lng - glng
        wlat += lat - glat
    return wlng, wlat


# ---------- 几何 ----------
def perp(p, a, b):
    x, y = p
    x1, y1 = a
    x2, y2 = b
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(x - x1, y - y1)
    t = ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))


def simplify(pts, eps):
    if len(pts) < 3:
        return pts
    dmax, idx = 0.0, 0
    for i in range(1, len(pts) - 1):
        d = perp(pts[i], pts[0], pts[-1])
        if d > dmax:
            dmax, idx = d, i
    if dmax > eps:
        return simplify(pts[:idx + 1], eps)[:-1] + simplify(pts[idx:], eps)
    return [pts[0], pts[-1]]


def haversine(a, b):
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(d))


def fmt_km(m):
    return '%.1f km' % (m / 1000.0)


# ---------- 抓取 ----------
def fetch_route(a_gcj, b_gcj, retries=4):
    """a_gcj/b_gcj = (lng, lat) GCJ-02。返回 (坐标列表GCJ-02[lat,lng], 距离m, 时长s, 路名统计)。"""
    aw = gcj2wgs(*a_gcj)
    bw = gcj2wgs(*b_gcj)
    # OSRM 参数与返回都是 lng,lat
    coords = '%f,%f;%f,%f' % (aw[0], aw[1], bw[0], bw[1])
    url = OSRM.format(coords=coords)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode('utf-8'))
            if data.get('code') != 'Ok' or not data.get('routes'):
                raise RuntimeError('OSRM 返回 %s' % data.get('code'))
            route = data['routes'][0]
            raw = route['geometry']['coordinates']
            out = []
            for lng, lat in raw:
                glng, glat = wgs2gcj(lng, lat)
                out.append([round(glat, 6), round(glng, 6)])
            roads = {}
            for leg in route.get('legs', []):
                for st in leg.get('steps', []):
                    name = st.get('name') or '无名路'
                    roads[name] = roads.get(name, 0) + st.get('distance', 0)
            top = sorted(roads.items(), key=lambda kv: -kv[1])[:6]
            return out, route['distance'], route['duration'], top
        except Exception as e:  # 公共实例偶发失败，退避重试
            last = e
            if i < retries - 1:
                time.sleep([2, 5, 8][min(i, 2)])
    raise RuntimeError('取路失败（重试 %d 次）：%s' % (retries, last))


def parse_lnglat(s):
    parts = str(s).replace('，', ',').split(',')
    if len(parts) != 2:
        raise argparse.ArgumentTypeError('坐标要写成 lng,lat（经度在前）：%s' % s)
    return float(parts[0]), float(parts[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--key', help='段名，如 d1_czn_wd（建议 d{天}_{起点}_{终点}）')
    ap.add_argument('--from', dest='a', type=parse_lnglat, help='起点 lng,lat（腾讯 GCJ-02）')
    ap.add_argument('--to', dest='b', type=parse_lnglat, help='终点 lng,lat')
    ap.add_argument('--legs', help='批量：JSON 文件 [{"key":..,"from":[lng,lat],"to":[lng,lat]}]')
    ap.add_argument('--out', help='输出/合并到该 JSON（缺省打到 stdout）')
    ap.add_argument('--eps', type=float, default=0.00035, help='简化阈值（度），0.00035≈35m，0 表示不简化')
    ap.add_argument('--sleep', type=float, default=0.5, help='段间隔秒数，对公共实例客气点')
    ap.add_argument('--wgs2gcj', metavar='lng,lat', help='只做坐标转换：WGS-84 → GCJ-02（OSM 取到的点用这个）')
    ap.add_argument('--gcj2wgs', metavar='lng,lat', help='只做坐标转换：GCJ-02 → WGS-84')
    args = ap.parse_args()

    if args.wgs2gcj or args.gcj2wgs:
        s = args.wgs2gcj or args.gcj2wgs
        lng, lat = parse_lnglat(s)
        out = wgs2gcj(lng, lat) if args.wgs2gcj else gcj2wgs(lng, lat)
        print('%.6f,%.6f' % (out[0], out[1]))
        return

    jobs = []
    if args.legs:
        with open(args.legs, 'r', encoding='utf-8') as f:
            for it in json.load(f):
                jobs.append((it['key'], tuple(it['from']), tuple(it['to'])))
    elif args.key and args.a and args.b:
        jobs.append((args.key, args.a, args.b))
    else:
        ap.error('要么给 --key/--from/--to，要么给 --legs')

    result = {}
    for i, (key, a, b) in enumerate(jobs):
        try:
            pts, dist, dur, roads = fetch_route(a, b)
        except Exception as e:
            print('[%s] 失败：%s' % (key, e), file=sys.stderr)
            continue
        if args.eps > 0:
            simp = simplify(pts, args.eps)
        else:
            simp = pts
        result[key] = simp
        top = ' / '.join('%s %s' % (n, fmt_km(d)) for n, d in roads[:4])
        print('[%s] 驾车 %s · 约 %.0f 分钟 · 原始 %d 点 → 简化 %d 点 · 主要道路：%s'
              % (key, fmt_km(dist), dur / 60.0, len(pts), len(simp), top), file=sys.stderr)
        if i < len(jobs) - 1:
            time.sleep(args.sleep)

    if not result:
        sys.exit('没有取到任何路段')

    if args.out:
        merged = {}
        if os.path.exists(args.out):
            try:
                with open(args.out, 'r', encoding='utf-8') as f:
                    old = json.load(f)
                if isinstance(old, dict):
                    merged = old
            except Exception:
                pass
        merged.update(result)
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, separators=(',', ':'))
        print('已写入 %s（共 %d 段）' % (args.out, len(merged)), file=sys.stderr)
    else:
        print(json.dumps(result, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
