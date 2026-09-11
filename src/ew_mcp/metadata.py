"""Framework structure: essential question -> indicator -> metric.

The E-W Framework is the navigation for this server. 
All of it is vendored verbatim from the pinned upstream commit, 
plus the narrative pages.

Everything here is small (~170 KB) and read constantly, so it is loaded once
into memory rather than queried.

One rule throughout: coverage claims come from DATA, not from metadata.
`metrics.json.years_available` disagreed with the actual data for 22 metrics in
the current build, so it is treated as a claim to reconcile, never a fact to
report. See loader.coverage.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any

from ew_mcp.constants import (
    DISAG_LABEL_FIXES,
    FRAMEWORK_DISAG_CROSSWALK,
    UNDOCUMENTED_METRIC_NOTE,
    UNDOCUMENTED_METRICS,
)
from ew_mcp.loader import DATA_DIR, narrative

# Sector flags are booleans on each record rather than a lookup table.
SECTORS = {
    "sector_prek": "Pre-K",
    "sector_k12": "K-12",
    "sector_postsec": "Postsecondary",
    "sector_work": "Workforce",
}

# Domain / type / cluster NAMES do not exist anywhere in the upstream metadata
# JSON — indicators.json carries bare ids (domain: 1, type: 1, cluster: "1").
# They are recovered from the narrative pages at runtime (see _taxonomy_names);
# these are the fallbacks if narrative.json is absent, taken from the deployed
# tool's own filter labels.
DOMAIN_FALLBACK = {
    1: "Academic Progress & Completion",
    2: "Career Readiness & Economic Success",
    3: "Social, Emotional & Physical Wellbeing",
}
TYPE_FALLBACK = {
    1: "Outcomes & Milestones",
    2: "E-W System Conditions",
    3: "Adjacent System Conditions",
}


def _load(stem: str):
    path = DATA_DIR / "metadata" / f"{stem}.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run scripts/build_ew_data.py")
    return json.loads(path.read_text())


@lru_cache(maxsize=1)
def metrics() -> dict[int, dict]:
    """metric_id -> record.

    Upstream has a DUPLICATE primary key: metric_id 222 appears twice, with two
    entirely different metrics (child-care subsidies, SNAP participation). We do
    not fail on it — an upstream typo should not block every rebuild — and we do
    not guess which is intended. The first record wins deterministically and the
    collision is recorded on the survivor so it can be surfaced rather than hidden.
    """
    out: dict[int, dict] = {}
    for rec in _load("metrics"):
        mid = rec["metric_id"]
        if mid in out:
            out[mid].setdefault("_duplicate_ids", []).append(rec.get("metric_full_name"))
            continue
        out[mid] = dict(rec)
    return out


@lru_cache(maxsize=1)
def indicators() -> dict[int, dict]:
    return {r["indicator_number"]: r for r in _load("indicators")}


@lru_cache(maxsize=1)
def essential_questions() -> dict[int, dict]:
    return {r["essential_question_id"]: r for r in _load("essential-questions")}


@lru_cache(maxsize=1)
def disaggregates() -> list[dict]:
    return _load("disaggregates")


@lru_cache(maxsize=1)
def state_lookup() -> dict[str, dict]:
    return _load("state-lookup")


@lru_cache(maxsize=1)
def framework() -> dict[str, Any]:
    """Framework structure scraped from the site, beyond indicators.

    This server surfaces only `disaggregates` from here (see
    recommended_disaggregates / framework_disag_status). The compiled file also
    carries evidence_based_practices, data_equity_principles, goals, and faq —
    kept in the build for now but not exposed as MCP surface, since this server
    is scoped to essential questions, indicators, metrics, and disaggregates.

    Optional for the same reason narrative.json is: if permission to reproduce
    educationtoworkforce.org content were withdrawn, deleting this file degrades
    the server rather than breaking it.
    """
    path = DATA_DIR / "framework.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def recommended_disaggregates() -> list[str]:
    """The 26 dimensions the framework recommends — not the 7 with data.

    The gap between these two lists is a finding, not an error. Without it the
    server answered a request for `parental education` with "Unknown
    disaggregate", which says the dimension does not exist rather than that the
    framework recommends it and this dataset does not reach it.
    """
    return [d["name"] for d in framework().get("disaggregates", [])]


def queryable_dimensions() -> list[str]:
    """Category names for dimensions that actually have rows.

    Not the same as the seven the metadata declares: `Income` (d7) is declared
    and empty. Quoting the declared count at a user promises a breakdown that
    cannot be produced, so every user-facing count comes from here.
    """
    from ew_mcp.loader import dimensions_in_data

    live = dimensions_in_data()
    return sorted(name for p, name in disag_categories().items() if p in live)


def framework_disag_status() -> list[tuple[str, str | None]]:
    """Each framework disaggregate paired with the dimension that reaches it.

    Returns (framework name, our prefix or None). None means the framework
    recommends the breakdown and this dataset cannot produce it — which is the
    whole point of carrying the list.
    """
    from ew_mcp.loader import dimensions_in_data

    live = dimensions_in_data()
    out = []
    for name in recommended_disaggregates():
        reachable = next(
            (p for p in FRAMEWORK_DISAG_CROSSWALK.get(name, ()) if p in live), None
        )
        out.append((name, reachable))
    return out


def match_recommended_disaggregate(name: str) -> str | None:
    """Loose match of a user's words against the framework's 26 recommendations."""
    n = name.lower().strip().replace("_", " ")
    if not n:
        return None
    for rec in recommended_disaggregates():
        r = rec.lower()
        if n == r or n in r or r in n:
            return rec
    return None


@lru_cache(maxsize=1)
def disag_labels() -> dict[str, str]:
    """Disaggregate code -> display label.

    DISAG_LABEL_FIXES patches `d3_nodisab`, which the data uses but
    disaggregates.json spells `d3_nodsab`. It is the only disaggregate value in
    the data with no metadata entry; unpatched it renders a real category as a
    bare code. Display-layer correction only — the stored value is untouched.
    """
    out = {f["value"]: f["label"] for d in disaggregates() for f in d["fields"]}
    out.update(DISAG_LABEL_FIXES)
    return out


@lru_cache(maxsize=1)
def disag_categories() -> dict[str, str]:
    """Prefix ("d1") -> category name ("Race or ethnicity")."""
    return {f"d{d['prefix']}": d["category_name"] for d in disaggregates()}


@lru_cache(maxsize=1)
def _taxonomy_names() -> dict[str, dict[int, str]]:  # noqa: D401
    """Recover domain/type names by joining narrative pages to indicators.

    The narrative carries human-readable names the upstream JSON lacks, so this
    prefers them and falls back to the tool's own labels.
    """
    dom: dict[int, str] = dict(DOMAIN_FALLBACK)
    typ: dict[int, str] = dict(TYPE_FALLBACK)
    by_name = {i["indicator_name"]: i for i in indicators().values()}
    for page in narrative().values():
        ind = by_name.get(page.get("indicator_name"))
        if not ind:
            continue
        if page.get("domain") and ind.get("domain"):
            dom[ind["domain"]] = page["domain"][0]
        if page.get("type") and ind.get("type"):
            typ[ind["type"]] = page["type"][0]
    return {"domain": dom, "type": typ}


def domain_name(domain_id: int | None) -> str | None:
    return _taxonomy_names()["domain"].get(domain_id) if domain_id else None


def type_name(type_id: int | None) -> str | None:
    return _taxonomy_names()["type"].get(type_id) if type_id else None


def _as_list(v) -> list[str]:
    """Upstream stores single values as scalars and multiples as lists."""
    if v is None:
        return []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def metric(metric_id: int) -> dict | None:
    return metrics().get(metric_id)


def metric_name(metric_id: int) -> str:
    m = metric(metric_id)
    if m and m.get("metric_full_name"):
        return m["metric_full_name"]
    if metric_id in UNDOCUMENTED_METRICS:
        return f"Metric {metric_id} (undocumented upstream)"
    return f"Metric {metric_id}"


def metric_note(metric_id: int) -> str | None:
    """Caveat that must ride along with this metric's values, if any."""
    if metric_id in UNDOCUMENTED_METRICS:
        return UNDOCUMENTED_METRIC_NOTE
    return None


def metric_indicator(metric_id: int) -> dict | None:
    m = metric(metric_id)
    if not m or not m.get("indicator_number"):
        return None
    return indicators().get(m["indicator_number"])


def indicator_slug(metric_id: int) -> str | None:
    """The educationtoworkforce.org slug, parsed out of notes_label."""
    m = metric(metric_id)
    if not m:
        return None
    found = re.findall(
        r"educationtoworkforce\.org/indicators/([a-z0-9-]+)", m.get("notes_label") or ""
    )
    return found[0] if found else None


def narrative_for_metric(metric_id: int) -> dict | None:
    slug = indicator_slug(metric_id)
    return narrative().get(slug) if slug else None


def narrative_for_indicator(indicator_number: int) -> dict | None:
    ind = indicators().get(indicator_number)
    if not ind:
        return None
    name = ind.get("indicator_name")
    for page in narrative().values():
        if page.get("indicator_name") == name:
            return page
    for mid in _as_list(ind.get("metrics")):
        via_metric = narrative_for_metric(int(mid))
        if via_metric:
            return via_metric
    return None


def indicator_metric_ids(indicator_number: int) -> list[int]:
    ind = indicators().get(indicator_number)
    return [int(m) for m in _as_list(ind.get("metrics"))] if ind else []


def eq_indicator_numbers(eq_id: int) -> list[int]:
    eq = essential_questions().get(eq_id)
    return [int(i) for i in _as_list(eq.get("indicator_list"))] if eq else []


def eq_metric_ids(eq_id: int) -> list[int]:
    out: list[int] = []
    for num in eq_indicator_numbers(eq_id):
        out.extend(indicator_metric_ids(num))
    return sorted(set(out))


def sectors_of(record: dict) -> list[str]:
    return [label for key, label in SECTORS.items() if record.get(key)]


_SEARCH_STOPWORDS = {
    "a", "an", "the", "of", "in", "on", "and", "or", "to", "for", "with", "by", "is", "are",
}


def _search_tokens(term: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", term.lower()) if w not in _SEARCH_STOPWORDS]


def search(term: str) -> dict[str, list]:
    """Token search across metrics, indicators and essential questions.

    Matches per-token (not whole-phrase), so word order and connector words
    don't matter — "neighborhood poverty" and "poverty in the neighborhood"
    hit the same records. Tries every token first; if that finds nothing and
    the query has more than one token, falls back to ranking records by how
    many tokens they match, so a near-miss phrase still surfaces the closest
    indicators instead of a flat refusal.
    """
    hits: dict[str, list] = {"essential_questions": [], "indicators": [], "metrics": []}
    tokens = _search_tokens(term)
    if not tokens:
        return hits

    def blob_of_eq(eq: dict) -> str:
        return eq["essential_question_name"].lower()

    def blob_of_indicator(num: int, ind: dict) -> str:
        blob = (ind.get("indicator_name") or "").lower()
        page = narrative_for_indicator(num)
        if page:
            blob += " " + " ".join(
                str(page.get(k) or "") for k in ("definition", "why_it_matters")
            ).lower()
        return blob

    def blob_of_metric(m: dict) -> str:
        return (m.get("metric_full_name") or "").lower()

    eq_blobs = {eq_id: blob_of_eq(eq) for eq_id, eq in essential_questions().items()}
    ind_blobs = {num: blob_of_indicator(num, ind) for num, ind in indicators().items()}
    metric_blobs = {mid: blob_of_metric(m) for mid, m in metrics().items()}

    def all_tokens_match(blob: str) -> bool:
        return all(tok in blob for tok in tokens)

    hits["essential_questions"] = [k for k, b in eq_blobs.items() if all_tokens_match(b)]
    hits["indicators"] = [k for k, b in ind_blobs.items() if all_tokens_match(b)]
    hits["metrics"] = [k for k, b in metric_blobs.items() if all_tokens_match(b)]

    if not any(hits.values()) and len(tokens) > 1:
        def ranked_by_token_overlap(blobs: dict) -> list:
            scored = ((k, sum(1 for tok in tokens if tok in b)) for k, b in blobs.items())
            scored = [(k, score) for k, score in scored if score > 0]
            scored.sort(key=lambda pair: -pair[1])
            return [k for k, _ in scored]

        hits["essential_questions"] = ranked_by_token_overlap(eq_blobs)
        hits["indicators"] = ranked_by_token_overlap(ind_blobs)
        hits["metrics"] = ranked_by_token_overlap(metric_blobs)

    return hits


def suggest_indicators(term: str, limit: int = 5) -> list[str]:
    """Nearest indicator names by fuzzy word match, for when search() finds
    nothing at all — e.g. a typo or a word the framework just doesn't use."""
    import difflib

    tokens = _search_tokens(term)
    if not tokens:
        return []

    vocab: dict[str, set[int]] = {}
    for num, ind in indicators().items():
        blob = (ind.get("indicator_name") or "").lower()
        page = narrative_for_indicator(num)
        if page:
            blob += " " + " ".join(
                str(page.get(k) or "") for k in ("definition", "why_it_matters")
            ).lower()
        for word in re.findall(r"[a-z0-9]+", blob):
            vocab.setdefault(word, set()).add(num)

    close_words: set[str] = set()
    for tok in tokens:
        close_words.update(difflib.get_close_matches(tok, vocab.keys(), n=3, cutoff=0.75))

    scored: dict[int, int] = {}
    for word in close_words:
        for num in vocab[word]:
            scored[num] = scored.get(num, 0) + 1

    ranked = sorted(scored.items(), key=lambda pair: -pair[1])[:limit]
    return [indicators()[num]["indicator_name"] for num, _ in ranked]


def available_metric_ids() -> set[int]:
    """Metric ids that actually have data (imported lazily to avoid a cycle)."""
    from ew_mcp.loader import query

    return {r[0] for r in query("select distinct metric_id from obs")}


def resolve_disaggregate(name: str) -> str | None:
    """Accept "race", "d1", "gender", "Race or ethnicity" -> "d1"."""
    n = name.lower().strip().replace("_", " ")
    aliases = {
        "race": "d1", "ethnicity": "d1", "race/ethnicity": "d1", "race or ethnicity": "d1",
        "gender": "d2", "sex": "d2",
        "disability": "d3", "disability status": "d3",
        "ell": "d4", "english learner": "d4", "english language learner": "d4",
        "income": "d7", "economically disadvantaged": "d5", "frl": "d5", "poverty": "d5",
        "homeless": "d6", "homelessness": "d6",
    }
    if n in aliases:
        return aliases[n]
    if re.fullmatch(r"d[1-7]", n):
        return n
    for prefix, label in disag_categories().items():
        if n == label.lower():
            return prefix
    return None


def all_metadata_flags() -> dict[str, Any]:
    """Upstream defects worth surfacing, read from the build's own report."""
    from ew_mcp.loader import manifest

    return manifest().get("validation", {})
