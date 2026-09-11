"""Rendering. This is where metric_type correctness lives.

Getting a value's type wrong here produces WRONG answers:

    percent             stored 0-1        -> 0.907 renders "90.7%"
    percent_hundredths  signed, -1.24..8.5 -> -0.115 renders "-11.5 pts"
    currency            0..569,000        -> "$62,027"
    numeric / hundredths                  -> plain

percent_hundredths  are the "…participation relative to share of student body" 
# representation-gap metrics, where the SIGN is the entire finding — 
# whether a student group is over- or under-represented.
Rendering one as a bare proportion, or dropping its sign, inverts the
conclusion. They are always shown signed and always labelled as a gap.
"""

from __future__ import annotations

import re
from html import unescape

from ew_mcp import metadata as md
from ew_mcp.constants import (
    FIVE_YEAR_CAVEAT,
    FRAMEWORK_URL,
    GEO_LABEL,
    MISSINGNESS_CAVEAT,
    TOOL_URL,
)
from ew_mcp.loader import manifest


def strip_html(text: str | None) -> str | None:
    """notes_label carries anchor tags; keep the words, drop the markup."""
    if not text:
        return None
    out = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", unescape(out)).strip() or None


def is_gap(metric_id: int, disag: str | None) -> bool:
    """True when THIS value is a representation gap rather than a level.

    A percent_hundredths metric is not uniformly a gap — the meaning depends on
    the disaggregate:

        disag is null  -> the overall participation RATE (0..1)
        disag is set   -> that group's gap vs its share of the student body (signed)

    Verified against the compiled data: across m83/85/86/104 the ungrouped
    values have ZERO negatives in 47,000-69,000 observations each, while the
    grouped values are half negative with a mean of exactly 0.0000 — which is
    definitionally what a share-difference decomposition sums to.

    This distinction is not published anywhere upstream, and missing it renders
    a 23% participation rate as a "+23.0 pts representation gap", which is a
    different and false claim.
    """
    return (md.metric(metric_id) or {}).get("metric_type") == "percent_hundredths" and (
        disag is not None
    )


def format_value(value: float | None, metric_id: int, disag: str | None = None) -> str:
    if value is None:
        return "—"
    kind = (md.metric(metric_id) or {}).get("metric_type")
    if kind == "percent_hundredths":
        if is_gap(metric_id, disag):
            # Signed, and named as a gap so it is never read as a level.
            return f"{value * 100:+.1f} pts"
        # Ungrouped: a rate, not a gap.
        return f"{value * 100:.1f}%"
    if kind == "percent":
        return f"{value * 100:.1f}%"
    if kind == "currency":
        return f"${value:,.0f}"
    if kind in ("numeric", "hundredths"):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    # No metric_type upstream (the undocumented metrics). Show it raw and let
    # the caveat explain — never guess a unit.
    return f"{value:g}"


def value_unit_note(metric_id: int, disaggregated: bool = True) -> str | None:
    """One line telling the model how to read this metric's numbers."""
    kind = (md.metric(metric_id) or {}).get("metric_type")
    if kind == "percent_hundredths":
        if not disaggregated:
            return (
                "The ungrouped value is an overall participation RATE, not a gap. "
                "Only the by-group values are representation gaps."
            )
        return (
            "By-group values are percentage-point GAPS relative to that group's share "
            "of the student body: positive = over-represented, negative = "
            "under-represented, and the sign is the finding. The ungrouped 'Total' row "
            "is a participation RATE, not a gap — do not compare the two directly."
        )
    if kind == "percent":
        return "Values are percentages of the relevant population."
    if kind is None:
        return "Unit unknown — no metric_type is published upstream for this metric."
    return None


def disag_label(code: str | None) -> str:
    """Label for a disaggregate code; None means the ungrouped total.

    "Total" rather than "All students": these metrics are not all about
    students — m47 is household broadband access, m166 is food security — and
    calling a population total "all students" silently misdescribes the
    denominator.
    """
    if not code:
        return "Total"
    return md.disag_labels().get(code, code)


def _bullet(label: str, text: str | None) -> str:
    return f"**{label}:** {text}\n\n" if text else ""


# ---------------------------------------------------------------------------
# Metrics and indicators
# ---------------------------------------------------------------------------


def format_metric_summary(metric_id: int, coverage: list[tuple] | None = None) -> str:
    """One-line-ish summary used in search results and lists."""
    name = md.metric_name(metric_id)
    m = md.metric(metric_id) or {}
    bits = [f"- **m{metric_id}** — {name}"]
    facts = []
    if m.get("metric_type"):
        facts.append(m["metric_type"])
    if (label := m.get("source_label")) and str(label).strip():
        facts.append(f"source: {str(label).strip()}")
    if coverage:
        levels = ", ".join(sorted({c[0] for c in coverage}))
        yrs = [c[2] for c in coverage] + [c[3] for c in coverage]
        facts.append(f"{min(yrs)}–{max(yrs)} at {levels}")
    if facts:
        bits.append(f"  ({'; '.join(facts)})")
    return "".join(bits)


def format_metric(metric_id: int, coverage: list[tuple], years: list[int]) -> str:
    """Full description of one metric: what it is, where it exists, caveats."""
    m = md.metric(metric_id) or {}
    name = md.metric_name(metric_id)
    out = [f"# m{metric_id} — {name}\n"]

    if note := md.metric_note(metric_id):
        out.append(f"> **{note}**\n")
    if m.get("_duplicate_ids"):
        out.append(
            f"> **Upstream defect:** metric_id {metric_id} is used for more than one metric "
            f"upstream (also: {'; '.join(str(x) for x in m['_duplicate_ids'])}). "
            "Reported as-is; not corrected here.\n"
        )

    out.append(_bullet("Type", m.get("metric_type") or "not published upstream"))
    if unit := value_unit_note(metric_id):
        out.append(f"{unit}\n\n")
    out.append(_bullet("Source", m.get("source_label")))
    if m.get("five_year"):
        out.append(f"> {FIVE_YEAR_CAVEAT}\n\n")

    # Coverage is DERIVED from data. metrics.json.years_available disagreed with
    # reality for 22 metrics in this build, so it is never reported as fact.
    if coverage:
        out.append("**Coverage (derived from the data, not from metadata):**\n\n")
        out.append("| Level | Places | Years | Observations |\n|---|---|---|---|\n")
        for lvl, places, y0, y1, n in sorted(coverage):
            span = f"{y0}" if y0 == y1 else f"{y0}–{y1}"
            out.append(f"| {lvl} | {places:,} | {span} | {n:,} |\n")
        out.append("\n")
        if years:
            out.append(f"Years present: {', '.join(str(y) for y in years)}\n\n")
    else:
        out.append("**No data is present for this metric at any level.**\n\n")

    if claimed := m.get("years_available"):
        claimed_set = {int(y) for y in claimed}
        if years and claimed_set != set(years):
            out.append(
                f"> Note: upstream metadata claims years {sorted(claimed_set)}, which "
                f"disagrees with the data above. The data is authoritative here.\n\n"
            )

    if disags := m.get("disag_available"):
        if disags != "FALSE":
            names = [
                md.disag_categories().get(f"d{d}", f"d{d}")
                for d in (disags if isinstance(disags, list) else [disags])
            ]
            out.append(_bullet("Disaggregates", ", ".join(names)))

    ind = md.metric_indicator(metric_id)
    if ind:
        out.append(
            _bullet(
                "Indicator",
                f"#{ind['indicator_number']} {ind['indicator_name']}"
                + (f" — domain: {md.domain_name(ind.get('domain'))}" if ind.get("domain") else ""),
            )
        )
    out.append(format_narrative(md.narrative_for_metric(metric_id)))
    return "".join(out)


def format_narrative(page: dict | None) -> str:
    """Render the E-W Framework's own prose for an indicator.

    Attribution names Mathematica specifically: this is Mathematica's writing,
    not Urban's and not ours, and a bare URL does not say so.
    """
    if not page:
        return ""
    out = ["\n## What the E-W Framework says\n\n"]
    out.append(_bullet("Definition", page.get("definition")))
    out.append(_bullet("Why it matters", page.get("why_it_matters")))
    out.append(_bullet("What to know about measurement", page.get("measurement")))
    out.append(_bullet("Recommended metrics", page.get("recommended_metrics")))
    out.append(_bullet("Types of data needed", page.get("data_types")))
    tax = []
    for key in ("sectors", "type", "domain", "clusters"):
        if page.get(key):
            tax.append(f"{key}: {', '.join(page[key])}")
    if tax:
        out.append(_bullet("Framework placement", " | ".join(tax)))
    if eqs := page.get("related_essential_questions"):
        out.append("**Related essential questions:**\n")
        out.extend(f"- {q}\n" for q in eqs)
        out.append("\n")
    if cites := page.get("citations"):
        out.append(f"**Sources cited by the Framework ({len(cites)}):**\n")
        out.extend(f"- {c}\n" for c in cites[:8])
        if len(cites) > 8:
            out.append(f"- …and {len(cites) - 8} more at {page['url']}\n")
        out.append("\n")
    out.append(f"Source: Education-to-Workforce Indicator Framework — Mathematica. {page['url']}\n")
    return "".join(out)


def format_indicator(number: int, metric_coverage: dict[int, list[tuple]]) -> str:
    ind = md.indicators().get(number)
    if not ind:
        return f"No indicator #{number}."
    out = [f"# Indicator {number} — {ind['indicator_name']}\n\n"]
    facts = []
    if s := md.sectors_of(ind):
        facts.append(f"sectors: {', '.join(s)}")
    if d := md.domain_name(ind.get("domain")):
        facts.append(f"domain: {d}")
    if t := md.type_name(ind.get("type")):
        facts.append(f"type: {t}")
    if facts:
        out.append(" | ".join(facts) + "\n\n")

    ids = md.indicator_metric_ids(number)
    out.append(f"**Metrics ({len(ids)}):**\n")
    for mid in ids:
        cov = metric_coverage.get(mid) or []
        marker = "" if cov else "  — *no data in this dataset*"
        out.append(format_metric_summary(mid, cov) + marker + "\n")
    out.append("\n")
    out.append(format_narrative(md.narrative_for_indicator(number)))
    return "".join(out)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def format_values_table(rows: list[tuple], show_place: bool = True) -> str:
    """rows: (geo_id, name, metric_id, disag, year, value)."""
    if not rows:
        return "No matching observations.\n"
    header = ("| Place " if show_place else "| ") + "| Metric | Group | Year | Value |\n"
    sep = ("|---" * (5 if show_place else 4)) + "|\n"
    out = [header, sep]
    for geo_id, name, mid, disag, year, value in rows:
        place = f"| {name or geo_id} " if show_place else "| "
        out.append(
            f"{place}| m{mid} | {disag_label(disag)} | {year} | "
            f"{format_value(value, mid, disag)} |\n"
        )
    return "".join(out)


def format_ranking(
    rows: list[tuple],
    metric_id: int,
    year: int,
    descending: bool,
    geo_level: str,
    disag: str | None = None,
    pool: tuple[int, int] | None = None,
) -> str:
    """Top/bottom N, with the size of the pool they were drawn from.

    `pool` is (places ranked, places at this level), and it has to travel with
    the table. Only places holding a value are ranked, and for some metrics that
    is 9% of counties — so a top-10 is a top-10 of 276, which without the
    denominator reads as a national extreme.
    """
    direction = "Highest" if descending else "Lowest"
    noun = GEO_LABEL.get(geo_level, geo_level).lower()
    noun = "counties" if noun == "county" else f"{noun}s"
    out = [
        f"# {direction} {len(rows)} {noun} — "
        f"m{metric_id} {md.metric_name(metric_id)}, {year}\n\n"
    ]
    if unit := value_unit_note(metric_id, disaggregated=disag is not None):
        out.append(f"{unit}\n\n")
    if disag:
        out.append(f"Group: **{disag_label(disag)}**\n\n")
    out.append("| # | Place | Value |\n|---|---|---|\n")
    for i, (geo_id, name, value) in enumerate(rows, 1):
        out.append(f"| {i} | {name or geo_id} | {format_value(value, metric_id, disag)} |\n")
    if pool:
        ranked, total = pool
        out.append(
            f"\nRanked over **{ranked:,} of {total:,} {noun}** — every place with a "
            f"value for m{metric_id} in {year}, ordered exactly."
        )
        if ranked < total:
            missing = total - ranked
            out.append(
                f" The other {missing:,} have no value for this metric and year and "
                "are absent from the ranking; that is missing data, not a low score."
            )
        out.append("\n")
    return "".join(out)


def format_caveats(metric_ids: list[int]) -> str:
    """Interpretation warnings that must ride along with results.

    These sit with the DATA, not only in describe_*, because a model that skips
    describe would otherwise present point estimates as precise. This dataset
    has no MOEs, no Ns and no suppression flags anywhere.
    """
    out = [f"\n> {MISSINGNESS_CAVEAT}\n"]
    if any((md.metric(m) or {}).get("five_year") for m in metric_ids):
        out.append(f"> {FIVE_YEAR_CAVEAT}\n")
    for mid in metric_ids:
        if note := md.metric_note(mid):
            out.append(f"> m{mid}: {note}\n")
    return "".join(out)


def format_footer(metric_ids: list[int] | None = None) -> str:
    """Provenance. Every number is traceable to the pinned upstream commit.

    Three separate credits, because they are three separate organisations: the
    framework is Mathematica's, the data tool is Urban's, and the underlying
    data is federal. All three have to appear — naming only the data tool and
    the federal source credits Urban for work it did not write.

    The tool URL matters as much as the citation: a user who checks the answer
    against apps.urban.org must see the same number, which is the whole defence
    against answering plausibly but wrongly under a trusted name.
    """
    mf = manifest()
    # `.strip()`: three metrics carry a present-but-blank source_label upstream,
    # which rendered as a bare "; " in this line.
    sources: list[str] = sorted(
        {
            str(label).strip()
            for m in (metric_ids or [])
            if (label := (md.metric(m) or {}).get("source_label")) and str(label).strip()
        }
    )
    lines = ["\n─────\n"]
    if sources:
        lines.append(f"Underlying source(s): {'; '.join(sources)}\n")
    lines.append(
        "Framework: Education-to-Workforce Indicator Framework — Mathematica "
        f"and the Bill & Melinda Gates Foundation · {FRAMEWORK_URL}\n"
    )
    lines.append(f"Data tool: Urban Institute · verify at {TOOL_URL}\n")
    # The version marker rides on the snapshot line rather than taking a
    # paragraph of its own: it is a fact about which build this is, and every
    # response carries this footer.
    lines.append(
        f"Snapshot: {mf['upstream_repo']}@{mf['upstream_commit'][:8]} "
        f"(built {mf['built_at'][:10]})\n"
    )
    lines.append('Full citation and framework overview: describe("framework")\n')
    return "".join(lines)
