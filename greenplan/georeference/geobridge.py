"""Injectable HTTP client for geobridge.ru's geodetic-point-catalog search API.

`GET {base_url}/title=<label>` returns a base64-encoded UTF-8 JSON array of
every point whose title *contains* the query -- confirmed live (2026-09-25)
to be a country-wide substring search, e.g. querying "1763" returns 27
candidates across 8 regions, from "17638" in Leningrad oblast to "с/т
"Аграрник" (1763)" in Ivanovo oblast. Exact-match filtering happens
client-side, in transform.py, not here.

Each raw candidate carries a lot of unrelated data -- an `author` object with
a real name/phone/email, a `rating` list of per-user ratings, media, spec
catalogs, etc. All of it is discarded immediately on parse: only
{title, lat, lng, region} survive into GeobridgePoint. `region` is kept
(despite not being in the original {label, lat, lng} sketch) because it is
not personal data and turned out to be necessary, not just nice-to-have: even
after restricting to an exact title match, real duplicate catalog numbers
occur within a single region (e.g. two different physical points both titled
"15521" in Московская область, 13km apart) -- see transform.py for how the
caller-supplied bbox resolves that ambiguity.
"""

from __future__ import annotations

import base64
import json
import urllib.parse
from dataclasses import dataclass

import httpx

BASE_URL = "https://geobridge.ru/maps/pp/api"
DEFAULT_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class GeobridgePoint:
    title: str
    lat: float
    lng: float
    region: str | None


def _escape_label(label: str) -> str:
    # Mirrors the site's own JS client (map255.js): "/" and "#" are replaced
    # before being placed in the URL path, since the API reads the whole
    # "title=..." segment as one raw path component, not a query string.
    return label.replace("/", "---").replace("#", "-----")


def fetch_points(
    label: str,
    *,
    client: httpx.Client,
    timeout: float = DEFAULT_TIMEOUT_S,
    base_url: str = BASE_URL,
) -> list[GeobridgePoint]:
    """Query geobridge for every point whose title contains `label`."""
    path = urllib.parse.quote(f"title={_escape_label(label)}", safe="=")
    response = client.get(f"{base_url}/{path}", timeout=timeout)
    response.raise_for_status()
    raw_items = json.loads(base64.b64decode(response.content).decode("utf-8"))

    points = []
    for item in raw_items:
        title, lat, lng = item.get("title"), item.get("lat"), item.get("lng")
        if title is None or lat is None or lng is None:
            continue
        region = (item.get("region") or {}).get("title")
        points.append(GeobridgePoint(title=title, lat=float(lat), lng=float(lng), region=region))
    return points
