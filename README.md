# education-to-workforce-mcp

An MCP server over the Education-to-Workforce (E-W) Indicator Framework — 20
essential questions and 99 indicators covering how people progress from early
education through the workforce, for the US, states, counties, and school
districts.

The **framework** is [Mathematica's](https://educationtoworkforce.org/), written
with the Bill & Melinda Gates Foundation. The **data tool** that compiles federal
data against it is the [Urban Institute's](https://apps.urban.org/features/education-workforce-framework-data/).
This server ships a compiled, pinned snapshot of that data — 11.8M observations
in ~24 MB of Parquet — so it answers locally and makes no network calls.

No API key required. Read-only.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

## Installation

```bash
git clone https://github.com/UrbanInstitute/education-to-workforce-mcp.git
cd education-to-workforce-mcp
uv sync
```

## Tools

| Tool | Purpose |
|------|---------|
| `search` | Browse essential questions, or find an indicator/metric by concept |
| `describe` | Explain one metric, indicator, essential question, or disaggregate — or the framework itself |
| `resolve_place` | Find the state/county/district geoid used by the data, from a name |
| `get_data` | Fetch metric values for places, or rank places on a metric |

**Typical workflow**: `search` → `describe` → `resolve_place` → `get_data`.

## Tool reference

### search

Browse the framework, or find a metric by concept.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string (optional) | A concept — `"gifted"`, `"student debt"`, `"chronic absence"`. Omit to list the 20 essential questions |

### describe

Explain one metric, indicator, essential question, disaggregate, or the framework overview.

| Parameter | Type | Description |
|-----------|------|-------------|
| `target` | string | `"m47"` (metric), `"i5"` (indicator), `"eq12"` (essential question), `"disaggregates"`, or `"framework"` |

### resolve_place

Find the geoid for a state, county, or school district, from a name.

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | string | Place name — `"Cook County"`, `"Illinois"`, `"Chicago Public Schools"` |
| `geo_level` | string (optional) | `"state"`, `"county"`, or `"district"` |

### get_data

Fetch metric values for places, or rank places on a metric.

| Parameter | Type | Description |
|-----------|------|-------------|
| `metric_ids` | string | Comma-separated ids: `"47"` or `"47,51"` |
| `geo_level` | string | `"national"`, `"state"`, `"county"`, or `"district"` |
| `geo_ids` | string (optional) | Comma-separated geoids from `resolve_place` |
| `state` | string (optional) | 2-digit state FIPS — every place at `geo_level` in that state |
| `years` | string (optional) | Comma-separated years: `"2022"` or `"2013,2022"`. Omit for all |
| `disaggregate` | string (optional) | A dimension (`"race"`, `"gender"`, `"disability"`, `"income"`) or one group code (`"d1_hispanic"`) |
| `rank` | string (optional) | `"highest"` or `"lowest"` to rank places instead of listing values |
| `limit` | integer (default: 10) | How many places to return when ranking |

---

## Running it

### MCP Inspector (interactive testing)

```bash
uv run mcp dev src/ew_mcp/server.py
```

Opens a browser at `http://localhost:6274` — connect, open **Tools**, and run
any tool with parameters.

### Claude Desktop / Claude Code / VS Code / Copilot CLI

```json
{
  "mcpServers": {
    "ew-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/education-to-workforce-mcp", "ew-mcp"]
    }
  }
}
```

| Client | Where it goes |
|---|---|
| Claude Desktop | `claude_desktop_config.json` — macOS: `~/Library/Application Support/Claude/`; Windows: `%APPDATA%\Claude\` |
| Claude Code | `.claude/settings.json`, or `claude mcp add ew-mcp -- uv run --directory /absolute/path/to/education-to-workforce-mcp NAME` |
| VS Code (Copilot) | `.vscode/settings.json`, nested as `{"mcp": {"servers": {...}}}` |

### stdio (direct)

```bash
uv run ew-mcp
```

### Streamable HTTP (hosted)

```bash
MCP_TRANSPORT=streamable-http PORT=8080 uv run ew-mcp
```

MCP is served at `POST /mcp`; `GET /health` is a plain unauthenticated health
check for a load balancer or orchestrator.

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `stdio` or `streamable-http` |
| `PORT` | `8080` | Listen port |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_ALLOWED_HOSTS` | _(unset)_ | Comma-separated `Host` allow-list |
| `MCP_ALLOWED_ORIGINS` | _(unset)_ | Comma-separated `Origin` allow-list |

**Set `MCP_ALLOWED_HOSTS` to the public hostname before exposing this beyond a
private network.** Both are unset by default, which leaves the SDK's
DNS-rebinding protection off — setting either turns it on. Note that enabling it
with an allow-list that omits the real hostname rejects every request.

## Hosted

Also available as a hosted streamable-HTTP server:
[https://educationdata.urban.org/mcp/ew](https://educationdata.urban.org/mcp/ew)

## Tests

```bash
uv run pytest -q
```

Tests run against the compiled data in `src/ew_mcp/data/` — no network. If the
store is absent they skip rather than fail.
