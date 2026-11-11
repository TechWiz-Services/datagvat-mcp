"""MCP tools, resources and prompts for data.gv.at."""

import functools
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field

from datagvat_mcp import __version__
from datagvat_mcp.client import MAX_RESULT_WINDOW, ApiError, DataGvAtClient, NotFoundError, validate_id
from datagvat_mcp.config import Settings, get_settings
from datagvat_mcp.normalize import (
    THEMES,
    detail_dataset,
    first_url,
    format_label,
    normalize_distribution,
    normalize_facets,
    pick,
    summarize_catalogue,
    summarize_dataset,
    vocab_term,
)
from datagvat_mcp.preview import PreviewError, PreviewFetcher, parse
from datagvat_mcp.quality import assess_quality
from datagvat_mcp.semantic import build_query, expand_query
from datagvat_mcp.similarity import find_related
from datagvat_mcp.throttle import HostThrottles

logger = logging.getLogger(__name__)

SUMMARY_FIELDS = ["id", "title", "description", "publisher", "catalog", "categories", "keywords", "distributions",
                  "modified", "issued", "is_hvd"]
PREVIEWABLE = {"CSV", "JSON", "GEOJSON", "TSV"}
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"

INSTRUCTIONS = """Search and inspect Austrian Open Government Data from data.gv.at (~76,000 datasets, mostly German metadata).

Typical workflow: search_datasets (or semantic_search_datasets for plain-language requests) -> get_dataset for
details and download links -> preview_schema / preview_data to look inside CSV/JSON files.
Use list_catalogues to find publisher catalogue ids (e.g. 'l9' = Stadt Wien) for filtering.
Search terms work best in German. Multiple words are OR-combined and ranked by relevance; use AND to require all.
Requests to data.gv.at are rate-limited and cached by this server, so prefer fewer, more specific calls."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)

SortBy = Literal["relevance", "modified_desc", "modified_asc", "issued_desc", "issued_asc", "title_asc", "title_desc"]


@dataclass
class Services:
    settings: Settings
    client: DataGvAtClient
    preview: PreviewFetcher


_services: Services | None = None
_configured_settings: Settings | None = None


def make_services(settings: Settings) -> Services:
    return Services(
        settings=settings,
        client=DataGvAtClient(settings),
        preview=PreviewFetcher(
            max_bytes=settings.preview_max_bytes,
            timeout=settings.request_timeout,
            throttles=HostThrottles(settings.rate_limit, settings.rate_burst, settings.queue_timeout),
            cache_entries=settings.preview_cache_entries,
            cache_ttl=settings.cache_ttl,
            contact=settings.contact,
        ),
    )


def get_services() -> Services:
    global _services
    if _services is None:
        _services = make_services(_configured_settings or get_settings())
    return _services


def set_services(services: Services | None) -> None:
    global _services
    _services = services


async def close_services() -> None:
    global _services
    if _services is not None:
        await _services.client.close()
        _services = None


def tool_errors(fn):
    """Turn expected failures into clean MCP tool errors."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except NotFoundError as e:
            raise ToolError(str(e)) from e
        except (ApiError, PreviewError, ValueError) as e:
            raise ToolError(str(e)) from e

    return wrapper


def _check_themes(themes: list[str] | None) -> list[str]:
    out = [t.strip().upper() for t in themes or [] if t.strip()]
    bad = [t for t in out if t not in THEMES]
    if bad:
        raise ToolError(f"Unknown theme code(s) {bad}. Valid: {', '.join(THEMES)}")
    return out


def _sort_param(sort_by: str, lang: str) -> str | None:
    if sort_by == "relevance":
        return None
    field, direction = sort_by.rsplit("_", 1)
    if field == "title":
        field = f"title.{lang}"
    return f"{field}+{direction}"


async def _search(
    svc: Services,
    query: str | None,
    themes: list[str] | None = None,
    formats: list[str] | None = None,
    catalogue_ids: list[str] | None = None,
    high_value_only: bool = False,
    min_date: str | None = None,
    max_date: str | None = None,
    sort_by: str = "relevance",
    limit: int = 10,
    page: int = 0,
    include_facets: bool = False,
) -> dict[str, Any]:
    lang = svc.settings.language
    facets: dict[str, list[str]] = {}
    if themes:
        facets["categories"] = themes
    if formats:
        facets["format"] = list(dict.fromkeys(f.strip().upper() for f in formats if f.strip()))
    if catalogue_ids:
        facets["catalog"] = [validate_id(c, "catalogue id") for c in catalogue_ids]
    if high_value_only:
        facets["is_hvd"] = ["true"]
    if (page + 1) * limit > MAX_RESULT_WINDOW:
        raise ToolError(f"Only the first {MAX_RESULT_WINDOW} results can be paged through; narrow the search.")
    result = await svc.client.search(
        query=query or None,
        facets=facets or None,
        sort=_sort_param(sort_by, lang),
        limit=limit,
        page=page,
        min_date=f"{min_date}T00:00:00Z" if min_date else None,
        max_date=f"{max_date}T23:59:59Z" if max_date else None,
        includes=SUMMARY_FIELDS,
        aggregation_fields=["catalog", "categories", "format", "license"] if include_facets else None,
    )
    count = int(result.get("count") or 0)
    out: dict[str, Any] = {
        "count": count,
        "page": page,
        "limit": limit,
        "has_more": (page + 1) * limit < min(count, MAX_RESULT_WINDOW),
        "results": [summarize_dataset(d, lang) for d in result.get("results") or [] if isinstance(d, dict)],
    }
    if include_facets:
        out["facets"] = normalize_facets(result.get("facets"), lang)
    return out


MAX_PREVIEW_CANDIDATES = 3


async def _preview_candidates(
    svc: Services, url: str | None, dataset_id: str | None, distribution_id: str | None
) -> list[tuple[str, dict[str, Any] | None]]:
    if url:
        return [(url.strip(), None)]
    if not dataset_id:
        raise ToolError("Pass either url or dataset_id")
    try:
        d = await svc.client.get_dataset(dataset_id)
    except NotFoundError as e:
        raise ToolError(f"Dataset {dataset_id!r} not found") from e
    dists = [x for x in d.get("distributions") or [] if isinstance(x, dict)]
    if distribution_id:
        dists = [x for x in dists if x.get("id") == distribution_id]
        if not dists:
            raise ToolError(f"Distribution {distribution_id!r} not found in dataset {dataset_id!r}")
    else:
        dists = [x for x in dists if (format_label(x.get("format")) or "").upper() in PREVIEWABLE] or dists
    out = []
    for dist in dists:
        link = first_url(dist.get("download_url")) or first_url(dist.get("access_url"))
        if link:
            out.append((link, normalize_distribution(dist, svc.settings.language)))
    if not out:
        raise ToolError("No distribution with a download/access URL found for this dataset")
    return out[:MAX_PREVIEW_CANDIDATES]


async def _fetch_preview(svc: Services, url, dataset_id, distribution_id):
    """Fetch the first working candidate (catalogue links are sometimes dead)."""
    candidates = await _preview_candidates(svc, url, dataset_id, distribution_id)
    failures = []
    for link, dist in candidates:
        try:
            return link, dist, await svc.preview.fetch(link)
        except PreviewError as e:
            failures.append(f"{link}: {e}")
    if len(failures) == 1:
        raise PreviewError(failures[0].split(": ", 1)[1])
    raise PreviewError("All candidate distributions failed: " + "; ".join(failures))


def build_server(settings: Settings | None = None) -> FastMCP:
    global _configured_settings
    settings = settings or get_settings()
    _configured_settings = settings
    hosts = settings.allowed_host_list
    security = (
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hosts,
            allowed_origins=[f"{s}://{h}" for h in hosts for s in ("https", "http")],
        )
        if hosts
        else TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )
    mcp = FastMCP(
        name="datagvat",
        instructions=INSTRUCTIONS,
        host=settings.host,
        port=settings.port,
        streamable_http_path=settings.path,
        stateless_http=settings.stateless,
        transport_security=security,
        log_level=settings.log_level.upper() if settings.log_level.upper() in
        ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL") else "INFO",
    )
    mcp._mcp_server.version = __version__

    # ------------------------------------------------------------ discovery

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def search_datasets(
        query: Annotated[
            str | None,
            Field(
                max_length=500,
                description=(
                    "Search text (German works best). Words are OR-combined and ranked by relevance; "
                    "write 'verkehr AND wien' to require all words, \"exact phrase\" in quotes, 'europ*' "
                    "for prefixes, 'gesundheit~' for fuzzy matching. NOT/-word are not supported. "
                    "Omit to list datasets by the other filters."
                ),
            ),
        ] = None,
        themes: Annotated[
            list[str] | None,
            Field(description="EU data theme codes (OR-combined): " + ", ".join(f"{k} ({v['en']})" for k, v in THEMES.items())),
        ] = None,
        formats: Annotated[
            list[str] | None, Field(description="File formats, e.g. CSV, JSON, GEOJSON, XLSX, PDF (OR-combined)")
        ] = None,
        catalogue_ids: Annotated[
            list[str] | None,
            Field(description="Restrict to publisher catalogues (ids from list_catalogues, e.g. 'l9' = Stadt Wien)"),
        ] = None,
        high_value_only: Annotated[bool, Field(description="Only EU High-Value Datasets")] = False,
        min_date: Annotated[
            str | None, Field(pattern=DATE_PATTERN, description="Only datasets modified/issued on or after YYYY-MM-DD")
        ] = None,
        max_date: Annotated[
            str | None, Field(pattern=DATE_PATTERN, description="Only datasets modified/issued on or before YYYY-MM-DD")
        ] = None,
        sort_by: Annotated[SortBy, Field(description="Sort order; 'relevance' needs a query")] = "relevance",
        limit: Annotated[int, Field(ge=1, le=50, description="Results per page")] = 10,
        page: Annotated[int, Field(ge=0, description="Page number, 0-based")] = 0,
        include_facets: Annotated[
            bool, Field(description="Also return counts per catalogue, theme, format and license (top 10 each)")
        ] = False,
    ) -> dict[str, Any]:
        """Search data.gv.at datasets with full-text query and filters. Returns compact summaries
        (title, description excerpt, publisher, themes, formats, dates, portal URL)."""
        return await _search(
            get_services(), query, _check_themes(themes), formats, catalogue_ids, high_value_only,
            min_date, max_date, sort_by, limit, page, include_facets,
        )

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def semantic_search_datasets(
        ctx: Context,
        natural_query: Annotated[
            str, Field(min_length=2, max_length=500, description="Plain-language request in German or English")
        ],
        themes: Annotated[list[str] | None, Field(description="Optional extra theme filter (codes)")] = None,
        formats: Annotated[list[str] | None, Field(description="Optional format filter, e.g. ['CSV']")] = None,
        limit: Annotated[int, Field(ge=1, le=50)] = 10,
        page: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Search with a plain-language request, e.g. 'Gesundheitsdaten aus Wien' or 'bike traffic counts in Graz'.
        If your client supports MCP sampling, the request is first turned into German keywords and themes by
        your own model; otherwise a relevance search on the raw text is used."""
        svc = get_services()
        user_themes = _check_themes(themes)
        expansion = await expand_query(ctx, natural_query)
        if expansion:
            query = build_query(expansion["keywords"])
            all_themes = list(dict.fromkeys(user_themes + expansion["themes"]))
            all_formats = list(dict.fromkeys((formats or []) + expansion["formats"]))
            result = await _search(svc, query, all_themes, all_formats, limit=limit, page=page)
            if result["count"] == 0 and len(all_themes) > len(user_themes):
                all_themes = user_themes  # model-guessed themes were too strict
                result = await _search(svc, query, all_themes, all_formats, limit=limit, page=page)
            interpreted = {"method": "llm_expansion", "query": query, "themes": all_themes, "formats": all_formats}
        else:
            result = await _search(svc, natural_query, user_themes, formats, limit=limit, page=page)
            interpreted = {"method": "plain_text", "query": natural_query, "themes": user_themes,
                           "formats": formats or []}
        return {"interpreted_as": interpreted, **result}

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def get_dataset(
        dataset_id: Annotated[str, Field(description="Dataset id (from search results)")],
    ) -> dict[str, Any]:
        """Full metadata of one dataset: description, publisher and contacts, licenses, update frequency,
        temporal coverage, provenance and all distributions (files/APIs) with their URLs."""
        svc = get_services()
        try:
            d = await svc.client.get_dataset(dataset_id)
        except NotFoundError as e:
            raise ToolError(f"Dataset {dataset_id!r} not found") from e
        return detail_dataset(d, svc.settings.language)

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def get_dataset_distributions(
        dataset_id: Annotated[str, Field(description="Dataset id")],
    ) -> dict[str, Any]:
        """List the distributions (downloadable files, APIs, services) of a dataset with format, license and URLs."""
        svc = get_services()
        try:
            d = await svc.client.get_dataset(dataset_id)
        except NotFoundError as e:
            raise ToolError(f"Dataset {dataset_id!r} not found") from e
        dists = [normalize_distribution(x, svc.settings.language) for x in d.get("distributions") or []
                 if isinstance(x, dict)]
        return {"dataset_id": d.get("id", dataset_id), "title": pick(d.get("title"), svc.settings.language),
                "count": len(dists), "distributions": dists}

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def list_catalogues(
        query: Annotated[
            str | None, Field(max_length=200, description="Search catalogue names, e.g. 'Wien', 'Umweltbundesamt'")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any]:
        """Find publisher catalogues. Without a query, returns the catalogues with the most datasets.
        Use the returned ids as catalogue_ids in search_datasets."""
        svc = get_services()
        lang = svc.settings.language
        ttl = svc.settings.cache_ttl_static
        if query:
            result = await svc.client.search(kind="catalogue", query=query, limit=limit,
                                             includes=["id", "title", "description", "count"], ttl=ttl)
            cats = [summarize_catalogue(c, lang, desc_len=200) for c in result.get("results") or []]
            cats.sort(key=lambda c: -(c.get("dataset_count") or 0))  # empty duplicate catalogues last
            return {"count": result.get("count", 0), "catalogues": cats}
        result = await svc.client.search(limit=0, aggregation_fields=["catalog"], ttl=ttl)
        top = normalize_facets(result.get("facets"), lang, top=limit).get("catalog", [])
        return {
            "note": "Top catalogues by number of datasets",
            "catalogues": [{"id": c.get("id"), "title": c.get("title"), "dataset_count": c.get("count")} for c in top],
        }

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def get_catalogue(
        catalogue_id: Annotated[str, Field(description="Catalogue id, e.g. 'l9'")],
        recent_datasets: Annotated[int, Field(ge=0, le=20, description="Also list the N most recently modified datasets")] = 5,
    ) -> dict[str, Any]:
        """Details of a publisher catalogue (description, dataset count) plus its most recently updated datasets."""
        svc = get_services()
        lang = svc.settings.language
        try:
            cat = summarize_catalogue(await svc.client.get_catalogue(catalogue_id), lang)
        except NotFoundError as e:
            raise ToolError(f"Catalogue {catalogue_id!r} not found; use list_catalogues to look up ids") from e
        if recent_datasets:
            result = await svc.client.search(facets={"catalog": [catalogue_id]}, sort="modified+desc",
                                             limit=recent_datasets, includes=SUMMARY_FIELDS)
            cat["recent_datasets"] = [summarize_dataset(d, lang, desc_len=160) for d in result.get("results") or []]
        return cat

    # ------------------------------------------------------------- analysis

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def find_related_datasets(
        dataset_id: Annotated[str, Field(description="Source dataset id")],
        limit: Annotated[int, Field(ge=1, le=30)] = 10,
        min_score: Annotated[float, Field(ge=0, le=100, description="Minimum similarity score (0-100)")] = 20,
    ) -> dict[str, Any]:
        """Find datasets similar to a given one (shared themes, keywords and publisher), ranked by a 0-100 score."""
        svc = get_services()
        try:
            return await find_related(svc.client, dataset_id, limit, min_score, svc.settings.language)
        except NotFoundError as e:
            raise ToolError(f"Dataset {dataset_id!r} not found") from e

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def analyze_dataset_quality(
        dataset_id: Annotated[str, Field(description="Dataset id")],
    ) -> dict[str, Any]:
        """Score a dataset's metadata quality (0-100) with a list of passed/failed checks: description, keywords,
        themes, contact, licenses, machine-readable/open formats, freshness, coverage."""
        svc = get_services()
        try:
            d = await svc.client.get_dataset(dataset_id)
        except NotFoundError as e:
            raise ToolError(f"Dataset {dataset_id!r} not found") from e
        return assess_quality(d, svc.settings.language)

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def compare_datasets(
        dataset_ids: Annotated[list[str], Field(min_length=2, max_length=5, description="2-5 dataset ids")],
    ) -> dict[str, Any]:
        """Compare 2-5 datasets side by side: publisher, formats, licenses, update frequency, coverage,
        freshness and metadata quality score."""
        svc = get_services()
        lang = svc.settings.language
        rows, missing = [], []
        theme_sets, kw_sets = [], []
        for ds_id in dict.fromkeys(dataset_ids):
            try:
                d = await svc.client.get_dataset(ds_id)
            except NotFoundError:
                missing.append(ds_id)
                continue
            detail = detail_dataset(d, lang)
            q = assess_quality(d, lang)
            theme_sets.append(set(detail.get("themes", [])))
            kw_sets.append({k.lower() for k in detail.get("keywords", [])})
            rows.append({
                "id": detail.get("id"),
                "title": detail.get("title"),
                "publisher": detail.get("publisher"),
                "formats": detail.get("formats", []),
                "licenses": detail.get("licenses", []),
                "update_frequency": detail.get("update_frequency"),
                "temporal_coverage": detail.get("temporal_coverage"),
                "modified": detail.get("modified"),
                "distribution_count": detail.get("distribution_count", 0),
                "quality_score": q["score"],
                "quality_missing": q["missing"],
                "url": detail.get("url"),
            })
        if not rows:
            raise ToolError(f"None of the datasets were found: {missing}")
        return {
            "datasets": rows,
            "common_themes": sorted(set.intersection(*theme_sets)) if theme_sets else [],
            "common_keywords": sorted(set.intersection(*kw_sets))[:15] if kw_sets else [],
            "not_found": missing,
        }

    # -------------------------------------------------------- vocabularies

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def list_vocabularies() -> dict[str, Any]:
        """List the controlled vocabularies used by data.gv.at (themes, file types, licenses, frequencies,
        EuroVoc, political geocoding, ...)."""
        vocabs = await get_services().client.list_vocabularies()
        return {
            "vocabularies": vocabs,
            "hint": "Useful ones: data-theme (theme codes), file-type (format codes), licence, frequency, eurovoc "
                    "(subject thesaurus). Browse with get_vocabulary or search_vocabulary_terms.",
        }

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def get_vocabulary(
        vocabulary_id: Annotated[str, Field(description="Vocabulary id from list_vocabularies, e.g. 'data-theme'")],
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        page: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """List the terms of a controlled vocabulary (paged; EuroVoc has ~7,000 terms)."""
        svc = get_services()
        result = await svc.client.vocabulary_terms(vocabulary_id, limit=limit, page=page)
        terms = [vocab_term(t, svc.settings.language) for t in result.get("results") or []]
        count = int(result.get("count") or 0)
        if not count and not terms:
            raise ToolError(f"Vocabulary {vocabulary_id!r} not found or empty; see list_vocabularies")
        return {"vocabulary": vocabulary_id, "count": count, "page": page,
                "has_more": (page + 1) * limit < count, "terms": terms}

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def search_vocabulary_terms(
        vocabulary_id: Annotated[str, Field(description="Vocabulary id, e.g. 'eurovoc', 'licence', 'file-type'")],
        query: Annotated[str, Field(min_length=1, max_length=200, description="Search text")],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Search for terms within a controlled vocabulary."""
        svc = get_services()
        result = await svc.client.vocabulary_terms(vocabulary_id, query=query, limit=limit)
        return {"vocabulary": vocabulary_id, "query": query, "count": int(result.get("count") or 0),
                "terms": [vocab_term(t, svc.settings.language) for t in result.get("results") or []]}

    # -------------------------------------------------------------- preview

    preview_target = {
        "url": Annotated[str | None, Field(description="Direct URL of a CSV/JSON file (download_url or access_url)")],
        "dataset_id": Annotated[
            str | None, Field(description="Alternatively a dataset id: its first CSV/JSON distribution is used")
        ],
        "distribution_id": Annotated[str | None, Field(description="With dataset_id: pick this distribution")],
        "format": Annotated[
            Literal["csv", "json"] | None, Field(description="Force the parser; auto-detected otherwise")
        ],
    }

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def preview_schema(
        url: preview_target["url"] = None,
        dataset_id: preview_target["dataset_id"] = None,
        distribution_id: preview_target["distribution_id"] = None,
        format: preview_target["format"] = None,
    ) -> dict[str, Any]:
        """Infer the columns of a CSV or JSON/GeoJSON file (name, type, example values) from its first part
        (at most 512 KB are downloaded)."""
        svc = get_services()
        link, dist, f = await _fetch_preview(svc, url, dataset_id, distribution_id)
        parsed = parse(f.content, f.partial, f.final_url, f.content_type, format, max_rows=1)
        parsed.pop("rows", None)
        parsed.pop("more_rows_available", None)
        return {"url": link, "distribution": dist, "content_type": f.content_type,
                "bytes_read": len(f.content), "partial_download": f.partial, **parsed}

    @mcp.tool(annotations=READ_ONLY)
    @tool_errors
    async def preview_data(
        url: preview_target["url"] = None,
        dataset_id: preview_target["dataset_id"] = None,
        distribution_id: preview_target["distribution_id"] = None,
        format: preview_target["format"] = None,
        max_rows: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Show the first rows of a CSV or JSON/GeoJSON file (at most 512 KB are downloaded). Handles
        semicolon-separated files, Windows-1252 encoding and decimal commas common in Austrian data."""
        svc = get_services()
        link, dist, f = await _fetch_preview(svc, url, dataset_id, distribution_id)
        parsed = parse(f.content, f.partial, f.final_url, f.content_type, format, max_rows=max_rows)
        return {"url": link, "distribution": dist, "content_type": f.content_type,
                "bytes_read": len(f.content), "partial_download": f.partial, **parsed}

    # ------------------------------------------------------------ resources

    @mcp.resource("datagvat://themes", mime_type="application/json")
    def themes_resource() -> dict[str, Any]:
        """EU data theme codes with German and English labels."""
        return THEMES

    @mcp.resource("datagvat://datasets/{dataset_id}", mime_type="application/json")
    async def dataset_resource(dataset_id: str) -> dict[str, Any]:
        """Full metadata of a dataset."""
        svc = get_services()
        return detail_dataset(await svc.client.get_dataset(dataset_id), svc.settings.language)

    @mcp.resource("datagvat://catalogues/{catalogue_id}", mime_type="application/json")
    async def catalogue_resource(catalogue_id: str) -> dict[str, Any]:
        """Catalogue details."""
        svc = get_services()
        return summarize_catalogue(await svc.client.get_catalogue(catalogue_id), svc.settings.language)

    # -------------------------------------------------------------- prompts

    @mcp.prompt()
    def find_datasets(topic: str) -> str:
        """Find and summarise datasets about a topic."""
        return (
            f"Find datasets on data.gv.at about: {topic}\n\n"
            "Use semantic_search_datasets (or search_datasets with German keywords). For the 3-5 most relevant "
            "results, call get_dataset and report title, publisher, what the data contains, formats, license, "
            "update frequency and the portal URL. Mention if a CSV/JSON preview would help."
        )

    @mcp.prompt()
    def quality_audit(dataset_id: str) -> str:
        """Audit the metadata quality of a dataset."""
        return (
            f"Audit dataset '{dataset_id}' on data.gv.at:\n"
            "1. get_dataset for the metadata\n2. analyze_dataset_quality for the score and failed checks\n"
            "3. preview_schema on its main CSV/JSON distribution, if any\n"
            "Summarise strengths and give concrete improvement suggestions for the publisher."
        )

    @mcp.prompt(name="compare_datasets")
    def compare_datasets_prompt(dataset_ids: str) -> str:
        """Compare datasets (comma-separated ids) and recommend one."""
        ids = [i.strip() for i in dataset_ids.split(",") if i.strip()]
        return (
            f"Compare these data.gv.at datasets: {', '.join(ids)}\n\n"
            "Call compare_datasets with these ids, present a comparison table (publisher, formats, license, "
            "update frequency, coverage, quality score) and recommend which one to use and why."
        )

    @mcp.prompt()
    def catalogue_overview(catalogue_id: str) -> str:
        """Overview of a publisher catalogue."""
        return (
            f"Give an overview of the data.gv.at catalogue '{catalogue_id}':\n"
            "1. get_catalogue (with recent datasets)\n"
            f"2. search_datasets with catalogue_ids=['{catalogue_id}'] and include_facets=true\n"
            "Summarise the publisher's focus areas (themes), main formats, licenses and how actively it is updated."
        )

    return mcp
