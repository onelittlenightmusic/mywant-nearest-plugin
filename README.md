# mywant-nearest-plugin

MyWant custom type plugin: finds the place of a chosen kind nearest to a point —
the nearest station to 自宅, the nearest convenience store to 会社.

The point is a place you have named with coordinates in MyWant (or `lat,lng`
written out). Stations in Japan come from [HeartRails Express](https://express.heartrails.com/)
— fast, and with their lines. Everything else, and stations outside Japan, come
from OpenStreetMap through the Overpass API, widening the circle until something
is there; the public Overpass server is often busy, so a search can wait and try
again for up to four minutes. Neither needs a key. Distances are straight-line.

## Installation

```bash
mywant custom install onelittlenightmusic/mywant-nearest-plugin
# or, from a checkout
mywant custom install ./mywant-nearest-plugin
```

The agent is read when the server starts, so restart the server after the first
install (`mywant stop && mywant start -D`).

## Usage

```yaml
metadata:
  name: nearest_station_home
  type: nearest
spec:
  params:
    origin: 自宅          # a named place with coordinates, or "35.69,139.68"
    target: station       # station (default), bus_stop, convenience_store, cafe,
                          # restaurant, hospital, park, toilets
    radius_m: 5000        # optional: how far to look at most
```

Result (want state): `summary` (`中野坂上（215m）`), `nearest`, `distance_m`,
`nearest_coordinate` (a `location_coordinate`, can be named as a place),
`nearest_station` (a `station`, only for `target: station` — ready for
`transit_search`'s `from`), `candidates` (the nearest five) and `source`
(`heartrails` or `openstreetmap`).

## Requirements

- Python 3.10+ (standard library only)
- The MyWant API at `MYWANT_URL` (default `http://localhost:8080`), to turn a
  place's name into coordinates

Map data © OpenStreetMap contributors, ODbL.
