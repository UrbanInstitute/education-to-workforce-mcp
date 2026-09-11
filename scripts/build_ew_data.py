#!/usr/bin/env python
"""Compile the pinned upstream E-W data into the server's query store.

    uv run --group dev python scripts/build_ew_data.py [--cache DIR]

What this does, and the one rule it follows:

    COMPILE, DON'T TRANSFORM.

The upstream JSON is reshaped losslessly into columnar form and nothing else.
No imputation, no derived metrics, no merging of data cuts, no correcting of
upstream values. Anything questionable is written to a validation report rather
than silently repaired, so that every number the server emits is traceable to
UPSTREAM_COMMIT.

Outputs (into src/ew_mcp/data/):
    ew.parquet          observations, sorted   (11.8M rows, ~27 MB)
    ew_names.parquet    geoid -> name          (14,090 places)
    ew_context.parquet  demographics, NO year dimension
    metadata/*.json     framework structure, verbatim upstream
    MANIFEST.json       provenance + row counts + the validation report

Why Parquet+DuckDB rather than SQLite/DuckDB-native: benchmarked all four.
SQLite was 1 GB and took 919 ms on a scan Parquet does in 11 ms; DuckDB's
native format was 370 MB for a gain nobody perceives. See docs/EW_PLAN.md §4.2.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ew_mcp.constants import (  # noqa: E402
    UPSTREAM_COMMIT,
    UPSTREAM_RAW,
    UPSTREAM_REPO,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "src" / "ew_mcp" / "data"

# Metadata files are vendored VERBATIM so that a refresh is a readable diff.
# Note state-lookup.json sits one level up from the rest, at src/data/.
METADATA_FILES = {
    "metrics": "src/data/metadata/metrics.json",
    "indicators": "src/data/metadata/indicators.json",
    "essential-questions": "src/data/metadata/essential-questions.json",
    "disaggregates": "src/data/metadata/disaggregates.json",
    "context": "src/data/metadata/context.json",
    "state-lookup": "src/data/state-lookup.json",
}

# 51 states + DC per directory upstream (no territories at these levels).
STATE_FIPS = [
    "01", "02", "04", "05", "06", "08", "09", "10", "11", "12", "13", "15",
    "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27",
    "28", "29", "30", "31", "32", "33", "34", "35", "36", "37", "38", "39",
    "40", "41", "42", "44", "45", "46", "47", "48", "49", "50", "51", "53",
    "54", "55", "56",
]


def fetch(url: str, cache: Path) -> bytes:
    """GET with an on-disk cache keyed by URL.

    The cache exists so a rebuild costs seconds rather than re-downloading
    178 MB, and so a build is reproducible offline once primed.
    """
    key = hashlib.sha256(url.encode()).hexdigest()[:24]
    hit = cache / key
    if hit.exists():
        return hit.read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": "UrbanInstitute-EW-MCP/0.1"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 - pinned https host
        body = r.read()
    cache.mkdir(parents=True, exist_ok=True)
    hit.write_bytes(body)
    return body


def fetch_json(url: str, cache: Path):
    return json.loads(fetch(url, cache))


def collect(cache: Path) -> tuple[list[tuple], dict, list[dict], list[str]]:
    """Download every source file and flatten to observation tuples."""
    warnings: list[str] = []
    obs: list[tuple] = []
    names: dict[tuple[str, str], str] = {}

    def add(level: str, blob: dict) -> None:
        for gid, rec in blob.items():
            if not isinstance(rec, dict):
                warnings.append(f"{level}/{gid}: record is not an object; skipped")
                continue
            if rec.get("name"):
                names[(level, gid)] = rec["name"]
            data = rec.get("data")
            if not data:
                # Real and expected: e.g. CA district 0699997 has a name but no
                # data. Recorded rather than silently dropped.
                warnings.append(f"{level}/{gid}: no data block")
                continue
            for key, series in data.items():
                base, _, disag = key.partition("_")
                if not base.startswith("m") or not base[1:].isdigit():
                    warnings.append(f"{level}/{gid}: unparseable metric key {key!r}")
                    continue
                mid = int(base[1:])
                for year, value in series.items():
                    if value is None:
                        continue
                    obs.append((level, gid, mid, disag or None, int(year), float(value)))

    print("  national + states…", flush=True)
    add("national", fetch_json(f"{UPSTREAM_RAW}/src/data/metrics/national.json", cache))
    add("state", fetch_json(f"{UPSTREAM_RAW}/src/data/metrics/states.json", cache))

    for level, folder in (("county", "counties"), ("district", "school_districts")):
        print(f"  {folder}… ", end="", flush=True)
        urls = [
            f"{UPSTREAM_RAW}/static/data/metrics/{folder}/{folder}_{fips}.json"
            for fips in STATE_FIPS
        ]
        with ThreadPoolExecutor(max_workers=8) as pool:
            blobs = list(pool.map(lambda u: fetch_json(u, cache), urls))
        for blob in blobs:
            add(level, blob)
        print(f"{len(blobs)} files", flush=True)

    # Authoritative names come from the metadata endpoints (they carry the
    # state suffix, e.g. "Autauga County, Alabama"); the per-metric files carry
    # only a bare name. Applied second so they win.
    print("  place names…", flush=True)
    for level, stem in (("state", "states"), ("county", "counties"),
                        ("district", "school_districts")):
        for row in fetch_json(f"{UPSTREAM_RAW}/static/data/metadata/{stem}.json", cache):
            names[(level, row["geoid"])] = row["name"]
    names[("national", "00")] = "United States"

    # Context: flat records, NO year dimension — a single vintage snapshot.
    # Kept as its own table for exactly that reason; forcing it into the
    # observations table with a sentinel year would invite "population in 2013"
    # answers that do not exist.
    print("  context…", flush=True)
    context: list[dict] = []
    for row in fetch_json(f"{UPSTREAM_RAW}/static/data/context/states.json", cache):
        context.append({"geo_level": "state", **row})
    for fips in STATE_FIPS:
        try:
            rows = fetch_json(f"{UPSTREAM_RAW}/static/data/context/counties/{fips}.json", cache)
        except Exception as exc:  # noqa: BLE001 - recorded, not fatal
            warnings.append(f"context/counties/{fips}: {exc}")
            continue
        for row in rows:
            context.append({"geo_level": "county", **row})

    return obs, names, context, warnings


def validate(obs: list[tuple], metadata: dict) -> dict:
    """Compare metadata claims against what the data actually contains.

    Everything here is REPORTED, never repaired. See constants.py for why.
    """
    metrics = metadata["metrics"]
    ids = Counter(m["metric_id"] for m in metrics)
    by_id = {m["metric_id"]: m for m in metrics}
    in_data = {o[2] for o in obs}
    in_tool = {m["metric_id"] for m in metrics if m.get("in_tool")}

    disag_in_data = {o[3] for o in obs if o[3]}
    disag_in_meta = {f["value"] for d in metadata["disaggregates"] for f in d["fields"]}

    # years_available is NOT trustworthy (it disagreed with the CSV export for
    # 18 of 60 metrics), so the server derives coverage from data. This records
    # the disagreement rather than acting on it.
    derived: dict[int, set[int]] = {}
    for _, _, mid, _, year, _ in obs:
        derived.setdefault(mid, set()).add(year)
    year_mismatch = []
    for mid, years in sorted(derived.items()):
        claimed = by_id.get(mid, {}).get("years_available") or []
        claimed_set = {int(y) for y in claimed}
        if claimed_set and claimed_set != years:
            year_mismatch.append(
                {"metric_id": mid, "claimed": sorted(claimed_set), "actual": sorted(years)}
            )

    return {
        "duplicate_metric_ids": {str(k): v for k, v in ids.items() if v > 1},
        "in_data_no_metadata": sorted(in_data - set(by_id)),
        "in_tool_no_data": sorted(in_tool - in_data),
        "in_tool_missing_metric_type": sorted(
            m["metric_id"] for m in metrics if m.get("in_tool") and not m.get("metric_type")
        ),
        "in_tool_missing_source_label": sorted(
            m["metric_id"] for m in metrics if m.get("in_tool") and not m.get("source_label")
        ),
        "disag_in_data_not_in_metadata": sorted(disag_in_data - disag_in_meta),
        "years_available_mismatch_count": len(year_mismatch),
        "years_available_mismatch": year_mismatch[:20],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cache", default=str(ROOT / ".ew_cache"), help="download cache dir")
    args = ap.parse_args()
    cache = Path(args.cache)

    import pyarrow as pa
    import pyarrow.parquet as pq

    t0 = time.time()
    print(f"Building from {UPSTREAM_REPO}@{UPSTREAM_COMMIT[:8]}")

    print("Downloading…")
    obs, names, context, warnings = collect(cache)
    print(f"  {len(obs):,} observations, {len(names):,} places")

    print("Vendoring metadata…")
    (OUT / "metadata").mkdir(parents=True, exist_ok=True)
    metadata = {}
    for stem, rel in METADATA_FILES.items():
        raw = fetch(f"{UPSTREAM_RAW}/{rel}", cache)
        (OUT / "metadata" / f"{stem}.json").write_bytes(raw)
        metadata[stem] = json.loads(raw)

    print("Validating (reporting only — upstream defects are not repaired)…")
    report = validate(obs, metadata)
    for k, v in report.items():
        if v and k != "years_available_mismatch":
            print(f"  {k}: {v if not isinstance(v, list) else v[:12]}")

    print("Writing Parquet…")
    OUT.mkdir(parents=True, exist_ok=True)
    # Sorted by (geo_level, geo_id, metric_id, year): row-group statistics then
    # let DuckDB skip nearly the whole file for place-centric queries, which are
    # the common case here. Measured 2-4x vs unsorted, for 8.5 MB. Sorting
    # metric-major instead would make ranking 3x faster and profiles 8x slower.
    obs.sort(key=lambda r: (r[0], r[1], r[2], r[4]))
    cols = list(zip(*obs, strict=True))
    table = pa.table(
        {
            "geo_level": pa.array(cols[0]).dictionary_encode(),
            "geo_id": pa.array(cols[1]).dictionary_encode(),
            "metric_id": pa.array(cols[2], pa.int16()),
            "disag": pa.array(cols[3]).dictionary_encode(),
            "year": pa.array(cols[4], pa.int16()),
            "value": pa.array(cols[5], pa.float32()),
        }
    )
    # row_group_size matters as much as the sort. pyarrow's default puts ~987K
    # rows in a group, which is too coarse for statistics to prune usefully.
    # Measured on the real artifact: 100K groups take a place profile from
    # 5.6ms -> 1.9ms and a time series from 5.4ms -> 2.2ms, costing 3.9 MB.
    pq.write_table(
        table,
        OUT / "ew.parquet",
        compression="zstd",
        compression_level=9,
        row_group_size=100_000,
    )

    name_rows = sorted(names.items())
    pq.write_table(
        pa.table(
            {
                "geo_level": pa.array([k[0] for k, _ in name_rows]).dictionary_encode(),
                "geo_id": pa.array([k[1] for k, _ in name_rows]),
                "name": pa.array([v for _, v in name_rows]),
            }
        ),
        OUT / "ew_names.parquet",
        compression="zstd",
    )

    ctx_keys = sorted({k for row in context for k in row})
    pq.write_table(
        pa.table({k: pa.array([row.get(k) for row in context]) for k in ctx_keys}),
        OUT / "ew_context.parquet",
        compression="zstd",
    )

    manifest = {
        "upstream_repo": UPSTREAM_REPO,
        "upstream_commit": UPSTREAM_COMMIT,
        "upstream_url": f"https://github.com/{UPSTREAM_REPO}/tree/{UPSTREAM_COMMIT}",
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "rows": {
            "observations": len(obs),
            "places": len(names),
            "context": len(context),
        },
        "bytes": {
            p.name: p.stat().st_size
            for p in sorted(OUT.glob("*.parquet"))
        },
        "coverage": {
            lvl: {
                "observations": sum(1 for o in obs if o[0] == lvl),
                "places": sum(1 for k in names if k[0] == lvl),
                "metrics": len({o[2] for o in obs if o[0] == lvl}),
            }
            for lvl in ("national", "state", "county", "district")
        },
        "validation": report,
        "collection_warnings": warnings[:50],
        "collection_warning_count": len(warnings),
    }
    (OUT / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n")

    total = sum(p.stat().st_size for p in OUT.rglob("*") if p.is_file())
    print(f"\nDone in {time.time() - t0:.0f}s — {total / 1e6:.1f} MB in {OUT.relative_to(ROOT)}")
    for p in sorted(OUT.glob("*.parquet")):
        print(f"  {p.name:22s} {p.stat().st_size / 1e6:7.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
