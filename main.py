#!/usr/bin/env python3
"""
nearest: 座標から1番近い駅（など、選んだ種類の場所）を探して JSON で出力する。

起点は名前をつけた座標の thing（「自宅」「会社」など、MyWant に座標が記録されて
いるもの）か、"35.69,139.68" のような緯度,経度。thing の名前は MyWant の API
（MYWANT_URL、既定 http://localhost:8080）で座標に引き直す。

駅は HeartRails Express（日本の駅の API、キー不要）に尋ねる。速く、落ちにくく、路線も
わかる。日本の外や、そこが答えないときは OpenStreetMap に。駅以外の種類は
OpenStreetMap だけで、Overpass API（キー不要）に起点の周りを半径を広げながら尋ねる。
公開の Overpass はよく混むので、間を広げながら何度か試す（overpass を参照）。

見つかったものは起点からの直線距離で並べ、名前が同じもの（JR と地下鉄で別の点に
なっている同じ駅など）はいちばん近い点だけ残す。

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
  "candidates": [{"name": "中野坂上", "distance_m": 520, "lat": ..., "lng": ...,
                  "lines": ["都営大江戸線", ...]}, ...],   // 近い順に5件。lines は HeartRails の駅だけ
  "source": "heartrails"               // heartrails / openstreetmap
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

HEARTRAILS_URL = "https://express.heartrails.com/api/json"

# 公開の Overpass はよく混んで 504 を、続けて尋ねると 429 を返す。混んでいても少しおけば
# 答えることが多い（同じ問い合わせが 504 の次に 2 秒で返る）ので、間を広げながら
# 本家に試し直す（429 には Retry-After を守る）。ほかの公開サーバー（mail.ru、
# private.coffee）は試したが、ここからは本家より答えなかったので使わない。
# (サーバー, その前に待つ秒)。
OVERPASS_MAIN = "https://overpass-api.de/api/interpreter"
OVERPASS_TRIES = tuple((OVERPASS_MAIN, wait) for wait in (0, 5, 10, 15, 25, 40))
OVERPASS_TIMEOUT_SECONDS = 20
# 探し始めてからここまでで諦める。agent.yaml の timeout_seconds（300）より手前で、
# 理由を書いて終われるように。
DEADLINE_SECONDS = 240
STARTED = time.monotonic()
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
    out = {"_message": message}
    if percentage is not None:
        out["_progress"] = percentage
    print(json.dumps(out, ensure_ascii=False), flush=True)


def error_out(message: str) -> None:
    # "error" ではなく "failure" で、終了コード 0 で返す。MyWant の実行側は "error" を
    # 持つ出力や 0 以外の終了を「実行の失敗」として捨て、state に何も書かないので、
    # want の error 欄（finalizeWhen.failed が見る）に理由が届かなくなる。
    # summary も空で返す：前の検索の答えが残っていると、want は失敗と達成の両方になる。
    print(json.dumps({"failure": message, "summary": ""}, ensure_ascii=False), flush=True)
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


def get_json(url, data=None, timeout=TIMEOUT_SECONDS):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.load(res)


def search_heartrails(lat, lng, max_radius):
    """日本の駅を近い順に。答えがない（日本の外）か届かないときは None。"""
    url = HEARTRAILS_URL + "?" + urllib.parse.urlencode({"method": "getStations", "x": lng, "y": lat})
    try:
        stations = (get_json(url).get("response") or {}).get("station") or []
    except (urllib.error.URLError, OSError, ValueError):
        return None
    best = {}
    for st in stations:
        try:
            slat, slng = float(st["y"]), float(st["x"])
        except (KeyError, TypeError, ValueError):
            continue
        name, line = st.get("name") or "", st.get("line") or ""
        dist = round(haversine_m(lat, lng, slat, slng))
        if not name or dist > max_radius:
            continue
        # 同じ駅が路線ごとに並ぶ：いちばん近い点に、路線をまとめる。
        c = best.setdefault(name, {"name": name, "distance_m": dist, "lat": slat, "lng": slng, "lines": []})
        if dist < c["distance_m"]:
            c.update(distance_m=dist, lat=slat, lng=slng)
        if line and line not in c["lines"]:
            c["lines"].append(line)
    return sorted(best.values(), key=lambda c: c["distance_m"]) or None


def overpass(lat, lng, radius, cond):
    query = f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];nwr(around:{int(radius)},{lat},{lng}){cond};out center tags 200;"
    data = urllib.parse.urlencode({"data": query}).encode()
    problems = []
    wait_more = 0
    for url, wait in OVERPASS_TRIES:
        wait = max(wait, wait_more)
        if time.monotonic() - STARTED + wait + OVERPASS_TIMEOUT_SECONDS > DEADLINE_SECONDS:
            break
        if wait:
            report_progress(None, f"OpenStreetMap が混んでいるので{wait}秒待ちます")
            time.sleep(wait)
        wait_more = 0
        host = urllib.parse.urlparse(url).hostname
        try:
            return get_json(url, data, timeout=OVERPASS_TIMEOUT_SECONDS).get("elements") or []
        except urllib.error.HTTPError as e:
            problems.append(f"{host}: HTTP {e.code}")
            if e.code == 429:
                try:
                    wait_more = min(60, int(e.headers.get("Retry-After") or 30))
                except ValueError:
                    wait_more = 30
        except (urllib.error.URLError, OSError, ValueError) as e:
            problems.append(f"{host}: {e}")
    error_out("OpenStreetMap（Overpass）が混んでいて答えませんでした。しばらくして試し直してください — " + "; ".join(problems))


def search_openstreetmap(lat, lng, max_radius, target, word):
    # 近くから広げる：街中なら最初の輪で足り、Overpass への負担も小さい。
    radii = [r for r in (800, 2000, 5000, 15000, 50000) if r < max_radius] + [max_radius]
    elements = []
    for i, radius in enumerate(radii):
        report_progress(30 + 60 * i // len(radii), f"半径{radius}mで{word}を探しています")
        elements = overpass(lat, lng, radius, TARGETS[target])
        if elements:
            break
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
    return sorted(best.values(), key=lambda c: c["distance_m"])


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

    word = TARGET_WORDS[target]
    candidates, source = None, "openstreetmap"
    if target == "station":
        report_progress(30, "駅を探しています")
        candidates, source = search_heartrails(lat, lng, max_radius), "heartrails"
    if not candidates:
        candidates, source = search_openstreetmap(lat, lng, max_radius, target, word), "openstreetmap"
    if not candidates:
        error_out(f"半径{max_radius}m以内に{word}が見つかりませんでした")
    candidates = candidates[:CANDIDATES]
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
        "source": source,
        # 前の検索が失敗していたら、その理由を消す（error_out の summary と対）。
        "failure": "",
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
