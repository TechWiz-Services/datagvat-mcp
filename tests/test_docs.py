"""Keep the documentation, the example files and the code in step."""

import re
import tomllib
from pathlib import Path

import pytest

from conftest import make_settings
from datagvat_mcp import __version__
from datagvat_mcp.config import Settings
from datagvat_mcp.server import INSTRUCTIONS, build_server

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTED_IN = ["README.md", "docs/TECHNICAL.md", ".env.example", "docker-compose.yml", "Dockerfile"]
SETTINGS = {f"DATAGVAT_{name.upper()}" for name in Settings.model_fields}
NOT_SETTINGS = {"DATAGVAT_LIVE_TESTS"}  # switches the live tests on; read by the test suite only
# Left out of the README on purpose (it is for newcomers); the technical reference has them.
HIDDEN_FROM_README = {"DATAGVAT_REQUEST_INTERVAL", "DATAGVAT_PREVIEW_REQUEST_INTERVAL"}


def read(name: str) -> str:
    path = ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not part of this checkout")
    return path.read_text(encoding="utf-8")


def test_version_is_the_same_everywhere():
    project = tomllib.loads(read("pyproject.toml"))["project"]
    assert project["version"] == __version__


@pytest.mark.parametrize("name", DOCUMENTED_IN)
def test_only_existing_settings_are_mentioned(name):
    mentioned = set(re.findall(r"DATAGVAT_[A-Z0-9_]+", read(name)))
    assert mentioned - SETTINGS - NOT_SETTINGS == set(), f"{name} mentions settings that do not exist"


def test_every_setting_is_documented_in_the_readme():
    mentioned = set(re.findall(r"DATAGVAT_[A-Z0-9_]+", read("README.md")))
    assert SETTINGS - HIDDEN_FROM_README - mentioned == set(), "settings missing from the README's table"
    assert mentioned & HIDDEN_FROM_README == set(), "the README is meant not to mention these settings"
    assert "docs/TECHNICAL.md" in read("README.md")


def _technical_rows() -> dict[str, str]:
    """Variable name -> the rest of its table row in docs/TECHNICAL.md."""
    return dict(re.findall(r"^\| `((?:DATAGVAT_|FORWARDED_)[A-Z0-9_]+)` \| (.+) \|$", read("docs/TECHNICAL.md"), re.M))


def _bound(value) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def test_technical_reference_lists_every_setting_with_its_default_and_range():
    rows = _technical_rows()
    assert SETTINGS - set(rows) == set(), "settings missing from docs/TECHNICAL.md"
    for var in sorted(SETTINGS):
        field = Settings.model_fields[var.removeprefix("DATAGVAT_").lower()]
        default_cell = rows[var].split(" | ")[0]
        if var in REQUIRED:
            assert default_cell.strip() == "required", var
        elif field.default in ("", None):
            assert default_cell.startswith("unset"), f"{var}: {default_cell}"
        else:
            shown = re.match(r"`([^`]*)`", default_cell)
            expected = str(field.default).lower() if isinstance(field.default, bool) else str(field.default)
            assert shown and _same(shown.group(1), expected), f"{var}: {default_cell} != {expected}"
        for meta in field.metadata:  # every bound the code enforces is written down
            for attr in ("ge", "gt", "le", "lt"):
                if (limit := getattr(meta, attr, None)) is not None:
                    assert _bound(limit) in rows[var], f"{var}: bound {limit} missing from {rows[var]}"


def test_the_minimum_pause_is_documented_where_it_is_allowed_to_be():
    from datagvat_mcp.config import MIN_REQUEST_INTERVAL

    rows = _technical_rows()
    for var in HIDDEN_FROM_README:
        assert f"{_bound(MIN_REQUEST_INTERVAL)}–" in rows[var], var
    readme = read("README.md")
    assert f"never below {_bound(MIN_REQUEST_INTERVAL)} s" in readme  # the floor itself is not a secret


@pytest.mark.parametrize("name", [".env.example", "docker-compose.yml"])
def test_every_setting_can_be_set_through_compose(name):
    mentioned = set(re.findall(r"DATAGVAT_[A-Z0-9_]+", read(name)))
    assert SETTINGS - mentioned == set(), f"settings missing from {name}"


def _same(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except ValueError:
        return a.strip().lower() == b.strip().lower()


REQUIRED = {"DATAGVAT_AUTH_TOKEN", "DATAGVAT_CONTACT"}  # the server refuses to start without them


def test_required_settings_are_what_the_code_demands():
    named = {re.match(r"DATAGVAT_[A-Z_]+", problem).group(0) for problem in Settings().startup_problems()}
    assert named == REQUIRED


def test_compose_and_env_example_state_the_real_defaults():
    defaults = {f"DATAGVAT_{k.upper()}": "" if v is None else str(v) for k, v in Settings().model_dump().items()}
    defaults["DATAGVAT_CACHE_DIR"] = "/cache"  # set by the image
    compose_text = read("docker-compose.yml")
    compose = dict(re.findall(r"^\s+(DATAGVAT_[A-Z0-9_]+): \$\{\1:?-([^}]*)\}$", compose_text, re.M))
    required = set(re.findall(r"^\s+(DATAGVAT_[A-Z0-9_]+): \$\{\1:\?[^}]+\}$", compose_text, re.M))
    example = dict(re.findall(r"^#?(DATAGVAT_[A-Z0-9_]+)=(.*)$", read(".env.example"), re.M))
    assert required == REQUIRED  # compose refuses to run without them, with a message
    assert set(compose) | required == set(example) == SETTINGS  # each passed through under its own name
    for source, values in (("docker-compose.yml", compose), (".env.example", example)):
        wrong = {k: v for k, v in values.items() if not _same(v, defaults[k])}
        assert not wrong, f"{source} states other defaults than the code: {wrong}"
    assert "${DATAGVAT_PREVIEW_BLOCKED_HOSTS-" in read("docker-compose.yml")  # empty in .env must stay empty


def test_readme_states_the_real_defaults():
    readme = read("README.md")
    defaults = Settings()
    assert f"the server waits {_bound(defaults.request_interval)} s before sending the next request" in readme
    row = re.search(r"\| `DATAGVAT_QUEUE_TIMEOUT` \| `([\d.]+)` \|", readme)
    assert row and float(row.group(1)) == defaults.queue_timeout
    row = re.search(r"\| `DATAGVAT_PREVIEW_MAX_BYTES` \| `(\d+)` \|", readme)
    assert row and int(row.group(1)) == defaults.preview_max_bytes
    assert f"`{defaults.api_base}`" in readme
    for name in sorted(REQUIRED):  # the table and the compose example both say it is required
        assert re.search(rf"\| `{name}` \| required \|", readme), name
        assert f"${{{name}:?" in readme, name


async def test_readme_lists_every_tool_resource_and_prompt():
    readme = read("README.md")
    mcp = build_server(make_settings())
    names = [t.name for t in await mcp.list_tools()] + [p.name for p in await mcp.list_prompts()]
    names += [str(r.uri) for r in await mcp.list_resources()]
    names += [t.uriTemplate.replace("{dataset_id}", "{id}").replace("{catalogue_id}", "{id}")
              for t in await mcp.list_resource_templates()]
    missing = [n for n in names if f"`{n}`" not in readme]
    assert len(names) == 21 and not missing, f"missing from the README: {missing}"


def test_agents_are_told_the_etiquette():
    text = INSTRUCTIONS.lower()
    assert "one request at a time" in text and "cc by 4.0" in text and "attribution" in text
