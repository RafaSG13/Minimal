"""Web search tool for the model, using the Brave Search API (needs BRAVE_API_KEY).

Create a key at https://api-dashboard.search.brave.com and export it as
``BRAVE_API_KEY``. Load the tool on demand with
``minimal --ext extensions/tools/web_search.py``, or copy the file to
``~/.minimal/extensions/tools/`` to always have it.
"""

import html
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

__version__ = "1.0"
__api__ = "1"

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
DEFAULT_COUNT = 5
MAX_COUNT = 20
"""Most results the API returns per request."""
TIMEOUT_S = 20


def _clean(text):
    """``text`` without the tags Brave marks matches with (``<strong>``) and with entities decoded."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def web_search(input, ctx):
    """Search the web; return numbered results as ``title``, ``url`` and ``description`` lines.

    Raises:
        ValueError: if ``BRAVE_API_KEY`` is not set or the query is empty.
        RuntimeError: if the API answers with an HTTP error.
    """
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        raise ValueError(
            "BRAVE_API_KEY is not set; create a key at https://api-dashboard.search.brave.com and export it"
        )
    query = input["query"].strip()
    if not query:
        raise ValueError("query is empty")
    count = max(1, min(MAX_COUNT, int(input.get("count", DEFAULT_COUNT))))

    request = urllib.request.Request(
        f"{ENDPOINT}?{urllib.parse.urlencode({'q': query, 'count': count})}",
        headers={"Accept": "application/json", "X-Subscription-Token": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Brave Search returned HTTP {e.code}: {body[:200]}") from e

    results = (data.get("web") or {}).get("results") or []
    if not results:
        return "No results"
    return "\n\n".join(
        f"{i}. {_clean(r.get('title', ''))}\n{r.get('url', '')}\n{_clean(r.get('description', ''))}"
        for i, r in enumerate(results, 1)
    )


def register(minimal):
    minimal.register_tool(
        name="web_search",
        run=web_search,
        description=(
            "Search the web with Brave Search. Returns numbered results: title, URL and snippet. "
            "Fetch a page with bash (curl) when the snippet is not enough."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query; operators such as site: and quotes work"},
                "count": {"type": "integer", "description": "Number of results (default 5, max 20)"},
            },
            "required": ["query"],
        },
    )
