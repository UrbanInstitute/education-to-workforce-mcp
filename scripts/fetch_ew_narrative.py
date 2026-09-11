#!/usr/bin/env python
"""Fetch and structure the 96 E-W Framework indicator pages.

    uv run --group dev python scripts/fetch_ew_narrative.py

Produces src/ew_mcp/data/narrative.json — the interpretive layer that makes
this server more than a numbers API: what each indicator means, why it matters,
and what to know before reading the number.

PERMISSION: educationtoworkforce.org content belongs to the Education-to-
Workforce Framework, not Urban. Reproducing it inside MCP responses is
redistribution and was explicitly authorised on 2026-07-29. If that ever
changes, drop narrative.json and the server degrades to link-outs (see
formatting.format_indicator, which treats narrative as optional).

Extraction rules:
  * VERBATIM, never summarised. This is someone else's writing and fidelity is
    the whole point — a paraphrase would put words in their mouth under their
    name, which is the exact failure the provenance discipline exists to avoid.
  * Parsed from stable semantic classes (view-detail__field-*, taxonomy-sidebar__*)
    rather than text heuristics, so a copy edit upstream does not silently
    change what we extract.
  * Inline footnote markers are stripped — they otherwise bleed into prose as
    "…attend these programs. 9 However…". Their title attributes carry full
    academic citations, which are kept in a separate `citations` field.

Slugs are discovered from metrics.json `notes_label`, so this script depends on
build_ew_data.py having run first.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "ew_mcp" / "data"
BASE = "https://educationtoworkforce.org/indicators/"
UA = "UrbanInstitute-EW-MCP/0.1 (+https://apps.urban.org/features/education-workforce-framework-data/)"

# Section -> CSS class on the wrapping div. "measurments" is upstream's spelling.
FIELDS = {
    "definition": ".view-detail__field-definition",
    "recommended_metrics": ".view-detail__field-metric",
    "data_types": ".view-detail__field-datasource",
    "why_it_matters": ".view-detail__field-matters",
    "measurement": ".view-detail__field-measurments",
    "source_frameworks": ".view-detail__field-frameworks",
}


def discover_slugs() -> dict[str, list[int]]:
    """Map indicator slug -> metric ids, from metrics.json notes_label links."""
    metrics = json.loads((DATA / "metadata" / "metrics.json").read_text())
    slugs: dict[str, list[int]] = {}
    for m in metrics:
        for slug in re.findall(
            r"educationtoworkforce\.org/indicators/([a-z0-9-]+)", m.get("notes_label") or ""
        ):
            slugs.setdefault(slug, []).append(m["metric_id"])
    return {k: sorted(set(v)) for k, v in sorted(slugs.items())}


def text_of(node) -> str | None:
    """Readable text for one section: paragraphs kept apart, lists bulleted."""
    from bs4 import BeautifulSoup

    if node is None:
        return None
    n = BeautifulSoup(str(node), "html.parser")
    for h in n.find_all(["h2", "h3"]):
        h.decompose()
    for hr in n.find_all("hr"):
        hr.decompose()
    for fn in n.select(".footnote__citations-wrapper"):
        fn.decompose()

    parts: list[str] = []
    for el in n.find_all(["p", "li"]):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not t:
            continue
        t = f"- {t}" if el.name == "li" else t
        if not parts or parts[-1] != t:  # taxonomy blocks repeat <p> then <a>
            parts.append(t)
    if not parts:
        t = re.sub(r"\s+", " ", n.get_text(" ", strip=True)).strip()
        parts = [t] if t else []
    return "\n\n".join(parts) or None


def taxonomy(soup, kind: str) -> list[str]:
    vals: list[str] = []
    box = soup.select_one(f".taxonomy-sidebar__{kind}")
    if not box:
        return vals
    for el in box.select(".taxonomy-sidebar__field__content a") or box.select(
        ".taxonomy-sidebar__field__content p"
    ):
        t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if t and t not in vals:
            vals.append(t)
    return vals


def parse(html: str, slug: str, metric_ids: list[int]) -> dict:
    from bs4 import BeautifulSoup

    s = BeautifulSoup(html, "html.parser")
    h1 = s.select_one("h1.block-title")
    rec: dict = {
        "slug": slug,
        "indicator_name": re.sub(r"^Indicator:\s*", "", h1.get_text(" ", strip=True))
        if h1
        else None,
        "url": BASE + slug,
        "metric_ids": metric_ids,
    }
    for key, sel in FIELDS.items():
        rec[key] = text_of(s.select_one(sel))

    # Sectors are a <ul> where applicability is marked by a --selected class,
    # NOT by presence — every page lists all four.
    rec["sectors"] = [
        li.get_text(" ", strip=True)
        for li in s.select(".taxonomy-sidebar__sectors li.taxonomy-sidebar__sectors--selected")
    ]
    rec["type"] = taxonomy(s, "type")
    rec["domain"] = taxonomy(s, "domain")
    rec["clusters"] = taxonomy(s, "cluster")

    eqs: list[str] = []
    for col in s.select(".view-detail__field-related__col"):
        h = col.find("h3")
        if h and "essential question" in h.get_text().lower():
            for el in col.find_all(["li", "a", "p"]):
                t = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
                if t.endswith("?") and t not in eqs:
                    eqs.append(t)
    rec["related_essential_questions"] = eqs

    cites: list[str] = []
    for a in s.select(".footnote__citation[title]"):
        t = re.sub(r"\s+", " ", a["title"]).strip()
        if t and t not in cites:
            cites.append(t)
    rec["citations"] = cites
    return rec


# ---------------------------------------------------------------------------
# Framework-level components
# ---------------------------------------------------------------------------
# The site has FIVE components; the indicator pages above are one of them. The
# other four were invisible to this server, which mattered most for
# disaggregates: the framework recommends 26 and this dataset carries 7, so
# `parental education` came back as "Unknown disaggregate" rather than as a
# recommended dimension the data does not reach.
#
# Only ENUMERATIONS are captured here — names, titles, numbers, links — not the
# prose bodies behind them. That is what the server needs to reason about
# coverage, and it keeps the reproduction footprint to what the 2026-07-29
# permission comfortably covers. The bodies stay one click away.
SITE = "https://educationtoworkforce.org"

COMPONENT_PAGES = {
    "disaggregates": "/disaggregates",
    "evidence_based_practices": "/evidence-based-practices",
    "data_equity_principles": "/data-equity-principles",
    "goals": "/apply",
    "faq": "/frequently-asked-questions",
}


def _get(path: str, cache: Path) -> str:
    hit = cache / (path.strip("/").replace("/", "_") + ".html")
    if not hit.exists():
        req = urllib.request.Request(SITE + path, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - pinned https host
            hit.write_bytes(r.read())
        time.sleep(0.3)
    return hit.read_text(encoding="utf-8", errors="replace")


def _flat(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()


def _grid(html: str) -> list[dict]:
    """Drupal responsive-grid cards: used by /disaggregates and /evidence-based-practices."""
    from bs4 import BeautifulSoup

    s = BeautifulSoup(html, "html.parser")
    out = []
    for item in s.select(".views-view-responsive-grid__item-inner"):
        title = item.select_one(".views-field-title")
        sector = item.select_one(".views-field-field-sector .field-content")
        link = item.select_one("a")
        if not title:
            continue
        href = link.get("href", "") if link else ""
        out.append(
            {
                "name": _flat(title),
                "sectors": _flat(sector).split() if sector else [],
                "url": SITE + href if href.startswith("/") else None,
            }
        )
    return out


def fetch_components(cache: Path) -> dict:
    """The four top-level framework components: about, essential questions,
    indicators and disaggregates."""
    from bs4 import BeautifulSoup

    out: dict = {}
    out["disaggregates"] = _grid(_get(COMPONENT_PAGES["disaggregates"], cache))
    out["evidence_based_practices"] = _grid(
        _get(COMPONENT_PAGES["evidence_based_practices"], cache)
    )

    # Principles render each field three times (combined, number, text); the
    # numbered heading is the stable anchor, so split on it and dedupe.
    s = BeautifulSoup(_get(COMPONENT_PAGES["data_equity_principles"], cache), "html.parser")
    seen: dict[int, str] = {}
    for el in s.select(".views-field .field-content"):
        t = _flat(el)
        m = re.match(r"^(\d+)\s+(\S.*)$", t)
        if m and len(m.group(2)) > 20:
            seen.setdefault(int(m.group(1)), m.group(2))
    out["data_equity_principles"] = [
        {"number": n, "text": seen[n]} for n in sorted(seen)
    ]

    # Goal links carry "View Goal" as their text and the /apply cards expose no
    # heading, so the real title comes from each goal page's own <h1>. Seven
    # extra requests beats de-slugging "goal-2-advance-k-12-student-outcomes"
    # into "Advance k 12 student outcomes".
    s = BeautifulSoup(_get(COMPONENT_PAGES["goals"], cache), "html.parser")
    goals: dict[str, dict] = {}
    for a in s.select('a[href*="/apply/goal-"]'):
        href = a["href"]
        slug = href.rstrip("/").rsplit("/", 1)[-1]
        m = re.match(r"goal-(\d+)-", slug)
        if not m or slug in goals:
            continue
        page = BeautifulSoup(_get(f"/apply/{slug}", cache), "html.parser")
        h1 = page.select_one("h1.block-title") or page.select_one("h1")
        goals[slug] = {
            "number": int(m.group(1)),
            "title": re.sub(r"^Goal\s*#?\d+:\s*", "", _flat(h1)) if h1 else slug,
            "url": href if href.startswith("http") else SITE + href,
        }
    out["goals"] = sorted(goals.values(), key=lambda g: g["number"])

    s = BeautifulSoup(_get(COMPONENT_PAGES["faq"], cache), "html.parser")
    out["faq"] = [
        {
            "question": _flat(item.select_one(".accordion-button")),
            "answer": _flat(item.select_one(".accordion-body")),
        }
        for item in s.select(".accordion-item")
        if item.select_one(".accordion-button") and item.select_one(".accordion-body")
    ]
    return out


def main() -> int:
    slugs = discover_slugs()
    print(f"{len(slugs)} indicator pages")
    cache = ROOT / ".ew_cache" / "narrative"
    cache.mkdir(parents=True, exist_ok=True)

    out: dict[str, dict] = {}
    for i, (slug, mids) in enumerate(slugs.items(), 1):
        hit = cache / f"{slug}.html"
        if not hit.exists():
            req = urllib.request.Request(BASE + slug, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - pinned https host
                hit.write_bytes(r.read())
            time.sleep(0.3)  # be a courteous client
        out[slug] = parse(hit.read_text(encoding="utf-8", errors="replace"), slug, mids)
        if i % 24 == 0:
            print(f"  {i}/{len(slugs)}")

    (DATA / "narrative.json").write_text(
        json.dumps(out, indent=1, ensure_ascii=False) + "\n"
    )

    size = (DATA / "narrative.json").stat().st_size
    print(f"\nnarrative.json — {len(out)} pages, {size / 1e6:.2f} MB")
    for k in list(FIELDS) + ["sectors", "type", "domain", "clusters", "citations"]:
        n = sum(1 for r in out.values() if r.get(k))
        flag = "" if n == len(out) else "   <- partial (verify this is genuine absence)"
        print(f"  {k:22s} {n}/{len(out)}{flag}")
    print(f"  {'total citations':22s} {sum(len(r['citations']) for r in out.values())}")
    if any(re.search(r"\. \d{1,2} [A-Z]", r.get("why_it_matters") or "") for r in out.values()):
        print("  WARNING: footnote markers still bleeding into prose", file=sys.stderr)

    # ---- framework components -------------------------------------------
    print("\nFetching framework components…")
    comps = fetch_components(cache)
    # fetched_at is not decoration: without it there is no way to tell which
    # revision of the framework the prose reflects, and the site drifts — its own
    # FAQ still says "seven data equity principles" while the principles page
    # lists eight.
    comps["fetched_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    comps["source"] = SITE
    (DATA / "framework.json").write_text(
        json.dumps(comps, indent=1, ensure_ascii=False) + "\n"
    )
    size = (DATA / "framework.json").stat().st_size
    print(f"framework.json — {size / 1e3:.0f} KB")
    expected = {
        "disaggregates": 26,
        "evidence_based_practices": 26,
        "data_equity_principles": 8,
        "goals": 7,
        "faq": 8,
    }
    ok = True
    for key, n in expected.items():
        got = len(comps.get(key) or [])
        flag = "" if got == n else f"   <- expected {n}; upstream may have changed"
        ok &= got == n
        print(f"  {key:26s} {got}{flag}")
    if not ok:
        print("  WARNING: component counts differ from the last known site state", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
