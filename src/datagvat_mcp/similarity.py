"""Find datasets related to a given dataset (shared themes, keywords, publisher)."""

from typing import Any

from datagvat_mcp.client import DataGvAtClient
from datagvat_mcp.normalize import keyword_labels, pick, summarize_dataset, theme_codes

CANDIDATE_FIELDS = ["id", "title", "description", "categories", "keywords", "catalog", "publisher", "modified",
                    "distributions", "is_hvd"]


def features(d: dict[str, Any]) -> dict[str, Any]:
    publisher = d.get("publisher") if isinstance(d.get("publisher"), dict) else {}
    cat = d.get("catalog") if isinstance(d.get("catalog"), dict) else {}
    return {
        "themes": set(theme_codes(d.get("categories"))),
        "keywords": {k.lower() for k in keyword_labels(d.get("keywords"))},
        "publisher": pick(publisher.get("name")) or cat.get("id"),
    }


def similarity(a: dict[str, Any], b: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    themes = a["themes"] & b["themes"]
    keywords = a["keywords"] & b["keywords"]
    same_pub = bool(a["publisher"]) and a["publisher"] == b["publisher"]
    score = min(len(themes) * 30, 60) + min(len(keywords) * 10, 30) + (15 if same_pub else 0)
    return min(score, 100), {
        "matching_themes": sorted(themes),
        "matching_keywords": sorted(keywords)[:8],
        "same_publisher": same_pub,
    }


def _quote(term: str) -> str:
    term = term.replace('"', " ").strip()
    return f'"{term}"' if " " in term else term


async def find_related(
    client: DataGvAtClient, dataset_id: str, limit: int = 10, min_score: float = 20, lang: str = "de"
) -> dict[str, Any]:
    source = await client.get_dataset(dataset_id)
    src = features(source)
    base = {
        "source": {"id": source.get("id", dataset_id), "title": pick(source.get("title"), lang)},
        "source_themes": sorted(src["themes"]),
        "source_keywords": sorted(src["keywords"])[:10],
    }
    if not src["themes"] and not src["keywords"]:
        return {**base, "related": [], "note": "Source dataset has no themes or keywords to compare."}

    # Keywords as an OR query ranked by relevance; themes as facet filter keep it on topic.
    query = " ".join(_quote(k) for k in sorted(src["keywords"])[:8]) or None
    facets = {"categories": sorted(src["themes"])} if src["themes"] else None
    candidates: list[dict[str, Any]] = []
    result = await client.search(query=query, facets=facets, limit=50, includes=CANDIDATE_FIELDS)
    candidates += result.get("results") or []
    if len(candidates) < 20 and query and facets:
        result = await client.search(query=query, limit=30, includes=CANDIDATE_FIELDS)
        candidates += result.get("results") or []

    seen = {source.get("id", dataset_id)}
    scored = []
    for cand in candidates:
        cid = cand.get("id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        score, why = similarity(src, features(cand))
        if score >= min_score:
            s = summarize_dataset(cand, lang, desc_len=160)
            scored.append({"similarity_score": score, **why, **s})
    scored.sort(key=lambda x: -x["similarity_score"])
    return {**base, "candidates_evaluated": len(seen) - 1, "related": scored[:limit]}
