"""Natural-language query expansion via MCP sampling (the client's own LLM).

If the client does not support sampling (or it fails / times out), callers
fall back to a plain relevance search with the original text.
"""

import asyncio
import json
import logging
import re
from typing import Any

from mcp.server.fastmcp import Context
from mcp.types import ClientCapabilities, SamplingCapability, SamplingMessage, TextContent

from datagvat_mcp.normalize import THEMES

logger = logging.getLogger(__name__)

SAMPLING_TIMEOUT = 25.0

SYSTEM_PROMPT = (
    "You translate a user's request for Austrian open government data into search parameters for "
    "the data.gv.at catalogue (mostly German metadata). Reply with one JSON object only."
)

PROMPT = """Request: {query}

Return JSON with these keys:
- "keywords": 2-8 short German search terms (add English only if the request is about international data); include synonyms and place names
- "themes": zero or more EU theme codes from {themes} that clearly apply (prefer zero over guessing)
- "formats": file formats explicitly requested (e.g. "CSV", "JSON", "GEOJSON"), otherwise []

Example for "health data from Vienna": {{"keywords": ["Gesundheit", "Wien", "Krankenhaus", "Spital"], "themes": ["HEAL"], "formats": []}}"""


def parse_expansion(text: str) -> dict[str, Any] | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    def str_list(key: str) -> list[str]:
        value = data.get(key)
        return [v.strip() for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []

    keywords = str_list("keywords")[:8]
    themes = [t.upper() for t in str_list("themes") if t.upper() in THEMES]
    formats = [f.upper() for f in str_list("formats") if re.fullmatch(r"[\w.-]{1,20}", f)]
    if not keywords:
        return None
    return {"keywords": list(dict.fromkeys(keywords)), "themes": list(dict.fromkeys(themes)), "formats": formats[:5]}


async def expand_query(ctx: Context | None, query: str) -> dict[str, Any] | None:
    if ctx is None:
        return None
    try:
        session = ctx.session
        if not session.check_client_capability(ClientCapabilities(sampling=SamplingCapability())):
            return None
        result = await asyncio.wait_for(
            session.create_message(
                messages=[
                    SamplingMessage(
                        role="user",
                        content=TextContent(type="text", text=PROMPT.format(query=query, themes=", ".join(THEMES))),
                    )
                ],
                system_prompt=SYSTEM_PROMPT,
                max_tokens=300,
                temperature=0,
            ),
            timeout=SAMPLING_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 - any sampling failure means: fall back
        logger.info("Sampling unavailable or failed (%s); using plain search", e.__class__.__name__)
        return None
    content = result.content
    text = content.text if isinstance(content, TextContent) else ""
    return parse_expansion(text)


def build_query(keywords: list[str]) -> str:
    """OR-combined keyword query (data.gv.at ranks by relevance)."""
    parts = []
    for k in keywords:
        k = k.replace('"', " ").strip()
        parts.append(f'"{k}"' if " " in k else k)
    return " ".join(parts)
