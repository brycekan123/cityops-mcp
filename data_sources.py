"""
API fetch logic for public data sources.
Imported by city_data_server.py — no MCP or SQLite here, just HTTP + clean rows.
"""

import requests
from datetime import date, timedelta

HEADERS = {"User-Agent": "CityOps-MCP-Agent/1.0"}

CITIES = {
    "atlanta":     (33.749,  -84.388),
    "new york":    (40.713,  -74.006),
    "los angeles": (34.052, -118.244),
    "chicago":     (41.878,  -87.630),
    "houston":     (29.760,  -95.369),
    "seattle":     (47.606, -122.332),
    "miami":       (25.775,  -80.208),
    "denver":      (39.739, -104.984),
}

OPEN_METEO_DAILY = "temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max"


def _parse_open_meteo(data: dict, location: str) -> list[dict]:
    """Convert Open-Meteo daily response into row dicts."""
    daily     = data["daily"]
    loaded_at = date.today().isoformat()
    rows = []
    for i, dt in enumerate(daily["time"]):
        rows.append({
            "location":  location.title(),
            "date":      dt,
            "temp_max":  daily["temperature_2m_max"][i],
            "temp_min":  daily["temperature_2m_min"][i],
            "precip_mm": daily["precipitation_sum"][i],
            "wind_mph":  daily["windspeed_10m_max"][i],
            "loaded_at": loaded_at,
        })
    return rows


def fetch_weather(location: str,
                  days: int = 16,
                  start_date: str | None = None,
                  end_date: str | None = None) -> list[dict]:
    """
    Fetch weather from Open-Meteo.

    Two modes:
      • Forecast (default): days=16 returns the next N days of forecast.
      • Historical:  provide start_date + end_date (YYYY-MM-DD).
        - Dates before today  → Open-Meteo archive API (actual observed data).
        - Dates overlap today → archive up to yesterday, forecast fills the rest.

    Columns returned: location, date, temp_max, temp_min, precip_mm, wind_mph, loaded_at.
    Supported cities: atlanta, new york, los angeles, chicago, houston, seattle, miami, denver.
    """
    key = location.lower().strip()
    if key not in CITIES:
        raise ValueError(f"Unknown city '{location}'. Supported: {sorted(CITIES)}")

    lat, lon = CITIES[key]
    today    = date.today()
    rows     = []

    common_params = {
        "latitude":         lat,
        "longitude":        lon,
        "daily":            OPEN_METEO_DAILY,
        "temperature_unit": "fahrenheit",
        "wind_speed_unit":  "mph",
        "timezone":         "auto",
    }

    if start_date and end_date:
        # ── Range mode ─────────────────────────────────────────────────────────
        start = date.fromisoformat(start_date)
        end   = date.fromisoformat(end_date)

        # Split at yesterday: past portion → archive, future portion → forecast
        archive_end   = min(end,   today - timedelta(days=1))
        forecast_start = max(start, today)
        forecast_end  = min(end,   today + timedelta(days=15))

        # Archive portion (historical, observed data)
        if start <= archive_end:
            r = requests.get(
                "https://archive-api.open-meteo.com/v1/archive",
                params={**common_params, "start_date": start.isoformat(),
                        "end_date": archive_end.isoformat()},
                timeout=20,
            )
            r.raise_for_status()
            rows += _parse_open_meteo(r.json(), location)

        # Forecast portion (future data within 16-day window)
        if forecast_start <= forecast_end:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={**common_params, "start_date": forecast_start.isoformat(),
                        "end_date": forecast_end.isoformat()},
                timeout=20,
            )
            r.raise_for_status()
            rows += _parse_open_meteo(r.json(), location)

        if not rows:
            raise ValueError(
                f"No data available for {location} between {start_date} and {end_date}. "
                f"Forecast only covers up to {(today + timedelta(days=15)).isoformat()}."
            )

    else:
        # ── Forecast mode ──────────────────────────────────────────────────────
        days = min(days, 16)
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={**common_params, "forecast_days": days},
            timeout=20,
        )
        r.raise_for_status()
        rows = _parse_open_meteo(r.json(), location)

    return rows


