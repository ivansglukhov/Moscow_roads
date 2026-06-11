import math
import tempfile
import unittest
from pathlib import Path

import build_moscow_roads_3d as moscow


class MoscowRoads3DTests(unittest.TestCase):
    def test_mercator_round_trip(self):
        lat, lon = 55.7558, 37.6173
        x, y = moscow.mercator_meters(lat, lon)
        got_lat, got_lon = moscow.inverse_mercator_meters(x, y)
        self.assertAlmostEqual(got_lat, lat, places=6)
        self.assertAlmostEqual(got_lon, lon, places=6)

    def test_highway_filter_handles_lists(self):
        self.assertTrue(moscow.has_target_highway(["residential", "primary"]))
        self.assertFalse(moscow.has_target_highway(["residential", "service"]))

    def test_parse_meters(self):
        self.assertEqual(moscow.parse_meters("24 m"), 24)
        self.assertAlmostEqual(moscow.parse_meters("30 ft"), 9.144)
        self.assertIsNone(moscow.parse_meters(""))

    def test_building_height_priority(self):
        self.assertEqual(moscow.building_height({"height": "18", "building:levels": "10"}), (18, "height"))
        self.assertEqual(moscow.building_height({"building:levels": "5"}), (15, "building:levels"))
        self.assertEqual(moscow.building_height({}), (12.0, "default"))

    def test_validate_payload_accepts_minimal_valid_data(self):
        payload = {
            "origin": {"x": 0, "y": 0},
            "bounds": {"widthM": 100, "depthM": 100},
            "roads": [
                {
                    "id": "road-0",
                    "points": [
                        {"x": 0, "z": 0, "lat": 55, "lon": 37, "elevation": 140},
                        {"x": 10, "z": 10, "lat": 55.1, "lon": 37.1, "elevation": 141},
                    ],
                }
            ],
            "buildings": [
                {
                    "id": "building-0",
                    "height": 12,
                    "baseElevation": 140,
                    "x": 1,
                    "z": 1,
                    "footprint": [{"x": 0, "z": 0}, {"x": 1, "z": 0}, {"x": 1, "z": 1}],
                }
            ],
            "meta": {},
        }
        moscow.validate_payload(payload)

    def test_validate_payload_rejects_bad_numbers(self):
        payload = {
            "origin": {"x": 0, "y": 0},
            "bounds": {},
            "roads": [{"points": [{"x": math.inf, "z": 0, "lat": 55, "lon": 37, "elevation": 1}]}],
            "buildings": [],
            "meta": {},
        }
        with self.assertRaises(ValueError):
            moscow.validate_payload(payload)

    def test_html_contains_threejs_mobile_and_gps(self):
        payload = {
            "origin": {"x": 0, "y": 0},
            "bounds": {"widthM": 100, "depthM": 100, "minX": -50, "minZ": -50, "maxX": 50, "maxZ": 50},
            "roads": [
                {
                    "id": "road-0",
                    "name": "primary",
                    "highway": "primary",
                    "points": [
                        {"x": 0, "z": 0, "lat": 55, "lon": 37, "elevation": 140},
                        {"x": 10, "z": 10, "lat": 55.1, "lon": 37.1, "elevation": 141},
                    ],
                }
            ],
            "buildings": [
                {
                    "id": "building-0",
                    "name": "",
                    "building": "yes",
                    "height": 12,
                    "heightSource": "default",
                    "baseElevation": 140,
                    "lat": 55,
                    "lon": 37,
                    "x": 1,
                    "z": 1,
                    "footprint": [{"x": 0, "z": 0}, {"x": 1, "z": 0}, {"x": 1, "z": 1}],
                }
            ],
            "meta": {
                "roadCount": 1,
                "buildingCount": 1,
                "bufferM": 120,
                "elevationSource": "https://elevation.nakarte.me/",
            },
        }
        rendered = moscow.build_html(payload)
        self.assertIn("THREE.WebGLRenderer", rendered)
        self.assertIn("ExtrudeGeometry", rendered)
        self.assertIn("navigator.geolocation", rendered)
        self.assertIn("watchPosition", rendered)
        self.assertIn("clearWatch", rendered)
        self.assertIn("gpsWatchId", rendered)
        self.assertIn('rel="manifest"', rendered)
        self.assertIn("navigator.serviceWorker.register", rendered)
        self.assertIn('id="menuToggle"', rendered)
        self.assertIn('id="tileRadius"', rendered)
        self.assertIn('id="roadLodRadius"', rendered)
        self.assertIn('id="buildingLodRadius"', rendered)
        self.assertIn('id="roadLegend"', rendered)
        self.assertIn('id="northNeedle"', rendered)
        self.assertIn("@media (max-width: 680px)", rendered)
        self.assertIn('fetch("./tiles/index.json")', rendered)
        self.assertIn("DecompressionStream", rendered)
        self.assertIn("fetch(`./tiles/${info.url}`)", rendered)
        self.assertIn('document.querySelector("#roadLodRadius").value', rendered)
        self.assertIn('document.querySelector("#buildingLodRadius").value', rendered)
        self.assertIn('id="buildingLodRadius"', rendered)
        self.assertIn('value="2000"', rendered)
        self.assertIn("function initialFocus()", rendered)
        self.assertIn("function hasBuildingTileNear(focus)", rendered)
        self.assertIn("updateLodLabels()", rendered)
        self.assertIn("majorRoadClasses", rendered)
        self.assertIn("function buildingColorForHeight(height)", rendered)
        self.assertIn("function buildingMaterialsForHeight(height)", rendered)
        self.assertIn("function buildRoadLegend()", rendered)
        self.assertIn("function updateNorthIndicator()", rendered)
        self.assertIn("updateNorthIndicator();", rendered)
        self.assertIn("function renderX(x)", rendered)
        self.assertIn("return -x", rendered)
        self.assertIn("function gpsErrorMessage(error)", rendered)
        self.assertIn("GPS требует HTTPS или localhost", rendered)
        self.assertIn("Доступ к геопозиции запрещён в браузере", rendered)
        self.assertNotIn("window.MOSCOW_ROAD_DATA", rendered)
        self.assertNotIn("fetch(\"./moscow-road-data.json\")", rendered)

    def test_build_tile_package_writes_index_and_tiles(self):
        payload = {
            "origin": {"x": 0, "y": 0},
            "bounds": {"widthM": 100, "depthM": 100, "minX": -50, "minZ": -50, "maxX": 50, "maxZ": 50},
            "roads": [
                {
                    "id": "road-0",
                    "name": "primary",
                    "highway": "primary",
                    "points": [
                        {"x": -10, "z": -10, "lat": 55, "lon": 37, "elevation": 140},
                        {"x": 10, "z": 10, "lat": 55.1, "lon": 37.1, "elevation": 141},
                    ],
                }
            ],
            "buildings": [
                {
                    "id": "building-0",
                    "name": "",
                    "building": "yes",
                    "height": 12,
                    "heightSource": "default",
                    "baseElevation": 140,
                    "lat": 55,
                    "lon": 37,
                    "x": 1,
                    "z": 1,
                    "footprint": [{"x": 0, "z": 0}, {"x": 1, "z": 0}, {"x": 1, "z": 1}],
                }
            ],
            "meta": {
                "roadCount": 1,
                "buildingCount": 1,
                "bufferM": 120,
                "elevationSource": "https://elevation.nakarte.me/",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            index = moscow.build_tile_package(payload, tile_dir=Path(directory), tile_size_m=50)
            self.assertGreaterEqual(index["meta"]["tileCount"], 1)
            self.assertEqual(index["meta"]["minElevation"], 140)
            self.assertEqual(index["meta"]["minBuildingHeight"], 12)
            self.assertEqual(index["meta"]["maxBuildingHeight"], 12)
            self.assertTrue((Path(directory) / "index.json").exists())
            self.assertTrue(any(Path(directory).glob("tile-*.json")))
            self.assertTrue(any(Path(directory).glob("tile-*.json.gz")))
            self.assertIn("gzipUrl", index["tiles"][0])


if __name__ == "__main__":
    unittest.main()
