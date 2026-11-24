"""map_embed — build a map (POI pins and/or a route) as a CMS-ready
snippet.

Card map-embed-tool (Tony 2026-10-13): trip/report pages need real maps —
POIs and routes (e.g. the southafrica trip pages). This tool is the
deterministic builder half: it composes the right Google Maps URL and
hands back snippet artifacts; the model then writes them into a page
with cms_publish_page / cms_edit (or sends the image/link in chat).

Surfaces produced:

- kind=interactive — Google Maps Embed API iframe src. directions mode
  for routes (origin/destination/waypoints), place mode for a single
  POI, a chained-directions view for several POIs (the embed API cannot
  pin independent places — chaining is the only way to show them all),
  view mode for a bare center/zoom. Needs GOOGLE_MAPS_API_KEY.
- kind=static — a frozen map IMAGE. With the key: Maps Static API —
  lettered markers per POI plus a real road-route overlay via the
  Routes API (encoded polyline; falls back to a flagged straight line).
  Without the key: OpenStreetMap staticmap with one red pin per
  resolved coordinate (names geocoded through Nominatim; no route line).
- open_url — always returned, no key needed: a maps/dir or maps/search
  link anyone can click for the live interactive map.

Honesty rules: kind=interactive with no key returns ok:false with
needs_key + the keyless fallbacks — never a dead iframe. kind=static
with no resolvable coordinates returns ok:false — never an empty map.
Static images on meta.public pages degrade to a link on the
cms-publish-site renderer (it escapes img/iframes); embed iframes
render on the tailnet cms-render viewer only.

Env:
    GOOGLE_MAPS_API_KEY   enables interactive embeds, Google static
                          images and the Routes API polyline
    ADA_MAP_NOMINATIM     geocoder base URL (default OSM public endpoint)
    ADA_MAP_UA            User-Agent for Nominatim calls
"""

from __future__ import annotations

import os
import re
import urllib.parse
from typing import Any

import httpx

EMBED = "https://www.google.com/maps/embed/v1"
STATIC = "https://maps.googleapis.com/maps/api/staticmap"
ROUTES = "https://routes.googleapis.com/directions/v2:computeRoutes"
OSM_STATIC = "https://staticmap.openstreetmap.de/staticmap.php"
GMAPS_DIR = "https://www.google.com/maps/dir/"
GMAPS_SEARCH = "https://www.google.com/maps/search/"

NOMINATIM = os.environ.get(
    "ADA_MAP_NOMINATIM", "https://nominatim.openstreetmap.org")
UA = os.environ.get("ADA_MAP_UA", "chaba-ada-map-embed/1.0")

_TRUE = {"yes", "true", "1", "on", "y"}
_MODES = ("driving", "walking", "bicycling", "transit")
_ROUTES_MODE = {"driving": "DRIVE", "walking": "WALK",
                "bicycling": "BICYCLE", "transit": "TRANSIT"}
_SIZE_RE = re.compile(r"^\d{2,4}x\d{2,4}$")

DECLARATION = {
    "name": "map_embed",
    "description": (
        "Build a map with POI pins and/or a route — returns an "
        "interactive embed iframe or a frozen static map image, an "
        "open-in-Maps link, and a ready markdown snippet for "
        "cms_publish_page/cms_edit."),
    "parameters": {
        "type": "object",
        "properties": {
            "places": {
                "type": "array",
                "description": "POI pins — each {name, lat, lon} "
                               "(name may be a bare place/address; "
                               "lat/lon optional but needed for the "
                               "keyless OSM fallback). Two+ places "
                               "with no route chain into a driving "
                               "route on interactive maps.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "lat": {"type": "number"},
                        "lon": {"type": "number"},
                    },
                },
            },
            "route": {
                "type": "object",
                "description": "Optional route — {origin, destination, "
                               "waypoints[], mode}. Endpoints take the "
                               "same shape as a places entry; mode is "
                               "driving|walking|bicycling|transit "
                               "(default driving).",
                "properties": {
                    "origin": {"type": "object"},
                    "destination": {"type": "object"},
                    "waypoints": {"type": "array",
                                  "items": {"type": "object"}},
                    "mode": {"type": "string"},
                },
            },
            "kind": {
                "type": "string",
                "description": "'interactive' (iframe embed — needs "
                               "GOOGLE_MAPS_API_KEY, renders on the "
                               "tailnet CMS viewer) or 'static' "
                               "(frozen image — works keyless via "
                               "OpenStreetMap; the right choice for "
                               "public pages and chat). Default "
                               "interactive.",
            },
            "chain": {
                "type": "string",
                "description": "'yes' (default): several places and no "
                               "route become a chained directions map "
                               "so all pins show — 'no' pins only the "
                               "first place.",
            },
            "title": {
                "type": "string",
                "description": "Map title — used as the image alt text "
                               "and link label.",
            },
            "center": {
                "type": "string",
                "description": "'lat,lon' viewport center — only for a "
                               "bare map with no places or route.",
            },
            "zoom": {
                "type": "number",
                "description": "Zoom 1-20 — static maps auto-pick from "
                               "the pin spread when omitted.",
            },
            "size": {
                "type": "string",
                "description": "Static image size WxH, default 640x360.",
            },
            "maptype": {
                "type": "string",
                "description": "Static basemap: roadmap (default), "
                               "satellite, terrain or hybrid.",
            },
        },
        "additionalProperties": False,
    },
}


# ------------------------------------------------------------- inputs

def _place(v: Any) -> dict[str, str]:
    """Normalize a place entry -> {name, coords, query}. Accepts a bare
    string ('Cape Town') or {name, lat, lon}; query is what a maps API
    can resolve (coords when given, else the name/address)."""
    if isinstance(v, str):
        v = {"name": v}
    v = v if isinstance(v, dict) else {}
    name = str(v.get("name") or v.get("q") or "").strip()
    coords = ""
    try:
        if v.get("lat") is not None and v.get("lon") is not None:
            coords = f"{float(v['lat']):.6f},{float(v['lon']):.6f}"
    except (TypeError, ValueError):
        coords = ""
    return {"name": name, "coords": coords, "query": coords or name}


def _places_arg(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [p for p in (_place(v) for v in raw) if p["query"]]


def _route_arg(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    origin = _place(raw.get("origin"))
    dest = _place(raw.get("destination") or raw.get("dest"))
    if not origin["query"] or not dest["query"]:
        return None
    mode = str(raw.get("mode") or "driving").strip().lower()
    if mode not in _MODES:
        mode = "driving"
    return {"origin": origin, "destination": dest,
            "waypoints": _places_arg(raw.get("waypoints")),
            "mode": mode}


def _route_stops(route: dict[str, Any]) -> list[dict[str, str]]:
    return [route["origin"], *route["waypoints"], route["destination"]]


def _size(v: Any) -> str:
    s = str(v or "").strip()
    return s if _SIZE_RE.match(s) else "640x360"


def _zoom(v: Any) -> int | None:
    try:
        z = int(v)
        return z if 1 <= z <= 20 else None
    except (TypeError, ValueError):
        return None


def _alt(title: str, places: list[dict[str, str]],
         route: dict[str, Any] | None) -> str:
    if title.strip():
        return title.strip()
    bits = [p["name"] or p["coords"] for p in places][:4]
    if route:
        o = route["origin"]["name"] or route["origin"]["query"]
        d = route["destination"]["name"] or route["destination"]["query"]
        bits = [f"{o} → {d}"] + bits
    return "Map: " + ", ".join(b for b in bits if b) if bits else "Map"


# ------------------------------------------------------------- URL builders

def _embed_url(key: str, places: list[dict[str, str]],
               route: dict[str, Any] | None, chain: bool,
               center: str, zoom: int | None) -> tuple[str, str]:
    """(embed_url, mode) — the iframe src. mode is directions|place|view."""
    enc = urllib.parse.quote
    if route or (len(places) > 1 and chain):
        stops = _route_stops(route) if route else places
        mode = (route or {}).get("mode", "driving")
        q = [f"key={enc(key)}", f"origin={enc(stops[0]['query'])}",
             f"destination={enc(stops[-1]['query'])}", f"mode={mode}"]
        if len(stops) > 2:
            q.append("waypoints=" + enc("|".join(
                s["query"] for s in stops[1:-1])))
        return f"{EMBED}/directions?" + "&".join(q), "directions"
    if places:
        return (f"{EMBED}/place?key={enc(key)}&q={enc(places[0]['query'])}",
                "place")
    if center:
        q = [f"key={enc(key)}", f"center={enc(center)}"]
        if zoom is not None:
            q.append(f"zoom={zoom}")
        return f"{EMBED}/view?" + "&".join(q), "view"
    return "", ""


def _open_url(places: list[dict[str, str]],
              route: dict[str, Any] | None) -> str:
    """Keyless click-to-open map — always buildable."""
    stops = _route_stops(route) if route else places
    if len(stops) > 1:
        q = {"api": "1", "origin": stops[0]["query"],
             "destination": stops[-1]["query"],
             "travelmode": (route or {}).get("mode", "driving")}
        if len(stops) > 2:
            q["waypoints"] = "|".join(s["query"] for s in stops[1:-1])
        return GMAPS_DIR + "?" + urllib.parse.urlencode(q)
    if stops:
        return (GMAPS_SEARCH + "?api=1&query="
                + urllib.parse.quote_plus(stops[0]["query"]))
    return ""


def _static_google(key: str, places: list[dict[str, str]],
                   route: dict[str, Any] | None, polyline: str,
                   size: str, zoom: int | None, center: str,
                   maptype: str) -> str:
    enc = urllib.parse.quote
    q = [f"key={enc(key)}", f"size={enc(size)}", f"maptype={maptype}"]
    for i, p in enumerate(places):
        lab = chr(65 + i) if i < 26 else ""
        q.append("markers=" + enc(
            f"color:red|label:{lab}|{p['query']}", safe="|"))
    if polyline:
        q.append("path=" + enc(
            "color:0x3366ffcc|weight:4|enc:" + polyline, safe=""))
    elif route:
        stops = "|".join(s["query"] for s in _route_stops(route))
        q.append("path=" + enc(
            "color:0x3366ff88|weight:3|" + stops, safe="|"))
    if center:
        q.append("center=" + enc(center))
    if zoom is not None:
        q.append(f"zoom={zoom}")
    return STATIC + "?" + "&".join(q)


def _auto_zoom(places: list[dict[str, str]]) -> tuple[str, int]:
    """bbox center + a rough zoom for a set of 'lat,lon' coords."""
    lats = [float(p.split(",")[0]) for p in places]
    lons = [float(p.split(",")[1]) for p in places]
    center = f"{sum(lats) / len(lats):.5f},{sum(lons) / len(lons):.5f}"
    span = max(max(lats) - min(lats), max(lons) - min(lons))
    zoom = 15
    for cap, z in ((0.02, 15), (0.1, 13), (0.5, 11), (2, 9), (10, 6)):
        if span <= cap:
            zoom = z
            break
        zoom = 4
    return center, zoom


def _static_osm(points: list[str], size: str,
                zoom: int | None) -> str:
    center, zauto = _auto_zoom(points)
    q = [f"center={center}", f"zoom={zoom or zauto}", f"size={size}"]
    for p in points:
        q.append(f"markers={p},red-pushpin")
    return OSM_STATIC + "?" + "&".join(q)


# ------------------------------------------------------------- remote calls

def _waypoint(p: dict[str, str]) -> dict[str, Any]:
    if p["coords"]:
        lat, lon = (float(x) for x in p["coords"].split(","))
        return {"location": {"latLng": {"latitude": lat,
                                        "longitude": lon}}}
    return {"address": p["query"]}


async def _route_polyline(client: httpx.AsyncClient, key: str,
                          route: dict[str, Any]
                          ) -> tuple[str, int | None, str]:
    """Routes API -> (encoded polyline, distance_m, duration 'Ns')."""
    body: dict[str, Any] = {
        "origin": _waypoint(route["origin"]),
        "destination": _waypoint(route["destination"]),
        "travelMode": _ROUTES_MODE[route["mode"]],
        "computeAlternativeRoutes": False,
    }
    if route["waypoints"]:
        body["intermediates"] = [_waypoint(w) for w in route["waypoints"]]
    r = await client.post(
        ROUTES, json=body,
        headers={"X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "routes.distanceMeters,"
                                     "routes.duration,"
                                     "routes.polyline.encodedPolyline"})
    r.raise_for_status()
    r0 = ((r.json() or {}).get("routes") or [{}])[0]
    poly = (r0.get("polyline") or {}).get("encodedPolyline") or ""
    return poly, r0.get("distanceMeters"), str(r0.get("duration") or "")


async def _geocode(client: httpx.AsyncClient, p: dict[str, str]) -> str:
    """name-only place -> 'lat,lon' via Nominatim ('' on a miss)."""
    r = await client.get(f"{NOMINATIM}/search", params={
        "format": "jsonv2", "q": p["query"], "limit": 1},
        headers={"User-Agent": UA})
    rows = r.json()
    if rows:
        return f"{float(rows[0]['lat']):.6f},{float(rows[0]['lon']):.6f}"
    return ""


async def _fill_coords(client: httpx.AsyncClient,
                       entries: list[dict[str, str]]) -> None:
    """Geocode name-only entries in place (for the keyless OSM path)."""
    for p in entries:
        if p["coords"] or not p["query"]:
            continue
        try:
            p["coords"] = await _geocode(client, p)
        except Exception:
            break  # geocoder down — render what resolved so far


# ------------------------------------------------------------- run

async def run(runner: Any, **args: Any) -> dict[str, Any]:
    places = _places_arg(args.get("places"))
    route = _route_arg(args.get("route"))
    kind = str(args.get("kind") or "interactive").strip().lower()
    chain = str(args.get("chain") or "yes").strip().lower() in _TRUE
    center = str(args.get("center") or "").strip()
    zoom = _zoom(args.get("zoom"))
    size = _size(args.get("size"))
    maptype = str(args.get("maptype") or "roadmap").strip().lower()
    if maptype not in ("roadmap", "satellite", "terrain", "hybrid"):
        maptype = "roadmap"
    title = str(args.get("title") or "")

    if kind not in ("interactive", "static"):
        return {"ok": False,
                "error": "kind must be 'interactive' or 'static'"}
    if not places and not route and not center:
        return {"ok": False,
                "error": "need places[], a route{}, or a center — pass "
                         "the POIs as places=[{name,lat,lon},...] and/or "
                         "route={origin,destination,waypoints,mode}"}
    if len(places) > 10:
        return {"ok": False,
                "error": "places is capped at 10 — split the map or "
                         "drop the least important stops"}

    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    open_url = _open_url(places, route)
    alt = _alt(title, places, route)
    warnings: list[str] = []

    if kind == "interactive":
        if not key:
            # Honest miss — an iframe with no key renders Google's
            # error page. Hand back the keyless options instead.
            out = {"ok": False, "needs_key": True,
                   "open_url": open_url,
                   "error": "interactive embeds need GOOGLE_MAPS_API_KEY "
                            "on the backend env — no iframe was built. "
                            "Re-call with kind='static' for a keyless "
                            "image, or send open_url: it opens the same "
                            "map in Google Maps with no key"}
            pts = [p["coords"] for p in places if p["coords"]]
            if pts:
                out["static_fallback"] = _static_osm(pts, size, zoom)
            return out
        embed, mode = _embed_url(key, places, route, chain, center, zoom)
        if not embed:
            return {"ok": False, "open_url": open_url,
                    "error": "nothing mappable — the embed API needs "
                             "places[], a route{}, or center+zoom"}
        if mode == "directions" and not route:
            warnings.append(
                "places shown as a chained driving route — the embed "
                "API cannot pin independent places; pass chain='no' "
                "for a single pin instead")
        elif mode == "directions" and places:
            warnings.append(
                "places[] are not drawn on a route embed — only the "
                "route's own stops get pins; use kind='static' to pin "
                "extra POIs alongside a route")
        elif len(places) > 1 and mode == "place":
            warnings.append(
                "only the first place is pinned — the embed API shows "
                "one place without a route; chain='yes' (default) "
                "chains them as stops")
        iframe = (f'<iframe src="{embed}" width="640" height="360" '
                  f'style="border:0" loading="lazy" allowfullscreen '
                  f'referrerpolicy="no-referrer-when-downgrade">'
                  f'</iframe>')
        md = iframe
        if open_url:
            md += f'\n\n[Open in Google Maps]({open_url})'
        return {
            "ok": True, "kind": kind, "provider": "google",
            "mode": mode, "embed_url": embed, "iframe_html": iframe,
            "open_url": open_url, "markdown": md, "alt": alt,
            "warnings": warnings,
            "note": "hand markdown to cms_publish_page/cms_edit — the "
                    "iframe renders on the tailnet CMS viewer; the "
                    "public cms-publish-site renderer escapes raw HTML, "
                    "so for meta.public pages kind='static' or the "
                    "open_url link is the durable map",
        }

    # kind == "static"
    image_url, provider = "", "google"
    distance_m, duration = None, ""
    if key:
        polyline = ""
        if route:
            try:
                async with httpx.AsyncClient(
                        timeout=15.0, follow_redirects=True) as client:
                    polyline, distance_m, duration = \
                        await _route_polyline(client, key, route)
            except Exception as exc:
                warnings.append(
                    f"Routes API failed ({type(exc).__name__}) — a "
                    "straight line between stops is drawn instead of "
                    "the road route")
        if not places and route:
            places = _route_stops(route)
        image_url = _static_google(key, places, route, polyline, size,
                                   zoom, center, maptype)
    else:
        provider = "osm"
        entries = places + (_route_stops(route) if route else [])
        need = [p for p in entries if not p["coords"]]
        if need:
            try:
                async with httpx.AsyncClient(
                        timeout=15.0, follow_redirects=True) as client:
                    await _fill_coords(client, need)
            except Exception:
                pass
        pts = [p["coords"] for p in entries if p["coords"]]
        if not pts:
            return {"ok": False, "open_url": open_url,
                    "error": "no key and no coordinates — none of the "
                             "places/route stops resolved to lat,lon "
                             "for the keyless OSM image. Pass lat/lon "
                             "in the entries, or send open_url (it "
                             "opens the live map with no key)"}
        if route:
            warnings.append(
                "GOOGLE_MAPS_API_KEY not set — OpenStreetMap image "
                "shows the stop pins only; the road-route line needs "
                "the key (Routes API)")
        image_url = _static_osm(pts, size, zoom)

    md = f"![{alt}]({image_url})"
    if open_url:
        md += f"\n\n[Open in Google Maps]({open_url})"
    out: dict[str, Any] = {
        "ok": True, "kind": kind, "provider": provider,
        "image_url": image_url, "open_url": open_url,
        "markdown": md, "alt": alt, "warnings": warnings,
        "note": "hand markdown to cms_publish_page/cms_edit — the "
                "image renders on the tailnet CMS viewer and degrades "
                "to a link on the public cms-publish-site renderer; "
                "archive image_url to the media host when the page "
                "must outlive the upstream map service",
    }
    if provider == "osm":
        out["note"] = (
            "keyless OpenStreetMap image — set GOOGLE_MAPS_API_KEY for "
            "Google basemaps and road routes. " + out["note"])
    if distance_m:
        out["distance_km"] = round(distance_m / 1000, 1)
    if duration.endswith("s"):
        try:
            out["duration_min"] = round(int(duration[:-1]) / 60)
        except ValueError:
            pass
    return out
