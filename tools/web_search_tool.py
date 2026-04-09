from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict

from langchain.tools import tool
from pydantic import BaseModel, Field


class WebSearchInput(BaseModel):
    query: str = Field(
        description=(
            "One narrow search query for a single topic. Keep it focused; run separate "
            "searches for separate topics."
        )
    )
    start_date: str | None = Field(
        default=None,
        description="Optional absolute lower date bound in YYYY-MM-DD.",
    )
    country: str | None = Field(
        default=None,
        description=(
            "Optional country to prioritize. Use this whenever the correct answer depends "
            "on a specific country or should prefer sources/results from a specific "
            "country. This is a general search-scope parameter for any topic, not a "
            "topic-specific option. Pass the lowercase English country name when the "
            "country is known, e.g. 'czech republic', 'united states', or 'germany'."
        ),
    )


def create_web_search_tool() -> Any:
    max_search_results = max(1, min(int(os.getenv("WEB_SEARCH_MAX_RESULTS", "6")), 10))

    @tool(args_schema=WebSearchInput)
    def web_search(query: str, start_date: str | None = None, country: str | None = None) -> str:
        """Search the web for one narrow topic and return source URLs/snippets.

        Required args:
        - query: one short search query for a single topic.

        Optional args:
        - start_date: absolute lower date bound in YYYY-MM-DD.
        - country: country to prioritize, e.g. "czech republic" or "united states".

        Returns:
        - normalized result list with URLs, titles, and snippets.

        Query rules:
        - keep query short: 1 to 4 words is preferred
        - search only one topic/entity per call
        - do not mix multiple goals like news + trials + FDA + treatments in one query
        - if you need broader coverage, run multiple separate web_search calls
        - if the correct answer depends on a specific country, pass country
        - country is a general search-scope parameter for any topic, not a query keyword or topic-specific hack
        - pass the lowercase English country name when known, e.g. country="czech republic"
        - if the query is about news, headlines, latest, recent, or updates, prefer start_date
        - when start_date is used, prefer news-style queries, not evergreen archive queries

        Notes:
        - requires TAVILY_API_KEY.
        - use parse_websites on returned URLs for grounded extraction.
        - when start_date is provided, this tool uses Tavily topic="news"
        - Tavily country boosting only works with topic="general"; when country is provided, this tool uses topic="general"

        Examples:
        - web_search(query="OpenAI announcements")
        - web_search(query="cancer trials")
        - web_search(query="openai announcements", start_date="2026-03-01")
        - web_search(query="android jobs", country="czech republic")
        """
        tavily_api_key = (os.getenv("TAVILY_API_KEY", "") or "").strip()
        if not tavily_api_key:
            return "Missing API key. Set TAVILY_API_KEY."

        try:
            from langchain_tavily import TavilySearch
        except Exception:
            return "Missing dependency. Install `langchain-tavily` and restart the app."

        search_depth = (os.getenv("TAVILY_SEARCH_DEPTH", "advanced") or "").strip().lower()
        if search_depth not in {"basic", "advanced"}:
            search_depth = "advanced"

        topic = (os.getenv("TAVILY_TOPIC", "general") or "").strip().lower()
        if topic not in {"general", "news", "finance"}:
            topic = "general"

        normalized_start_date = (start_date or "").strip()
        if normalized_start_date:
            try:
                datetime.strptime(normalized_start_date, "%Y-%m-%d")
            except ValueError:
                return "web_search failed: start_date must use YYYY-MM-DD."
            topic = "news"

        normalized_country = " ".join((country or "").split()).strip().lower()
        if normalized_country:
            # Tavily supports country boosting only with the general topic, so
            # country takes precedence over the news-topic default.
            topic = "general"

        query_text = " ".join((query or "").split())
        query_terms = [part for part in re.split(r"\s+", query_text) if part]
        if not query_terms:
            return "web_search failed: query is required."

        include_domains = [d.strip() for d in (os.getenv("TAVILY_INCLUDE_DOMAINS", "") or "").split(",") if d.strip()]
        exclude_domains = [d.strip() for d in (os.getenv("TAVILY_EXCLUDE_DOMAINS", "") or "").split(",") if d.strip()]
        max_results = max(1, min(int(os.getenv("WEB_SEARCH_MAX_RESULTS", str(max_search_results))), 10))

        tavily_tool = TavilySearch(
            max_results=max_results,
            topic=topic,
            search_depth=search_depth,
            include_answer=False,
            include_raw_content=False,
        )
        tool_input: Dict[str, Any] = {"query": query_text}
        if normalized_start_date:
            tool_input["start_date"] = normalized_start_date
        if normalized_country:
            tool_input["country"] = normalized_country
        if include_domains:
            tool_input["include_domains"] = include_domains
        if exclude_domains:
            tool_input["exclude_domains"] = exclude_domains

        country_fallback_used = False
        try:
            raw = tavily_tool.invoke(tool_input)
        except Exception as exc:
            return f"web_search failed (Tavily). error={exc}"

        response_obj: Dict[str, Any] = {}
        if isinstance(raw, dict):
            response_obj = raw
        elif isinstance(raw, str):
            text = raw.strip()
            if text.startswith("{") and text.endswith("}"):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        response_obj = parsed
                except Exception:
                    response_obj = {}
        elif hasattr(raw, "content"):
            content = getattr(raw, "content", "")
            if isinstance(content, str):
                text = content.strip()
                if text.startswith("{") and text.endswith("}"):
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            response_obj = parsed
                    except Exception:
                        response_obj = {}

        results = response_obj.get("results") or []
        if normalized_country and not results:
            fallback_input = dict(tool_input)
            fallback_input.pop("country", None)
            try:
                fallback_raw = tavily_tool.invoke(fallback_input)
            except Exception:
                fallback_raw = None
            fallback_response_obj: Dict[str, Any] = {}
            if isinstance(fallback_raw, dict):
                fallback_response_obj = fallback_raw
            elif isinstance(fallback_raw, str):
                fallback_text = fallback_raw.strip()
                if fallback_text.startswith("{") and fallback_text.endswith("}"):
                    try:
                        fallback_parsed = json.loads(fallback_text)
                        if isinstance(fallback_parsed, dict):
                            fallback_response_obj = fallback_parsed
                    except Exception:
                        fallback_response_obj = {}
            elif hasattr(fallback_raw, "content"):
                fallback_content = getattr(fallback_raw, "content", "")
                if isinstance(fallback_content, str):
                    fallback_text = fallback_content.strip()
                    if fallback_text.startswith("{") and fallback_text.endswith("}"):
                        try:
                            fallback_parsed = json.loads(fallback_text)
                            if isinstance(fallback_parsed, dict):
                                fallback_response_obj = fallback_parsed
                        except Exception:
                            fallback_response_obj = {}

            fallback_results = fallback_response_obj.get("results") or []
            if fallback_results:
                response_obj = fallback_response_obj
                results = fallback_results
                country_fallback_used = True

        lines = [f"query={query_text}", f"topic={topic}"]
        if normalized_start_date:
            lines.append(f"start_date={normalized_start_date}")
        if normalized_country:
            lines.append(f"country={normalized_country}")
        if country_fallback_used:
            lines.append("country_fallback=without_country_no_results")
        lines.append("sources:")
        if results:
            seen = set()
            count = 0
            for item in results:
                url = str((item or {}).get("url", "")).strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                count += 1
                title = " ".join(str((item or {}).get("title", "")).split())
                content = " ".join(str((item or {}).get("content", "")).split())
                lines.append(f"{count}. {url}")
                if title:
                    lines.append(f"   title: {title[:180]}")
                if content:
                    lines.append(f"   snippet: {content[:280]}")
                if count >= max_results:
                    break
            if count == 0:
                lines.append("(no URLs returned)")
        else:
            lines.append("(no URLs returned)")

        lines.append("\nNote: Use parse_websites on selected URLs for grounded summaries.")
        return "\n".join(lines)

    return web_search
