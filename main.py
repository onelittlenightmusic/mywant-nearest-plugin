#!/usr/bin/env python3
"""
nearest: 座標から1番近い駅（など、選んだ種類の場所）を OpenStreetMap で探して JSON で出力する。

起点は名前をつけた座標の thing（「自宅」「会社」など、MyWant に座標が記録されて
いるもの）か、"35.69,139.68" のような緯度,経度。thing の名前は MyWant の API
（MYWANT_URL、既定 http://localhost:8080）で座標に引き直す。

探す場所は Overpass API（OpenStreetMap、キー不要）に、起点の周りを半径を広げながら
尋ねる。見つかったものは起点からの直線距離で並べ、名前が同じもの（JR と地下鉄で
別の点になっている同じ駅など）はいちばん近い点だけ残す。

使い方:
  python3 main.py <起点> [種類] [最大半径m]

  種類: station（既定）, bus_stop, convenience_store, cafe, restaurant,
        hospital, park, toilets

出力形式 (JSON):
{
  "origin": "自宅", "origin_coordinate": {"lat": 35.69, "lng": 139.68, "type": "location_coordinate"},
  "target": "station",
  "summary": "中野坂上（520m）",
  "nearest": "中野坂上", "distance_m": 520,
  "nearest_coordinate": {"lat": 35.69, "lng": 139.68, "type": "location_coordinate"},
  "nearest_station": "中野坂上",      // target が station のときだけ。他は ""
  "candidates": [{"name": "中野坂上", "distance_m": 520, "lat": ..., "lng": ...}, ...]  // 近い順に5件
}
失敗したときは {"failure": "..."}（終了コード 0。error_out を参照）。
"""
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 公開の Overpass はよく混んで 429 / 504 を返す。本家は混んでいても数秒おけば答える
# ことが多いので、間をおいて本家を試し直し、その合間にほかの公開サーバーも試す。
OVERPASS_MAIN = "https://overpass-api.de/api/interpreter"
OVERPASS_TRIES = (
    OVERPASS_MAIN,
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    OVERPASS_MAIN,
    "https://overpass.private.coffee/api/interpreter",
    OVERPASS_MAIN,
)
OVERPASS_TIMEOUT_SECONDS = 15
MYWANT_URL = os.environ.get("MYWANT_URL", "http://localhost:8080")
# Overpass が User-Agent のない問い合わせを断るため、名乗る。
USER_AGENT = "mywant-nearest-plugin/1.0 (+https://github.com/onelittlenightmusic/mywant)"
TIMEOUT_SECONDS = 30
CANDIDATES = 5

# 種類 → OpenStreetMap のタグ条件（Overpass の書き方）。
TARGETS = {
    "station": '["railway"~"^(station|halt)$"]["name"]',
    "bus_stop": '["highway"="bus_stop"]["name"]',
    "convenience_store": '["shop"="convenience"]',
    "cafe": '["amenity"="cafe"]',
    "restaurant": '["amenity"="restaurant"]',
    "hospital": '["amenity"="hospital"]',
    "park": '["leisure"="park"]',
    "toilets": '["amenity"="toilets"]',
}
TARGET_WORDS = {
    "station": "駅", "bus_stop": "バス停", "convenience_store": "コンビニ", "cafe": "カフェ",
    "restaurant": "レストラン", "hospital": "病院", "park": "公園", "toilets": "トイレ",
}

# 座標を持ちうる thing の種類。名前をつける画面は値をその欄の subType で分けるので、
# 同じ「場所」がいくつかの種類に入りうる（place_arrival の resolvePlaceCoords と同じ）。
COORD_KINDS = ("place", "location_coordinate", "location", "coordinate", "address")


def report_progress(percentage, message=""):
    print(json.dumps({"_progress": percentage, "_message": message}, ensure_ascii=False), flush=True)


def error_out(message: str) -> None:
    # "error" ではなく "failure" で、終了コード 0 で返す。MyWant の実行側は "error" を
    # 持つ出力や 0 以外の終了を「実行の失敗」として捨て、state に何も書かないので、
    # want の error 欄（finalizeWhen.failed が見る）に理由が届かなくなる。
    print(json.dumps({"failure": message}, ensure_ascii=False), flush=True)
    sys.exit(0)


def coord_of(value):
    """{lat, lng} の形の値から (lat, lng) を取り出す。なければ None。"""
    if not isinstance(value, dict):
        return None
    lat = next((value[k] for k in ("lat", "latitude") if k in value), None)
    lng = next((value[k] for k in ("lng", "lon", "longitude") if k in value), None)
    try:
        return float(lat), float(lng)
    except (TypeError, ValueError):
        return None


def resolve_origin(origin: str):
    """起点を (lat, lng) に。"緯度,経度" ならそのまま、名前なら MyWant の thing から。"""
    m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$", origin)
    if m:
        return float(m.group(1)), float(m.group(2))
    try:
        req = urllib.request.Request(f"{MYWANT_URL}/api/v1/things", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            things = json.load(res).get("things") or []
    except (urllib.error.URLError, OSError, ValueError) as e:
        error_out(f"MyWant に「{origin}」の座標を尋ねられませんでした: {e}")
    found = []
    for t in things:
        for d in t.get("definitions") or []:
            if origin not in (d.get("name"), t.get("value")):
                continue
            c = coord_of(d.get("value"))
            if c:
                kind = d.get("subtype") or t.get("subtype") or ""
                found.append((COORD_KINDS.index(kind) if kind in COORD_KINDS else len(COORD_KINDS), c))
    if not found:
        error_out(f"「{origin}」の座標が見つかりません。座標に名前をつけた場所を選ぶか、35.69,139.68 のように緯度,経度で書いてください")
    return min(found)[1]


def haversine_m(lat1, lng1, lat2, lng2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def overpass(lat, lng, radius, cond):
    query = f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];nwr(around:{int(radius)},{lat},{lng}){cond};out center tags 200;"
    data = urllib.parse.urlencode({"data": query}).encode()
    problems = []
    for i, url in enumerate(OVERPASS_TRIES):
        if i:
            time.sleep(2 * i)
        req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT_SECONDS) as res:
                return json.load(res).get("elements") or []
        except urllib.error.HTTPError as e:
            problems.append(f"{urllib.parse.urlparse(url).hostname}: HTTP {e.code}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            problems.append(f"{urllib.parse.urlparse(url).hostname}: {e}")
    error_out("OpenStreetMap（Overpass）がどこも答えませんでした — " + "; ".join(problems))


def main(args):
    if not args:
        error_out("使い方: python3 main.py <起点> [種類] [最大半径m]")
    origin = args[0]
    target = args[1] if len(args) > 1 and args[1] else "station"
    if target not in TARGETS:
        error_out(f"種類「{target}」は探せません。選べるのは {', '.join(TARGETS)} です")
    try:
        max_radius = max(100, int(float(args[2]))) if len(args) > 2 and args[2] else 5000
    except ValueError:
        error_out(f"最大半径「{args[2]}」は数ではありません")

    report_progress(10, f"「{origin}」の座標を確かめています")
    lat, lng = resolve_origin(origin)

    # 近くから広げる：街中なら最初の輪で足り、Overpass への負担も小さい。
    radii = [r for r in (800, 2000, 5000, 15000, 50000) if r < max_radius] + [max_radius]
    word = TARGET_WORDS[target]
    elements = []
    for i, radius in enumerate(radii):
        report_progress(20 + 60 * i // len(radii), f"半径{radius}mで{word}を探しています")
        elements = overpass(lat, lng, radius, TARGETS[target])
        if elements:
            break
    if not elements:
        error_out(f"半径{max_radius}m以内に{word}が見つかりませんでした")

    best = {}
    for e in elements:
        elat = e.get("lat", (e.get("center") or {}).get("lat"))
        elng = e.get("lon", (e.get("center") or {}).get("lon"))
        if elat is None or elng is None:
            continue
        tags = e.get("tags") or {}
        name = tags.get("name") or tags.get("name:ja") or tags.get("name:en") or f"（名前のない{word}）"
        dist = round(haversine_m(lat, lng, elat, elng))
        if name not in best or dist < best[name]["distance_m"]:
            best[name] = {"name": name, "distance_m": dist, "lat": elat, "lng": elng}
    candidates = sorted(best.values(), key=lambda c: c["distance_m"])[:CANDIDATES]
    if not candidates:
        error_out(f"{word}は見つかりましたが、場所が読めませんでした")
    top = candidates[0]

    report_progress(100, "見つかりました")
    print(json.dumps({
        "origin": origin,
        "origin_coordinate": {"lat": lat, "lng": lng, "type": "location_coordinate"},
        "target": target,
        "summary": f"{top['name']}（{top['distance_m']}m）",
        "nearest": top["name"],
        "distance_m": top["distance_m"],
        "nearest_coordinate": {"lat": top["lat"], "lng": top["lng"], "type": "location_coordinate"},
        "nearest_station": top["name"] if target == "station" else "",
        "candidates": candidates,
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
