# Moscow Roads 3D

Static Three.js viewer for Moscow drive roads and nearby buildings.

## Open Locally

```powershell
python -m http.server 8000 --bind 127.0.0.1
```

Then open:

```text
http://127.0.0.1:8000/moscow-roads-3d.html
```

Do not open the HTML through `file://`: lazy tile loading uses `fetch`.

## Android

Install Termux from F-Droid, copy this folder to the phone, then run:

```bash
termux-setup-storage
cd /sdcard/Download/road
python -m http.server 8000 --bind 127.0.0.1
```

Open on the phone:

```text
http://127.0.0.1:8000/moscow-roads-3d.html
```

GPS requires HTTPS or localhost. A computer LAN URL such as `http://192.168.x.x:8000` may load the map but browser geolocation can be blocked.

## Rebuild

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe build_moscow_roads_3d.py --all-drive-roads --reuse-existing-buildings --max-buildings 8000 --buffer-m 80 --simplify-m 28 --road-spacing-m 260 --elevation-batch-size 250 --tile-size-m 2500
```

The current checked-in dataset contains all OSMnx drive roads for Moscow and reuses the already downloaded building set with elevation data.
