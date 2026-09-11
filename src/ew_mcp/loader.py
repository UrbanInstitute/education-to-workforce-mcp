"""Data access for the E-W MCP server.

This is the ONLY module that knows how the compiled store is laid out. Every
other module goes through the functions here, so swapping Parquet for something
else is a one-file change. 

The connection is opened lazily and read-only. Nothing here mutates anything:
the store is rebuilt by scripts/build_ew_data.py, never by the server.
"""

from __future__ import annotations

import json
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb

DATA_DIR = Path(__file__).parent / "data"

# Tool functions are synchronous, so the MCP SDK runs each call in a worker
# thread. A DuckDBPyConnection is not safe to use from several threads at once,
# so each thread gets its own cursor over the one connection — cursors share the
# registered views and the underlying data, and cost nothing to create.
_local = threading.local()


class DataMissingError(RuntimeError):
    """The compiled store is absent — the build script has not been run."""


@lru_cache(maxsize=1)
def _con() -> duckdb.DuckDBPyConnection:
    """A read-only in-process connection with the parquet files registered.

    Views rather than tables: DuckDB reads the Parquet lazily, so startup stays
    ~3 ms regardless of file size and memory is only touched for rows a query
    actually needs.
    """
    obs = DATA_DIR / "ew.parquet"
    if not obs.exists():
        raise DataMissingError(
            f"Compiled data not found at {obs}.\n"
            "Run: uv run --group dev python scripts/build_ew_data.py"
        )
    con = duckdb.connect(database=":memory:")
    con.execute(f"create view obs as select * from read_parquet('{obs}')")
    con.execute(
        f"create view names as select * from read_parquet('{DATA_DIR / 'ew_names.parquet'}')"
    )
    ctx = DATA_DIR / "ew_context.parquet"
    if ctx.exists():
        con.execute(f"create view context as select * from read_parquet('{ctx}')")
    return con


@lru_cache(maxsize=1)
def manifest() -> dict[str, Any]:
    """Provenance: which upstream commit these numbers came from."""
    path = DATA_DIR / "MANIFEST.json"
    if not path.exists():
        raise DataMissingError(f"{path} missing — run scripts/build_ew_data.py")
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def narrative() -> dict[str, dict]:
    """Indicator prose, keyed by slug. Optional by design.

    If permission to redistribute educationtoworkforce.org content were ever
    withdrawn, deleting narrative.json degrades the server to link-outs rather
    than breaking it.
    """
    path = DATA_DIR / "narrative.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _cursor() -> duckdb.DuckDBPyConnection:
    """This thread's cursor, created on first use."""
    cur = getattr(_local, "cur", None)
    if cur is None:
        cur = _con().cursor()
        _local.cur = cur
    return cur


def query(sql: str, params: list | None = None) -> list[tuple]:
    return _cursor().execute(sql, params or []).fetchall()


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def fetch_values(
    metric_ids: list[int],
    geo_level: str,
    geo_ids: list[str] | None = None,
    years: list[int] | None = None,
    disaggregate: str | None = None,
    include_total: bool = True,
    disag_exact: str | None = None,
) -> list[tuple]:
    """Observations as (geo_id, name, metric_id, disag, year, value).

    `disaggregate` is a PREFIX like "d1" (race) — passing it returns every
    category in that dimension. `disag_exact` is ONE code like "d1_hispanic"
    and returns only that group; it wins over the prefix when both are given.
    Keeping them separate matters: the caller who asks for one group used to
    get all eight, because only the prefix ever reached this query.

    `include_total` keeps the disag-is-null rows, which are how "all students"
    is represented upstream (there is no "_total" code).
    """
    where = ["o.metric_id = any(?)", "o.geo_level = ?"]
    params: list[Any] = [metric_ids, geo_level]
    if geo_ids:
        where.append("o.geo_id = any(?)")
        params.append(geo_ids)
    if years:
        where.append("o.year = any(?)")
        params.append(years)

    if disag_exact and include_total:
        where.append("(o.disag is null or o.disag = ?)")
        params.append(disag_exact)
    elif disag_exact:
        where.append("o.disag = ?")
        params.append(disag_exact)
    elif disaggregate and include_total:
        where.append("(o.disag is null or starts_with(o.disag, ?))")
        params.append(disaggregate + "_")
    elif disaggregate:
        where.append("starts_with(o.disag, ?)")
        params.append(disaggregate + "_")
    elif not include_total:
        where.append("o.disag is not null")
    else:
        where.append("o.disag is null")

    return query(
        f"""select o.geo_id, n.name, o.metric_id, o.disag, o.year, o.value
            from obs o left join names n
              on n.geo_level = o.geo_level and n.geo_id = o.geo_id
            where {' and '.join(where)}
            order by o.geo_id, o.metric_id, o.year, o.disag""",
        params,
    )


def places_in_state(geo_level: str, state_fips: str) -> list[str]:
    """Every geoid at `geo_level` inside a state.

    County and district ids are prefixed by their state FIPS, so this needs no
    crosswalk — and it is what makes "all counties in Florida" one call rather
    than 67 ids enumerated by hand.
    """
    return [
        r[0]
        for r in query(
            "select distinct geo_id from obs where geo_level = ? "
            "and starts_with(geo_id, ?) order by geo_id",
            [geo_level, state_fips],
        )
    ]


def context_table(
    geo_level: str, geo_ids: list[str], variables: list[str]
) -> dict[str, dict[str, float]]:
    """Context variables for many places at once, for correlational questions.

    Returns {geo_id: {variable: value}}. Context carries NO year — callers must
    not present it as a time series.
    """
    have = {r[0] for r in query("describe context")}
    wanted = [v for v in variables if v in have]
    if not wanted:
        return {}
    cols = ", ".join(f'"{v}"' for v in wanted)
    rows = query(
        f"select geoid, {cols} from context where geo_level = ? and geoid = any(?)",
        [geo_level, geo_ids],
    )
    return {
        r[0]: {v: r[i + 1] for i, v in enumerate(wanted) if r[i + 1] is not None}
        for r in rows
    }


def rank_places(
    metric_id: int,
    geo_level: str,
    year: int,
    descending: bool = True,
    limit: int = 10,
    disag: str | None = None,
    state_fips: str | None = None,
) -> list[tuple]:
    """Top/bottom N places on one metric.
    """
    where = ["o.metric_id = ?", "o.geo_level = ?", "o.year = ?", "o.value is not null"]
    params: list[Any] = [metric_id, geo_level, year]
    where.append("o.disag = ?" if disag else "o.disag is null")
    if disag:
        params.append(disag)
    if state_fips:
        where.append("starts_with(o.geo_id, ?)")
        params.append(state_fips)
    return query(
        f"""select o.geo_id, n.name, o.value
            from obs o left join names n
              on n.geo_level = o.geo_level and n.geo_id = o.geo_id
            where {' and '.join(where)}
            order by o.value {'desc' if descending else 'asc'}
            limit ?""",
        [*params, limit],
    )


def disag_dimensions(metric_ids: list[int], geo_level: str | None = None) -> dict[int, set[str]]:
    """Which disaggregate dimensions each metric ACTUALLY carries, from data.

    Needed because the metadata declares seven dimensions and the data carries
    at most two for most metrics — `d7` (Income) has no rows anywhere. Without
    this the server answers a request for a breakdown that does not exist by
    silently returning the ungrouped total.
    """
    sql = (
        "select metric_id, substr(disag, 1, 2) from obs "
        "where metric_id = any(?) and disag is not null"
    )
    params: list[Any] = [metric_ids]
    if geo_level:
        sql += " and geo_level = ?"
        params.append(geo_level)
    out: dict[int, set[str]] = {m: set() for m in metric_ids}
    for mid, prefix in query(sql + " group by 1, 2", params):
        out[mid].add(prefix)
    return out


@lru_cache(maxsize=1)
def dimensions_in_data() -> frozenset[str]:
    """Disaggregate prefixes that have at least one row anywhere.

    Distinct from what the metadata declares: d7 (Income) is declared upstream
    and has no rows at all, so "declared" and "queryable" are different sets.
    """
    return frozenset(
        r[0] for r in query("select distinct substr(disag, 1, 2) from obs where disag is not null")
    )


def rank_pool(
    metric_id: int,
    geo_level: str,
    year: int,
    disag: str | None = None,
    state_fips: str | None = None,
) -> tuple[int, int]:
    """(places with a value, places at this level) for a ranking.

    A ranking covers only the places that HAVE a value — missing places are
    absent from it, not ranked low. Coverage runs as thin as 9% of counties for
    some metrics, so a top-10 without this denominator invites reading a
    national extreme into what is a top-10 of a small subset.
    """
    where = ["metric_id = ?", "geo_level = ?", "year = ?", "value is not null"]
    params: list[Any] = [metric_id, geo_level, year]
    where.append("disag = ?" if disag else "disag is null")
    if disag:
        params.append(disag)
    level_where = ["geo_level = ?"]
    level_params: list[Any] = [geo_level]
    if state_fips:
        where.append("starts_with(geo_id, ?)")
        params.append(state_fips)
        level_where.append("starts_with(geo_id, ?)")
        level_params.append(state_fips)
    ranked = query(
        f"select count(distinct geo_id) from obs where {' and '.join(where)}", params
    )[0][0]
    total = query(
        f"select count(distinct geo_id) from obs where {' and '.join(level_where)}",
        level_params,
    )[0][0]
    return ranked, total


def coverage(metric_id: int) -> list[tuple]:
    """Which levels/years a metric actually has, derived from data."""
    return query(
        """select geo_level, count(distinct geo_id), min(year), max(year), count(*)
           from obs where metric_id = ? group by geo_level""",
        [metric_id],
    )


def years_for(metric_id: int, geo_level: str | None = None) -> list[int]:
    sql = "select distinct year from obs where metric_id = ?"
    params: list[Any] = [metric_id]
    if geo_level:
        sql += " and geo_level = ?"
        params.append(geo_level)
    return sorted(r[0] for r in query(sql + " order by year", params))


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------


def find_places(name: str, geo_level: str | None = None, limit: int = 25) -> list[tuple]:
    """Case-insensitive substring search over place names.

    Ranked, and the caller is expected to disambiguate rather than auto-pick:
    "Washington County" exists in ~30 states, and a silently wrong pick is
    indistinguishable from a right one once it reaches a number.
    """
    where = ["lower(name) like ?"]
    params: list[Any] = [f"%{name.lower()}%"]
    if geo_level:
        where.append("geo_level = ?")
        params.append(geo_level)
    return query(
        f"""select geo_level, geo_id, name,
                   case when lower(name) = ? then 0
                        when starts_with(lower(name), ?) then 1
                        else 2 end as rank
            from names where {' and '.join(where)}
            order by rank, length(name), name limit ?""",
        [name.lower(), name.lower(), *params, limit],
    )


def place_name(geo_level: str, geo_id: str) -> str | None:
    rows = query(
        "select name from names where geo_level = ? and geo_id = ?", [geo_level, geo_id]
    )
    return rows[0][0] if rows else None


