"""Tests for the EW MCP server.

These run against the REAL compiled store rather than mocks. That is a
deliberate departure from the edp_mcp tests, which mock HTTP because their
upstream is a live API. Here the data IS the artifact we ship, so a test
against a mock would verify nothing about what users get. The whole store is
~24 MB and every query is single-digit milliseconds, so there is no cost to it.

If the store is missing, the suite skips rather than fails — a fresh clone
should not report red before scripts/build_ew_data.py has run.
"""

from __future__ import annotations

import re

import pytest

from ew_mcp import formatting as fmt
from ew_mcp import loader
from ew_mcp import metadata as md
from ew_mcp import server as srv
from ew_mcp.constants import (
    FRAMEWORK_ABOUT_URL,
    FRAMEWORK_CITATION,
    FRAMEWORK_EQ_URL,
    TOOL_URL,
    UNDOCUMENTED_METRICS,
)

pytestmark = pytest.mark.skipif(
    not (loader.DATA_DIR / "ew.parquet").exists(),
    reason="compiled store absent; run scripts/build_ew_data.py",
)


# ---------------------------------------------------------------------------
# Value rendering — the highest-consequence code in the server
# ---------------------------------------------------------------------------


def test_percent_renders_as_percentage_not_proportion():
    # m47 is stored 0-1. Rendering 0.907 as "0.9%" or "0.907" would be wrong
    # by two orders of magnitude.
    assert fmt.format_value(0.907, 47) == "90.7%"


@pytest.mark.parametrize("metric_id", [83, 85, 86, 104])
def test_gap_metrics_always_keep_their_sign(metric_id):
    """The sign IS the finding for representation-gap metrics.

    A negative value means a group is UNDER-represented. Dropping or flipping
    the sign inverts an equity conclusion, which is the worst failure this
    server could have.
    """
    assert md.metric(metric_id)["metric_type"] == "percent_hundredths"
    assert fmt.format_value(-0.1517, metric_id, "d1_hispanic").startswith("-")
    assert fmt.format_value(0.2278, metric_id, "d1_white").startswith("+")
    note = fmt.value_unit_note(metric_id)
    assert "under-represented" in note and "over-represented" in note


def test_gap_metric_is_not_rendered_as_a_plain_percentage():
    # Guards against someone "simplifying" percent_hundredths into percent.
    assert fmt.format_value(-0.1517, 86, "d1_white") != fmt.format_value(-0.1517, 47)
    assert "pts" in fmt.format_value(-0.1517, 86, "d1_white")


@pytest.mark.parametrize("metric_id", [83, 85, 86, 104])
def test_ungrouped_value_of_a_gap_metric_is_a_rate_not_a_gap(metric_id):
    """The meaning of percent_hundredths depends on the disaggregate.

    Ungrouped  -> overall participation RATE (0..1, never negative)
    By group   -> that group's gap vs its share of the student body (signed)

    Established from the data itself, since it is documented nowhere upstream:
    the ungrouped series has ZERO negatives across tens of thousands of
    observations, while the grouped series is ~half negative with mean 0.0000.
    Rendering the ungrouped value as "+23.0 pts" would claim a 23-point
    representation gap where the truth is a 23% participation rate.
    """
    neg_ungrouped = loader.query(
        "select count(*) from obs where metric_id=? and disag is null and value<0",
        [metric_id],
    )[0][0]
    assert neg_ungrouped == 0

    mean_grouped = loader.query(
        "select avg(value) from obs where metric_id=? and disag is not null", [metric_id]
    )[0][0]
    assert abs(mean_grouped) < 0.01, "grouped gaps should centre on zero"

    assert not fmt.is_gap(metric_id, None)
    assert fmt.is_gap(metric_id, "d1_black")
    assert fmt.format_value(0.2298, metric_id, None) == "23.0%"
    assert fmt.format_value(0.2298, metric_id, "d1_black") == "+23.0 pts"


def test_gap_metric_note_distinguishes_total_from_groups():
    note = fmt.value_unit_note(86, disaggregated=True)
    assert "RATE" in note and "not a gap" in note


def test_currency_and_numeric_do_not_get_percent_treatment():
    assert fmt.format_value(62027, 87).startswith("$")
    assert "%" not in fmt.format_value(32.03, 169)


def test_undocumented_metric_renders_raw_and_says_so():
    mid = UNDOCUMENTED_METRICS[0]
    assert md.metric(mid) is None
    assert "%" not in fmt.format_value(0.5, mid)  # never guess a unit
    assert "unknown" in (fmt.value_unit_note(mid) or "").lower()
    assert md.metric_note(mid) is not None


def test_missing_value_is_not_zero():
    assert fmt.format_value(None, 47) == "—"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_null_disaggregate_is_total_not_all_students():
    # Not every metric is about students (m47 is household broadband).
    assert fmt.disag_label(None) == "Total"


def test_upstream_disaggregate_typo_is_patched_at_display_layer():
    """Data says d3_nodisab; disaggregates.json says d3_nodsab.

    Unpatched this renders a real category as a bare code.
    """
    label = fmt.disag_label("d3_nodisab")
    assert label != "d3_nodisab"
    assert "disabilit" in label.lower()
    # ...and the raw stored value is untouched.
    codes = {r[0] for r in loader.query("select distinct disag from obs where disag is not null")}
    assert "d3_nodisab" in codes


# ---------------------------------------------------------------------------
# Upstream defects are surfaced, not hidden
# ---------------------------------------------------------------------------


def test_duplicate_metric_id_is_recorded_not_silently_dropped():
    m = md.metric(222)
    assert m is not None
    assert m.get("_duplicate_ids"), "the second m222 record should be recorded"


def test_validation_report_captures_known_defects():
    v = md.all_metadata_flags()
    assert v["duplicate_metric_ids"] == {"222": 2}
    assert set(UNDOCUMENTED_METRICS).issubset(set(v["in_data_no_metadata"]))
    assert "d3_nodisab" in v["disag_in_data_not_in_metadata"]


# ---------------------------------------------------------------------------
# Coverage is derived from data, never from metadata claims
# ---------------------------------------------------------------------------


def test_coverage_comes_from_data_not_years_available():
    """metrics.json.years_available is unreliable — 22 metrics disagree."""
    v = md.all_metadata_flags()
    assert v["years_available_mismatch_count"] > 0
    bad = v["years_available_mismatch"][0]
    derived = loader.years_for(bad["metric_id"])
    assert derived == bad["actual"] != bad["claimed"]


def test_metric_with_no_data_reports_absence_rather_than_inventing():
    # m222 is in_tool upstream but has no observations anywhere.
    assert loader.coverage(222) == []
    body = fmt.format_metric(222, [], [])
    assert "No data is present" in body


# ---------------------------------------------------------------------------
# Ranking — exact by construction
# ---------------------------------------------------------------------------


def test_ranking_is_ordered_by_value_not_by_id():
    """The bug edp_mcp shipped: first-N-by-id presented as top-N-by-metric."""
    rows = loader.rank_places(47, "county", 2022, descending=True, limit=25)
    values = [r[2] for r in rows]
    assert values == sorted(values, reverse=True)
    ids = [r[0] for r in rows]
    assert ids != sorted(ids), "ranked output must not be in geo_id order"


def test_ranking_direction_is_honoured():
    hi = loader.rank_places(47, "county", 2022, descending=True, limit=5)
    lo = loader.rank_places(47, "county", 2022, descending=False, limit=5)
    assert hi[0][2] > lo[0][2]


def test_ranking_covers_the_whole_level_not_a_page():
    n_ranked = len(loader.rank_places(47, "county", 2022, limit=100_000))
    n_total = loader.query(
        "select count(*) from obs where metric_id=47 and geo_level='county' "
        "and year=2022 and disag is null and value is not null"
    )[0][0]
    assert n_ranked == n_total


# ---------------------------------------------------------------------------
# Places
# ---------------------------------------------------------------------------


def test_ambiguous_place_returns_all_candidates():
    rows = loader.find_places("Cook County", "county")
    ids = {r[1] for r in rows}
    assert {"17031", "27031"}.issubset(ids), "must not auto-pick one Cook County"


def test_exact_match_ranks_above_substring():
    rows = loader.find_places("Illinois", "state")
    assert rows[0][2] == "Illinois"


def test_district_ids_are_seven_digit_nces_leaids():
    rows = loader.query("select distinct geo_id from obs where geo_level='district' limit 50")
    assert all(len(r[0]) == 7 and r[0].isdigit() for r in rows)


def test_geo_id_zero_padding():
    # "6" is California; an unpadded id from a spreadsheet must still resolve.
    assert srv._norm_geo_id("6", "state") == "06"
    assert srv._norm_geo_id("17031", "county") == "17031"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_get_data_returns_values_with_provenance():
    out = srv.get_data(
        metric_ids="47", geo_level="county", geo_ids="17031", years="2022"
    )
    assert "Cook County, Illinois" in out
    assert "90.7%" in out
    assert "eaaa0a29" in out, "must cite the pinned upstream commit"
    assert "apps.urban.org" in out, "must offer a way to verify"


def test_get_data_caveats_ride_with_results():
    """A model that skips describe_metric must still see the caveat."""
    out = srv.get_data(
        metric_ids="47", geo_level="county", geo_ids="17031", years="2022"
    )
    assert "margins of error" in out


def test_absence_is_reported_as_a_finding():
    out = srv.get_data(
        metric_ids="222", geo_level="county", geo_ids="17031", years="2022"
    )
    assert "real finding" in out


def test_unknown_geo_level_is_actionable():
    out = srv.get_data(metric_ids="47", geo_level="tract", geo_ids="1")
    assert "tract" in out.lower() and "national" in out
    assert "not loaded" in out


def test_bad_metric_id_names_the_problem():
    out = srv.get_data(metric_ids="banana", geo_level="state", geo_ids="17")
    assert "must be numbers" in out


def test_ranking_refuses_a_whole_dimension_and_lists_the_groups():
    """Ranking needs one series; "race" is eight of them."""
    out = srv.get_data(
        metric_ids="86", geo_level="state", rank="lowest", disaggregate="race"
    )
    assert "one specific group" in out
    assert "d1_hispanic" in out


def test_ranking_by_one_group_works():
    out = srv.get_data(
        metric_ids="86", geo_level="state", rank="lowest",
        years="2020", disaggregate="d1_hispanic", limit=3,
    )
    assert "Group: **Hispanic**" in out
    assert "pts" in out


def test_ranking_refuses_multiple_metrics():
    out = srv.get_data(metric_ids="47,51", geo_level="county", rank="highest")
    assert "exactly one metric_id" in out


def test_rank_on_year_without_data_lists_valid_years():
    out = srv.get_data(
        metric_ids="47", geo_level="county", rank="highest", years="1999"
    )
    assert "Available:" in out


def test_describe_eq_lists_indicators_and_metrics_with_data():
    out = srv.describe("eq1")
    assert "eq1." in out
    assert "Indicators:" in out
    assert "Metrics with data:" in out


def test_describe_metric_includes_framework_narrative():
    out = srv.describe("m1")
    assert "What the E-W Framework says" in out
    assert "educationtoworkforce.org" in out


def test_describe_metric_warns_on_metadata_year_disagreement():
    bad = md.all_metadata_flags()["years_available_mismatch"][0]["metric_id"]
    out = srv.describe(f"m{bad}")
    assert "disagrees with the data" in out


def test_search_finds_by_concept_not_just_metric_name():
    out = srv.search(query="pre-K")
    assert "m1" in out or "Indicator" in out


def test_search_miss_suggests_a_next_step():
    out = srv.search(query="zzzznotathing")
    assert "search()" in out


def test_search_with_no_query_lists_the_essential_questions():
    out = srv.search()
    assert "eq1." in out
    assert "metrics have data" in out


# ---------------------------------------------------------------------------
# Answering the question that was asked
#
# Every test here guards against returning a plausible answer of the WRONG
# SHAPE instead of refusing or explaining. That failure class is worse than an
# error, because nothing downstream can detect it.
# ---------------------------------------------------------------------------


def test_absent_breakdown_is_stated_not_answered_with_totals():
    """`income` (d7) is declared upstream and has zero rows anywhere."""
    out = srv.get_data(
        metric_ids="83", geo_level="county", geo_ids="17031",
        years="2021", disaggregate="income",
    )
    assert "No Income breakdown exists for m83" in out
    assert "not a group value" in out


def test_absent_breakdown_says_what_the_metric_does_have():
    """A refusal that names the alternative is recoverable; one that doesn't isn't."""
    out = srv.get_data(
        metric_ids="83", geo_level="county", geo_ids="17031",
        years="2021", disaggregate="income",
    )
    assert "Race or ethnicity" in out and "Gender" in out


def test_absent_breakdown_does_not_promise_by_group_gaps():
    """The gap note describes values that were never returned — it must not fire."""
    out = srv.get_data(
        metric_ids="83", geo_level="county", geo_ids="17031",
        years="2021", disaggregate="income",
    )
    assert "By-group values are percentage-point GAPS" not in out


def test_metric_with_no_disaggregates_at_all_says_so():
    out = srv.get_data(
        metric_ids="87", geo_level="county", geo_ids="17031",
        years="2021", disaggregate="race",
    )
    assert "no disaggregated data at all" in out


def test_one_group_code_returns_only_that_group():
    """Asking for d1_hispanic returns that group alone, not all eight."""
    out = srv.get_data(
        metric_ids="83", geo_level="county", geo_ids="17031",
        years="2021", disaggregate="d1_hispanic",
    )
    groups = set(re.findall(r"\| m83 \| ([^|]+?) \|", out))
    assert groups == {"Hispanic", "Total"}, groups


def test_ranking_reports_the_pool_it_ranked_over():
    """m166 covers 276 of 3,144 counties; a top-10 must not read as national."""
    out = srv.get_data(
        metric_ids="166", geo_level="county", rank="highest", years="2022", limit=3
    )
    assert "276 of 3,144 counties" in out


def test_ranking_says_missing_is_not_a_low_score():
    out = srv.get_data(
        metric_ids="166", geo_level="county", rank="highest", years="2022", limit=3
    )
    assert "missing data, not a low score" in out


def test_ranking_no_longer_claims_to_be_exhaustive():
    out = srv.get_data(
        metric_ids="166", geo_level="county", rank="highest", years="2022", limit=3
    )
    assert "computed over the complete dataset" not in out


def test_oversized_response_is_refused_not_truncated():
    """A cut table is indistinguishable from a complete one downstream."""
    out = srv.get_data(
        metric_ids="83", geo_level="district", state="48", years="2021"
    )
    assert "Cannot answer that as asked" in out
    assert "Response truncated" not in out


def test_row_cap_is_reachable_within_the_char_budget():
    """The two caps must agree, or the row cap is decorative."""
    from ew_mcp.constants import MAX_RESPONSE_CHARS, MAX_ROWS_RETURNED

    assert MAX_ROWS_RETURNED * 58 < MAX_RESPONSE_CHARS


def test_every_metric_in_a_result_is_named():
    """value_unit_note is None for currency, which left m87 rendered unlabelled."""
    out = srv.get_data(
        metric_ids="87", geo_level="county", geo_ids="17031", years="2021"
    )
    assert "Median student debt" in out


def test_blank_source_label_does_not_leak_into_provenance():
    """Three metrics carry a present-but-blank source_label upstream."""
    out = fmt.format_footer([7, 70, 71])
    assert "; \n" not in out and not out.rstrip().endswith(";")


# ---------------------------------------------------------------------------
# Disaggregates
#
# This server is scoped to essential questions, indicators, metrics, and the
# disaggregates that break a metric down. The gap that mattered most: the
# framework recommends 26 disaggregates and this dataset has 7, so a request
# for a real recommended dimension came back as "Unknown".
# ---------------------------------------------------------------------------


def test_compiled_disaggregates_cover_all_26_recommended():
    fw = md.framework()
    assert len(fw["disaggregates"]) == 26


def test_framework_capture_records_when_it_was_fetched():
    assert md.framework()["fetched_at"].startswith("20")


def test_recommended_but_unavailable_disaggregate_is_not_called_unknown():
    out = srv.get_data(
        metric_ids="83", geo_level="county", geo_ids="17031",
        years="2021", disaggregate="parental education",
    )
    assert "Unknown disaggregate" not in out
    assert "Parental education level" in out
    assert "does not carry it" in out


@pytest.mark.parametrize(
    "asked", ["LGBT status", "urbanicity", "justice involvement", "home language"]
)
def test_framework_disaggregates_are_recognised_by_name(asked):
    out = srv.get_data(
        metric_ids="83", geo_level="county", geo_ids="17031",
        years="2021", disaggregate=asked,
    )
    assert "26 disaggregates the E-W Framework" in out


def test_disaggregates_component_marks_what_is_reachable():
    out = srv.describe("disaggregates")
    assert "26 recommended, 6 queryable here" in out
    assert "✓ **Race and ethnicity**" in out    # in the data
    assert "— LGBT status" in out               # recommended only


def test_income_is_reachable_via_d5_not_the_empty_d7():
    """d7 is declared upstream with no rows; d5 is the one that answers."""
    status = dict(md.framework_disag_status())
    assert status["Income level"] == "d5"


def test_crosswalk_never_marks_an_empty_dimension_queryable():
    live = loader.dimensions_in_data()
    assert "d7" not in live
    for _name, prefix in md.framework_disag_status():
        assert prefix is None or prefix in live


def test_framework_overview_names_what_this_server_covers():
    out = srv.describe("framework")
    for word in ("essential questions", "indicators", "disaggregates"):
        assert word in out.lower()


def test_server_survives_without_framework_json(monkeypatch):
    """Same permission-withdrawal posture as narrative.json."""
    monkeypatch.setattr(md, "framework", lambda: {})
    assert md.recommended_disaggregates() == []
    assert md.match_recommended_disaggregate("urbanicity") is None


# ---------------------------------------------------------------------------
# Attribution
#
# The framework is Mathematica's; only the data tool is Urban's. Crediting
# Urban for both tells a user asking "what is the E-W framework?" the wrong
# author, so these tests pin the three credits apart.
# ---------------------------------------------------------------------------


def test_footer_credits_mathematica_for_the_framework():
    out = fmt.format_footer([47])
    assert "Mathematica" in out, "the framework's author must be named"
    assert "Education-to-Workforce Indicator Framework" in out


def test_footer_keeps_the_framework_and_the_data_tool_apart():
    """The regression: 'Data: E-W Framework Data Tool — Urban Institute'.

    Urban must be credited for the tool it built and not for the framework it
    did not write, so the two credits carry separate labels.
    """
    out = fmt.format_footer([47])
    framework_line = next(ln for ln in out.splitlines() if ln.startswith("Framework:"))
    tool_line = next(ln for ln in out.splitlines() if ln.startswith("Data tool:"))
    assert "Mathematica" in framework_line and "Urban" not in framework_line
    assert "Urban Institute" in tool_line and "Mathematica" not in tool_line


def test_describe_framework_gives_the_publishers_own_citation():
    out = srv.describe("framework")
    assert FRAMEWORK_CITATION in out, "quoted as published, not paraphrased"
    assert "Gonzalez" in out and "Mathematica" in out


@pytest.mark.parametrize("alias", ["framework", "Framework", "E-W", "ew"])
def test_describe_framework_is_reachable_by_the_obvious_names(alias):
    out = srv.describe(alias)
    assert "Education-to-Workforce Indicator Framework" in out


def test_describe_framework_names_urban_only_for_the_data():
    out = srv.describe("framework")
    assert "Urban Institute compiled federal data" in out


def test_describe_framework_links_out_to_the_framework_site():
    """The user should be able to leave for the source, not just be told about it."""
    out = srv.describe("framework")
    assert FRAMEWORK_EQ_URL in out
    assert FRAMEWORK_ABOUT_URL in out
    assert TOOL_URL in out


def test_essential_question_list_links_to_the_framework_site():
    out = srv.search()
    assert FRAMEWORK_EQ_URL in out


def test_narrative_prose_is_attributed_to_mathematica_not_just_a_url():
    """This is Mathematica's writing; a bare link does not say whose it is."""
    page = loader.narrative()["enrollment-public-pre-k"]
    out = fmt.format_narrative(page)
    assert "Mathematica" in out
    assert page["url"] in out


# ---------------------------------------------------------------------------
# Narrative
# ---------------------------------------------------------------------------


def test_narrative_is_present_and_attributed():
    pages = loader.narrative()
    assert len(pages) == 96
    page = pages["enrollment-public-pre-k"]
    assert page["definition"] and page["why_it_matters"]
    assert page["url"].startswith("https://educationtoworkforce.org/")


def test_narrative_has_no_footnote_marker_bleed():
    import re

    for slug, page in loader.narrative().items():
        for field in ("why_it_matters", "measurement", "definition"):
            text = page.get(field) or ""
            assert not re.search(r"\. \d{1,2} [A-Z]", text), f"{slug}.{field}"


def test_server_survives_without_narrative(monkeypatch):
    """Permission could be withdrawn; the server must degrade, not break."""
    monkeypatch.setattr(loader, "narrative", lambda: {})
    md.narrative_for_metric.__globals__["narrative"] = lambda: {}
    assert fmt.format_narrative(None) == ""


# ---------------------------------------------------------------------------
# Eval-driven: capabilities the H/I/J prompt set requires
# (docs/EW_EVAL.md — categories Neighborhood conditions, EQ-12, EQ-19)
# ---------------------------------------------------------------------------


def test_all_places_in_a_state_is_reachable():
    """H prompts ask "counties in Florida…" constantly.

    Before this existed the only route was enumerating 67 ids by hand.
    """
    fl = loader.places_in_state("county", "12")
    assert len(fl) == 67
    assert all(g.startswith("12") for g in fl)


def test_context_is_queryable_across_places():
    """H prompts relate an outcome to neighbourhood conditions.

    Context was loadable for ONE place but not across places, so a
    correlational question had no route.
    """
    ids = loader.places_in_state("county", "12")
    ctx = loader.context_table("county", ids, ["geo_medincome", "geo_pctba"])
    assert len(ctx) > 50
    assert all("geo_medincome" in v for v in ctx.values())


def test_with_context_labels_its_missing_year_dimension():
    out = srv.get_data(
        metric_ids="106", geo_level="county", state="12", years="2017"
    )
    assert "Median income" in out
    assert "no year" in out


def test_no_data_error_names_available_years_not_every_id():
    """A 67-id dump is unusable; the available years are what the caller needs."""
    out = srv.get_data(
        metric_ids="106", geo_level="county", state="12", years="2022"
    )
    assert "67 places" in out
    assert "2017" in out
    assert "12001" not in out


def test_institution_level_questions_are_refused_not_faked():
    """J prompts ask about specific colleges; EW has no institution level."""
    out = srv.get_data(metric_ids="87", geo_level="institution", geo_ids="1")
    assert "Unknown geo_level" in out


def test_instructions_survive_client_truncation():
    """Server instructions must fit inside the smallest client budget.

    MCP clients truncate this field silently — the Claude Code CLI cuts at 2,048
    characters mid-sentence and reports nothing. This block once ran to 3,257,
    so the last 1,209 characters never reached any model: the representation-gap
    sign convention, the no-margins-of-error rule, and NOT COVERED. Wrong
    numbers, delivered confidently, with no error anywhere to notice.

    Guidance that must reach the model belongs in a tool description or the
    result text. This test guards the budget, not the wording.
    """
    assert len(srv.mcp.instructions) < 2048
