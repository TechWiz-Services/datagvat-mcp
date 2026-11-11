"""Turn verbose hub-search documents into compact, single-language dicts.

Raw API objects carry labels in ~25 languages and many null fields; passing
them through unchanged wastes most of an agent's context window.
"""

import html
import re
from typing import Any

PORTAL_DATASET_URL = "https://www.data.gv.at/katalog/datasets/{id}"

THEMES: dict[str, dict[str, str]] = {
    "AGRI": {"de": "Landwirtschaft, Fischerei, Forstwirtschaft und Nahrungsmittel", "en": "Agriculture, fisheries, forestry and food"},
    "ECON": {"de": "Wirtschaft und Finanzen", "en": "Economy and finance"},
    "EDUC": {"de": "Bildung, Kultur und Sport", "en": "Education, culture and sport"},
    "ENER": {"de": "Energie", "en": "Energy"},
    "ENVI": {"de": "Umwelt", "en": "Environment"},
    "GOVE": {"de": "Regierung und öffentlicher Sektor", "en": "Government and public sector"},
    "HEAL": {"de": "Gesundheit", "en": "Health"},
    "INTR": {"de": "Internationale Themen", "en": "International issues"},
    "JUST": {"de": "Justiz, Rechtssystem und öffentliche Sicherheit", "en": "Justice, legal system and public safety"},
    "OP_DATPRO": {"de": "Vorläufige Daten", "en": "Provisional data"},
    "REGI": {"de": "Regionen und Städte", "en": "Regions and cities"},
    "SOCI": {"de": "Bevölkerung und Gesellschaft", "en": "Population and society"},
    "TECH": {"de": "Wissenschaft und Technologie", "en": "Science and technology"},
    "TRAN": {"de": "Verkehr", "en": "Transport"},
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n\s*\n+")


def pick(value: Any, lang: str = "de") -> str | None:
    """Pick one language from a multilingual value ({"de": .., "en": ..})."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for k in (lang, "de", "en"):
            v = value.get(k)
            if isinstance(v, str) and v:
                return v
        for v in value.values():
            if isinstance(v, str) and v:
                return v
        return None
    if isinstance(value, list):
        for item in value:
            v = pick(item, lang)
            if v:
                return v
        return None
    return str(value)


def clean_text(text: str | None, max_len: int | None = None) -> str | None:
    """Strip HTML tags/entities, normalise whitespace, optionally truncate."""
    if not text:
        return None
    text = re.sub(r"<br\s*/?>[ \t]*\r?\n?", "\n", text, flags=re.I)
    text = html.unescape(_TAG_RE.sub("", text))
    text = _NL_RE.sub("\n\n", _WS_RE.sub(" ", text))
    text = "\n".join(line.strip() for line in text.split("\n")).strip()
    if max_len and len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"
    return text or None


def first_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, list):
        for v in value:
            if isinstance(v, str) and v:
                return v
    return None


def compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop None / empty values."""
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def theme_codes(categories: Any) -> list[str]:
    out = []
    for c in categories or []:
        code = c.get("id") if isinstance(c, dict) else c
        if isinstance(code, str) and code:
            out.append(code.rsplit("/", 1)[-1].upper())
    return list(dict.fromkeys(out))


def keyword_labels(keywords: Any, limit: int | None = None) -> list[str]:
    out = []
    for k in keywords or []:
        label = (k.get("label") or k.get("id")) if isinstance(k, dict) else k
        if isinstance(label, str) and label.strip():
            out.append(label.strip())
    out = list(dict.fromkeys(out))
    return out[:limit] if limit else out


def format_label(fmt: Any) -> str | None:
    if isinstance(fmt, dict):
        return fmt.get("id") or fmt.get("label")
    return fmt if isinstance(fmt, str) and fmt else None


def license_label(lic: Any, lang: str = "de") -> str | None:
    if isinstance(lic, dict):
        return pick(lic.get("label"), lang) or lic.get("id") or lic.get("resource")
    return lic if isinstance(lic, str) and lic else None


def dataset_formats(dataset: dict[str, Any]) -> list[str]:
    fmts = [format_label(d.get("format")) for d in dataset.get("distributions") or [] if isinstance(d, dict)]
    return sorted({f.upper() for f in fmts if f})


def catalogue_ref(dataset: dict[str, Any], lang: str) -> dict[str, Any] | None:
    cat = dataset.get("catalog")
    if not isinstance(cat, dict) or not cat.get("id"):
        return None
    return compact({"id": cat.get("id"), "title": pick(cat.get("title"), lang)})


def dataset_url(dataset: dict[str, Any]) -> str | None:
    if dataset.get("id"):
        return PORTAL_DATASET_URL.format(id=dataset["id"])
    return dataset.get("resource")


def summarize_dataset(d: dict[str, Any], lang: str = "de", desc_len: int = 280) -> dict[str, Any]:
    publisher = d.get("publisher") if isinstance(d.get("publisher"), dict) else {}
    return compact(
        {
            "id": d.get("id"),
            "title": clean_text(pick(d.get("title"), lang)),
            "description": clean_text(pick(d.get("description"), lang), desc_len),
            "publisher": pick(publisher.get("name"), lang),
            "catalogue": catalogue_ref(d, lang),
            "themes": theme_codes(d.get("categories")),
            "keywords": keyword_labels(d.get("keywords"), 8),
            "formats": dataset_formats(d),
            "modified": d.get("modified"),
            "issued": d.get("issued"),
            "high_value_dataset": True if d.get("is_hvd") else None,
            "url": dataset_url(d),
        }
    )


def normalize_distribution(dist: dict[str, Any], lang: str = "de") -> dict[str, Any]:
    return compact(
        {
            "id": dist.get("id"),
            "title": clean_text(pick(dist.get("title"), lang)),
            "format": format_label(dist.get("format")),
            "media_type": dist.get("media_type") if isinstance(dist.get("media_type"), str) else None,
            "access_url": first_url(dist.get("access_url")),
            "download_url": first_url(dist.get("download_url")),
            "license": license_label(dist.get("license"), lang),
            "byte_size": dist.get("byte_size"),
            "modified": dist.get("modified"),
            "description": clean_text(pick(dist.get("description"), lang), 500),
        }
    )


def _contacts(value: Any) -> list[dict[str, Any]]:
    out = []
    for c in value if isinstance(value, list) else [value] if isinstance(value, dict) else []:
        if not isinstance(c, dict):
            continue
        email = c.get("email")
        if isinstance(email, str):
            email = email.removeprefix("mailto:")
        out.append(compact({"name": pick(c.get("name") or c.get("organisation_name"), "de"), "email": email}))
    return [c for c in out if c]


def detail_dataset(d: dict[str, Any], lang: str = "de") -> dict[str, Any]:
    out = summarize_dataset(d, lang, desc_len=4000)
    out["keywords"] = keyword_labels(d.get("keywords"))
    dists = [normalize_distribution(x, lang) for x in d.get("distributions") or [] if isinstance(x, dict)]
    publisher = d.get("publisher") if isinstance(d.get("publisher"), dict) else {}
    temporal = [
        compact({"from": t.get("gte"), "to": t.get("lte")}) for t in d.get("temporal") or [] if isinstance(t, dict)
    ]
    freq = d.get("accrual_periodicity")
    out.update(
        compact(
            {
                "publisher_contact": compact(
                    {
                        "homepage": publisher.get("homepage"),
                        "email": (publisher.get("email") or "").removeprefix("mailto:") or None,
                    }
                ),
                "contact_points": _contacts(d.get("contact_point")),
                "licenses": sorted({x["license"] for x in dists if x.get("license")}),
                "update_frequency": (pick(freq.get("label"), lang) if isinstance(freq, dict) else None),
                "temporal_coverage": [t for t in temporal if t],
                "languages": [x.get("id") for x in d.get("language") or [] if isinstance(x, dict) and x.get("id")],
                "landing_page": first_url(
                    [p.get("resource") if isinstance(p, dict) else p for p in d.get("landing_page") or []]
                    if isinstance(d.get("landing_page"), list)
                    else d.get("landing_page")
                ),
                "provenance": clean_text(
                    pick([p.get("label") if isinstance(p, dict) else p for p in d.get("provenance") or []], lang),
                    1500,
                ),
                "catalogue_record": compact(d.get("catalog_record") or {}) or None,
                "distribution_count": len(dists),
                "distributions": dists,
            }
        )
    )
    return out


def summarize_catalogue(c: dict[str, Any], lang: str = "de", desc_len: int = 600) -> dict[str, Any]:
    publisher = c.get("publisher") if isinstance(c.get("publisher"), dict) else {}
    return compact(
        {
            "id": c.get("id"),
            "title": clean_text(pick(c.get("title"), lang)),
            "description": clean_text(pick(c.get("description"), lang), desc_len),
            "dataset_count": c.get("count"),
            "publisher": pick(publisher.get("name"), lang),
            "homepage": first_url(c.get("homepage")),
            "languages": [x.get("id") for x in c.get("language") or [] if isinstance(x, dict) and x.get("id")],
            "part_of": c.get("is_part_of"),
            "modified": c.get("modified"),
            "issued": c.get("issued"),
        }
    )


def normalize_facets(facets: Any, lang: str = "de", top: int = 10) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for f in facets or []:
        if not isinstance(f, dict) or not f.get("id"):
            continue
        items = [i for i in f.get("items") or [] if isinstance(i, dict) and i.get("count")]
        items.sort(key=lambda i: -i["count"])
        if items:
            out[f["id"]] = [
                compact({"id": i.get("id"), "title": pick(i.get("title"), lang), "count": i["count"]})
                for i in items[:top]
            ]
    return out


def vocab_term(t: dict[str, Any], lang: str = "de") -> dict[str, Any]:
    return compact(
        {
            "id": t.get("id"),
            "label": pick(t.get("pref_label") or t.get("label"), lang),
            "uri": t.get("resource"),
        }
    )
