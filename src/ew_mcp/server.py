"""MCP server for the Education-to-Workforce Framework Data.

Four tools:

    search        browse essential questions and indicators, or find a metric
                  by concept
    describe      a metric, indicator, essential question, disaggregate — or
                  the framework itself
    resolve_place name -> geoid
    get_data      the numbers

edp_mcp navigates dataset -> variable. This server navigates essential question
-> indicator -> metric, and disaggregates each metric by the framework's
recommended dimensions (race, gender, disability, and so on).
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from ew_mcp import __version__, loader
from ew_mcp import formatting as fmt
from ew_mcp import metadata as md
from ew_mcp.constants import (
    FRAMEWORK_ABOUT_URL,
    FRAMEWORK_CITATION,
    FRAMEWORK_EQ_URL,
    FRAMEWORK_URL,
    GEO_ID_WIDTH,
    GEO_LEVELS,
    MAX_PLACE_CANDIDATES,
    MAX_RESPONSE_CHARS,
    MAX_ROWS_RETURNED,
    TOOL_URL,
)
from ew_mcp.runtime import add_health_route, serve

# Benchmarks are added automatically for a handful of places. 
BENCHMARK_MAX_PLACES = 5

mcp = MCPServer(
    name="Education-to-Workforce Framework Data",
    version=__version__,
    website_url=FRAMEWORK_URL,
    # Policy that applies across tools lives here rather than in each tool's
    # description: this is sent once, a description is re-sent with every tool
    # listing. Descriptions carry only what is specific to their own tool.
    #
    # HARD BUDGET: keep this under ~1,800 characters. MCP clients truncate
    # server instructions — the Claude Code CLI cuts at 2,048 and says nothing,
    # and the Messages API connector appears to drop them entirely, delivering
    # only tool descriptions. Anything that MUST reach the model belongs in a
    # tool description or in the result text, both of which arrive whole. 
    instructions=(
        "You are connected to data compiled against the Education-to-Workforce "
        "(E-W) Indicator Framework, which \"highlights key connections needed "
        "between systems to support students as they progress from early "
        'education through their career" (educationtoworkforce.org). Coverage '
        "here: the US, states, counties and school districts, 2013-2022.\n\n"

        "WHO MADE WHAT — do not merge these. The FRAMEWORK is Mathematica's "
        "(Gonzalez et al., 2022, with the Gates Foundation and Mirror Group): the "
        "essential questions, the indicators, the metric definitions, and all the "
        "narrative prose you will see quoted. The DATA TOOL that compiles federal "
        "data against it is the Urban Institute's. The underlying data is federal "
        "and named per metric. Asked who made the framework, say Mathematica and "
        "the Bill & Melinda Gates Foundation — "
        'and call describe("framework") for the citation and the overview.\n\n'

        "STRUCTURE: 20 essential questions -> indicators -> recommended "
        "metrics; this server has data for 99 indicators' worth. The framework "
        "recommends 26 disaggregates (ways to break a metric down) and this "
        'dataset carries 6. describe("framework") for the overview, '
        'describe("disaggregates") for what is queryable.\n\n'

        "PATH: search -> describe -> resolve_place -> get_data.\n\n"

        "GEOGRAPHY: pass a geoid, not a name — resolve_place turns a name into "
        "candidates. Ids are 2-digit state FIPS, 5-digit county FIPS, and 7-digit "
        "NCES LEAIDs for districts.\n\n"

        "NEVER RESOLVE A PLACE SILENTLY. Many names repeat — there are three Cook "
        "Counties and a Washington County in about 30 states. Name the place and "
        "geoid you used in your answer.\n\n"

        "COVERAGE IS UNEVEN AND THAT IS THE POINT. Many places have no data for a "
        "given metric, and the framework treats 'we cannot answer this here' as a "
        "real finding — report it as one rather than quietly substituting a "
        "different geography or a different grain."
    ),
)

add_health_route(mcp, "ew-mcp")

# Every tool here reads a static local dataset and never writes. openWorldHint is
# False, unlike edp_mcp: nothing leaves the process, so answers depend only on
# the compiled snapshot named in the footer.
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


def _err(msg: str) -> str:
    return f"**Cannot answer that as asked.**\n\n{msg}\n"


def _norm_geo_id(geo_id: str, geo_level: str) -> str:
    """Zero-pad ids. Spreadsheets eat leading zeros; '6' means California."""
    return str(geo_id).strip().zfill(GEO_ID_WIDTH.get(geo_level, 5))


def _check_level(geo_level: str) -> str | None:
    if geo_level not in GEO_LEVELS:
        return _err(
            f"Unknown geo_level {geo_level!r}. Valid: {', '.join(GEO_LEVELS)}.\n\n"
            "This dataset stops at school district — it has no individual schools "
            "or colleges, and census tracts are not loaded."
        )
    return None


def _cap(text: str) -> str:
    """Refuse an oversized response rather than truncating it.

    A cut table is indistinguishable from a complete one once a model starts
    counting or averaging over it, and the server cannot know whether the
    dropped rows mattered. Complete or refused — the same rule the row cap
    enforces.
    """
    if len(text) <= MAX_RESPONSE_CHARS:
        return text
    return _err(
        f"That response would be {len(text):,} characters, over the "
        f"{MAX_RESPONSE_CHARS:,} limit.\n\n"
        "Returning part of it would hand you a partial table that looks "
        "complete, so it is refused instead. Narrow the request: fewer metrics, "
        "fewer places, a single year, or one disaggregate group instead of a "
        "whole dimension."
    )


def _no_breakdown_note(
    metric_id: int, prefix: str | None, exact: str | None, available: set[str]
) -> str:
    """State plainly that a requested breakdown does not exist for this metric.

    Without this, asking for an absent dimension returns a Total-only table
    under a note explaining how to read by-group gaps — a number of a different
    kind from the one asked for, with nothing marking the difference. `d7` has
    no rows anywhere and most metrics carry at most race and gender, so this is
    a common request rather than an edge case.
    """
    asked = (
        f"**{fmt.disag_label(exact)}**"
        if exact
        else md.disag_categories().get(prefix or "", prefix or "that")
    )
    if available:
        names = ", ".join(sorted(md.disag_categories().get(p, p) for p in available))
        where = f"m{metric_id} is broken out by: {names}."
    else:
        where = f"m{metric_id} has no disaggregated data at all."
    return (
        f"> **No {asked} breakdown exists for m{metric_id}.** The Total row below "
        f"is the ungrouped value, not a group value. {where}\n\n"
    )


# describe() aliases that reach the disaggregates breakdown rather than a metric.
_DISAGGREGATE_ALIASES = ("disaggregate", "disaggregates", "breakdowns", "subgroups")


def _describe_disaggregates() -> str:
    """List the framework's recommended disaggregates, marking what this dataset reaches."""
    status = md.framework_disag_status()
    if not status:
        return _err(
            "Framework disaggregate data is not installed. Run "
            "scripts/fetch_ew_narrative.py, or see " + FRAMEWORK_URL
        )
    n_ok = sum(1 for _, p in status if p)
    out = [
        f"# Disaggregates — {len(status)} recommended, {n_ok} queryable here\n\n"
        "The framework recommends breaking data down by these dimensions. This "
        "dataset reaches a few of them, and the difference is a finding about "
        "the data rather than a gap in the framework.\n\n"
    ]
    for name, prefix in status:
        if prefix:
            out.append(f"- ✓ **{name}** — `disaggregate=\"{prefix}\"`\n")
        else:
            out.append(f"- — {name}\n")
    out.append(
        "\n✓ = queryable here. — = recommended by the framework, not carried "
        "by this dataset.\n\nNote that income appears in the metadata as a "
        "band breakdown (`d7`) with no rows anywhere; economically "
        "disadvantaged (`d5`) is the income-related dimension that has data.\n"
    )
    out.append(f"\nFull guidance: {FRAMEWORK_URL}disaggregates\n")
    return _cap("".join(out)) + fmt.format_footer()


def _describe_framework() -> str:
    """What the E-W Framework is, who wrote it, and how to cite it.

    Counts are derived, so this cannot drift from the compiled data.
    """
    n_eq = len(md.essential_questions())
    n_ind = len(md.indicators())
    n_have = len(md.available_metric_ids())
    n_disag_have = len(md.queryable_dimensions())
    return (
        "# The Education-to-Workforce Indicator Framework\n\n"

        'A framework, not a dataset. In the framework\'s own words: "The E-W '
        'Framework offers guidance for using data to promote equitable outcomes '
        'and economic security for all," and it "highlights key connections '
        'needed between systems to support students as they progress from early '
        'education through their career" (educationtoworkforce.org).\n\n'

        "**Who wrote it:** Mathematica, with the Bill & Melinda Gates "
        "Foundation and Mirror Group. It draws on a 15-member external advisory "
        "board of E-W data experts and leaders — state and district "
        "policymakers, researchers, and policy advocates — plus input sessions "
        "with staff and partners from five collective impact organizations.\n\n"

        "**Who built the data:** Urban Institute compiled federal data against "
        "the framework in its E-W Framework Data Tool. That is the source of "
        "every number here.\n\n"

        "**What this server covers:**\n\n"
        f'- **{n_eq} essential questions** — "Questions essential for E-W data '
        'systems to answer about how students are progressing from early '
        'education through career." `search()`\n'
        f'- **{n_ind} indicators**, and their metrics — "Student outcomes and '
        'milestones and related system conditions associated with economic '
        f'mobility and security." **{n_have} metrics have data here.** '
        '`describe("i5")`\n'
        f"- **{n_disag_have} queryable disaggregates** — ways to break a metric "
        'down by group (race, gender, disability, and more). `describe("disaggregates")`\n\n'

        "Coverage is uneven by design, and a question this data cannot answer for "
        "a place is a finding the framework wants surfaced, not hidden.\n\n"

        "## Cite the framework as\n\n"
        f"> {FRAMEWORK_CITATION}\n\n"
        "This is the publisher's own suggested citation, quoted as published.\n\n"

        "## Where to read more\n\n"
        f"- The framework: {FRAMEWORK_URL}\n"
        f"- The 20 essential questions: {FRAMEWORK_EQ_URL}\n"
        f"- About, and the citation above: {FRAMEWORK_ABOUT_URL}\n"
        f"- The data tool, to check any number here: {TOOL_URL}\n"
    )


@mcp.tool(title="Search the framework", annotations=READ_ONLY)
def search(query: str | None = None) -> str:
    """Browse the Education-to-Workforce Framework, or find its metrics by
    concept. Searches metric and indicator names plus the framework's own
    definitions, so conceptual terms match even when a metric is worded
    differently. Matches per-word and ignores word order, so "neighborhood
    poverty" and "poverty in the neighborhood" hit the same records — try a
    short multi-word phrase before assuming something isn't covered.

    NOT COVERED anywhere in this dataset: individual schools, individual
    colleges, and local labour market data. It stops at school district. Say so
    rather than answering from a different grain.

    Args:
        query: A concept — "gifted", "student debt", "chronic absence".
            Omit to list the 20 essential questions, which is the best
            starting point.
    """
    have = md.available_metric_ids()

    if not query or not query.strip():
        out = ["# E-W Framework — the 20 essential questions\n\n"]
        for eq_id, eq in sorted(md.essential_questions().items()):
            mids = md.eq_metric_ids(eq_id)
            n = sum(1 for m in mids if m in have)
            out.append(f"**eq{eq_id}.** {eq['essential_question_name']}\n")
            out.append(f"  - {n} of {len(mids)} metrics have data")
            if s := md.sectors_of(eq):
                out.append(f" | {', '.join(s)}")
            out.append("\n\n")
        out.append(f"\nThese questions on the framework's own site: {FRAMEWORK_EQ_URL}\n")
        out.append(
            '\nNext: describe("eq5") for one question, search("gifted") for a '
            'concept, or describe("framework") for what this is and who wrote it.\n'
        )
        return _cap("".join(out)) + fmt.format_footer()

    hits = md.search(query)
    if not any(hits.values()):
        suggestions = md.suggest_indicators(query)
        tip = (
            "Closest indicator names on file: " + "; ".join(suggestions) + ".\n\n"
            if suggestions
            else ""
        )
        return _err(
            f"Nothing matches {query!r}.\n\n"
            f"{tip}"
            "Try a broader or single-word term, or call search() with no query "
            "to browse the framework."
        )
    out = [f"# Matches for {query!r}\n\n"]

    if hits["essential_questions"]:
        out.append("## Essential questions\n\n")
        for eq_id in hits["essential_questions"]:
            name = md.essential_questions()[eq_id]["essential_question_name"]
            out.append(f"- **eq{eq_id}** {name}\n")
        out.append("\n")

    if hits["indicators"]:
        out.append("## Indicators\n\n")
        for num in hits["indicators"][:25]:
            ind = md.indicators()[num]
            mids = md.indicator_metric_ids(num)
            n = sum(1 for m in mids if m in have)
            out.append(
                f"- **i{num}** {ind['indicator_name']} ({n}/{len(mids)} metrics with data)\n"
            )
        out.append("\n")

    if hits["metrics"]:
        out.append("## Metrics\n\n")
        for mid in hits["metrics"][:40]:
            mark = "" if mid in have else "  *(no data)*"
            out.append(fmt.format_metric_summary(mid) + mark + "\n")
        out.append("\n")

    out.append('Next: describe("m47"), then get_data.\n')
    return _cap("".join(out)) + fmt.format_footer()


@mcp.tool(title="Describe a metric or indicator", annotations=READ_ONLY)
def describe(target: str) -> str:
    """Explain one Education-to-Workforce metric, indicator, or essential
    question, with the years and geographies it actually covers and a link to
    the framework's own page for it.

    Coverage is derived from the data rather than upstream metadata, which
    disagrees with reality for 22 metrics.

    LINK PEOPLE TO THE SOURCE: every metric and indicator described here carries
    the framework's own page for it at educationtoworkforce.org. Pass that link
    on when someone wants the definition, the evidence or the measurement
    guidance rather than a number — it beats paraphrasing, and it is where the
    framework lives.

    Args:
        target: "m47" (metric), "i5" (indicator), "eq12" (essential question);
            a bare number is read as a metric id. Also "framework" for the
            overview and citation, or "disaggregates" for the queryable
            breakdown dimensions.
    """
    t = str(target).strip().lower().replace(" ", "")

    if t in ("framework", "e-w", "ew", "e-wframework", "ewframework"):
        return _describe_framework()

    if t in _DISAGGREGATE_ALIASES:
        return _describe_disaggregates()

    try:
        if t.startswith("eq"):
            kind, num = "eq", int(t[2:])
        elif t.startswith("i"):
            kind, num = "i", int(t[1:])
        elif t.startswith("m"):
            kind, num = "m", int(t[1:])
        else:
            kind, num = "m", int(t)  # bare number: metrics are the common case
    except ValueError:
        return _err(
            f"Could not read {target!r}.\n\n"
            'Pass "m47" (metric), "i5" (indicator), or "eq12" (essential question). '
            "Use search() to find one."
        )

    if kind == "eq":
        eq = md.essential_questions().get(num)
        if not eq:
            return _err(f"No essential question eq{num}. Valid: eq1-eq20.")
        have = md.available_metric_ids()
        out = [f"# eq{num}. {eq['essential_question_name']}\n\n"]
        if s := md.sectors_of(eq):
            out.append(f"Sectors: {', '.join(s)}\n\n")
        out.append("**Indicators:**\n")
        for ind_num in md.eq_indicator_numbers(num):
            ind = md.indicators().get(ind_num)
            if not ind:
                continue
            mids = md.indicator_metric_ids(ind_num)
            n = sum(1 for m in mids if m in have)
            out.append(
                f"- **i{ind_num}** {ind['indicator_name']} ({n}/{len(mids)} with data)\n"
            )
        with_data = [m for m in md.eq_metric_ids(num) if m in have]
        out.append("\n**Metrics with data:**\n")
        for mid in with_data:
            out.append(fmt.format_metric_summary(mid, loader.coverage(mid)) + "\n")
        if not with_data:
            out.append(
                "\n_None. This essential question cannot be answered with this "
                "dataset at any geography — which the framework treats as a finding._\n"
            )
        return _cap("".join(out)) + fmt.format_footer(with_data)

    if kind == "i":
        if num not in md.indicators():
            return _err(f"No indicator i{num}. Valid: i1-i99.")
        cov = {mid: loader.coverage(mid) for mid in md.indicator_metric_ids(num)}
        return _cap(fmt.format_indicator(num, cov)) + fmt.format_footer(
            md.indicator_metric_ids(num)
        )

    if num not in md.metrics() and num not in md.available_metric_ids():
        return _err(f"No metric m{num}. Use search() to find one.")
    return _cap(
        fmt.format_metric(num, loader.coverage(num), loader.years_for(num))
    ) + fmt.format_footer([num])


@mcp.tool(title="Resolve a place name to a geoid", annotations=READ_ONLY)
def resolve_place(name: str, geo_level: str | None = None) -> str:
    """Find the state, county, or school-district geoid used by get_data.

    Returns ranked candidates and does NOT auto-pick: ~30 states have a
    "Washington County", and a silently wrong pick is indistinguishable from a
    right one once it reaches a number. Name the place and geoid you chose in
    your answer. When candidates span geo levels (a county and a district of the
    same name), ASK — they are different grains with different coverage, so the
    wrong pick changes what is answerable, not just the number.

    For every place in a state, skip this and use get_data(state="12").

    Args:
        name: Place name — "Cook County", "Illinois", "Chicago Public Schools".
            District names are official NCES names.
        geo_level: "state", "county", or "district".
    """
    if geo_level and (e := _check_level(geo_level)):
        return e
    rows = loader.find_places(name, geo_level, MAX_PLACE_CANDIDATES + 1)
    if not rows:
        return _err(
            f"No place matches {name!r}"
            + (f" at level {geo_level}" if geo_level else "")
            + ".\n\nDistrict names are official NCES names — try 'City of Chicago "
            "School District 299' rather than 'Chicago'. This dataset has no "
            "individual schools or colleges."
        )
    out = [f"# Places matching {name!r}\n\n| Level | geo_id | Name |\n|---|---|---|\n"]
    for lvl, gid, nm, _rank in rows[:MAX_PLACE_CANDIDATES]:
        out.append(f"| {lvl} | `{gid}` | {nm} |\n")
    if len(rows) > MAX_PLACE_CANDIDATES:
        out.append("\n…more matches exist. Narrow the name or pass geo_level.\n")
    return "".join(out)


@mcp.tool(title="Get metric values", annotations=READ_ONLY)
def get_data(
    metric_ids: str,
    geo_level: str,
    geo_ids: str | None = None,
    state: str | None = None,
    years: str | None = None,
    disaggregate: str | None = None,
    rank: str | None = None,
    limit: int = 10,
) -> str:
    """Fetch Education-to-Workforce metric values for places, or rank places on
    a metric. Choose places one of three ways: `geo_ids` for specific ones,
    `state` for every place at that level in a state, or `rank` for the extremes.

    State and national benchmarks are added automatically for a handful of
    places; demographic context is added for a whole state.

    Coverage is uneven: districts carry ~35 metrics, counties ~62, and many
    places have no data for a given metric. "No data here" is a real finding —
    report it rather than substituting a different geography.

    Every result carries its own reading notes and caveats. Use them: values are
    rendered for you, some metrics are signed representation gaps rather than
    rates, and there are NO margins of error, sample sizes or denominators
    anywhere in this dataset, so never call a difference significant.

    Args:
        metric_ids: Comma-separated ids — "47" or "47,51".
        geo_level: "national", "state", "county", or "district".
        geo_ids: Comma-separated geoids from resolve_place.
        state: 2-digit state FIPS — every place at geo_level in that state.
        years: Comma-separated years — "2022" or "2013,2022". Omit for all.
        disaggregate: A dimension ("race", "gender", "disability", "ell",
            "income") for every group, or one group code ("d1_hispanic").
            Ranking requires a single group code.
        rank: "highest" or "lowest" to rank places instead of listing values.
        limit: How many places to return when ranking.
    """
    if e := _check_level(geo_level):
        return e
    try:
        mids = [int(m.strip()) for m in metric_ids.split(",") if m.strip()]
    except ValueError:
        return _err(f"metric_ids must be numbers; got {metric_ids!r}.")
    if not mids:
        return _err("Pass at least one metric_id.")

    yrs = None
    if years:
        try:
            yrs = [int(y.strip()) for y in years.split(",") if y.strip()]
        except ValueError:
            return _err(f"years must be numbers; got {years!r}.")

    # `disaggregate` takes either a whole dimension ("race" -> every group) or
    # one specific group ("d1_hispanic"). Check the exact code FIRST, since a
    # code also starts with a valid prefix.
    disag_prefix = None
    disag_exact = None
    if disaggregate:
        if disaggregate in md.disag_labels():
            disag_exact = disaggregate
            disag_prefix = disaggregate.split("_")[0]
        else:
            disag_prefix = md.resolve_disaggregate(disaggregate)
            if not disag_prefix:
                # Before calling it unknown, check the framework's own list of
                # 26 recommended disaggregates. "The framework recommends this
                # and the data does not carry it" is a different and far more
                # useful answer than "no such thing".
                if rec := md.match_recommended_disaggregate(disaggregate):
                    live = md.queryable_dimensions()
                    return _err(
                        f"**{rec}** is one of the 26 disaggregates the E-W Framework "
                        "recommends, but this dataset does not carry it.\n\n"
                        f"The framework recommends 26 dimensions; {len(live)} have "
                        "data here: " + ", ".join(live)
                        + ".\n\nThat gap is a real finding about this data — the "
                        "framework treats an unanswerable question as information. "
                        'See describe("disaggregates") for the full list.'
                    )
                return _err(
                    f"Unknown disaggregate {disaggregate!r}.\n\nPass a dimension: "
                    + ", ".join(md.queryable_dimensions())
                    + "\nor one specific group code, e.g. `d1_hispanic`.\n\n"
                    'The framework recommends 26 dimensions in total — see '
                    'describe("disaggregates").'
                )

    # ---- ranking ----------------------------------------------------------
    if rank:
        if rank not in ("highest", "lowest"):
            return _err('rank must be "highest" or "lowest".')
        if len(mids) != 1:
            return _err("Ranking needs exactly one metric_id.")
        available = loader.years_for(mids[0], geo_level)
        if not available:
            return _err(
                f"m{mids[0]} has no data at {geo_level} level. "
                f'Check describe("m{mids[0]}") for where it does exist.'
            )
        year = (yrs or [max(available)])[0]
        if year not in available:
            return _err(
                f"m{mids[0]} has no {geo_level} data for {year}. "
                f"Available: {', '.join(str(y) for y in available)}."
            )
        # Ranking needs ONE series, so a whole dimension is ambiguous. Refusing
        # beats silently ranking the ungrouped total when the caller asked about
        # a group — for gap metrics those mean genuinely different things.
        if disaggregate and not disag_exact:
            groups = [
                f["value"]
                for d in md.disaggregates()
                if f"d{d['prefix']}" == disag_prefix
                for f in d["fields"]
            ]
            return _err(
                f"Ranking needs one specific group, not the whole {disaggregate!r} "
                "dimension.\n\nPass one of: " + ", ".join(f"`{g}`" for g in groups)
            )
        state_fips = _norm_geo_id(state, "state") if state else None
        rows = loader.rank_places(
            mids[0], geo_level, year, descending=(rank == "highest"),
            limit=min(limit, 200), disag=disag_exact, state_fips=state_fips,
        )
        if not rows:
            return _err(f"No {geo_level} data for m{mids[0]} in {year}.")
        # How many places were actually in the running. Without it a top-10
        # drawn from 9% of counties reads as a national extreme.
        pool = loader.rank_pool(
            mids[0], geo_level, year, disag=disag_exact, state_fips=state_fips
        )
        body = fmt.format_ranking(
            rows, mids[0], year, rank == "highest", geo_level, disag_exact, pool=pool
        )
        return _cap(body) + fmt.format_caveats(mids) + fmt.format_footer(mids)

    # ---- values -----------------------------------------------------------
    whole_state = False
    if geo_ids:
        ids = [_norm_geo_id(g, geo_level) for g in geo_ids.split(",") if g.strip()]
    elif state:
        # County and district ids carry their state FIPS as a prefix, so "every
        # county in Florida" needs no crosswalk.
        ids = loader.places_in_state(geo_level, _norm_geo_id(state, "state"))
        whole_state = True
        if not ids:
            return _err(f"No {geo_level} places found in state {state!r}.")
    elif geo_level == "national":
        ids = ["00"]
    else:
        return _err(
            "Choose places: geo_ids= for specific ones (see resolve_place), "
            "state= for a whole state, or rank= to rank them all."
        )

    rows = loader.fetch_values(
        mids, geo_level, ids, yrs, disag_prefix, include_total=True,
        disag_exact=disag_exact,
    )
    if not rows:
        asked = (
            f" for {', '.join(ids)}"
            if len(ids) <= 4
            else f" for any of the {len(ids)} places requested"
        )
        hint = ""
        for mid in mids:
            got = loader.years_for(mid, geo_level)
            hint += f"\n- m{mid} at {geo_level} level has: " + (
                ", ".join(str(y) for y in got) if got else "no data at this level"
            )
        return _err(
            f"No data for {', '.join(f'm{m}' for m in mids)} at {geo_level} level"
            f"{asked}" + (f" in {years}" if years else "") + f".{hint}\n\n"
            "This may be a real finding — the framework treats an unanswerable "
            "question as information — but check the years above first."
        )
    if len(rows) > MAX_ROWS_RETURNED:
        return _err(
            f"That would return {len(rows):,} rows (limit {MAX_ROWS_RETURNED:,}). "
            "Narrow to fewer metrics, places, or years."
        )

    # Whether each metric actually returned grouped rows, rather than whether a
    # breakdown was asked for. These differ whenever the dimension is absent.
    grouped_back = {m: False for m in mids}
    for _gid, _nm, mid, disag, _yr, _val in rows:
        if disag:
            grouped_back[mid] = True

    out = ["# Values\n\n"]
    if whole_state:
        out.append(f"All {len(ids)} {geo_level} places in state {state}.\n\n")
    have_dims = loader.disag_dimensions(mids, geo_level) if disaggregate else {}
    for mid in mids:
        grouped = grouped_back.get(mid, False)
        # The name is unconditional, because value_unit_note returns None for
        # currency, numeric and hundredths — leaving those metrics to render as
        # a bare "m87 | $13,081" with the name appearing nowhere at all.
        line = f"**m{mid}** — {md.metric_name(mid)}."
        if unit := fmt.value_unit_note(mid, disaggregated=grouped):
            line += f" {unit}"
        out.append(line + "\n\n")
        if disaggregate and not grouped:
            out.append(
                _no_breakdown_note(mid, disag_prefix, disag_exact, have_dims.get(mid, set()))
            )
    out.append(fmt.format_values_table(rows))

    if whole_state:
        ctx = loader.context_table(
            geo_level, ids, ["geo_population", "geo_medincome", "geo_pctba"]
        )
        if ctx:
            out.append("\n## Context\n\n")
            out.append(
                "_A single snapshot with no year — not aligned to the years above._\n\n"
            )
            out.append("| Place | Population | Median income | % bachelor's+ |\n")
            out.append("|---|---|---|---|\n")
            for gid in ids:
                c = ctx.get(gid)
                if not c:
                    continue
                nm = loader.place_name(geo_level, gid) or gid
                pop = f"{int(c['geo_population']):,}" if c.get("geo_population") else "—"
                inc = f"${int(c['geo_medincome']):,}" if c.get("geo_medincome") else "—"
                ba = f"{c['geo_pctba'] * 100:.0f}%" if c.get("geo_pctba") is not None else "—"
                out.append(f"| {nm} | {pop} | {inc} | {ba} |\n")

    elif len(ids) <= BENCHMARK_MAX_PLACES and geo_level != "national":
        if geo_level in ("county", "district"):
            st = loader.fetch_values(
                mids, "state", sorted({g[:2] for g in ids}), yrs, disag_prefix,
                disag_exact=disag_exact,
            )
            if st:
                out.append("\n### State benchmark\n\n")
                out.append(fmt.format_values_table(st))
        nat = loader.fetch_values(
            mids, "national", ["00"], yrs, disag_prefix, disag_exact=disag_exact
        )
        if nat:
            out.append("\n### National benchmark\n\n")
            out.append(fmt.format_values_table(nat))

    return _cap("".join(out)) + fmt.format_caveats(mids) + fmt.format_footer(mids)


def main() -> None:
    serve(mcp)


if __name__ == "__main__":
    main()
