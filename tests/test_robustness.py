"""data.gv.at's documents are outside our control: an unexpected shape must never crash a tool.

Every field of the sample documents is replaced in turn by values of the wrong
type; the views must still build and stay JSON-serialisable.
"""

import copy
import json

import pytest

from conftest import CATALOGUE, DATASET, FACETS, VOCAB_TERMS
from datagvat_mcp.normalize import (
    detail_dataset,
    normalize_distribution,
    normalize_facets,
    summarize_catalogue,
    summarize_dataset,
    vocab_term,
)
from datagvat_mcp.quality import assess_quality
from datagvat_mcp.similarity import features, similarity

JUNK = [
    None, "", "text", 0, 42, 1.5, True, [], ["a", None, 1], {}, {"x": 1}, [{"x": None}], [[1]], [None],
    {"de": None}, {"de": 7}, {"id": 5, "label": 7, "name": ["x"], "resource": {}}, [{"id": {"a": 1}, "label": [1]}],
]
junk = pytest.mark.parametrize("junk", JUNK, ids=[f"junk{i}" for i in range(len(JUNK))])


def check_dataset(d) -> None:
    for view in (summarize_dataset(d), detail_dataset(d), assess_quality(d)):
        json.dumps(view)
    f = features(d)
    similarity(f, features(DATASET))


@junk
@pytest.mark.parametrize("field", sorted(DATASET))
def test_dataset_field_of_unexpected_type(field, junk):
    d = copy.deepcopy(DATASET)
    d[field] = junk
    check_dataset(d)


@junk
@pytest.mark.parametrize("field", sorted({k for dist in DATASET["distributions"] for k in dist} | {"byte_size", "media_type", "download_url", "modified"}))
def test_distribution_field_of_unexpected_type(field, junk):
    d = copy.deepcopy(DATASET)
    d["distributions"][0][field] = junk
    check_dataset(d)
    json.dumps(normalize_distribution(d["distributions"][0]))


@junk
@pytest.mark.parametrize("path", [
    ("publisher", "name"), ("publisher", "email"), ("publisher", "homepage"),
    ("catalog", "id"), ("catalog", "title"),
    ("accrual_periodicity", "label"), ("accrual_periodicity", "resource"),
    ("catalog_record", "modified"),
])
def test_nested_field_of_unexpected_type(path, junk):
    d = copy.deepcopy(DATASET)
    d[path[0]][path[1]] = junk
    check_dataset(d)


@junk
@pytest.mark.parametrize("field", ["categories", "keywords", "distributions", "contact_point", "temporal", "language",
                                   "provenance"])
def test_list_item_of_unexpected_type(field, junk):
    d = copy.deepcopy(DATASET)
    d[field] = [junk, *d[field], junk]
    check_dataset(d)


@junk
@pytest.mark.parametrize("field", sorted(CATALOGUE))
def test_catalogue_field_of_unexpected_type(field, junk):
    c = copy.deepcopy(CATALOGUE)
    c[field] = junk
    json.dumps(summarize_catalogue(c))


@junk
def test_whole_documents_of_unexpected_type(junk):
    json.dumps(normalize_facets(junk))
    json.dumps(normalize_facets([junk, *FACETS, {"id": "x", "items": junk}, {"id": "y", "items": [junk]}]))
    for field in VOCAB_TERMS[0]:
        json.dumps(vocab_term({**VOCAB_TERMS[0], field: junk}))


def test_facet_counts_of_unexpected_type_are_skipped():
    facets = [{"id": "format", "items": [{"id": "CSV", "count": "12"}, {"id": "PDF", "count": 3},
                                         {"id": "X", "count": None}, {"id": "Y", "count": True}]}]
    assert normalize_facets(facets) == {"format": [{"id": "PDF", "count": 3}]}
