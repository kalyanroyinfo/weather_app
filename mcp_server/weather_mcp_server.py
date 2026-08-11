"""
Weather-forecast MCP server.

Exposes weather tools over MCP (Model Context Protocol) so a Databricks
Agent Bricks agent can call them like any other tool:
    - get_current_weather(location)
    - get_forecast(location, days)
    - get_travel_recommendation(location, days_ahead)

These tools are thin wrappers around weather_broker.py - all HTTP calls,
JSON parsing, and threshold logic live there. This file only handles the
MCP protocol surface: tool registration, docstrings the agent reads to
decide how to call each tool, and the streamable-HTTP transport Databricks'
MCP client/gateway expects when hosting your own MCP server as a
Databricks App.

Run locally:
    python weather_mcp_server.py
"""

import os
import logging

from fastmcp import FastMCP

import weather_broker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("weather-mcp-server")

mcp = FastMCP("weather-forecast")


@mcp.tool
def get_current_weather(location: str) -> dict:
    """
    Get current weather conditions for a location.

    Args:
        location: City name, US zip code, lat/lon (e.g. "48.8567,2.3508"),
            or any location string WeatherAPI.com's search accepts.

    Returns:
        A dict with location_name, region, country, temp_c, temp_f,
        feelslike_c, humidity, wind_kph, wind_mph, precip_mm, condition,
        and observed_at. On failure, a dict with a single "error" key -
        report this to the user rather than guessing at a value.
    """
    return weather_broker.get_current_conditions(location)


@mcp.tool
def get_forecast(location: str, days: int) -> dict:
    """
    Get a multi-day weather forecast for a location.

    Args:
        location: City name, US zip code, lat/lon, or any location string
            WeatherAPI.com's search accepts.
        days: Number of days to forecast, starting today. Must be 1-3
            (free-tier limit) - values outside that range return a clean
            error instead of a guess or a silently-truncated result.

    Returns:
        A dict with location_name, region, country, and a "forecast" list,
        one entry per day: date, max_temp_c, min_temp_c, avg_temp_c,
        chance_of_rain, chance_of_snow, condition. On failure, a dict with
        a single "error" key.
    """
    return weather_broker.get_forecast(location, days)


@mcp.tool
def get_travel_recommendation(location: str, days_ahead: int = 0) -> dict:
    """
    Get a simple travel recommendation (umbrella, jacket, etc.) for a
    location, derived from that day's forecast using fixed thresholds:
    chance_of_rain > 40% -> umbrella, chance_of_snow > 40% -> boots,
    max_temp_c < 10 -> warm jacket, max_temp_c > 30 -> light clothing and
    sun protection. This is a judgment call applied to the raw forecast,
    not a passthrough of the API response.

    Args:
        location: City name, US zip code, lat/lon, or any location string
            WeatherAPI.com's search accepts.
        days_ahead: How many days from today the target day is (0 = today,
            1 = tomorrow, 2 = day after tomorrow). Must be 0-2.

    Returns:
        A dict with location_name, date, the raw figures reasoned over,
        and a "recommendations" list of plain-language strings. On
        failure, a dict with a single "error" key.
    """
    return weather_broker.get_travel_recommendation(location, days_ahead)


if __name__ == "__main__":
    # Databricks Apps route external HTTP traffic to this port via app.yaml;
    # streamable-http is the transport Databricks' MCP client/gateway
    # expects (see https://docs.databricks.com/aws/en/agents/mcp-tools/custom-mcp).
    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", 8000)))
    mcp.run(transport="http", host="0.0.0.0", port=port)
