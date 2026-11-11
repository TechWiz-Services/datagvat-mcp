from conftest import CATALOGUE, DATASET, DATASET_2, DATASET_3, FACETS, VOCAB_TERMS
from datagvat_mcp.normalize import (
    clean_text,
    detail_dataset,
    normalize_distribution,
    normalize_facets,
    pick,
    summarize_catalogue,
    summarize_dataset,
    theme_codes,
    vocab_term,
)


def test_pick_language_with_fallbacks():
    assert pick({"de": "Haus", "en": "House"}, "en") == "House"
    assert pick({"de": "Haus", "en": "House"}, "fr") == "Haus"
    assert pick({"en": "House"}, "de") == "House"
    assert pick({"fi": "Talo"}, "de") == "Talo"
    assert pick({"de": "", "en": None}) is None
    assert pick("plain") == "plain"
    assert pick([None, {"de": "x"}]) == "x"
    assert pick(None) is None


def test_clean_text_strips_html_and_truncates():
    assert clean_text("a <b>bold</b><br>\nnext &amp; more") == "a bold\nnext & more"
    assert clean_text("x" * 50, 10) == "x" * 9 + "…"
    assert clean_text("   ") is None
    assert clean_text(None) is None


def test_theme_codes_from_ids_and_uris():
    assert theme_codes([{"id": "soci"}, {"id": "http://publications.europa.eu/resource/authority/data-theme/HEAL"},
                        {"id": "SOCI"}]) == ["SOCI", "HEAL"]


def test_summarize_dataset_is_compact():
    s = summarize_dataset(DATASET)
    assert s["title"] == "Bevölkerung nach Geburtsbundesland seit 2008 - Wien"
    assert "<b>" not in s["description"] and "Geburtsbundesland" in s["description"]
    assert s["publisher"] == "Stadt Wien"
    assert s["catalogue"] == {"id": "l9", "title": "Stadt Wien"}
    assert s["themes"] == ["SOCI"]
    assert s["formats"] == ["CSV", "PDF"]
    assert s["url"] == "https://www.data.gv.at/katalog/datasets/" + DATASET["id"]
    assert "issued" not in s and "high_value_dataset" not in s  # None/False dropped
    assert summarize_dataset(DATASET_2)["high_value_dataset"] is True
    assert summarize_dataset(DATASET_3)["title"] == "Budget 2026"  # English-only title


def test_summary_description_is_truncated():
    d = dict(DATASET, description={"de": "Wort " * 200})
    assert len(summarize_dataset(d)["description"]) <= 280


def test_detail_dataset():
    d = detail_dataset(DATASET)
    assert d["distribution_count"] == 2
    csv = d["distributions"][0]
    assert csv["access_url"] == "https://files.example.org/bevoelkerung.csv"  # list -> first url
    assert csv["format"] == "CSV"
    assert d["licenses"] == ["https://creativecommons.org/licenses/by/4.0/deed.de"]
    assert d["contact_points"] == [{"name": "MA 23", "email": "post@ma23.wien.gv.at"}]
    assert d["publisher_contact"]["email"] == "open@post.wien.gv.at"
    assert d["update_frequency"] == "annual"
    assert d["temporal_coverage"] == [{"from": "2008-01-01", "to": "2025-01-01"}]
    assert d["languages"] == ["de"]
    assert d["keywords"] == ["Bevölkerung", "Geburtsort", "Bundesland", "Wien"]
    assert "Statistik Austria" in d["provenance"]


def test_detail_handles_sparse_dataset():
    d = detail_dataset(DATASET_3)
    assert d["id"] == "minimal-1" and d["distribution_count"] == 1
    assert "licenses" not in d and "contact_points" not in d


def test_normalize_distribution_license_label():
    dist = {"id": "x", "license": {"label": {"de": "CC BY 4.0"}, "resource": "https://cc/by"}, "format": "csv",
            "download_url": ["https://a/x.csv"]}
    n = normalize_distribution(dist)
    assert n["license"] == "CC BY 4.0" and n["download_url"] == "https://a/x.csv" and n["format"] == "csv"


def test_catalogue_summary():
    c = summarize_catalogue(CATALOGUE)
    assert c == {"id": "l9", "title": "Stadt Wien", "description": "Open Government Data (OGD) in Wien ...",
                 "dataset_count": 780, "languages": ["de"], "part_of": "data-gv-at",
                 "modified": "2026-02-12T07:58:10Z", "issued": "2026-02-12T07:58:10Z"}


def test_facets_sorted_trimmed_and_null_safe():
    f = normalize_facets(FACETS, top=1)
    assert "dataScope" not in f  # null items skipped
    assert f["catalog"] == [{"id": "harvest-oh-old", "title": "OHOLD Harvester", "count": 29806}]
    assert f["categories"][0]["title"] == "Bevölkerung und Gesellschaft"


def test_vocab_term():
    assert vocab_term(VOCAB_TERMS[1], "en") == {
        "id": "HEAL", "label": "Health", "uri": "http://publications.europa.eu/resource/authority/data-theme/HEAL"}
