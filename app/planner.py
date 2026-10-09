"""Core logic: find green spots (OpenStreetMap), read the weather (Open-Meteo),
pick a loop that fits the free time, and let an open-weight LLM narrate it.

Every network piece degrades gracefully, so the app still returns a usable plan
when the map service, the weather service or the model is unreachable.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import os
import time

import httpx

UA = {"User-Agent": "touch-grass-planner/0.1 (student hackathon project)"}
OVERPASS_URLS = [
    os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter"),
    "https://overpass.kumi.systems/api/interpreter",
]
WALK_KMH = 4.8   # relaxed walking pace
DETOUR = 1.3     # streets are not straight lines
CACHE_TTL = 600  # seconds; be kind to the public Overpass servers
_cache: dict = {}

GENERIC_NAMES = {
    "water": "Water edge",
    "park": "Green space",
    "trees": "Patch of trees",
    "grass": "Open grass",
    "street": "Turn-around point",
}
KIND_WEIGHT = {"water": 0.10, "park": 0.10, "trees": 0.06, "grass": 0.0, "street": -0.1}

NOTICE = {
    "water": ["How the light moves on the water", "Any bird or insect near the edge", "A sound you can hear over the traffic"],
    "park": ["The tallest tree you can see", "Three different leaf shapes", "Who else is outside right now"],
    "trees": ["One tree with interesting bark", "Where the shade starts and stops", "A bird call you can't name yet"],
    "grass": ["One flower or weed in bloom", "How the ground feels different from pavement", "The widest bit of sky you can find"],
    "street": ["A plant growing where it shouldn't", "The sky between two buildings", "A sound you normally tune out"],
}
GENERAL_NOTICE = ["Your breathing when you slow down", "Something that changed since yesterday"]

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "heavy showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}


# ---------- geometry ----------
def haversine_km(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    d = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(d))


def destination(lat, lon, bearing_deg, km):
    d, b = km / 6371.0, math.radians(bearing_deg)
    la1, lo1 = math.radians(lat), math.radians(lon)
    la2 = math.asin(math.sin(la1) * math.cos(d) + math.cos(la1) * math.sin(d) * math.cos(b))
    lo2 = lo1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(la1), math.cos(d) - math.sin(la1) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


# ---------- OpenStreetMap ----------
def classify(tags: dict):
    if tags.get("natural") in ("water", "wetland") or tags.get("waterway"):
        return "water"
    if tags.get("leisure") in ("park", "garden", "nature_reserve"):
        return "park"
    if tags.get("natural") == "wood" or tags.get("landuse") == "forest":
        return "trees"
    if tags.get("landuse") in ("grass", "meadow"):
        return "grass"
    return None


def parse_spots(elements: list) -> list[dict]:
    raw = []
    for el in elements:
        tags = el.get("tags", {})
        kind = classify(tags)
        if not kind:
            continue
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or lon is None:
            continue
        name = tags.get("name") or tags.get("name:en")
        raw.append({"name": name or GENERIC_NAMES[kind], "named": bool(name), "kind": kind, "lat": lat, "lon": lon})
    raw.sort(key=lambda s: (not s["named"], -KIND_WEIGHT[s["kind"]]))
    out, seen = [], set()
    for s in raw:
        if s["name"].lower() in seen and s["named"]:
            continue
        if any(o["kind"] == s["kind"] and haversine_km((o["lat"], o["lon"]), (s["lat"], s["lon"])) < 0.12 for o in out):
            continue
        seen.add(s["name"].lower())
        out.append(s)
    return out


async def fetch_spots(client: httpx.AsyncClient, lat: float, lon: float, radius_m: int) -> list[dict]:
    key = ("ov", round(lat, 3), round(lon, 3), radius_m // 100)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return hit[1]
    around = f"(around:{radius_m},{lat},{lon})"
    query = (
        "[out:json][timeout:20];("
        f'nwr{around}["leisure"~"^(park|garden|nature_reserve)$"];'
        f'nwr{around}["natural"~"^(water|wood|wetland)$"];'
        f'nwr{around}["waterway"~"^(river|stream|canal)$"];'
        f'nwr{around}["landuse"~"^(forest|grass|meadow)$"];'
        ");out center tags 80;"
    )
    elements = None
    for url in OVERPASS_URLS:
        try:
            r = await client.post(url, data={"data": query}, headers=UA, timeout=25)
            r.raise_for_status()
            elements = r.json().get("elements", [])
            break
        except Exception:
            continue
    if elements is None:
        raise RuntimeError("map data service unreachable")
    spots = parse_spots(elements)
    _cache[key] = (time.time(), spots)
    return spots


# ---------- loop selection ----------
def choose_loop(start: tuple, spots: list[dict], target_km: float) -> dict:
    """Pick one or two waypoints so start -> A (-> B) -> start is close to target_km."""
    reach = max(0.3, target_km / (2 * DETOUR) * 1.2)
    cands = []
    for s in spots:
        d = haversine_km(start, (s["lat"], s["lon"]))
        if 0.08 <= d <= reach:
            cands.append({**s, "d": d})
    cands.sort(key=lambda s: (-(s["named"]), -KIND_WEIGHT[s["kind"]], s["d"]))
    cands = cands[:14]

    best, best_cost = None, 1e9
    for a in cands:
        length = 2 * a["d"] * DETOUR
        cost = abs(length - target_km) + 0.1 - 0.08 * a["named"] - KIND_WEIGHT[a["kind"]]
        if cost < best_cost:
            best, best_cost = ([a], length), cost
    for a, b in itertools.combinations(cands, 2):
        pa, pb = (a["lat"], a["lon"]), (b["lat"], b["lon"])
        length = (a["d"] + haversine_km(pa, pb) + b["d"]) * DETOUR
        cost = abs(length - target_km) + (0.2 if a["kind"] == b["kind"] else 0.0)
        cost -= 0.08 * (a["named"] + b["named"]) + KIND_WEIGHT[a["kind"]] + KIND_WEIGHT[b["kind"]]
        if cost < best_cost:
            best, best_cost = ([a, b], length), cost

    if best:
        stops, length = best
        return _loop(stops, length, synthetic=False)
    return synthetic_loop(start, target_km)


def synthetic_loop(start: tuple, target_km: float) -> dict:
    """No green spot found (or map data down): an equilateral block loop from the start."""
    side = target_km / (3 * DETOUR)
    a = destination(start[0], start[1], 30, side)
    b = destination(start[0], start[1], 90, side)
    stops = [
        {"name": "Turn-around point A", "kind": "street", "lat": a[0], "lon": a[1], "named": False},
        {"name": "Turn-around point B", "kind": "street", "lat": b[0], "lon": b[1], "named": False},
    ]
    return _loop(stops, target_km, synthetic=True)


def _loop(stops: list[dict], length_km: float, synthetic: bool) -> dict:
    clean = [{k: s[k] for k in ("name", "kind", "lat", "lon")} for s in stops]
    return {
        "stops": clean,
        "km": round(length_km, 1),
        "minutes": max(5, round(length_km / WALK_KMH * 60)),
        "synthetic": synthetic,
    }


def directions_url(start: tuple, stops: list[dict]) -> str:
    pts = [f"{start[0]:.5f},{start[1]:.5f}"] + [f"{s['lat']:.5f},{s['lon']:.5f}" for s in stops]
    q = (
        f"https://www.google.com/maps/dir/?api=1&travelmode=walking&origin={pts[0]}&destination={pts[0]}"
        f"&waypoints={'|'.join(pts[1:])}"
    )
    return q


# ---------- weather ----------
async def fetch_weather(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    r = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon, "timezone": "auto",
            "current": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m,is_day",
        },
        headers=UA, timeout=10,
    )
    r.raise_for_status()
    c = r.json()["current"]
    code = int(c.get("weather_code", 0))
    temp, feels = c["temperature_2m"], c["apparent_temperature"]
    precip, day = c.get("precipitation", 0) or 0, bool(c.get("is_day", 1))
    desc = WMO.get(code, "mixed")
    if code >= 95 or precip >= 1:
        verdict, line = "skip", f"{desc.capitalize()} outside. Save this walk for later."
    elif precip > 0 or 51 <= code <= 67 or 80 <= code <= 82:
        verdict, line = "caution", f"{desc.capitalize()} right now. Take a jacket or wait it out."
    elif feels >= 38:
        verdict, line = "caution", f"Feels like {feels:.0f}°C. Keep to shade and carry water."
    elif feels <= 5:
        verdict, line = "caution", f"Feels like {feels:.0f}°C. Dress warm and keep moving."
    elif not day:
        verdict, line = "caution", "It's dark. Stick to lit, familiar paths."
    else:
        verdict, line = "go", f"Good conditions: {desc}, {temp:.0f}°C."
    return {"temp": temp, "feels": feels, "precip": precip, "wind": c.get("wind_speed_10m"),
            "desc": desc, "is_day": day, "verdict": verdict, "headline": line}


# ---------- narration ----------
def llm_base() -> str:
    hostport = os.getenv("LLM_HOSTPORT")
    if hostport:
        return f"http://{hostport}/v1"
    return os.getenv("LLM_BASE_URL", "http://localhost:11434/v1").rstrip("/")


def template_narration(loop: dict, weather: dict | None, minutes: int) -> dict:
    stops = loop["stops"]
    names = " and ".join(s["name"] for s in stops)
    notes = []
    for i, s in enumerate(stops):
        notes.append("Slow down here and look around before heading on." if i == 0 else "Take one slow breath, then turn back.")
    notice = []
    for s in stops:
        notice += NOTICE.get(s["kind"], [])
    notice = list(dict.fromkeys(notice + GENERAL_NOTICE))[:5]
    tip = "Phone in your pocket, eyes up." if not weather or weather["verdict"] == "go" else weather["headline"]
    return {
        "title": f"A {loop['minutes']}-minute loop via {stops[0]['name']}",
        "intro": f"You have about {minutes} minutes. This loop passes {names} and brings you back to where you started.",
        "stop_notes": notes,
        "notice": notice,
        "tip": tip,
    }


async def llm_narration(client: httpx.AsyncClient, ctx: dict) -> dict | None:
    model = os.getenv("LLM_MODEL", "qwen2.5:3b")
    headers = {"Authorization": f"Bearer {os.getenv('LLM_API_KEY', 'ollama')}"}
    system = (
        "You write short, warm walking plans for university students on a break. "
        "Use ONLY the stops, distances and weather given. Never invent places. "
        "Reply with JSON only, with keys: title (string, max 8 words), intro (2 sentences), "
        "stop_notes (array of one short sentence per stop, same order), "
        "notice (array of exactly 5 short things to look, listen or feel for, specific to the stop types and weather), "
        "tip (one sentence)."
    )
    body = {
        "model": model, "temperature": 0.7,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(ctx)}],
    }
    for use_json_mode in (True, False):
        payload = {**body, **({"response_format": {"type": "json_object"}} if use_json_mode else {})}
        try:
            r = await client.post(f"{llm_base()}/chat/completions", json=payload, headers=headers, timeout=60)
            if r.status_code >= 400 and use_json_mode:
                continue
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
            data = json.loads(text[text.index("{"): text.rindex("}") + 1])
            n = len(ctx["stops"])
            notes = [str(x) for x in data.get("stop_notes", [])][:n]
            notice = [str(x) for x in data.get("notice", [])][:5]
            if not notes or len(notice) < 3 or not data.get("intro"):
                return None
            notes += [""] * (n - len(notes))
            return {"title": str(data.get("title", ""))[:80] or "Your loop", "intro": str(data["intro"]),
                    "stop_notes": notes, "notice": notice, "tip": str(data.get("tip", ""))}
        except Exception:
            if not use_json_mode:
                return None
    return None


# ---------- orchestration ----------
async def build_plan(lat: float, lon: float, minutes: int) -> dict:
    start = (lat, lon)
    target_km = minutes / 60 * WALK_KMH
    radius_m = int(max(350, target_km * 1000 / (2 * DETOUR) * 1.2))
    warnings: list[str] = []

    async with httpx.AsyncClient() as client:
        spots_r, weather_r = await asyncio.gather(
            fetch_spots(client, lat, lon, radius_m), fetch_weather(client, lat, lon), return_exceptions=True
        )
        if isinstance(spots_r, Exception):
            warnings.append("Map data was unreachable, so this is a simple block loop instead of a green route.")
            loop = synthetic_loop(start, target_km)
        else:
            loop = choose_loop(start, spots_r, target_km)
            if loop["synthetic"]:
                warnings.append("No parks or water found within walking range, so this is a simple block loop.")
        weather = None
        if isinstance(weather_r, Exception):
            warnings.append("Weather was unreachable. Check the sky before you go.")
        else:
            weather = weather_r

        ctx = {
            "free_minutes": minutes, "loop_km": loop["km"], "loop_minutes": loop["minutes"],
            "stops": [{"name": s["name"], "type": s["kind"]} for s in loop["stops"]],
            "weather": weather and {"summary": weather["desc"], "temp_c": weather["temp"], "feels_like_c": weather["feels"]},
        }
        narration = await llm_narration(client, ctx)

    narrator = os.getenv("LLM_MODEL", "qwen2.5:3b")
    if narration is None:
        narration = template_narration(loop, weather, minutes)
        narrator = "template"
        warnings.append("The language model was unreachable, so the text came from a built-in template.")

    return {
        "minutes": minutes, "start": {"lat": lat, "lon": lon}, "loop": loop, "weather": weather,
        "narration": narration, "narrator": narrator,
        "directions_url": directions_url(start, loop["stops"]), "warnings": warnings,
    }
