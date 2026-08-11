# Weather-Prediction MCP Server + Agent Bricks Agent

An MCP server exposing weather-forecast tools, deployed as a Databricks App,
wired to a Databricks Agent Bricks Supervisor Agent that answers
natural-language weather questions and makes simple travel recommendations.

This guide is written so anyone can reproduce the whole thing from scratch,
including on a brand-new, completely free Databricks account.

## Architecture

```
Agent Bricks "weather-agent" (Supervisor Agent)
        |
        | MCP tool calls: get_current_weather, get_forecast, get_travel_recommendation
        v
mcp_server/weather_mcp_server.py   (Databricks App, name starts with "mcp-")
        |
        | thin delegation
        v
mcp_server/weather_broker.py  --(HTTPS/REST)-->  WeatherAPI.com
        |
        | fetches key via
        v
Databricks secret scope "weather" / key "weatherapi-key"
```

- `mcp_server/weather_mcp_server.py` — FastMCP server exposing the 3 tools over
  streamable HTTP, deployed as its own Databricks App.
- `mcp_server/weather_broker.py` — broker adapter: all HTTP calls to
  WeatherAPI.com and JSON parsing live here; every function returns a plain
  dict, and returns `{"error": "..."}` on failure instead of raising, so
  nothing ever surfaces a raw stack trace to the agent.
- No dashboard app was built — Databricks Free Edition currently allows only
  one app per account, so that slot went to the required MCP server rather
  than the optional dashboard stretch goal.

## Tools

| Tool | Description |
|---|---|
| `get_current_weather(location)` | Current temperature, feels-like, humidity, wind, precipitation, and conditions for a location. |
| `get_forecast(location, days)` | Multi-day forecast (1-3 days — WeatherAPI.com's free-tier limit): daily high/low, chance of rain/snow, conditions. |
| `get_travel_recommendation(location, days_ahead)` | Derived recommendation (umbrella / jacket / sun protection / none) based on fixed thresholds applied to that day's forecast (chance of rain or snow > 40%, max temp < 10°C or > 30°C) — not a passthrough of the raw API response. |

## Prerequisites

- A **Databricks Free Edition** account — no cloud account (AWS/Azure/GCP),
  no credit card, and no paid workspace required. Sign up at
  `https://login.databricks.com/` and choose Free Edition; a workspace is
  provisioned automatically. Free Edition already includes everything this
  project needs: Databricks Apps (1 app per account, serverless-only,
  24h compute auto-stop) and Agent Bricks.
- A free **WeatherAPI.com** account (`https://www.weatherapi.com/`, free
  plan, no card) for an API key.
- The Databricks CLI installed locally (`databricks --version`).
- A GitHub account, for hosting the repo.

## Step 1 — Authenticate the CLI against your workspace

```bash
databricks auth login --host <your-free-edition-workspace-url> --profile weather-hw
```

This opens a browser OAuth login and saves a named profile (`weather-hw`) to
`~/.databrickscfg`. Using a distinct profile name matters if you already have
other Databricks profiles configured for other workspaces — every command
below passes `--profile weather-hw` explicitly so it never accidentally hits
a different workspace.

## Step 2 — Scaffold the project with Databricks Asset Bundles (DAB)

```bash
databricks bundle init --profile weather-hw
```

Answer the prompts:
- Project name: any valid identifier (letters/numbers/underscores only).
- Include a job / ETL pipeline / wheel package: **no** to all three — none of
  that applies to hosting an MCP server as a Databricks App.
- Use serverless compute: **yes** — Free Edition is serverless-only anyway.

This generates a `databricks.yml` plus sample job/pipeline/notebook
scaffolding. Delete everything you said "no" to (`resources/sample_job.job.yml`,
`resources/*.pipeline.yml`, `fixtures/`, `src/<project>/`, `src/<project>_etl/`,
sample notebooks, sample tests, `pyproject.toml`) and trim the generated
`databricks.yml` down to just the `bundle:`, `include:`, and a single `dev`
target (no `artifacts:`/`variables:` blocks, which only existed for the
job/pipeline/wheel options you declined).

Validate after trimming:

```bash
databricks bundle validate --profile weather-hw
```

## Step 3 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial DAB scaffold"
git remote add origin <your-repo-url>
git branch -M main
git push -u origin main
```

Add `.env` to `.gitignore` up front — local secret values must never be
committed (Databricks secrets, covered below, are the only place the real
API key ever lives).

## Step 4 — Write the MCP server code

Create an `mcp_server/` folder containing:
- `weather_broker.py` — all HTTP calls to WeatherAPI.com (`/current.json`,
  `/forecast.json`) and JSON parsing. Reads the API key via a Databricks
  secret (`WorkspaceClient().secrets.get_secret(scope, key)`, base64-decoded —
  Databricks' secret-retrieval API always returns values base64-encoded,
  regardless of how they were stored). Every function returns a plain dict,
  and `{"error": "..."}` on any failure (bad location, network error,
  WeatherAPI error response) instead of raising — so nothing ever leaks a raw
  stack trace upward.
- `weather_mcp_server.py` — a `FastMCP` app with three `@mcp.tool`-decorated
  functions, each a one-line delegation into `weather_broker.py` (no HTTP
  calls or parsing logic in this file at all). Runs with
  `mcp.run(transport="http", host="0.0.0.0", port=...)` — streamable HTTP is
  the transport Databricks' MCP client/gateway expects.

## Step 5 — Store the WeatherAPI.com key as a Databricks secret

```bash
databricks secrets create-scope weather --profile weather-hw
databricks secrets put-secret weather weatherapi-key --profile weather-hw
```

The second command prompts interactively for the value — never pass it via
a `--string-value` flag, which would leave it sitting in your shell history.

## Step 6 — `requirements.txt`, `app.yaml`, and the DAB `apps` resource

`mcp_server/requirements.txt`:
```
databricks-sdk>=0.30.0
requests>=2.31.0
fastmcp>=3.2.0
```

`mcp_server/app.yaml`:
```yaml
command:
  - "python"
  - "weather_mcp_server.py"

resources:
  - name: requirements
    source:
      path: ./requirements.txt

env:
  - name: WEATHER_SECRET_SCOPE
    value: "weather"
  - name: WEATHER_API_KEY_SECRET_KEY
    value: "weatherapi-key"
```

`resources/weather_mcp_app.yml` (a new file — automatically picked up by the
`include: resources/*.yml` line in `databricks.yml`):
```yaml
resources:
  apps:
    weather_mcp_app:
      name: "mcp-weather-server"
      description: "Weather forecast MCP server - current conditions, forecast, and travel recommendation tools"
      source_code_path: ../mcp_server
      permissions:
        - level: CAN_USE
          group_name: users
```

Two gotchas that aren't obvious from the docs:
- **`source_code_path` is relative to the file it's declared in** (i.e.
  relative to `resources/`), not the bundle root — hence `../mcp_server`, not
  `./mcp_server`.
- **The app's name must start with `mcp-`** — Agent Bricks' "Custom MCP
  Server" tool picker only lists Databricks Apps whose name matches that
  prefix. Pick the name before your first deploy if possible; renaming later
  means deleting the old app and deploying a new one under the new name
  (relevant on Free Edition, which allows only one app at a time).

## Step 7 — Deploy

```bash
databricks bundle validate --profile weather-hw
databricks bundle deploy --profile weather-hw
databricks bundle run weather_mcp_app --profile weather-hw
```

The last command prints the app's URL once it starts
(`https://<app-name>-<workspace-id>.<cloud>.databricksapps.com`).

## Step 8 — Grant the app access to the secret

The app runs under its own service principal, not your user account — so
the secret scope ACL you have by default doesn't cover it. The first real
tool call will fail with something like:

```
does not have secret-scopes.secrets/get permission on scope weather ... client_id=<app-service-principal-id>
```

Grab that client ID from the error (or `databricks apps get <app-name>`) and grant it read access:

```bash
databricks secrets put-acl weather <app-service-principal-client-id> READ --profile weather-hw
```

## Step 9 — Build the Agent Bricks agent

1. In the workspace sidebar: **Agents** → **Create new Agent**.
2. Choose **Supervisor Agent** (the type built to combine MCP tools/sub-agents
   into one tool-calling agent — Free Edition's Agent Bricks doesn't offer a
   separate "Custom LLM" type, Supervisor Agent covers this case).
3. Under **Tools and sub-agents**, add a **Custom MCP Server**, select your
   deployed app, and attach all three tools. Remove the default `python_exec`
   UC Function tool if it's present — not needed here.
4. Paste the system prompt below into the agent's instructions field.
5. Save the agent under a name (e.g. `weather-agent`).

### System prompt

```
You are a weather assistant. You answer questions about current conditions,
short-term forecasts, and simple travel/clothing recommendations, using ONLY
the three tools available to you: get_current_weather, get_forecast, and
get_travel_recommendation.

Tool selection rules:
- "What's the weather / temperature / conditions right now" -> get_current_weather(location).
- "N-day forecast" or questions about multiple days -> get_forecast(location, days),
  where days is 1-3. This data source's free tier does not support forecasts
  beyond 3 days out - if asked for something further out (e.g. "next week"),
  tell the user that's beyond what you can forecast, rather than guessing.
- "Should I bring an umbrella/jacket", "what should I wear", "this weekend",
  or any request for advice -> get_travel_recommendation(location, days_ahead),
  where days_ahead is 0 (today), 1 (tomorrow), or 2 (day after tomorrow).
  Convert relative dates (e.g. "tomorrow", "Saturday") to the correct small
  integer yourself, and state which date you used so the user can correct you
  if you guessed wrong.

Guardrails:
- Never state a temperature, forecast, or recommendation that did not come
  directly from a tool call in this conversation. Do not use prior knowledge
  or general assumptions about a location's climate.
- If a tool's result contains an "error" key, do not retry silently or
  guess a substitute answer - tell the user plainly what went wrong (e.g. the
  location couldn't be resolved, or the request is outside the 3-day forecast
  window) and ask them to clarify or try a different location.
- If a location name is ambiguous (e.g. a city that exists in multiple
  countries), ask the user to clarify before calling a tool, rather than
  picking one arbitrarily.
- When giving a recommendation, always mention the specific number that
  justifies it (e.g. "60% chance of rain"), not just the verdict.

Keep responses concise and conversational.
```

## Step 10 — Test it

Use the agent's built-in test panel (or Playground, selecting a model plus
the external MCP server as an ad hoc tool). Each tool call requires manual
approval in test mode — click approve to let it actually execute.

## Demo

**1. Current conditions**
> Q: What's the weather in Kolkata?
> Tool call: `get_current_weather({"location": "Kolkata"})`
> A: Currently in Kolkata, it's 28.9°C (84°F) with patchy light rain and thunder
> in the area. It feels like 36.4°C due to the high humidity of 89%. Winds are
> light at 9.4 km/h, and there's been about 0.2 mm of precipitation.

**2. Forecast**
> Q: What's the 3-day forecast for Chicago?
> Tool call: `get_forecast({"location": "Chicago", "days": 3})`
> A: Today (Aug 11): misty, high 28.8°C / low 22°C, 70% chance of rain.
> Tomorrow (Aug 12): sunny, high 27.1°C / low 20.8°C, 81% chance of rain.
> Thursday (Aug 13): sunny, high 26.7°C / low 20.7°C, 17% chance of rain.
> You'll likely want an umbrella for the next couple of days, especially
> tomorrow with that 81% rain chance.

**3. Recommendation**
> Q: Will it rain in Dum Dum, Kolkata tomorrow? Should I bring an umbrella?
> Tool call: `get_travel_recommendation({"location": "Dum Dum, Kolkata", "days_ahead": 1})`
> A: Yes, rain is likely in Dum Dum tomorrow (August 12th). There's a 63%
> chance of rain with thundery outbreaks expected nearby. Temperatures will
> range from 28°C to 33.3°C. You'll probably want to carry an umbrella!

**Bonus: guardrail (ambiguous location)**
> Q: What's the weather in Dumdum?
> A: I found a location called Dumdum in Syria, but I suspect you're asking
> about Dum Dum in Kolkata, West Bengal, India (near the airport). Could you
> clarify which Dumdum you're asking about?
> *(No tool called - the agent asked for clarification instead of guessing
> which location, per the system prompt's guardrail.)*
