# ---------------------------------------------------------------------------
# Upstream provenance
# ---------------------------------------------------------------------------
# The data is static JSON committed to this repo, NOT an API. 
UPSTREAM_REPO = "UrbanInstitute/education-to-workforce"
UPSTREAM_COMMIT = "eaaa0a299bd43868a2cb7c6cbb5d370c59df9272"
UPSTREAM_RAW = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{UPSTREAM_COMMIT}"

# ---------------------------------------------------------------------------
# Attribution — three layers, and collapsing them misattributes authored work
# ---------------------------------------------------------------------------
# The FRAMEWORK — 20 essential questions, 99 indicators, the metric definitions
# and narrative prose is Mathematica's, written with the Bill & Melinda Gates 
# Foundation and Mirror Group. The DATA TOOL that compiles federal data against 
# it is the Urban Institute's. The underlying DATA is federal, and is already 
# surfaced per-metric through source_label.

# Verbatim from https://educationtoworkforce.org/about-framework
FRAMEWORK_CITATION = (
    "Gonzalez, Naihobe, Elizabeth Alberty, Stacey Brockman, Tutrang Nguyen, "
    "Matthew Johnson, Sheldon Bond, Krista O'Connell, Adrianna Corriveau, "
    "Megan Shoji, Megan Streeter, Jennifer Engle, Chelsea Goodly, Adrian N. "
    "Neely, Mary Aleta White, Mindelyn Anderson, Channing Matthews, Leana "
    'Mason, and Sheryl Felecia Mean. "Education-to-Workforce indicator '
    'framework: Using data to promote equity and economic security for all." '
    "Seattle: Mathematica, August 2022."
)

# Where to send someone who wants the framework itself rather than a number.
# Indicator pages are linked per-metric already, from narrative.json's own url.
FRAMEWORK_URL = "https://educationtoworkforce.org/"
FRAMEWORK_ABOUT_URL = "https://educationtoworkforce.org/about-framework"
FRAMEWORK_EQ_URL = "https://educationtoworkforce.org/essential-questions"

# The public tool a user can check an answer against.
TOOL_URL = "https://apps.urban.org/features/education-workforce-framework-data/"

# ---------------------------------------------------------------------------
# Geographic levels
# ---------------------------------------------------------------------------
# Keys as they appear in the compiled store. District ids are 7-digit NCES
# LEAIDs — the SAME key edp_mcp uses — so an agent with both servers connected
# can join E-W indicators to EDP enrollment/finance on the same id.
GEO_LEVELS = ("national", "state", "county", "district")

GEO_ID_WIDTH = {"national": 2, "state": 2, "county": 5, "district": 7}

GEO_LABEL = {
    "national": "United States",
    "state": "State",
    "county": "County",
    "district": "School district",
}

# Census tracts exist upstream (3,144 files, 517 MB raw) but are deferred:
# tract answers are thin and heavily suppressed, and they triple the build.

# ---------------------------------------------------------------------------
# Value semantics — REQUIRED for correct rendering
# ---------------------------------------------------------------------------
# metric_type drives formatting, and getting it wrong produces wrong answers
# rather than ugly ones. Observed ranges in the compiled data:
#
#   percent             0 .. 1            stored as a PROPORTION, not 0-100
#   percent_hundredths  -1.244 .. +8.5    a SIGNED gap; negative is meaningful
#   numeric             0 .. 113,042
#   currency            0 .. 569,000
#   hundredths          0 .. 3
#
# percent_hundredths is the dangerous one: these are the "…participation
# relative to share of student body" representation-gap metrics, where the SIGN
# is the finding (over- vs under-represented). Never render one without its
# sign, and never describe a small one as "no difference".

# ---------------------------------------------------------------------------
# Missingness
# ---------------------------------------------------------------------------
# Blank means UNKNOWN and only that. Upstream draws no distinction between
# suppressed, not collected, and not applicable.  
# There are NO margins of error, NO denominators, NO sample sizes and NO
# suppression flags anywhere in this dataset — so the server must never present
# a value as precise, or a difference between two values as significant.
MISSINGNESS_CAVEAT = (
    "Values are point estimates with no margins of error, sample sizes, or "
    "denominators. A blank means the value is unavailable — upstream does not "
    "distinguish suppressed from not-collected. Differences between places or "
    "years may not be meaningful."
)

# ACS 5-year estimates: the year label is the END of a 5-year window, not a
# single year, so adjacent years overlap and are not independent observations.
FIVE_YEAR_CAVEAT = (
    "This metric is a 5-year estimate: the year shown is the end of a 5-year "
    "window, so adjacent years overlap and should not be compared as independent."
)

# ---------------------------------------------------------------------------
# Known upstream issues   
# ---------------------------------------------------------------------------
# These are upstream problems and not ours to correct. 
# The build records them in a validation report and proceeds. 
#
#   * metric_id 222 appears TWICE with different metrics (child-care subsidies,
#     SNAP participation). Both in_tool; neither has data.
#   * m56, m57, m77, m224 carry real data with no metrics.json entry at all.
#     (m190 and m50 also lack entries but are explained: context.json shows
#     they are context variables, not tool metrics.)
#   * m222 and m227 are in_tool with no data anywhere. Harmless: coverage is
#     DERIVED from data, so they simply never appear.
#   * m231-m234 have no source_label. We omit the provenance line rather than guess.
UNDOCUMENTED_METRICS = (56, 57, 77, 224)
UNDOCUMENTED_METRIC_NOTE = (
    "No metadata is published upstream for this metric — its name, type and "
    "source are unknown. Values are shown as stored."
)

# ---------------------------------------------------------------------------
# Framework disaggregates -> this dataset's dimensions
# ---------------------------------------------------------------------------
# The framework recommends 26 disaggregates; this data carries 7 declared, of
# which 6 have any rows. The two vocabularies do not share wording ("Race and
# ethnicity" vs "Race or ethnicity", "Individuals experiencing homelessness" vs
# "Experiencing homelessness"), so the mapping is written out rather than
# guessed by string similarity — a near-miss here would mark a dimension
# queryable that isn't.
#
# "Income level" maps to BOTH d5 and d7: the framework has one income concept,
# the data splits it into economically-disadvantaged (d5, has rows) and income
# bands (d7, declared upstream with no rows anywhere). Reachable via d5 only.
FRAMEWORK_DISAG_CROSSWALK = {
    "Race and ethnicity": ("d1",),
    "Gender": ("d2",),
    "Disability status": ("d3",),
    "English learner": ("d4",),
    "Income level": ("d5", "d7"),
    "Individuals experiencing homelessness": ("d6",),
}

# The ONE upstream defect we do patch, and only at the display layer.
# The data uses `d3_nodisab`; disaggregates.json spells it `d3_nodsab`. It is
# the only disaggregate value in the data with no metadata entry, and unpatched
# it renders a real category as a bare code in user-facing output. This is a
# label correction, never a data edit.
DISAG_LABEL_FIXES = {"d3_nodisab": "Not individuals with disabilities"}

# ---------------------------------------------------------------------------
# Response budget
# ---------------------------------------------------------------------------
# Results are returned COMPLETE or refused, never silently truncated,
# because the server cannot know whether the dropped rows mattered to the question.
# Unlike edp_mcp this is a formatting limit only —
# every query is local and sub-11ms, so there is no upstream cost to a refusal.
MAX_RESPONSE_CHARS = 60_000

# Derived from the char budget, not chosen independently: a values row renders
# at ~58 characters, and 1,017 rows (Texas districts, one metric, one year)
# render to 61,424 — past the limit above. A row cap set any higher than the
# char budget can carry is unreachable, which lets a table be cut mid-render
# instead of refused. Leaves headroom for headers, benchmarks and caveats.
MAX_ROWS_RETURNED = 800

# Candidates returned by resolve_place. We return ranked candidates and never
# auto-pick: with 14,090 places and many repeated names ("Washington County"
# exists in 30 states), a silently wrong pick is indistinguishable downstream
# from a right one. 
MAX_PLACE_CANDIDATES = 25
