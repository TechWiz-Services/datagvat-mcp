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


# The documents come from a third party: never assume a field has the documented type.


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def dict_items(value: Any) -> list[dict[str, Any]]:
    """The dict elements of a list; a single dict counts as a one-element list."""
    if isinstance(value, dict):
        return [value]
    return [x for x in as_list(value) if isinstance(x, dict)]


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
    if isinstance(value, bool):
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
    """First URL of a string, a list of strings or a list of {"resource": url} objects."""
    if isinstance(value, str):
        return value or None
    for v in as_list(value):
        url = as_str(v.get("resource")) if isinstance(v, dict) else as_str(v)
        if url:
            return url
    return None


def compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drop None / empty values."""
    return {k: v for k, v in d.items() if v is not None and v != "" and v != [] and v != {}}


def scalar(value: Any) -> str | int | float | None:
    """Pass a plain value through (dates, ids, sizes); drop anything structured."""
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else None


def theme_codes(categories: Any) -> list[str]:
    out = []
    for c in as_list(categories):
        code = c.get("id") if isinstance(c, dict) else c
        if isinstance(code, str) and code:
            out.append(code.rsplit("/", 1)[-1].upper())
    return list(dict.fromkeys(out))


def keyword_labels(keywords: Any, limit: int | None = None) -> list[str]:
    out = []
    for k in as_list(keywords):
        label = (as_str(k.get("label")) or k.get("id")) if isinstance(k, dict) else k
        if isinstance(label, str) and label.strip():
            out.append(label.strip())
    out = list(dict.fromkeys(out))
    return out[:limit] if limit else out


def format_label(fmt: Any) -> str | None:
    if isinstance(fmt, dict):
        return as_str(fmt.get("id")) or as_str(fmt.get("label"))
    return as_str(fmt)


def license_label(lic: Any, lang: str = "de") -> str | None:
    if isinstance(lic, dict):
        return pick(lic.get("label"), lang) or as_str(lic.get("id")) or as_str(lic.get("resource"))
    return as_str(lic)


def dataset_formats(dataset: dict[str, Any]) -> list[str]:
    fmts = [format_label(d.get("format")) for d in dict_items(dataset.get("distributions"))]
    return sorted({f.upper() for f in fmts if f})


def catalogue_ref(dataset: dict[str, Any], lang: str) -> dict[str, Any] | None:
    cat = as_dict(dataset.get("catalog"))
    if not as_str(cat.get("id")):
        return None
    return compact({"id": cat["id"], "title": pick(cat.get("title"), lang)})


def dataset_url(dataset: dict[str, Any]) -> str | None:
    if as_str(dataset.get("id")):
        return PORTAL_DATASET_URL.format(id=dataset["id"])
    return as_str(dataset.get("resource"))


def _language_ids(value: Any) -> list[str]:
    return [x["id"] for x in dict_items(value) if as_str(x.get("id"))]


def summarize_dataset(d: dict[str, Any], lang: str = "de", desc_len: int = 280) -> dict[str, Any]:
    return compact(
        {
            "id": scalar(d.get("id")),
            "title": clean_text(pick(d.get("title"), lang)),
            "description": clean_text(pick(d.get("description"), lang), desc_len),
            "publisher": pick(as_dict(d.get("publisher")).get("name"), lang),
            "catalogue": catalogue_ref(d, lang),
            "themes": theme_codes(d.get("categories")),
            "keywords": keyword_labels(d.get("keywords"), 8),
            "formats": dataset_formats(d),
            "modified": scalar(d.get("modified")),
            "issued": scalar(d.get("issued")),
            "high_value_dataset": True if d.get("is_hvd") in (True, "true") else None,
            "url": dataset_url(d),
        }
    )


def normalize_distribution(dist: dict[str, Any], lang: str = "de") -> dict[str, Any]:
    return compact(
        {
            "id": scalar(dist.get("id")),
            "title": clean_text(pick(dist.get("title"), lang)),
            "format": format_label(dist.get("format")),
            "media_type": as_str(dist.get("media_type")),
            "access_url": first_url(dist.get("access_url")),
            "download_url": first_url(dist.get("download_url")),
            "license": license_label(dist.get("license"), lang),
            "byte_size": scalar(dist.get("byte_size")),
            "modified": scalar(dist.get("modified")),
            "description": clean_text(pick(dist.get("description"), lang), 500),
        }
    )


def _email(value: Any) -> str | None:
    return (as_str(value) or "").removeprefix("mailto:") or None


def _contacts(value: Any) -> list[dict[str, Any]]:
    out = [
        compact({"name": pick(c.get("name") or c.get("organisation_name"), "de"), "email": _email(c.get("email"))})
        for c in dict_items(value)
    ]
    return [c for c in out if c]


def detail_dataset(d: dict[str, Any], lang: str = "de") -> dict[str, Any]:
    out = summarize_dataset(d, lang, desc_len=4000)
    out["keywords"] = keyword_labels(d.get("keywords"))
    dists = [normalize_distribution(x, lang) for x in dict_items(d.get("distributions"))]
    publisher = as_dict(d.get("publisher"))
    temporal = [compact({"from": scalar(t.get("gte")), "to": scalar(t.get("lte"))}) for t in dict_items(d.get("temporal"))]
    record = as_dict(d.get("catalog_record"))
    provenance = [p.get("label") if isinstance(p, dict) else p for p in as_list(d.get("provenance"))]
    out.update(
        compact(
            {
                "publisher_contact": compact(
                    {"homepage": first_url(publisher.get("homepage")), "email": _email(publisher.get("email"))}
                ),
                "contact_points": _contacts(d.get("contact_point")),
                "licenses": sorted({x["license"] for x in dists if x.get("license")}),
                "update_frequency": pick(as_dict(d.get("accrual_periodicity")).get("label"), lang),
                "temporal_coverage": [t for t in temporal if t],
                "languages": _language_ids(d.get("language")),
                "landing_page": first_url(d.get("landing_page")),
                "provenance": clean_text(pick(provenance, lang), 1500),
                "catalogue_record": compact(
                    {"issued": scalar(record.get("issued")), "modified": scalar(record.get("modified"))}
                ),
                "distribution_count": len(dists),
                "distributions": dists,
            }
        )
    )
    return out


def summarize_catalogue(c: dict[str, Any], lang: str = "de", desc_len: int = 600) -> dict[str, Any]:
    return compact(
        {
            "id": scalar(c.get("id")),
            "title": clean_text(pick(c.get("title"), lang)),
            "description": clean_text(pick(c.get("description"), lang), desc_len),
            "dataset_count": as_int(c.get("count")),
            "publisher": pick(as_dict(c.get("publisher")).get("name"), lang),
            "homepage": first_url(c.get("homepage")),
            "languages": _language_ids(c.get("language")),
            "part_of": scalar(c.get("is_part_of")),
            "modified": scalar(c.get("modified")),
            "issued": scalar(c.get("issued")),
        }
    )


def _count(item: dict[str, Any]) -> int:
    return as_int(item.get("count")) or 0


def normalize_facets(facets: Any, lang: str = "de", top: int = 10) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for f in dict_items(facets):
        facet_id = as_str(f.get("id"))
        items = sorted((i for i in dict_items(f.get("items")) if _count(i) > 0), key=lambda i: -_count(i))
        if facet_id and items:
            out[facet_id] = [
                compact({"id": scalar(i.get("id")), "title": pick(i.get("title"), lang), "count": _count(i)})
                for i in items[:top]
            ]
    return out


def vocab_term(t: dict[str, Any], lang: str = "de") -> dict[str, Any]:
    return compact(
        {
            "id": scalar(t.get("id")),
            "label": pick(t.get("pref_label") or t.get("label"), lang),
            "uri": as_str(t.get("resource")),
        }
    )
