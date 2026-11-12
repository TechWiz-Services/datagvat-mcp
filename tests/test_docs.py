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
DOCUMENTED_IN = ["README.md", ".env.example", "docker-compose.yml", "Dockerfile"]
SETTINGS = {f"DATAGVAT_{name.upper()}" for name in Settings.model_fields}
NOT_SETTINGS = {"DATAGVAT_LIVE_TESTS"}  # switches the live tests on; read by the test suite only


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
    assert SETTINGS - mentioned == set(), "settings missing from the README's configuration table"


@pytest.mark.parametrize("name", [".env.example", "docker-compose.yml"])
def test_every_setting_can_be_set_through_compose(name):
    mentioned = set(re.findall(r"DATAGVAT_[A-Z0-9_]+", read(name)))
    assert SETTINGS - mentioned == set(), f"settings missing from {name}"


def _same(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except ValueError:
        return a.strip().lower() == b.strip().lower()


def test_compose_and_env_example_state_the_real_defaults():
    defaults = {f"DATAGVAT_{k.upper()}": "" if v is None else str(v) for k, v in Settings().model_dump().items()}
    defaults["DATAGVAT_CACHE_DIR"] = "/cache"  # set by the image
    compose = dict(re.findall(r"^\s+(DATAGVAT_[A-Z0-9_]+): \$\{\1:?-([^}]*)\}$", read("docker-compose.yml"), re.M))
    example = dict(re.findall(r"^#?(DATAGVAT_[A-Z0-9_]+)=(.*)$", read(".env.example"), re.M))
    assert set(compose) == set(example) == SETTINGS  # each passed through under its own name
    for source, values in (("docker-compose.yml", compose), (".env.example", example)):
        wrong = {k: v for k, v in values.items() if not _same(v, defaults[k])}
        assert not wrong, f"{source} states other defaults than the code: {wrong}"
    assert "${DATAGVAT_PREVIEW_BLOCKED_HOSTS-" in read("docker-compose.yml")  # empty in .env must stay empty


def test_readme_states_the_real_defaults():
    readme = read("README.md")
    defaults = Settings()
    row = re.search(r"\| `DATAGVAT_REQUEST_INTERVAL` \| `([\d.]+)` \|", readme)
    assert row and float(row.group(1)) == defaults.request_interval
    row = re.search(r"\| `DATAGVAT_QUEUE_TIMEOUT` \| `([\d.]+)` \|", readme)
    assert row and float(row.group(1)) == defaults.queue_timeout
    row = re.search(r"\| `DATAGVAT_PREVIEW_MAX_BYTES` \| `(\d+)` \|", readme)
    assert row and int(row.group(1)) == defaults.preview_max_bytes
    assert f"`{defaults.api_base}`" in readme


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
