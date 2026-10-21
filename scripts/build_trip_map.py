#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 trip-data.json 注入 assets/template.html，产出一张可交互的行程攻略图。

用法：
    python scripts/build_trip_map.py trip-data.json -o 行程图.html
    python scripts/build_trip_map.py trip-data.json -o 行程图.html --strict

脚本做三件事：校验数据（结构 / 引用 / 坐标量级 / 路段端点偏差）、注入、体检报告。
体检发现的问题默认只告警；加 --strict 时出现 error 即退出码 1。
"""
import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(os.path.dirname(HERE), 'assets', 'template.html')
PLACEHOLDER = '/*__TRIP_DATA__*/null'

errors = []
warns = []
notes = []


def err(msg):
    errors.append(msg)


def warn(msg):
    warns.append(msg)


def haversine(a, b):
    """a/b = [lat, lng]，返回米。"""
    r = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(d))


def validate(trip):
    meta = trip.get('meta') or {}
    if not meta.get('title'):
        err('meta.title 为空：顶部标题会显示成「行程攻略图」')

    days = trip.get('days') or []
    if not days:
        err('days 为空，页面将没有任何行程卡片')
        return
    day_ns = sorted(d.get('n') for d in days)
    if day_ns != list(range(1, len(days) + 1)):
        warn('days 的 n 不连续：%s（建议从 1 起连续编号，筛选条与配色按 n 取值）' % day_ns)
    for d in days:
        if not d.get('color'):
            warn('DAY %s 缺 color，会落回默认蓝色' % d.get('n'))
        if not d.get('rows'):
            warn('DAY %s 没有 rows，卡片里会是空的' % d.get('n'))
        if not d.get('legs'):
            warn('DAY %s 没有 legs，地图上这天不会有轨迹' % d.get('n'))

    spots = trip.get('spots') or []
    if not spots:
        err('spots 为空，地图上不会有任何标点')
    ids, coords = {}, {}
    for s in spots:
        sid = s.get('id')
        if not sid:
            err('存在没有 id 的 spot：%s' % s.get('name'))
            continue
        if sid in ids:
            err('spot id 重复：%s（id 必须唯一，legs 靠它引用）' % sid)
        ids[sid] = s
        lat, lng = s.get('lat'), s.get('lng')
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            err('%s 的 lat/lng 不是数字' % sid)
            continue
        if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
            err('%s 坐标越界：lat=%s lng=%s（多半是 lat/lng 写反了）' % (sid, lat, lng))
        coords[sid] = [lat, lng]
        if s.get('day') not in day_ns:
            err('%s 的 day=%s 不在 days 里（标点会永远处于筛选态之外）' % (sid, s.get('day')))
        for extra in (s.get('days') or []):
            if extra not in day_ns:
                warn('%s 的 days 里含不存在的 DAY %s' % (sid, extra))

    nav = trip.get('navData') or {}
    for key, pts in nav.items():
        if not isinstance(pts, list) or len(pts) < 2:
            err('navData[%s] 点数不足 2，无法成线' % key)
            continue
        for p in pts:
            if not (isinstance(p, (list, tuple)) and len(p) == 2):
                err('navData[%s] 里有点不是 [lat,lng] 两元组' % key)
                break
            if not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180):
                err('navData[%s] 坐标越界：%s' % (key, p))
                break

    for d in days:
        for i, leg in enumerate(d.get('legs') or []):
            f, t = leg.get('from'), leg.get('to')
            if f not in ids:
                err('DAY %s 第 %d 段 from=%s 不存在' % (d.get('n'), i + 1, f))
            if t not in ids:
                err('DAY %s 第 %d 段 to=%s 不存在' % (d.get('n'), i + 1, t))
            if not leg.get('label'):
                warn('DAY %s 第 %d 段没有 label，直线/导航视图的说明会缺失' % (d.get('n'), i + 1))
            k = leg.get('nav')
            if k and k not in nav:
                warn('DAY %s 第 %d 段声明 nav=%s，但 navData 里没有这条线，只能退回直线' % (d.get('n'), i + 1, k))
            # 端点偏差：路网线首尾应当贴着两个 POI，否则说明取错了点（比如反了、或取到隔壁景区）
            if k in nav and f in coords and t in coords:
                pts = nav[k]
                try:
                    d0 = haversine(coords[f], pts[0])
                    d1 = haversine(coords[t], pts[-1])
                except Exception:
                    continue
                if min(d0, haversine(coords[f], pts[-1])) < 4000 and d1 > 4000:
                    warn('navData[%s] 终点离 %s 约 %.1fkm，可能取反了方向' % (k, t, d1 / 1000.0))
                elif d0 > 4000:
                    warn('navData[%s] 起点离 %s 约 %.1fkm' % (k, f, d0 / 1000.0))

    views = trip.get('views') or {}
    if 'all' not in views:
        warn("views 缺 'all'（默认总览视角），首屏会退到第一天")
    for d in days:
        if str(d.get('n')) not in views:
            warn('views 缺 DAY %s 的视角，点这天时不会移动地图' % d.get('n'))
        v = views.get(str(d.get('n')))
        if v and (not v.get('center') or len(v['center']) != 2):
            warn('views[%s].center 格式不对，应为 [lat, lng]' % d.get('n'))


def mileage_report(trip):
    """用路网 polyline 反算每天的真实里程，和作者标注的距离对一对。"""
    nav = trip.get('navData') or {}
    lines = []
    for d in sorted(trip.get('days') or [], key=lambda x: x.get('n', 0)):
        km = 0.0
        for leg in d.get('legs') or []:
            pts = nav.get(leg.get('nav') or '')
            if not pts or len(pts) < 2:
                continue
            for i in range(1, len(pts)):
                km += haversine(pts[i - 1], pts[i]) / 1000.0
        if km > 0:
            stated = d.get('distanceKm')
            tag = ''
            if isinstance(stated, (int, float)):
                diff = km - stated
                tag = '（标注 %skm，偏差 %+.0f%%）' % (stated, diff / max(stated, 1) * 100)
                if abs(diff) > max(20, stated * 0.25):
                    warn('DAY %s 路网实测 %.0fkm 与标注 %skm 偏差较大，核对一下是否绕路或标注写错'
                         % (d.get('n'), km, stated))
            lines.append('  DAY %-2s 路网实测 %5.0f km%s' % (d.get('n'), km, tag))
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('data', help='trip-data.json 路径')
    ap.add_argument('-o', '--out', help='输出 HTML 路径（默认同名 .html）')
    ap.add_argument('-t', '--template', default=TEMPLATE)
    ap.add_argument('--strict', action='store_true', help='有 error 时以退出码 1 结束')
    args = ap.parse_args()

    with open(args.data, 'r', encoding='utf-8') as f:
        trip = json.load(f)

    validate(trip)

    with open(args.template, 'r', encoding='utf-8') as f:
        tpl = f.read()
    if PLACEHOLDER not in tpl:
        sys.exit('模板里找不到注入点 %s，检查 --template 是否被改坏' % PLACEHOLDER)

    payload = json.dumps(trip, ensure_ascii=False, separators=(',', ':'))
    # 防止数据里出现 </script> 把脚本标签提前闭合
    payload = payload.replace('</', '<\\/')
    html = tpl.replace(PLACEHOLDER, '/*__TRIP_DATA__*/' + payload)

    out = args.out or (os.path.splitext(args.data)[0] + '.html')
    with open(out, 'w', encoding='utf-8') as f:
        f.write(html)

    trips = sorted(trip.get('days') or [], key=lambda x: x.get('n', 0))
    n_spots = len(trip.get('spots') or [])
    n_nav = len(trip.get('navData') or {})
    n_pts = sum(len(v) for v in (trip.get('navData') or {}).values())

    print('=' * 62)
    print('行程图已生成：%s' % os.path.abspath(out))
    print('  天数 %d · 标点 %d · 路网 %d 段 / %d 节点 · 体积 %.0f KB'
          % (len(trips), n_spots, n_nav, n_pts, os.path.getsize(out) / 1024.0))
    print('  标题：%s' % (trip.get('meta', {}).get('title') or '(空)'))
    report = mileage_report(trip)
    if report:
        print('里程自检（沿路网累加，仅驾车段）：')
        print('\n'.join(report))
    for n in notes:
        print('  · ' + n)
    if warns:
        print('告警 %d 条：' % len(warns))
        for w in warns:
            print('  ! ' + w)
    if errors:
        print('错误 %d 条（必须修）：' % len(errors))
        for e in errors:
            print('  x ' + e)
    print('=' * 62)

    if errors and args.strict:
        sys.exit(1)


if __name__ == '__main__':
    main()
