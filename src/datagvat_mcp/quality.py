"""Metadata quality assessment computed from a dataset's own metadata.

data.gv.at does not expose piveau's MQA metrics (/metrics returns 404), so
the score is derived locally from completeness checks loosely modelled on the
DCAT-AP / MQA dimensions (findability, accessibility, reusability, contextuality).
"""

from datetime import date, datetime
from typing import Any

from datagvat_mcp.normalize import clean_text, dataset_formats, keyword_labels, pick, theme_codes

MACHINE_READABLE = {
    "CSV", "JSON", "GEOJSON", "XML", "RDF", "RDF_XML", "TURTLE", "JSON_LD", "XLSX", "XLS", "ODS",
    "TSV", "PARQUET", "SHP", "GPKG", "KML", "GML", "WMS_SRVC", "WFS_SRVC", "API", "SPARQLQ",
}
OPEN_FORMATS = MACHINE_READABLE - {"XLSX", "XLS", "SHP"}


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _has_frequency(freq: Any) -> bool:
    if not isinstance(freq, dict):
        return bool(freq) and str(freq).upper() not in ("UNKNOWN", "UNKNOWN_FREQUENCY")
    marker = f"{freq.get('label') or ''} {freq.get('resource') or ''}".upper()
    return bool(marker.strip()) and "UNKNOWN" not in marker


def assess_quality(d: dict[str, Any], lang: str = "de", today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    dists = [x for x in d.get("distributions") or [] if isinstance(x, dict)]
    formats = set(dataset_formats(d))
    desc = clean_text(pick(d.get("description"), lang)) or ""
    publisher = d.get("publisher") if isinstance(d.get("publisher"), dict) else {}
    modified = _parse_date(d.get("modified")) or _parse_date((d.get("catalog_record") or {}).get("modified"))
    age_days = (today - modified).days if modified else None

    checks: list[tuple[str, int, bool, str]] = [
        ("title", 5, bool(pick(d.get("title"), lang)), "Dataset has a title"),
        ("description", 10, len(desc) >= 80, f"Description has at least 80 characters (has {len(desc)})"),
        ("keywords", 10, len(keyword_labels(d.get("keywords"))) >= 3, "At least 3 keywords"),
        ("themes", 10, bool(theme_codes(d.get("categories"))), "Assigned to at least one EU data theme"),
        ("publisher", 5, bool(pick(publisher.get("name"), lang)), "Publisher is named"),
        ("contact_point", 10, bool(d.get("contact_point")) or bool(publisher.get("email")), "Contact information"),
        ("distributions", 5, bool(dists), "Has at least one distribution"),
        (
            "access_urls",
            5,
            bool(dists) and all(x.get("access_url") or x.get("download_url") for x in dists),
            "Every distribution has an access or download URL",
        ),
        (
            "licenses",
            15,
            bool(dists) and all(x.get("license") for x in dists),
            "Every distribution states a license",
        ),
        ("machine_readable", 10, bool(formats & MACHINE_READABLE), "Offers a machine-readable format"),
        ("open_format", 5, bool(formats & OPEN_FORMATS), "Offers a non-proprietary format"),
        (
            "up_to_date",
            5,
            age_days is not None and age_days <= 730,
            f"Modified within the last 2 years ({'last modified ' + str(modified) if modified else 'no date'})",
        ),
        ("update_frequency", 3, _has_frequency(d.get("accrual_periodicity")), "Update frequency is stated"),
        ("coverage", 2, bool(d.get("temporal") or d.get("spatial")), "Temporal or spatial coverage is stated"),
    ]
    score = sum(w for _, w, ok, _ in checks if ok)
    total = sum(w for _, w, _, _ in checks)
    score = round(100 * score / total)
    rating = "good" if score >= 80 else "fair" if score >= 55 else "poor"
    return {
        "dataset_id": d.get("id"),
        "title": clean_text(pick(d.get("title"), lang)),
        "score": score,
        "rating": rating,
        "formats": sorted(formats),
        "distribution_count": len(dists),
        "last_modified": str(modified) if modified else None,
        "checks": [{"check": n, "passed": ok, "weight": w, "detail": t} for n, w, ok, t in checks],
        "missing": [t for _, _, ok, t in checks if not ok],
        "note": "Computed from the dataset's metadata; data.gv.at publishes no official quality metrics.",
    }
