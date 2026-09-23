# Offline coastline source

`ne_10m_coastline.geojson.gz` is Natural Earth's 1:10m coastline data, bundled so
the places map makes no map-service request at runtime.

- Source: https://github.com/nvkelso/natural-earth-vector/blob/ca96624a56bd078437bca8184e78163e5039ad19/geojson/ne_10m_coastline.geojson
- SHA-256 of the source GeoJSON before gzip: `6f75ae0e0de157b14946e2255eb1f5486d9a13819032e26d4610852d296788f6`
- Licence: public domain, per https://www.naturalearthdata.com/about/

The coordinates are generalized at 1:10m scale. This is geographic context
for the stored place markers, not a street map or navigation layer.
