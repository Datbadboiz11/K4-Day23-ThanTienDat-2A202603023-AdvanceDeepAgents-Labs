"""tools.py - STUDENT IMPLEMENTS.  Source tools for the research agents.   Guide: GUIDE.md, part 1.

Rules for every tool:
  * runs on the HOST (not in the sandbox): API keys must never enter the sandbox;
  * returns a STRING (JSON text of compact records) and NEVER raises:
        "NO RESULTS"  when the source answers with nothing,
        "ERROR: ..."  when the source keeps failing after the retries (the agent then tries another source);
  * the docstring is the tool description the LLM reads: keep it precise (what it does, what it returns, when to use it).
Try your tools without any agent:   python tools.py
"""
import json  # noqa: F401
import os  # noqa: F401
import time  # noqa: F401
import xml.etree.ElementTree  # noqa: F401  (arXiv answers with Atom XML)

import httpx  # noqa: F401
from langchain_core.tools import tool

# ---- constants (given) ----
ARXIV_URL = "https://export.arxiv.org/api/query"  # https only: http answers 301
HF_DAILY_URL = "https://huggingface.co/api/daily_papers"
HF_SEARCH_URL = "https://huggingface.co/api/papers/search"
EXA_URL = "https://mcp.exa.ai/mcp"


class RetryableError(Exception):
    """Given. Raise it inside a call to ask with_retry to wait and try again (retry_after in seconds, optional)."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# ---- TODO 1: retry helper ----
def with_retry(fn, *, attempts=5, base=1.0, cap=30.0):
    """Call fn(); when it raises RetryableError or temporary network errors, wait and call it again."""
    import random

    for attempt in range(attempts):
        try:
            return fn()
        except (RetryableError, httpx.HTTPStatusError, httpx.TransportError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 500, 502, 503, 504):
                raise
            if attempt == attempts - 1:
                raise

            delay = None
            if isinstance(exc, RetryableError) and exc.retry_after is not None:
                try:
                    delay = float(exc.retry_after)
                except (ValueError, TypeError):
                    delay = None
            elif isinstance(exc, httpx.HTTPStatusError):
                retry_after_hdr = exc.response.headers.get("Retry-After")
                if retry_after_hdr:
                    try:
                        delay = float(retry_after_hdr)
                    except ValueError:
                        pass

            if delay is None:
                backoff = base * (2**attempt)
                jitter = random.uniform(0, 0.5 * base)
                delay = min(cap, backoff + jitter)
            else:
                delay = min(cap, delay)

            time.sleep(delay)


_LAST_ARXIV_CALL = 0.0


def _arxiv_rate_limit():
    global _LAST_ARXIV_CALL
    now = time.time()
    elapsed = now - _LAST_ARXIV_CALL
    if elapsed < 3.0:
        time.sleep(3.0 - elapsed)
    _LAST_ARXIV_CALL = time.time()


# ---- TODO 2: arXiv ----
@tool
def arxiv_search(query: str, max_results: int = 10) -> str:
    """Search arXiv papers by keywords, newest first. Returns a JSON list of {id, url, published, title, summary}."""
    import re
    import xml.etree.ElementTree as ET

    terms = [t for t in re.findall(r"[\w-]+", query) if t.upper() not in ("AND", "OR", "NOT") and t]
    if not terms:
        return "NO RESULTS"

    search_query = " AND ".join(f"all:{t}" for t in terms)
    limit = max(1, min(int(max_results), 30))

    def _fetch():
        _arxiv_rate_limit()
        resp = httpx.get(
            ARXIV_URL,
            params={
                "search_query": search_query,
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": limit,
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.text

    try:
        xml_text = with_retry(_fetch, cap=60.0)
        root = ET.fromstring(xml_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        if not entries:
            return "NO RESULTS"

        records = []
        for entry in entries:
            id_elem = entry.find("atom:id", ns)
            if id_elem is None or not id_elem.text:
                continue
            raw_id = id_elem.text.strip().split("/abs/")[-1]
            paper_id = re.sub(r"v\d+$", "", raw_id)
            url = f"https://arxiv.org/abs/{paper_id}"

            pub_elem = entry.find("atom:published", ns)
            published = pub_elem.text.strip()[:10] if pub_elem is not None and pub_elem.text else ""

            title_elem = entry.find("atom:title", ns)
            title = " ".join((title_elem.text or "").split()) if title_elem is not None else ""

            sum_elem = entry.find("atom:summary", ns)
            summary = " ".join((sum_elem.text or "").split())[:600] if sum_elem is not None else ""

            records.append({
                "id": paper_id,
                "url": url,
                "published": published,
                "title": title,
                "summary": summary,
            })

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ---- TODO 3: Hugging Face ----
@tool
def hf_daily_papers(limit: int = 30, date: str = "", keyword: str = "") -> str:
    """Hugging Face Daily Papers = what is trending in AI research. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars} sorted by upvotes. `date` is YYYY-MM-DD (empty = latest).
    `keyword` filters title/summary; there is no topic search on this endpoint (use hf_search_papers for a topic)."""
    clamped_limit = max(1, min(int(limit), 100))
    params = {"limit": clamped_limit}
    if date and date.strip():
        params["date"] = date.strip()

    def _fetch():
        resp = httpx.get(HF_DAILY_URL, params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    try:
        items = with_retry(_fetch, cap=30.0)
        if not isinstance(items, list) or not items:
            return "NO RESULTS"

        records = []
        for item in items:
            paper = item.get("paper") if isinstance(item, dict) else None
            if not paper or not isinstance(paper, dict) or not paper.get("id"):
                continue
            p_id = str(paper["id"])
            url = f"https://huggingface.co/papers/{p_id}"
            published = str(paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            title = " ".join(str(paper.get("title") or item.get("title") or "").split())
            summary = " ".join(str(paper.get("summary") or item.get("summary") or "").split())[:600]
            upvotes = int(paper.get("upvotes") or 0)
            github = str(paper.get("githubRepo") or "")
            stars = int(paper.get("githubStars") or 0)

            records.append({
                "id": p_id,
                "url": url,
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
            })

        if keyword and keyword.strip():
            kw = keyword.strip().lower()
            records = [r for r in records if kw in r["title"].lower() or kw in r["summary"].lower()]

        records.sort(key=lambda r: r["upvotes"], reverse=True)

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


@tool
def hf_search_papers(query: str, limit: int = 10) -> str:
    """Search Hugging Face papers by topic. Returns a JSON list of
    {id, url, published, title, summary, upvotes, github, stars}."""
    if not query or not query.strip():
        return "NO RESULTS"
    clamped_limit = max(1, min(int(limit), 50))
    params = {"q": query.strip(), "limit": clamped_limit}

    def _fetch():
        resp = httpx.get(HF_SEARCH_URL, params=params, timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    try:
        items = with_retry(_fetch, cap=30.0)
        if not isinstance(items, list) or not items:
            return "NO RESULTS"

        records = []
        for item in items:
            paper = item.get("paper") if isinstance(item, dict) else None
            if not paper or not isinstance(paper, dict) or not paper.get("id"):
                continue
            p_id = str(paper["id"])
            url = f"https://huggingface.co/papers/{p_id}"
            published = str(paper.get("publishedAt") or item.get("publishedAt") or "")[:10]
            title = " ".join(str(paper.get("title") or item.get("title") or "").split())
            sum_text = paper.get("ai_summary") or paper.get("summary") or item.get("summary") or ""
            summary = " ".join(str(sum_text).split())[:600]
            upvotes = int(paper.get("upvotes") or 0)
            github = str(paper.get("githubRepo") or "")
            stars = int(paper.get("githubStars") or 0)

            records.append({
                "id": p_id,
                "url": url,
                "published": published,
                "title": title,
                "summary": summary,
                "upvotes": upvotes,
                "github": github,
                "stars": stars,
            })

        if not records:
            return "NO RESULTS"
        return json.dumps(records, ensure_ascii=False)
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


# ---- TODO 4: web search / fetch through the Exa MCP endpoint ----
def _call_exa_mcp(tool_name: str, arguments: dict) -> str:
    """Send JSON-RPC tools/call to Exa MCP endpoint, handle SSE and rate limits, and redact API key on errors."""
    exa_key = (os.getenv("EXA_API_KEY") or "").strip()
    endpoint = f"{EXA_URL}?exaApiKey={exa_key}" if exa_key else EXA_URL
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if exa_key:
        headers["Authorization"] = f"Bearer {exa_key}"

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }

    def _post():
        resp = httpx.post(endpoint, json=payload, headers=headers, timeout=45.0)
        resp.raise_for_status()

        # Parse SSE or direct JSON
        text = resp.text
        data = None
        for line in text.splitlines():
            line_str = line.strip()
            if line_str.startswith("data:"):
                try:
                    data = json.loads(line_str[len("data:"):].strip())
                    break
                except json.JSONDecodeError:
                    continue
        if data is None:
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                raise RuntimeError(f"Cannot parse MCP response: {text[:300]}")

        if data.get("error"):
            err = data["error"]
            err_msg = str(err.get("message") or err)
            if "rate limit" in err_msg.lower() or err.get("code") == -32000:
                raise RetryableError(f"Exa rate limited: {err_msg}", retry_after=20.0)
            raise RuntimeError(f"Exa error: {err_msg}")

        result = data.get("result") or {}
        is_error = result.get("isError") is True
        meta = result.get("_meta") or {}
        if meta.get("rate_limited") or meta.get("rateLimited") or "rate limit" in str(meta).lower():
            raise RetryableError("Exa rate limited (_meta)", retry_after=20.0)

        texts = []
        for item in result.get("content", []):
            if isinstance(item, dict) and item.get("type") == "text":
                txt = item.get("text", "")
                if "free mcp rate limit" in txt.lower() or "hit exa's" in txt.lower():
                    raise RetryableError(f"Exa rate limited in content: {txt[:200]}", retry_after=20.0)
                texts.append(txt)
            elif isinstance(item, str):
                texts.append(item)

        content_str = "\n\n".join(texts).strip()
        if is_error:
            if "rate limit" in content_str.lower():
                raise RetryableError(f"Exa rate limited in content: {content_str[:200]}", retry_after=20.0)
            raise RuntimeError(content_str or "Exa tool execution error")

        return content_str

    try:
        return with_retry(_post, cap=60.0)
    except Exception as exc:
        msg = str(exc)
        if exa_key:
            msg = msg.replace(exa_key, "<REDACTED>")
        raise RuntimeError(msg) from None


@tool
def web_search(query: str, objective: str = "", num_results: int = 5) -> str:
    """Search the web (Exa). Describe the ideal page in natural language. Returns clean text of the top results with URLs."""
    if not query or not query.strip():
        return "NO RESULTS"
    obj = objective.strip() if objective and objective.strip() else f"Find research papers and articles about {query}"
    count = max(1, min(int(num_results), 10))

    try:
        text = _call_exa_mcp("web_search_exa", {"query": query.strip(), "objective": obj, "numResults": count})
        if not text:
            return "NO RESULTS"
        return text
    except Exception as exc:
        exa_key = (os.getenv("EXA_API_KEY") or "").strip()
        msg = str(exc)
        if exa_key:
            msg = msg.replace(exa_key, "<REDACTED>")
        return f"ERROR: {type(exc).__name__}: {msg}"


@tool
def web_fetch(url: str) -> str:
    """Read the full content of one web page (e.g. an arXiv abstract page) as markdown. Long pages are truncated."""
    if not url or not url.strip():
        return "NO RESULTS"

    try:
        text = _call_exa_mcp("web_fetch_exa", {"urls": [url.strip()]})
        if not text:
            return "NO RESULTS"
        return text[:12000]
    except Exception as exc:
        exa_key = (os.getenv("EXA_API_KEY") or "").strip()
        msg = str(exc)
        if exa_key:
            msg = msg.replace(exa_key, "<REDACTED>")
        return f"ERROR: {type(exc).__name__}: {msg}"




# ---- TODO 5: registry (the researcher subagent gets exactly these) ----
SOURCE_TOOLS = [arxiv_search, hf_daily_papers, hf_search_papers, web_search, web_fetch]


if __name__ == "__main__":
    for name, fn, args in [
        ("arxiv_search", arxiv_search, {"query": "world model", "max_results": 3}),
        ("hf_daily_papers", hf_daily_papers, {"limit": 20}),
        ("hf_search_papers", hf_search_papers, {"query": "world model", "limit": 3}),
        ("web_search", web_search, {"query": "survey paper on world models", "num_results": 2}),
        ("web_fetch", web_fetch, {"url": "https://arxiv.org/abs/1803.10122"}),
    ]:
        try:
            print(f"== {name}\n{fn.invoke(args)[:400]}\n")
        except NotImplementedError as exc:
            print(f"== {name}: not implemented yet ({exc})\n")
