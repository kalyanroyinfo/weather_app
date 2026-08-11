"""
WeatherAPI.com broker adapter.

Thin wrapper around WeatherAPI.com's REST API (current.json, forecast.json).
No MCP/FastMCP concerns live here - just HTTP calls, JSON parsing, and the
Databricks secret lookup for the API key. Every public function returns a
plain dict; on failure it returns {"error": "<message>"} instead of raising,
so the MCP tool layer never leaks a raw stack trace to the agent.
"""

import base64
import os

import requests
from databricks.sdk import WorkspaceClient

_w = WorkspaceClient()

_SECRET_SCOPE = os.environ.get("WEATHER_SECRET_SCOPE", "weather")
_API_KEY_SECRET_KEY = os.environ.get("WEATHER_API_KEY_SECRET_KEY", "weatherapi-key")
_BASE_URL = "https://api.weatherapi.com/v1"

_api_key: str | None = None


def _secret(key: str) -> str:
    """Fetch and base64-decode a value from the Databricks secret scope."""
    secret = _w.secrets.get_secret(scope=_SECRET_SCOPE, key=key)
    return base64.b64decode(secret.value).decode("utf-8")


def _get_api_key() -> str:
    """Return the cached WeatherAPI.com key, fetching it from secrets on first use."""
    global _api_key
    if _api_key is None:
        _api_key = _secret(_API_KEY_SECRET_KEY)
    return _api_key


def get_current_conditions(location: str) -> dict:
    """
    Get current weather conditions for a location.

    Args:
        location: City name, US zip code, lat/lon (e.g. "48.8567,2.3508"),
            or any location string WeatherAPI.com's search accepts.

    Returns:
        A dict with location_name, region, country, temp_c, temp_f,
        feelslike_c, humidity, wind_kph, wind_mph, precip_mm, condition,
        and observed_at. On failure, a dict with a single "error" key
        containing a clean, human-readable message - never a raw exception
        or stack trace.
    """
    try:
        response = requests.get(
            f"{_BASE_URL}/current.json",
            params={"key": _get_api_key(), "q": location},
            timeout=10,
        )
        data = response.json()
    except requests.RequestException as e:
        return {"error": f"Could not reach WeatherAPI.com: {e}"}

    if "error" in data:
        return {"error": data["error"].get("message", "Unknown WeatherAPI.com error")}

    location_data = data["location"]
    current = data["current"]
    return {
        "location_name": location_data["name"],
        "region": location_data["region"],
        "country": location_data["country"],
        "temp_c": current["temp_c"],
        "temp_f": current["temp_f"],
        "feelslike_c": current["feelslike_c"],
        "humidity": current["humidity"],
        "wind_kph": current["wind_kph"],
        "wind_mph": current["wind_mph"],
        "precip_mm": current["precip_mm"],
        "condition": current["condition"]["text"],
        "observed_at": current["last_updated"],
    }


def get_forecast(location: str, days: int) -> dict:
    """
    Get a multi-day weather forecast for a location.

    Args:
        location: City name, US zip code, lat/lon, or any location string
            WeatherAPI.com's search accepts.
        days: Number of days to forecast, starting today (1-3). WeatherAPI.com's
            free tier only returns up to 3 days of forecast; anything outside
            that range returns a clean error rather than a guess or a
            silently-truncated result.

    Returns:
        A dict with location_name, region, country, and a "forecast" list,
        one entry per day: date, max_temp_c, min_temp_c, avg_temp_c,
        chance_of_rain, chance_of_snow, condition. On failure, a dict with
        a single "error" key.
    """
    if not isinstance(days, int) or days < 1 or days > 3:
        return {
            "error": f"days must be between 1 and 3 (free-tier limit), got {days!r}"
        }

    try:
        response = requests.get(
            f"{_BASE_URL}/forecast.json",
            params={"key": _get_api_key(), "q": location, "days": days},
            timeout=10,
        )
        data = response.json()
    except requests.RequestException as e:
        return {"error": f"Could not reach WeatherAPI.com: {e}"}

    if "error" in data:
        return {"error": data["error"].get("message", "Unknown WeatherAPI.com error")}

    location_data = data["location"]
    forecast_days = []
    for day_entry in data["forecast"]["forecastday"]:
        day = day_entry["day"]
        forecast_days.append({
            "date": day_entry["date"],
            "max_temp_c": day["maxtemp_c"],
            "min_temp_c": day["mintemp_c"],
            "avg_temp_c": day["avgtemp_c"],
            "chance_of_rain": day["daily_chance_of_rain"],
            "chance_of_snow": day["daily_chance_of_snow"],
            "condition": day["condition"]["text"],
        })

    return {
        "location_name": location_data["name"],
        "region": location_data["region"],
        "country": location_data["country"],
        "forecast": forecast_days,
    }


def get_travel_recommendation(location: str, days_ahead: int = 0) -> dict:
    """
    Give a simple travel recommendation (umbrella, jacket, etc.) for a
    location, based on that day's forecast.

    Applies fixed thresholds to the raw forecast data rather than just
    passing it through - this is the "derived judgment call" the tool is
    for, not a passthrough of the API response:
        - chance_of_rain > 40%  -> recommend an umbrella
        - chance_of_snow > 40%  -> recommend waterproof boots
        - max_temp_c < 10       -> recommend a warm jacket
        - max_temp_c > 30       -> recommend light clothing + sun protection
    More than one can apply at once (e.g. cold AND rainy).

    Args:
        location: City name, US zip code, lat/lon, or any location string
            WeatherAPI.com's search accepts.
        days_ahead: How many days from today the target day is (0 = today,
            1 = tomorrow, 2 = day after tomorrow). Must be 0-2, since it
            maps onto get_forecast's 1-3 day free-tier limit.

    Returns:
        A dict with location_name, date, the raw figures reasoned over
        (max_temp_c, min_temp_c, chance_of_rain, chance_of_snow, condition),
        and a "recommendations" list of plain-language strings. On failure
        (bad location, API error, or days_ahead out of range), a dict with
        a single "error" key.
    """
    if not isinstance(days_ahead, int) or days_ahead < 0 or days_ahead > 2:
        return {
            "error": (
                f"days_ahead must be between 0 (today) and 2 (day after "
                f"tomorrow) - free-tier limit, got {days_ahead!r}"
            )
        }

    forecast = get_forecast(location, days=days_ahead + 1)
    if "error" in forecast:
        return forecast

    day = forecast["forecast"][days_ahead]

    recommendations = []
    if day["chance_of_rain"] > 40:
        recommendations.append(
            f"Bring an umbrella - {day['chance_of_rain']}% chance of rain."
        )
    if day["chance_of_snow"] > 40:
        recommendations.append(
            f"Wear waterproof boots - {day['chance_of_snow']}% chance of snow."
        )
    if day["max_temp_c"] < 10:
        recommendations.append(
            f"Bring a warm jacket - high of only {day['max_temp_c']}\u00b0C."
        )
    if day["max_temp_c"] > 30:
        recommendations.append(
            f"Dress light and use sun protection - high of {day['max_temp_c']}\u00b0C."
        )
    if not recommendations:
        recommendations.append("No special precautions needed - conditions look mild.")

    return {
        "location_name": forecast["location_name"],
        "date": day["date"],
        "max_temp_c": day["max_temp_c"],
        "min_temp_c": day["min_temp_c"],
        "chance_of_rain": day["chance_of_rain"],
        "chance_of_snow": day["chance_of_snow"],
        "condition": day["condition"],
        "recommendations": recommendations,
    }