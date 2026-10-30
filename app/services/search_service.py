"""Web search for the "belajar X" flow.

Providers, tried in order of quality:
1. Tavily - purpose-built for LLM research, returns cleaned content.
2. SearXNG - self-hosted meta search when ``SEARXNG_BASE_URL`` is set.
3. DuckDuckGo - keyless fallback via the ``ddgs`` package.

Results are normalised to a single shape so the summariser does not care where
they came from. Fetched page text is truncated hard before it ever reaches a
prompt: research is the easiest place to accidentally spend a fortune.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.core.errors import ToolError
from app.core.logging_setup import get_logger

log = get_logger(__name__)

MAX_RESULTS = 8
MAX_PAGE_CHARS = 6000
FETCH_CONCURRENCY = 4
USER_AGENT = "Mozilla/5.0 (compatible; PersonalAgentBot/1.0; +https://example.local)"


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    content: str = ""
    provider: str = ""
    score: float = 0.0

    def as_source_line(self) -> str:
        return f"- [{self.title[:90]}]({self.url})"


@dataclass
class ResearchBundle:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)
    fetched_pages: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def ok(self) -> bool:
        return bool(self.results)

    def sources(self) -> list[str]:
        return [result.url for result in self.results]

    def context(self, max_chars: int = 12000) -> str:
        """Compact evidence block for the summariser, bounded in size."""
        blocks: list[str] = []
        used = 0
        for index, result in enumerate(self.results, start=1):
            body = (result.content or result.snippet or "").strip()
            body = re.sub(r"\s+", " ", body)
            if not body:
                continue
            entry = f"[{index}] {result.title}\nURL: {result.url}\n{body[:1800]}"
            if used + len(entry) > max_chars:
                break
            blocks.append(entry)
            used += len(entry)
        return "\n\n".join(blocks)


class SearchService:
    def __init__(self, settings):
        self.settings = settings

    async def search(self, query: str, *, max_results: int = MAX_RESULTS, deep: bool = True) -> ResearchBundle:
        bundle = ResearchBundle(query=query)
        errors: list[str] = []

        for provider, fn in (
            ("tavily", self._tavily),
            ("searxng", self._searxng),
            ("duckduckgo", self._duckduckgo),
        ):
            try:
                results = await fn(query, max_results)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{provider}: {exc}")
                log.warning("search provider %s failed: %s", provider, exc)
                continue
            if results:
                bundle.results = results
                bundle.providers_used.append(provider)
                break

        if not bundle.results:
            raise ToolError("Pencarian gagal di semua provider: " + "; ".join(errors)[:400])

        if deep:
            await self._enrich(bundle)
        return bundle

    # ------------------------------------------------------------ providers
    async def _tavily(self, query: str, max_results: int) -> list[SearchResult]:
        if not self.settings.tavily_api_key:
            return []
        payload = {
            "api_key": self.settings.tavily_api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "advanced",
            "include_answer": False,
            "include_raw_content": False,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post("https://api.tavily.com/search", json=payload)
            response.raise_for_status()
            data = response.json()
        return [
            SearchResult(
                title=str(item.get("title") or "")[:200],
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or "")[:2000],
                provider="tavily",
                score=float(item.get("score") or 0.0),
            )
            for item in data.get("results", [])
            if item.get("url")
        ]

    async def _searxng(self, query: str, max_results: int) -> list[SearchResult]:
        base = (self.settings.searxng_base_url or "").rstrip("/")
        if not base:
            return []
        params = {"q": query, "format": "json", "language": "id-ID,en-US"}
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.get(f"{base}/search", params=params)
            response.raise_for_status()
            data = response.json()
        return [
            SearchResult(
                title=str(item.get("title") or "")[:200],
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or "")[:2000],
                provider="searxng",
            )
            for item in data.get("results", [])[:max_results]
            if item.get("url")
        ]

    async def _duckduckgo(self, query: str, max_results: int) -> list[SearchResult]:
        def _run() -> list[SearchResult]:
            try:
                from ddgs import DDGS  # type: ignore
            except ImportError:
                from duckduckgo_search import DDGS  # type: ignore

            out: list[SearchResult] = []
            with DDGS() as ddgs:
                for item in ddgs.text(query, max_results=max_results, region="wt-wt"):
                    url = item.get("href") or item.get("url") or ""
                    if not url:
                        continue
                    out.append(
                        SearchResult(
                            title=str(item.get("title") or "")[:200],
                            url=url,
                            snippet=str(item.get("body") or "")[:2000],
                            provider="duckduckgo",
                        )
                    )
            return out

        return await asyncio.to_thread(_run)

    # -------------------------------------------------------------- enrich
    async def _enrich(self, bundle: ResearchBundle) -> None:
        """Fetch page text for the top results so the summary is grounded."""
        semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)
        targets = bundle.results[:5]

        async def fetch(result: SearchResult) -> None:
            async with semaphore:
                try:
                    text = await self.fetch_page(result.url)
                    if text:
                        result.content = text
                        bundle.fetched_pages += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("fetch failed %s: %s", result.url, exc)

        await asyncio.gather(*(fetch(result) for result in targets))

    async def fetch_page(self, url: str, *, max_chars: int = MAX_PAGE_CHARS) -> str:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type and "text" not in content_type:
                return ""
            return self.extract_text(response.text)[:max_chars]

    @staticmethod
    def extract_text(html: str) -> str:
        """Strip a page down to readable text without pulling in a heavy dep."""
        html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
        html = re.sub(r"(?is)<br\s*/?>", "\n", html)
        html = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", html)
        text = re.sub(r"(?s)<[^>]+>", " ", html)
        text = _unescape(text)
        lines = [line.strip() for line in text.splitlines()]
        lines = [line for line in lines if len(line) > 30]
        joined = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", joined)


def _unescape(text: str) -> str:
    import html as html_module

    return html_module.unescape(text)


SUMMARY_SYSTEM = """Kamu peneliti yang rapi dan jujur.

Tugas: rangkum hasil pencarian jadi catatan belajar yang padat, faktual, dan
langsung berguna. Aturan:
- Bahasa Indonesia.
- Struktur: 1 kalimat inti, lalu 3-6 poin kunci, lalu bagian "Catatan praktis"
  kalau ada yang bisa langsung dipakai.
- Sebutkan angka/fakta konkret dari sumber. Jangan mengarang.
- Kalau sumber saling bertentangan, tulis perbedaannya.
- Maksimal 350 kata.
- Jangan menulis "berdasarkan hasil pencarian" - langsung ke isinya.
- Sertakan nomor rujukan seperti [1], [2] pada klaim penting."""


async def summarize_research(bundle: ResearchBundle, gateway, *, user_id: int = 0) -> str:
    """Produce the study note. Falls back to a structured digest without an LLM."""
    if not bundle.ok:
        raise ToolError("Tidak ada hasil untuk dirangkum")

    if gateway is not None and gateway.available():
        try:
            result = await gateway.complete(
                [
                    {"role": "system", "content": SUMMARY_SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"Topik: {bundle.query}\n\n"
                            f"Sumber:\n{bundle.context()}\n\n"
                            "Tulis catatan belajarnya."
                        ),
                    },
                ],
                tier="big",
                user_id=user_id,
                temperature=0.2,
                max_tokens=1400,
            )
            summary = result.text.strip()
            if summary:
                return summary
        except Exception as exc:  # noqa: BLE001
            log.warning("research summarisation failed: %s", exc)

    return _fallback_digest(bundle)


def _fallback_digest(bundle: ResearchBundle) -> str:
    lines = [f"Ringkasan cepat: {bundle.query}", ""]
    for index, result in enumerate(bundle.results[:6], start=1):
        body = re.sub(r"\s+", " ", (result.content or result.snippet or "")).strip()
        lines.append(f"{index}. {result.title}")
        if body:
            lines.append(f"   {body[:300]}")
    lines.append("")
    lines.append("Sumber:")
    lines.extend(f"- {url}" for url in bundle.sources()[:6])
    return "\n".join(lines)


async def learn_topic(
    query: str,
    *,
    search: SearchService,
    gateway,
    memory,
    user_id: int,
    progress=None,
) -> dict:
    """Full /belajar pipeline: search -> fetch -> summarise -> persist."""
    if progress:
        await progress(10, f"Mencari sumber tentang: {query}")
    bundle = await search.search(query, deep=True)
    if progress:
        await progress(45, f"Ketemu {len(bundle.results)} sumber, membaca isinya")

    summary = await summarize_research(bundle, gateway, user_id=user_id)
    if progress:
        await progress(80, "Menyimpan ke memori permanen")

    await memory.remember_learning(user_id, query, summary, bundle.sources())
    await memory.remember_topic(user_id, summary, topic=query, source="research")

    note_path = await _write_note(memory, query, summary, bundle)
    if progress:
        await progress(100, "Selesai")

    return {
        "topic": query,
        "summary": summary,
        "sources": bundle.sources(),
        "providers": bundle.providers_used,
        "fetched_pages": bundle.fetched_pages,
        "note_path": str(note_path),
    }


async def _write_note(memory, query: str, summary: str, bundle: ResearchBundle) -> Path:
    from app.core.utils import run_blocking

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", query.lower()).strip("-")[:50] or "catatan"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = Path(memory.settings.outputs_path) / "research" / f"{stamp}-{slug}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"# {query}\n\n_Dibuat: {datetime.now(timezone.utc).isoformat()}_\n\n"
        f"{summary}\n\n## Sumber\n"
        + "\n".join(f"- {url}" for url in bundle.sources())
        + "\n"
    )
    await run_blocking(target.write_text, body, encoding="utf-8")
    return target
