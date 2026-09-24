# Offline basemap sources

Two layers, both gzipped GeoJSON, both committed and both read from inside the
image at runtime. The places map makes no map-service request of any kind: no
tile URL, no CDN, no Overpass call. Neither file is ever fetched by a running
container.

## `<slug>.basemap.geojson.gz` — the city backdrop

The streets, water and parks under the place markers. One file per city in
`data/cities.yml`.

- Source: OpenStreetMap contributors, via the Overpass API
  (<https://www.openstreetmap.org/copyright>)
- Licence: **ODbL 1.0**. Attribution is shown under the map and on the Data
  coverage tab, and travels in each file's `properties`.
- Staged by: `services/ingestor/fetch_basemap.py`, which is where the query,
  the tag-to-layer mapping and every constant below live.
- Staged on: 2026-09-24.

One bounded Overpass query per city over a 20 km box (±10 km) around the city
centre, asking only for what is drawn:

| layer | OSM selector | drawn as |
|---|---|---|
| `road_major` | `highway` = motorway, trunk, primary | line |
| `road_minor` | `highway` = secondary, tertiary | line |
| `waterway` | `waterway` = river, canal | line |
| `coastline` | `natural=coastline` | line |
| `water` | `natural=water` | filled polygon |
| `park` | `leisure` = park, garden | filled polygon |

Everything else in the box is discarded, including names, tags and ids: the
file carries geometry and a layer name, because that is all the map draws.

Deliberate limits, each of which is visible in the rendered map:

- **Residential streets are excluded.** They roughly quadruple the file for
  detail that is sub-pixel at the view the map opens at.
- **Geometry is simplified to 12 m** (Douglas-Peucker). A 20 km wide plot is
  about 20 m per pixel, so the simplification is under half a pixel there; it
  is visible only if you zoom well past the staged extent.
- **Coordinates are rounded to 5 decimal places**, about 1 m.
- **Relations are not requested.** A multipolygon lake would need member
  resolution for geometry the closed ways already cover. The visible cost is
  the Thames: it is mapped as a multipolygon, so London draws its `waterway`
  centreline rather than a filled channel. Lakes that are single closed ways,
  like the Serpentine, do fill.
- **20 km is the whole of it.** Pan beyond the box and the backdrop stops.

Result: 57 KB (Reykjavík) to 505 KB (London), 1.15 MB for all five.

To re-stage — needs the internet, and the reviewer never has to run it:

```sh
docker compose -f compose.yml -f compose.connected.yml run --rm --no-deps \
    ingestor python -m services.ingestor.fetch_basemap
```

The public Overpass mirrors are a free shared service and refuse or time out
under load; the script tries four of them twice each and fails loudly rather
than writing a half-empty file.

## `ne_10m_coastline.geojson.gz` — the fallback shoreline

Natural Earth's 1:10m coastline. It was the only basemap geometry in the build
until the extracts above were staged, and it is now drawn **only for a city
with no staged extract**.

- Source: <https://github.com/nvkelso/natural-earth-vector/blob/ca96624a56bd078437bca8184e78163e5039ad19/geojson/ne_10m_coastline.geojson>
- SHA-256 of the source GeoJSON before gzip:
  `6f75ae0e0de157b14946e2255eb1f5486d9a13819032e26d4610852d296788f6`
- Licence: public domain, per <https://www.naturalearthdata.com/about/>

Why it was demoted rather than kept alongside: 1:10m is about 1 km of
generalization, and at the ~10 km view this map opens at, that is visible and
wrong. Rendered over Reykjavík it put the shoreline several hundred metres
inland of streets that are accurate to a few metres — two layers from two
sources, visibly disagreeing about where the sea is. `natural=coastline` from
the same OSM extract as the streets agrees with them by construction.

It is still shipped because it is the graceful-degradation path: a city added
to `data/cities.yml` without a staging run gets axes, markers and a rough
shoreline instead of an empty panel.
