import argparse
import html
import json
import math
import re
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA_JSON = ROOT / "moscow-road-data.json"
HTML_FILE = ROOT / "moscow-roads-3d.html"
ELEVATION_CACHE = ROOT / "elevation-cache.json"
TILE_DIR = ROOT / "tiles"

PLACE_NAME = "Moscow, Russia"
ROAD_CLASSES = {"motorway", "trunk", "primary", "secondary"}
DEFAULT_BUILDING_HEIGHT_M = 12.0
LEVEL_HEIGHT_M = 3.0


def is_finite_number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def mercator_meters(lat, lon):
    radius = 6378137.0
    clamped_lat = max(-85.05112878, min(85.05112878, lat))
    x = radius * math.radians(lon)
    y = radius * math.log(math.tan(math.pi / 4 + math.radians(clamped_lat) / 2))
    return x, y


def inverse_mercator_meters(x, y):
    radius = 6378137.0
    lon = math.degrees(x / radius)
    lat = math.degrees(2 * math.atan(math.exp(y / radius)) - math.pi / 2)
    return lat, lon


def geometry_parts(geometry):
    if geometry is None or geometry.is_empty:
        return []
    kind = geometry.geom_type
    if kind in {"LineString", "Polygon"}:
        return [geometry]
    if kind.startswith("Multi") or kind == "GeometryCollection":
        return [part for part in geometry.geoms if not part.is_empty]
    return []


def normalize_highway(value):
    if isinstance(value, list):
        values = value
    elif isinstance(value, tuple):
        values = list(value)
    else:
        values = [value]
    return [str(item) for item in values if item]


def has_target_highway(value, allowed=ROAD_CLASSES):
    return any(item in allowed for item in normalize_highway(value))


def parse_meters(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    text = str(value).strip().lower().replace(",", ".")
    if not text or text in {"nan", "none"}:
        return None
    feet = re.search(r"(-?\d+(?:\.\d+)?)\s*(?:ft|feet|')", text)
    if feet:
        return max(0.0, float(feet.group(1)) * 0.3048)
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return max(0.0, float(match.group(0)))


def building_height(tags):
    height = parse_meters(tags.get("height"))
    if height and height > 0:
        return round(height, 2), "height"
    levels = parse_meters(tags.get("building:levels"))
    if levels and levels > 0:
        return round(levels * LEVEL_HEIGHT_M, 2), "building:levels"
    return DEFAULT_BUILDING_HEIGHT_M, "default"


def point_key(lat, lon):
    return f"{lat:.6f},{lon:.6f}"


def load_elevation_cache(path=ELEVATION_CACHE):
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_elevation_cache(cache, path=ELEVATION_CACHE):
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def fetch_elevations(points, retries=4):
    body = "\n".join(f"{lat:.6f} {lon:.6f}" for lat, lon in points)
    request = urllib.request.Request(
        "https://elevation.nakarte.me/",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "text/plain;charset=UTF-8",
            "Origin": "https://nakarte.me",
            "Referer": "https://nakarte.me/",
            "Accept": "*/*",
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                values = response.read().decode("utf-8").split()
            return [float(value) for value in values]
        except (TimeoutError, urllib.error.URLError) as error:
            if attempt >= retries:
                raise
            wait_s = min(25, 3 * (attempt + 1))
            print(f"Nakarte elevation request failed ({error}); retrying in {wait_s}s")
            time.sleep(wait_s)


def elevations_for_points(points, cache_path=ELEVATION_CACHE, batch_size=250, pause_s=0.2):
    cache = load_elevation_cache(cache_path)
    missing = []
    seen = set()
    for lat, lon in points:
        key = point_key(lat, lon)
        if key not in cache and key not in seen:
            missing.append((round(lat, 6), round(lon, 6)))
            seen.add(key)

    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        print(f"Fetching elevations {start + 1}-{start + len(batch)} of {len(missing)}")
        elevations = fetch_elevations(batch)
        if len(elevations) != len(batch):
            raise RuntimeError(f"Got {len(elevations)} elevations for {len(batch)} points")
        for (lat, lon), elevation in zip(batch, elevations):
            cache[point_key(lat, lon)] = round(float(elevation), 2)
        save_elevation_cache(cache, cache_path)
        if pause_s and start + batch_size < len(missing):
            time.sleep(pause_s)

    return [cache[point_key(lat, lon)] for lat, lon in points]


def line_sample_points(line, spacing_m=140):
    length = line.length
    if length <= spacing_m:
        distances = [0, length]
    else:
        count = max(2, int(math.ceil(length / spacing_m)) + 1)
        distances = [length * index / (count - 1) for index in range(count)]
    points = []
    for distance in distances:
        point = line.interpolate(distance)
        lat, lon = inverse_mercator_meters(point.x, point.y)
        points.append((lat, lon, point.x, point.y))
    return points


def polygon_outer_ring(polygon):
    if polygon.geom_type == "Polygon":
        return polygon.exterior
    if polygon.geom_type == "MultiPolygon":
        largest = max(polygon.geoms, key=lambda part: part.area)
        return largest.exterior
    return None


def validate_payload(payload):
    errors = []
    for key in ("origin", "bounds", "roads", "buildings", "meta"):
        if key not in payload:
            errors.append(f"missing {key}")
    for road_index, road in enumerate(payload.get("roads", [])):
        points = road.get("points", [])
        if len(points) < 2:
            errors.append(f"roads[{road_index}] has fewer than two points")
        for point_index, point in enumerate(points[:200]):
            for key in ("x", "z", "lat", "lon", "elevation"):
                if not is_finite_number(point.get(key)):
                    errors.append(f"roads[{road_index}].points[{point_index}].{key} is not finite")
    for building_index, building in enumerate(payload.get("buildings", [])):
        for key in ("height", "baseElevation", "x", "z"):
            if not is_finite_number(building.get(key)):
                errors.append(f"buildings[{building_index}].{key} is not finite")
        if len(building.get("footprint", [])) < 3:
            errors.append(f"buildings[{building_index}] footprint has fewer than three points")
    if errors:
        raise ValueError("; ".join(errors[:25]))


def tile_key_for_point(x, z, bounds, tile_size_m):
    col = math.floor((x - bounds["minX"]) / tile_size_m)
    row = math.floor((z - bounds["minZ"]) / tile_size_m)
    return int(col), int(row)


def tile_id(col, row):
    return f"{col}_{row}"


def feature_bounds(points):
    return {
        "minX": min(point["x"] for point in points),
        "maxX": max(point["x"] for point in points),
        "minZ": min(point["z"] for point in points),
        "maxZ": max(point["z"] for point in points),
    }


def intersecting_tile_keys(bounds, map_bounds, tile_size_m):
    min_col, min_row = tile_key_for_point(bounds["minX"], bounds["minZ"], map_bounds, tile_size_m)
    max_col, max_row = tile_key_for_point(bounds["maxX"], bounds["maxZ"], map_bounds, tile_size_m)
    keys = []
    for col in range(min_col, max_col + 1):
        for row in range(min_row, max_row + 1):
            keys.append((col, row))
    return keys


def build_tile_package(payload, tile_dir=TILE_DIR, tile_size_m=2500):
    tile_dir.mkdir(exist_ok=True)
    for path in tile_dir.glob("tile-*.json"):
        path.unlink()

    bounds = payload["bounds"]
    tiles = {}
    elevations = []

    def ensure_tile(col, row):
        key = tile_id(col, row)
        if key not in tiles:
            min_x = bounds["minX"] + col * tile_size_m
            min_z = bounds["minZ"] + row * tile_size_m
            tiles[key] = {
                "id": key,
                "col": col,
                "row": row,
                "bounds": {
                    "minX": round(min_x, 3),
                    "minZ": round(min_z, 3),
                    "maxX": round(min_x + tile_size_m, 3),
                    "maxZ": round(min_z + tile_size_m, 3),
                },
                "roads": [],
                "buildings": [],
            }
        return tiles[key]

    for road in payload["roads"]:
        road_bounds = feature_bounds(road["points"])
        for point in road["points"]:
            elevations.append(point["elevation"])
        for col, row in intersecting_tile_keys(road_bounds, bounds, tile_size_m):
            ensure_tile(col, row)["roads"].append(road)

    for building in payload["buildings"]:
        building_bounds = feature_bounds(building["footprint"])
        elevations.append(building["baseElevation"] + building["height"])
        for col, row in intersecting_tile_keys(building_bounds, bounds, tile_size_m):
            ensure_tile(col, row)["buildings"].append(building)

    tile_summaries = []
    for key, tile in sorted(tiles.items()):
        tile_path = tile_dir / f"tile-{key}.json"
        tile_path.write_text(json.dumps(tile, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tile_summaries.append(
            {
                "id": key,
                "col": tile["col"],
                "row": tile["row"],
                "url": f"tile-{key}.json",
                "bounds": tile["bounds"],
                "roadCount": len(tile["roads"]),
                "buildingCount": len(tile["buildings"]),
            }
        )

    index = {
        "origin": payload["origin"],
        "bounds": bounds,
        "meta": {
            **payload["meta"],
            "tileSizeM": tile_size_m,
            "tileCount": len(tile_summaries),
            "minElevation": round(min(elevations), 2) if elevations else 0,
        },
        "tiles": tile_summaries,
    }
    (tile_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def build_payload(
    place=PLACE_NAME,
    buffer_m=120,
    simplify_m=18,
    max_buildings=12000,
    road_spacing_m=140,
    elevation_batch_size=250,
    all_drive_roads=False,
    reuse_existing_buildings=False,
):
    import geopandas as gpd
    import osmnx as ox

    ox.settings.use_cache = True
    ox.settings.log_console = True
    ox.settings.requests_timeout = 600

    area = ox.geocode_to_gdf(place).iloc[0].geometry
    graph = ox.graph_from_polygon(area, network_type="drive", simplify=True)
    _, edges = ox.graph_to_gdfs(graph)
    if not all_drive_roads:
        edges = edges[edges["highway"].apply(has_target_highway)].copy()
    if edges.empty:
        raise RuntimeError("No target highways found")

    edges_3857 = edges.to_crs(3857)
    edges_3857["geometry"] = edges_3857.geometry.simplify(simplify_m, preserve_topology=True)
    road_buffer_3857 = edges_3857.geometry.buffer(buffer_m).union_all()
    reused_payload = json.loads(DATA_JSON.read_text(encoding="utf-8")) if reuse_existing_buildings and DATA_JSON.exists() else None
    if reused_payload:
        buildings_3857 = None
    else:
        road_buffer_4326 = gpd.GeoSeries([road_buffer_3857], crs=3857).to_crs(4326).iloc[0]

        tags = {"building": True}
        buildings = ox.features_from_polygon(road_buffer_4326, tags)
        buildings = buildings[buildings.geometry.notna()].copy()
        buildings = buildings[buildings.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        buildings_3857 = buildings.to_crs(3857)
        buildings_3857 = buildings_3857[buildings_3857.intersects(road_buffer_3857)].copy()
        buildings_3857["area_m2"] = buildings_3857.geometry.area
        buildings_3857 = buildings_3857.sort_values("area_m2", ascending=False)
        if max_buildings and len(buildings_3857) > max_buildings:
            buildings_3857 = buildings_3857.head(max_buildings).copy()

    all_bounds = list(edges_3857.total_bounds)
    if reused_payload:
        origin = reused_payload["origin"]
        building_local_bounds = reused_payload["bounds"]
        all_bounds = [
            min(all_bounds[0], origin["x"] + building_local_bounds["minX"]),
            min(all_bounds[1], origin["y"] + building_local_bounds["minZ"]),
            max(all_bounds[2], origin["x"] + building_local_bounds["maxX"]),
            max(all_bounds[3], origin["y"] + building_local_bounds["maxZ"]),
        ]
    elif not buildings_3857.empty:
        building_bounds = list(buildings_3857.total_bounds)
        all_bounds = [
            min(all_bounds[0], building_bounds[0]),
            min(all_bounds[1], building_bounds[1]),
            max(all_bounds[2], building_bounds[2]),
            max(all_bounds[3], building_bounds[3]),
        ]
    if not reused_payload:
        origin = {"x": (all_bounds[0] + all_bounds[2]) / 2, "y": (all_bounds[1] + all_bounds[3]) / 2}

    road_requests = []
    road_samples = []
    for index, (_, row) in enumerate(edges_3857.iterrows()):
        for part in geometry_parts(row.geometry):
            if part.geom_type != "LineString" or part.length < 20:
                continue
            samples = line_sample_points(part, road_spacing_m)
            road_samples.append((index, row, samples))
            road_requests.extend((lat, lon) for lat, lon, _, _ in samples)

    building_requests = []
    building_rows = []
    if not reused_payload:
        for index, (_, row) in enumerate(buildings_3857.iterrows()):
            ring = polygon_outer_ring(row.geometry)
            if ring is None:
                continue
            simplified = row.geometry.simplify(max(1.5, simplify_m * 0.35), preserve_topology=True)
            ring = polygon_outer_ring(simplified)
            if ring is None:
                continue
            centroid = row.geometry.representative_point()
            lat, lon = inverse_mercator_meters(centroid.x, centroid.y)
            building_requests.append((lat, lon))
            building_rows.append((index, row, simplified, centroid, lat, lon))

    requested_points = road_requests + building_requests
    elevations = elevations_for_points(requested_points, batch_size=elevation_batch_size) if requested_points else []
    road_elevations = elevations[: len(road_requests)]
    building_elevations = elevations[len(road_requests) :]

    roads = []
    elevation_offset = 0
    for road_id, row, samples in road_samples:
        road_points = []
        for lat, lon, x, y in samples:
            road_points.append(
                {
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "x": round(x - origin["x"], 3),
                    "z": round(y - origin["y"], 3),
                    "elevation": road_elevations[elevation_offset],
                }
            )
            elevation_offset += 1
        highways = normalize_highway(row.get("highway"))
        roads.append(
            {
                "id": f"road-{len(roads)}",
                "name": str(row.get("name") or highways[0] if highways else "road"),
                "highway": highways[0] if highways else "road",
                "points": road_points,
            }
        )

    if reused_payload:
        buildings_payload = reused_payload["buildings"]
    else:
        buildings_payload = []
        for (index, row, simplified, centroid, lat, lon), elevation in zip(building_rows, building_elevations):
            ring = polygon_outer_ring(simplified)
            coords = list(ring.coords)
            if len(coords) > 80:
                step = math.ceil(len(coords) / 80)
                coords = coords[::step]
            footprint = [{"x": round(x - origin["x"], 3), "z": round(y - origin["y"], 3)} for x, y in coords[:-1]]
            if len(footprint) < 3:
                continue
            height, height_source = building_height(row)
            buildings_payload.append(
                {
                    "id": f"building-{len(buildings_payload)}",
                    "name": "" if not row.get("name") or str(row.get("name")) == "nan" else str(row.get("name")),
                    "building": "" if not row.get("building") or str(row.get("building")) == "nan" else str(row.get("building")),
                    "height": height,
                    "heightSource": height_source,
                    "baseElevation": round(elevation, 2),
                    "lat": round(lat, 6),
                    "lon": round(lon, 6),
                    "x": round(centroid.x - origin["x"], 3),
                    "z": round(centroid.y - origin["y"], 3),
                    "footprint": footprint,
                }
            )

    payload = {
        "origin": {"x": round(origin["x"], 3), "y": round(origin["y"], 3)},
        "bounds": {
            "minX": round(all_bounds[0] - origin["x"], 3),
            "minZ": round(all_bounds[1] - origin["y"], 3),
            "maxX": round(all_bounds[2] - origin["x"], 3),
            "maxZ": round(all_bounds[3] - origin["y"], 3),
            "widthM": round(all_bounds[2] - all_bounds[0], 3),
            "depthM": round(all_bounds[3] - all_bounds[1], 3),
        },
        "roads": roads,
        "buildings": buildings_payload,
        "meta": {
            "place": place,
            "roadClasses": "all_drive" if all_drive_roads else sorted(ROAD_CLASSES),
            "allDriveRoads": all_drive_roads,
            "reusedBuildings": bool(reused_payload),
            "bufferM": buffer_m,
            "simplifyM": simplify_m,
            "roadSpacingM": road_spacing_m,
            "roadCount": len(roads),
            "buildingCount": len(buildings_payload),
            "maxBuildings": max_buildings,
            "elevationSource": "https://elevation.nakarte.me/",
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    validate_payload(payload)
    return payload


def build_html(payload=None):
    if payload is None:
        payload = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    title = html.escape(f"3D магистрали Москвы: {payload['meta']['roadCount']} дорог")
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #081016;
      --panel: rgba(12, 18, 24, 0.88);
      --line: rgba(217, 226, 236, 0.18);
      --text: #edf4f8;
      --muted: #a7b4bd;
      --accent: #7ee787;
      --road1: #ffcc33;
      --road2: #ff5a3c;
      --road3: #38bdf8;
      --road4: #b5f36d;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100%; margin: 0; overflow: hidden; background: var(--bg); color: var(--text); font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif; }}
    canvas {{ display: block; width: 100%; height: 100%; touch-action: none; }}
    .hud {{ position: fixed; top: 16px; left: 16px; width: min(360px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto; padding: 14px; border: 1px solid var(--line); background: var(--panel); backdrop-filter: blur(18px); border-radius: 8px; z-index: 10; box-shadow: 0 18px 60px rgba(0,0,0,.35); }}
    h1 {{ margin: 0 0 8px; font-size: 18px; line-height: 1.15; font-weight: 760; }}
    .meta {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }}
    .stat {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px; min-width: 0; }}
    .stat b {{ display: block; font-size: 17px; line-height: 1.05; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .stat span {{ display: block; margin-top: 4px; color: var(--muted); font-size: 11px; }}
    .row {{ display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 10px; min-height: 38px; border-top: 1px solid var(--line); padding: 9px 0; }}
    .row label {{ color: var(--muted); font-size: 13px; }}
    input[type=range] {{ width: 142px; accent-color: var(--accent); }}
    button {{ min-height: 36px; border: 1px solid var(--line); color: var(--text); background: rgba(255,255,255,.06); border-radius: 6px; padding: 0 10px; font: inherit; cursor: pointer; }}
    button[aria-pressed=true], button.primary {{ border-color: rgba(126,231,135,.55); background: rgba(126,231,135,.16); }}
    .actions {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 10px; }}
    .readout {{ margin-top: 10px; color: var(--muted); font-size: 12px; line-height: 1.35; }}
    .menu-toggle {{ display: none; position: fixed; top: 12px; left: 12px; z-index: 13; width: 44px; height: 44px; padding: 0; font-size: 24px; }}
    .scrim {{ display: none; position: fixed; inset: 0; z-index: 9; background: rgba(0,0,0,.42); }}
    .gps-dot {{ position: fixed; right: 14px; bottom: 14px; z-index: 8; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); background: rgba(0,0,0,.38); font-size: 12px; }}
    .tooltip {{ position: fixed; z-index: 20; pointer-events: none; display: none; max-width: 280px; transform: translate(12px, 12px); padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: rgba(9, 13, 18, .94); color: var(--text); font-size: 12px; line-height: 1.3; }}
    @media (max-width: 680px) {{
      .menu-toggle {{ display: block; }}
      .hud {{ transform: translateX(calc(-100% - 24px)); transition: transform .2s ease; top: 68px; max-height: calc(100vh - 84px); }}
      body.menu-open .hud {{ transform: translateX(0); }}
      body.menu-open .scrim {{ display: block; }}
      .meta {{ grid-template-columns: repeat(2, 1fr); }}
      input[type=range] {{ width: 118px; }}
    }}
  </style>
</head>
<body>
  <button class="menu-toggle" id="menuToggle" aria-label="Меню" aria-expanded="false">☰</button>
  <div class="scrim" id="menuScrim"></div>
  <aside class="hud" id="hud">
    <h1>Магистрали Москвы 3D</h1>
    <div class="meta">
      <div class="stat"><b id="roadCount">0</b><span>дорог</span></div>
      <div class="stat"><b id="buildingCount">0</b><span>зданий</span></div>
      <div class="stat"><b id="bufferM">0 м</b><span>буфер</span></div>
    </div>
    <div class="row"><label for="heightScale">Высоты</label><input id="heightScale" type="range" min="0.5" max="4" step="0.1" value="1.4"></div>
    <div class="row"><label for="buildingScale">Здания</label><input id="buildingScale" type="range" min="0.5" max="3" step="0.1" value="1"></div>
    <div class="row"><label for="roadWidth">Ширина дорог</label><input id="roadWidth" type="range" min="2" max="18" step="1" value="7"></div>
    <div class="row"><label for="tileRadius">Радиус тайлов</label><input id="tileRadius" type="range" min="1" max="5" step="1" value="3"></div>
    <div class="actions">
      <button id="toggleRoads" aria-pressed="true">Дороги</button>
      <button id="toggleBuildings" aria-pressed="true">Здания</button>
      <button id="toggleBase" aria-pressed="true">Основание</button>
      <button id="gpsButton" class="primary">GPS</button>
    </div>
    <div class="readout" id="readout">Загрузка сцены...</div>
  </aside>
  <div class="gps-dot" id="gpsStatus">GPS выключен</div>
  <div class="tooltip" id="tooltip"></div>
  <script>window.MOSCOW_ROAD_DATA = {payload_json};</script>
  <script type="importmap">
  {{
    "imports": {{
      "three": "https://cdn.jsdelivr.net/npm/three@0.164.1/build/three.module.js",
      "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/"
    }}
  }}
  </script>
  <script type="module">
import * as THREE from "three";
import {{ OrbitControls }} from "three/addons/controls/OrbitControls.js";
import {{ Line2 }} from "three/addons/lines/Line2.js";
import {{ LineGeometry }} from "three/addons/lines/LineGeometry.js";
import {{ LineMaterial }} from "three/addons/lines/LineMaterial.js";

const data = window.MOSCOW_ROAD_DATA;
const readout = document.querySelector("#readout");
const gpsStatus = document.querySelector("#gpsStatus");
const tooltip = document.querySelector("#tooltip");
const roadColors = {{ motorway: 0xffcc33, trunk: 0xff5a3c, primary: 0x38bdf8, secondary: 0xb5f36d }};
let heightScale = Number(document.querySelector("#heightScale").value);
let buildingScale = Number(document.querySelector("#buildingScale").value);
let roadWidth = Number(document.querySelector("#roadWidth").value);
let loadRadiusTiles = Number(document.querySelector("#tileRadius").value);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x081016);

const renderer = new THREE.WebGLRenderer({{ antialias: true, powerPreference: "high-performance" }});
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);

const camera = new THREE.PerspectiveCamera(48, window.innerWidth / window.innerHeight, 5, 180000);
const maxSpan = Math.max(data.bounds.widthM, data.bounds.depthM);
camera.position.set(maxSpan * 0.24, Math.max(1800, maxSpan * 0.42), maxSpan * 0.58);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.06;
controls.maxDistance = maxSpan * 2.2;
controls.target.set(0, 0, 0);

scene.add(new THREE.HemisphereLight(0xd8f3ff, 0x0f1b22, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.2);
sun.position.set(-12000, 26000, 16000);
scene.add(sun);

const roadGroup = new THREE.Group();
const buildingGroup = new THREE.Group();
const baseGroup = new THREE.Group();
const gpsGroup = new THREE.Group();
scene.add(baseGroup, roadGroup, buildingGroup, gpsGroup);

const elevations = [];
data.roads.forEach(road => road.points.forEach(point => elevations.push(point.elevation)));
data.buildings.forEach(building => elevations.push(building.baseElevation + building.height));
const minElevation = Math.min(...elevations);

function yFor(elevation) {{
  return (elevation - minElevation) * heightScale;
}}

function renderX(x) {{
  return -x;
}}

function dataX(renderedX) {{
  return -renderedX;
}}

function mercatorMeters(lat, lon) {{
  const radius = 6378137.0;
  const clamped = Math.max(-85.05112878, Math.min(85.05112878, lat));
  const x = radius * lon * Math.PI / 180;
  const y = radius * Math.log(Math.tan(Math.PI / 4 + clamped * Math.PI / 360));
  return {{ x, y }};
}}

function localFromLatLon(lat, lon) {{
  const point = mercatorMeters(lat, lon);
  return {{ x: point.x - data.origin.x, z: point.y - data.origin.y }};
}}

function clearGroup(group) {{
  while (group.children.length) {{
    const child = group.children.pop();
    child.geometry?.dispose?.();
    child.material?.dispose?.();
  }}
}}

function makeLine(points, color, width) {{
  const positions = [];
  points.forEach(point => positions.push(renderX(point.x), yFor(point.elevation) + 10, point.z));
  const geometry = new LineGeometry();
  geometry.setPositions(positions);
  const material = new LineMaterial({{ color, linewidth: width, worldUnits: true, transparent: true, opacity: 0.92 }});
  material.resolution.set(window.innerWidth, window.innerHeight);
  const line = new Line2(geometry, material);
  line.computeLineDistances();
  return line;
}}

function buildRoads() {{
  clearGroup(roadGroup);
  for (const road of data.roads) {{
    const color = roadColors[road.highway] || 0xd1d5db;
    const line = makeLine(road.points, color, roadWidth);
    line.userData = {{ type: "road", name: road.name, highway: road.highway }};
    roadGroup.add(line);
  }}
}}

function buildBuildings() {{
  clearGroup(buildingGroup);
  const material = new THREE.MeshStandardMaterial({{ color: 0xb8c7c9, roughness: 0.76, metalness: 0.02, transparent: true, opacity: 0.88 }});
  const roofMaterial = new THREE.MeshStandardMaterial({{ color: 0xe8f0ef, roughness: 0.7 }});
    for (const building of data.buildings) {{
    const shape = new THREE.Shape();
    building.footprint.forEach((point, index) => {{
      if (index === 0) shape.moveTo(renderX(point.x), -point.z);
      else shape.lineTo(renderX(point.x), -point.z);
    }});
    shape.closePath();
    const height = Math.max(2, building.height * buildingScale);
    const geometry = new THREE.ExtrudeGeometry(shape, {{ depth: height, bevelEnabled: false }});
    geometry.rotateX(-Math.PI / 2);
    geometry.translate(0, yFor(building.baseElevation), 0);
    const mesh = new THREE.Mesh(geometry, [material, roofMaterial]);
    mesh.userData = {{ type: "building", name: building.name, height: building.height, source: building.heightSource }};
    buildingGroup.add(mesh);
  }}
}}

function buildBase() {{
  clearGroup(baseGroup);
  const geometry = new THREE.PlaneGeometry(data.bounds.widthM * 1.04, data.bounds.depthM * 1.04, 1, 1);
  const material = new THREE.MeshBasicMaterial({{ color: 0x111c20, transparent: true, opacity: 0.72, side: THREE.DoubleSide }});
  const plane = new THREE.Mesh(geometry, material);
  plane.rotation.x = -Math.PI / 2;
  plane.position.y = -4;
  baseGroup.add(plane);
}}

function rebuildVertical() {{
  buildRoads();
  buildBuildings();
  buildGpsMarker();
}}

let lastGps = null;
function buildGpsMarker() {{
  clearGroup(gpsGroup);
  if (!lastGps) return;
  const local = localFromLatLon(lastGps.lat, lastGps.lon);
  const y = yFor(lastGps.elevation ?? minElevation) + 80;
  const marker = new THREE.Mesh(
    new THREE.SphereGeometry(Math.max(45, maxSpan * 0.0012), 24, 16),
    new THREE.MeshStandardMaterial({{ color: 0x7ee787, emissive: 0x1f7a3a, emissiveIntensity: 0.55 }})
  );
  marker.position.set(local.x, y, local.z);
  gpsGroup.add(marker);
}}

function setMenuOpen(isOpen) {{
  document.body.classList.toggle("menu-open", isOpen);
  document.querySelector("#menuToggle").setAttribute("aria-expanded", String(isOpen));
}}

document.querySelector("#menuToggle").addEventListener("click", () => setMenuOpen(!document.body.classList.contains("menu-open")));
document.querySelector("#menuScrim").addEventListener("click", () => setMenuOpen(false));
document.querySelector("#heightScale").addEventListener("input", event => {{ heightScale = Number(event.target.value); rebuildVertical(); }});
document.querySelector("#buildingScale").addEventListener("input", event => {{ buildingScale = Number(event.target.value); buildBuildings(); }});
document.querySelector("#roadWidth").addEventListener("input", event => {{ roadWidth = Number(event.target.value); buildRoads(); }});
document.querySelector("#tileRadius").addEventListener("input", event => {{ loadRadiusTiles = Number(event.target.value); refreshTiles?.(true); }});
document.querySelector("#toggleRoads").addEventListener("click", event => {{ roadGroup.visible = !roadGroup.visible; event.currentTarget.setAttribute("aria-pressed", roadGroup.visible); }});
document.querySelector("#toggleBuildings").addEventListener("click", event => {{ buildingGroup.visible = !buildingGroup.visible; event.currentTarget.setAttribute("aria-pressed", buildingGroup.visible); }});
document.querySelector("#toggleBase").addEventListener("click", event => {{ baseGroup.visible = !baseGroup.visible; event.currentTarget.setAttribute("aria-pressed", baseGroup.visible); }});
document.querySelector("#gpsButton").addEventListener("click", () => {{
  if (!navigator.geolocation) {{
    gpsStatus.textContent = "GPS недоступен";
    return;
  }}
  gpsStatus.textContent = "Ищу позицию...";
  navigator.geolocation.getCurrentPosition(position => {{
    lastGps = {{ lat: position.coords.latitude, lon: position.coords.longitude, elevation: minElevation }};
    const local = localFromLatLon(lastGps.lat, lastGps.lon);
    buildGpsMarker();
    controls.target.set(local.x, 0, local.z);
    camera.position.set(local.x + maxSpan * 0.08, Math.max(1200, maxSpan * 0.14), local.z + maxSpan * 0.12);
    gpsStatus.textContent = `GPS: ${{lastGps.lat.toFixed(5)}}, ${{lastGps.lon.toFixed(5)}}`;
    setMenuOpen(false);
  }}, error => {{
    gpsStatus.textContent = `GPS: ${{error.message}}`;
  }}, {{ enableHighAccuracy: true, timeout: 10000, maximumAge: 20000 }});
}});

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
renderer.domElement.addEventListener("pointermove", event => {{
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObjects(buildingGroup.children, false);
  if (!hits.length) {{
    tooltip.style.display = "none";
    return;
  }}
  const data = hits[0].object.userData;
  tooltip.innerHTML = `<b>${{data.name || "Здание"}}</b><br>${{Math.round(data.height)}} м · ${{data.source}}`;
  tooltip.style.left = `${{event.clientX}}px`;
  tooltip.style.top = `${{event.clientY}}px`;
  tooltip.style.display = "block";
}});

window.addEventListener("resize", () => {{
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  roadGroup.traverse(child => child.material?.resolution?.set(window.innerWidth, window.innerHeight));
}});

document.querySelector("#roadCount").textContent = data.meta.roadCount.toLocaleString("ru-RU");
document.querySelector("#buildingCount").textContent = data.meta.buildingCount.toLocaleString("ru-RU");
document.querySelector("#bufferM").textContent = `${{data.meta.bufferM}} м`;
readout.textContent = `Источник высот: ${{data.meta.elevationSource}}. Вращение: drag, зум: pinch/колесо.`;

buildBase();
buildRoads();
buildBuildings();
controls.update();

function animate() {{
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}}
animate();
  </script>
</body>
</html>
"""

def build_html(payload=None):
    if payload is None:
        payload = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    title = html.escape(f"3D магистрали Москвы: {payload['meta']['roadCount']} дорог")
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      --bg: #081016;
      --panel: rgba(12, 18, 24, 0.88);
      --line: rgba(217, 226, 236, 0.18);
      --text: #edf4f8;
      --muted: #a7b4bd;
      --accent: #7ee787;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ width: 100%; height: 100%; margin: 0; overflow: hidden; background: var(--bg); color: var(--text); font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif; }}
    canvas {{ display: block; width: 100%; height: 100%; touch-action: none; }}
    .hud {{ position: fixed; top: 16px; left: 16px; width: min(380px, calc(100vw - 32px)); max-height: calc(100vh - 32px); overflow: auto; padding: 14px; border: 1px solid var(--line); background: var(--panel); backdrop-filter: blur(18px); border-radius: 8px; z-index: 10; box-shadow: 0 18px 60px rgba(0,0,0,.35); }}
    h1 {{ margin: 0 0 8px; font-size: 18px; line-height: 1.15; font-weight: 760; }}
    .meta {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin: 12px 0; }}
    .stat {{ border: 1px solid var(--line); border-radius: 6px; padding: 8px; min-width: 0; }}
    .stat b {{ display: block; font-size: 17px; line-height: 1.05; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    .stat span {{ display: block; margin-top: 4px; color: var(--muted); font-size: 11px; }}
    .row {{ display: grid; grid-template-columns: 1fr auto; align-items: center; gap: 10px; min-height: 38px; border-top: 1px solid var(--line); padding: 9px 0; }}
    .row label {{ color: var(--muted); font-size: 13px; }}
    input[type=range] {{ width: 142px; accent-color: var(--accent); }}
    button {{ min-height: 36px; border: 1px solid var(--line); color: var(--text); background: rgba(255,255,255,.06); border-radius: 6px; padding: 0 10px; font: inherit; cursor: pointer; }}
    button[aria-pressed=true], button.primary {{ border-color: rgba(126,231,135,.55); background: rgba(126,231,135,.16); }}
    .actions {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin-top: 10px; }}
    .readout {{ margin-top: 10px; color: var(--muted); font-size: 12px; line-height: 1.35; }}
    .menu-toggle {{ display: none; position: fixed; top: 12px; left: 12px; z-index: 13; width: 44px; height: 44px; padding: 0; font-size: 24px; }}
    .scrim {{ display: none; position: fixed; inset: 0; z-index: 9; background: rgba(0,0,0,.42); }}
    .gps-dot {{ position: fixed; right: 14px; bottom: 14px; z-index: 8; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; color: var(--muted); background: rgba(0,0,0,.38); font-size: 12px; }}
    .tooltip {{ position: fixed; z-index: 20; pointer-events: none; display: none; max-width: 280px; transform: translate(12px, 12px); padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: rgba(9, 13, 18, .94); color: var(--text); font-size: 12px; line-height: 1.3; }}
    @media (max-width: 680px) {{
      .menu-toggle {{ display: block; }}
      .hud {{ transform: translateX(calc(-100% - 24px)); transition: transform .2s ease; top: 68px; max-height: calc(100vh - 84px); }}
      body.menu-open .hud {{ transform: translateX(0); }}
      body.menu-open .scrim {{ display: block; }}
      .meta {{ grid-template-columns: repeat(2, 1fr); }}
      input[type=range] {{ width: 118px; }}
    }}
  </style>
</head>
<body>
  <button class="menu-toggle" id="menuToggle" aria-label="Меню" aria-expanded="false">☰</button>
  <div class="scrim" id="menuScrim"></div>
  <aside class="hud" id="hud">
    <h1>Магистрали Москвы 3D</h1>
    <div class="meta">
      <div class="stat"><b id="roadCount">0</b><span>дорог</span></div>
      <div class="stat"><b id="buildingCount">0</b><span>зданий</span></div>
      <div class="stat"><b id="tileCount">0</b><span>тайлов</span></div>
    </div>
    <div class="row"><label for="heightScale">Высоты</label><input id="heightScale" type="range" min="0.5" max="4" step="0.1" value="1.4"></div>
    <div class="row"><label for="buildingScale">Здания</label><input id="buildingScale" type="range" min="0.5" max="3" step="0.1" value="1"></div>
    <div class="row"><label for="roadWidth">Ширина дорог</label><input id="roadWidth" type="range" min="2" max="18" step="1" value="7"></div>
    <div class="row"><label for="tileRadius">Радиус тайлов</label><input id="tileRadius" type="range" min="1" max="5" step="1" value="3"></div>
    <div class="actions">
      <button id="toggleRoads" aria-pressed="true">Дороги</button>
      <button id="toggleBuildings" aria-pressed="true">Здания</button>
      <button id="toggleBase" aria-pressed="true">Основание</button>
      <button id="gpsButton" class="primary">GPS</button>
    </div>
    <div class="readout" id="readout">Загрузка индекса тайлов...</div>
  </aside>
  <div class="gps-dot" id="gpsStatus">GPS выключен</div>
  <div class="tooltip" id="tooltip"></div>
  <script type="importmap">
  {{
    "imports": {{
      "three": "https://cdn.jsdelivr.net/npm/three@0.164.1/build/three.module.js",
      "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.164.1/examples/jsm/"
    }}
  }}
  </script>
  <script type="module">
import * as THREE from "three";
import {{ OrbitControls }} from "three/addons/controls/OrbitControls.js";
import {{ Line2 }} from "three/addons/lines/Line2.js";
import {{ LineGeometry }} from "three/addons/lines/LineGeometry.js";
import {{ LineMaterial }} from "three/addons/lines/LineMaterial.js";

let data = null;
const readout = document.querySelector("#readout");
const gpsStatus = document.querySelector("#gpsStatus");
const tooltip = document.querySelector("#tooltip");
const roadColors = {{ motorway: 0xffcc33, trunk: 0xff5a3c, primary: 0x38bdf8, secondary: 0xb5f36d }};
let heightScale = Number(document.querySelector("#heightScale").value);
let buildingScale = Number(document.querySelector("#buildingScale").value);
let roadWidth = Number(document.querySelector("#roadWidth").value);
let maxSpan = 100000;
let minElevation = 0;
let lastGps = null;
let gpsWatchId = null;
let loadRadiusTiles = Number(document.querySelector("#tileRadius").value);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x081016);

const renderer = new THREE.WebGLRenderer({{ antialias: true, powerPreference: "high-performance" }});
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setSize(window.innerWidth, window.innerHeight);
document.body.appendChild(renderer.domElement);

const camera = new THREE.PerspectiveCamera(48, window.innerWidth / window.innerHeight, 5, 180000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.06;

scene.add(new THREE.HemisphereLight(0xd8f3ff, 0x0f1b22, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 1.2);
sun.position.set(-12000, 26000, 16000);
scene.add(sun);

const tileRoot = new THREE.Group();
const baseGroup = new THREE.Group();
const gpsGroup = new THREE.Group();
scene.add(baseGroup, tileRoot, gpsGroup);

const loadedTiles = new Map();
const loadingTiles = new Set();

function yFor(elevation) {{
  return (elevation - minElevation) * heightScale;
}}

function renderX(x) {{
  return -x;
}}

function dataX(renderedX) {{
  return -renderedX;
}}

function mercatorMeters(lat, lon) {{
  const radius = 6378137.0;
  const clamped = Math.max(-85.05112878, Math.min(85.05112878, lat));
  const x = radius * lon * Math.PI / 180;
  const y = radius * Math.log(Math.tan(Math.PI / 4 + clamped * Math.PI / 360));
  return {{ x, y }};
}}

function localFromLatLon(lat, lon) {{
  const point = mercatorMeters(lat, lon);
  return {{ x: point.x - data.origin.x, z: point.y - data.origin.y }};
}}

function disposeObject(object) {{
  object.traverse(child => {{
    child.geometry?.dispose?.();
    if (Array.isArray(child.material)) child.material.forEach(material => material.dispose?.());
    else child.material?.dispose?.();
  }});
}}

function clearGroup(group) {{
  while (group.children.length) {{
    const child = group.children.pop();
    disposeObject(child);
  }}
}}

function makeLine(points, color, width) {{
  const positions = [];
  points.forEach(point => positions.push(renderX(point.x), yFor(point.elevation) + 10, point.z));
  const geometry = new LineGeometry();
  geometry.setPositions(positions);
  const material = new LineMaterial({{ color, linewidth: width, worldUnits: true, transparent: true, opacity: 0.92 }});
  material.resolution.set(window.innerWidth, window.innerHeight);
  const line = new Line2(geometry, material);
  line.computeLineDistances();
  return line;
}}

function addRoads(group, roads) {{
  for (const road of roads) {{
    const color = roadColors[road.highway] || 0xd1d5db;
    const line = makeLine(road.points, color, roadWidth);
    line.userData = {{ type: "road", name: road.name, highway: road.highway }};
    group.add(line);
  }}
}}

function addBuildings(group, buildings) {{
  const wall = new THREE.MeshStandardMaterial({{ color: 0xb8c7c9, roughness: 0.76, metalness: 0.02, transparent: true, opacity: 0.88 }});
  const roof = new THREE.MeshStandardMaterial({{ color: 0xe8f0ef, roughness: 0.7 }});
  for (const building of buildings) {{
    const shape = new THREE.Shape();
    building.footprint.forEach((point, index) => {{
      if (index === 0) shape.moveTo(renderX(point.x), -point.z);
      else shape.lineTo(renderX(point.x), -point.z);
    }});
    shape.closePath();
    const height = Math.max(2, building.height * buildingScale);
    const geometry = new THREE.ExtrudeGeometry(shape, {{ depth: height, bevelEnabled: false }});
    geometry.rotateX(-Math.PI / 2);
    geometry.translate(0, yFor(building.baseElevation), 0);
    const mesh = new THREE.Mesh(geometry, [wall, roof]);
    mesh.userData = {{ type: "building", name: building.name, height: building.height, source: building.heightSource }};
    group.add(mesh);
  }}
}}

function buildTile(tile) {{
  const group = new THREE.Group();
  const roads = new THREE.Group();
  const buildings = new THREE.Group();
  roads.name = "roads";
  buildings.name = "buildings";
  roads.visible = document.querySelector("#toggleRoads").getAttribute("aria-pressed") === "true";
  buildings.visible = document.querySelector("#toggleBuildings").getAttribute("aria-pressed") === "true";
  addRoads(roads, tile.roads || []);
  addBuildings(buildings, tile.buildings || []);
  group.add(roads, buildings);
  group.userData = {{ tileId: tile.id }};
  return group;
}}

function tileCoordForPoint(x, z) {{
  const size = data.meta.tileSizeM;
  return {{
    col: Math.floor((dataX(x) - data.bounds.minX) / size),
    row: Math.floor((z - data.bounds.minZ) / size)
  }};
}}

function wantedTileIds(center) {{
  const current = tileCoordForPoint(center.x, center.z);
  const wanted = new Set();
  for (let col = current.col - loadRadiusTiles; col <= current.col + loadRadiusTiles; col++) {{
    for (let row = current.row - loadRadiusTiles; row <= current.row + loadRadiusTiles; row++) {{
      const id = `${{col}}_${{row}}`;
      if (data.tileMap.has(id)) wanted.add(id);
    }}
  }}
  return wanted;
}}

async function loadTile(id) {{
  if (loadedTiles.has(id) || loadingTiles.has(id)) return;
  const info = data.tileMap.get(id);
  if (!info) return;
  loadingTiles.add(id);
  try {{
    const response = await fetch(`./tiles/${{info.url}}`);
    if (!response.ok) throw new Error(`tile ${{id}}: ${{response.status}}`);
    const tile = await response.json();
    const group = buildTile(tile);
    tileRoot.add(group);
    loadedTiles.set(id, {{ group, tile }});
  }} finally {{
    loadingTiles.delete(id);
  }}
}}

function unloadTile(id) {{
  const loaded = loadedTiles.get(id);
  if (!loaded) return;
  tileRoot.remove(loaded.group);
  disposeObject(loaded.group);
  loadedTiles.delete(id);
}}

let lastTileRefresh = 0;
async function refreshTiles(force = false) {{
  if (!data) return;
  const now = performance.now();
  if (!force && now - lastTileRefresh < 350) return;
  lastTileRefresh = now;
  const wanted = wantedTileIds(controls.target);
  for (const id of [...loadedTiles.keys()]) {{
    if (!wanted.has(id)) unloadTile(id);
  }}
  await Promise.all([...wanted].map(loadTile));
  readout.textContent = `Загружено тайлов: ${{loadedTiles.size}} / ${{data.meta.tileCount}}`;
}}

function rebuildLoadedTiles() {{
  for (const [id, loaded] of [...loadedTiles]) {{
    tileRoot.remove(loaded.group);
    disposeObject(loaded.group);
    loaded.group = buildTile(loaded.tile);
    tileRoot.add(loaded.group);
    loadedTiles.set(id, loaded);
  }}
  buildGpsMarker();
}}

function buildBase() {{
  clearGroup(baseGroup);
  const geometry = new THREE.PlaneGeometry(data.bounds.widthM * 1.04, data.bounds.depthM * 1.04, 1, 1);
  const material = new THREE.MeshBasicMaterial({{ color: 0x111c20, transparent: true, opacity: 0.72, side: THREE.DoubleSide }});
  const plane = new THREE.Mesh(geometry, material);
  plane.rotation.x = -Math.PI / 2;
  plane.position.y = -4;
  baseGroup.add(plane);
}}

function buildGpsMarker() {{
  clearGroup(gpsGroup);
  if (!lastGps) return;
  const local = localFromLatLon(lastGps.lat, lastGps.lon);
  const marker = new THREE.Mesh(
    new THREE.SphereGeometry(Math.max(45, maxSpan * 0.0012), 24, 16),
    new THREE.MeshStandardMaterial({{ color: 0x7ee787, emissive: 0x1f7a3a, emissiveIntensity: 0.55 }})
  );
  marker.position.set(renderX(local.x), yFor(lastGps.elevation ?? minElevation) + 80, local.z);
  gpsGroup.add(marker);
}}

function setLayerVisible(groupName, visible) {{
  for (const loaded of loadedTiles.values()) {{
    const layer = loaded.group.getObjectByName(groupName);
    if (layer) layer.visible = visible;
  }}
}}

function setMenuOpen(isOpen) {{
  document.body.classList.toggle("menu-open", isOpen);
  document.querySelector("#menuToggle").setAttribute("aria-expanded", String(isOpen));
}}

document.querySelector("#menuToggle").addEventListener("click", () => setMenuOpen(!document.body.classList.contains("menu-open")));
document.querySelector("#menuScrim").addEventListener("click", () => setMenuOpen(false));
document.querySelector("#heightScale").addEventListener("input", event => {{ heightScale = Number(event.target.value); rebuildLoadedTiles(); }});
document.querySelector("#buildingScale").addEventListener("input", event => {{ buildingScale = Number(event.target.value); rebuildLoadedTiles(); }});
document.querySelector("#roadWidth").addEventListener("input", event => {{ roadWidth = Number(event.target.value); rebuildLoadedTiles(); }});
document.querySelector("#tileRadius").addEventListener("input", event => {{ loadRadiusTiles = Number(event.target.value); refreshTiles(true); }});
document.querySelector("#toggleRoads").addEventListener("click", event => {{ const visible = event.currentTarget.getAttribute("aria-pressed") !== "true"; event.currentTarget.setAttribute("aria-pressed", visible); setLayerVisible("roads", visible); }});
document.querySelector("#toggleBuildings").addEventListener("click", event => {{ const visible = event.currentTarget.getAttribute("aria-pressed") !== "true"; event.currentTarget.setAttribute("aria-pressed", visible); setLayerVisible("buildings", visible); }});
document.querySelector("#toggleBase").addEventListener("click", event => {{ baseGroup.visible = !baseGroup.visible; event.currentTarget.setAttribute("aria-pressed", baseGroup.visible); }});

function gpsErrorMessage(error) {{
  if (!window.isSecureContext) return "GPS требует HTTPS или localhost";
  if (error.code === error.PERMISSION_DENIED) return "Доступ к геопозиции запрещён в браузере";
  if (error.code === error.POSITION_UNAVAILABLE) return "Позиция сейчас недоступна";
  if (error.code === error.TIMEOUT) return "GPS не ответил вовремя";
  return error.message || "GPS не сработал";
}}

async function handleGpsPosition(position) {{
  const wasEmpty = !lastGps;
  lastGps = {{ lat: position.coords.latitude, lon: position.coords.longitude, elevation: minElevation }};
  const local = localFromLatLon(lastGps.lat, lastGps.lon);
  const x = renderX(local.x);
  const dx = x - controls.target.x;
  const dz = local.z - controls.target.z;
  controls.target.set(x, 0, local.z);
  if (wasEmpty) {{
    camera.position.set(x - maxSpan * 0.08, Math.max(1200, maxSpan * 0.14), local.z + maxSpan * 0.12);
  }} else {{
    camera.position.x += dx;
    camera.position.z += dz;
  }}
  buildGpsMarker();
  await refreshTiles(true);
  gpsStatus.textContent = `GPS: ${{lastGps.lat.toFixed(5)}}, ${{lastGps.lon.toFixed(5)}}`;
  setMenuOpen(false);
}}

document.querySelector("#gpsButton").addEventListener("click", () => {{
  const gpsButton = document.querySelector("#gpsButton");
  if (gpsWatchId !== null) {{
    navigator.geolocation.clearWatch(gpsWatchId);
    gpsWatchId = null;
    gpsButton.setAttribute("aria-pressed", "false");
    gpsStatus.textContent = "GPS выключен";
    return;
  }}
  if (!navigator.geolocation) {{
    gpsStatus.textContent = "GPS недоступен";
    return;
  }}
  if (!window.isSecureContext) {{
    gpsStatus.textContent = "GPS требует HTTPS или localhost";
    return;
  }}
  gpsStatus.textContent = "Ищу позицию...";
  gpsButton.setAttribute("aria-pressed", "true");
  gpsWatchId = navigator.geolocation.watchPosition(handleGpsPosition, error => {{
    gpsButton.setAttribute("aria-pressed", "false");
    if (gpsWatchId !== null) {{
      navigator.geolocation.clearWatch(gpsWatchId);
      gpsWatchId = null;
    }}
    gpsStatus.textContent = `GPS: ${{gpsErrorMessage(error)}}`;
  }}, {{ enableHighAccuracy: true, timeout: 10000, maximumAge: 20000 }});
}});

const raycaster = new THREE.Raycaster();
const pointer = new THREE.Vector2();
renderer.domElement.addEventListener("pointermove", event => {{
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const buildingLayers = [];
  for (const loaded of loadedTiles.values()) {{
    const layer = loaded.group.getObjectByName("buildings");
    if (layer?.visible) buildingLayers.push(...layer.children);
  }}
  const hits = raycaster.intersectObjects(buildingLayers, false);
  if (!hits.length) {{
    tooltip.style.display = "none";
    return;
  }}
  const hitData = hits[0].object.userData;
  tooltip.innerHTML = `<b>${{hitData.name || "Здание"}}</b><br>${{Math.round(hitData.height)}} м · ${{hitData.source}}`;
  tooltip.style.left = `${{event.clientX}}px`;
  tooltip.style.top = `${{event.clientY}}px`;
  tooltip.style.display = "block";
}});

window.addEventListener("resize", () => {{
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  tileRoot.traverse(child => child.material?.resolution?.set(window.innerWidth, window.innerHeight));
}});

async function init() {{
  const response = await fetch("./tiles/index.json");
  if (!response.ok) throw new Error(`index: ${{response.status}}`);
  data = await response.json();
  data.tileMap = new Map(data.tiles.map(tile => [tile.id, tile]));
  maxSpan = Math.max(data.bounds.widthM, data.bounds.depthM);
  minElevation = data.meta.minElevation;
  camera.position.set(maxSpan * 0.24, Math.max(1800, maxSpan * 0.42), maxSpan * 0.58);
  controls.maxDistance = maxSpan * 2.2;
  controls.target.set(0, 0, 0);
  document.querySelector("#roadCount").textContent = data.meta.roadCount.toLocaleString("ru-RU");
  document.querySelector("#buildingCount").textContent = data.meta.buildingCount.toLocaleString("ru-RU");
  document.querySelector("#tileCount").textContent = data.meta.tileCount.toLocaleString("ru-RU");
  buildBase();
  await refreshTiles(true);
}}

function animate() {{
  requestAnimationFrame(animate);
  controls.update();
  refreshTiles(false);
  renderer.render(scene, camera);
}}

init().then(animate).catch(error => {{
  console.error(error);
  readout.textContent = `Ошибка загрузки: ${{error.message}}`;
}});
  </script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="Build a 3D map of Moscow arterial roads and nearby buildings.")
    parser.add_argument("--place", default=PLACE_NAME)
    parser.add_argument("--buffer-m", type=float, default=120)
    parser.add_argument("--simplify-m", type=float, default=18)
    parser.add_argument("--road-spacing-m", type=float, default=140)
    parser.add_argument("--max-buildings", type=int, default=12000)
    parser.add_argument("--elevation-batch-size", type=int, default=250)
    parser.add_argument("--tile-size-m", type=float, default=2500)
    parser.add_argument("--all-drive-roads", action="store_true")
    parser.add_argument("--reuse-existing-buildings", action="store_true")
    parser.add_argument("--html-only", action="store_true")
    args = parser.parse_args()

    if args.html_only:
        payload = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    else:
        payload = build_payload(
            place=args.place,
            buffer_m=args.buffer_m,
            simplify_m=args.simplify_m,
            max_buildings=args.max_buildings,
            road_spacing_m=args.road_spacing_m,
            elevation_batch_size=args.elevation_batch_size,
            all_drive_roads=args.all_drive_roads,
            reuse_existing_buildings=args.reuse_existing_buildings,
        )
        DATA_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tile_index = build_tile_package(payload, tile_size_m=args.tile_size_m)
    HTML_FILE.write_text(build_html(payload), encoding="utf-8")
    print(
        json.dumps(
            {
                "data": str(DATA_JSON),
                "html": str(HTML_FILE),
                "tiles": str(TILE_DIR),
                "tileCount": tile_index["meta"]["tileCount"],
                "meta": payload["meta"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
