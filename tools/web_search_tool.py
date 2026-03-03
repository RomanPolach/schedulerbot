from __future__ import annotations

import json
import os
from typing import Any, Dict

from langchain.tools import tool


def create_web_search_tool() -> Any:
    max_search_results = max(1, min(int(os.getenv("WEB_SEARCH_MAX_RESULTS", "6")), 10))

    @tool
    def web_search(query: str, include_model_answer: bool = False) -> str:
        """Search the web via LangChain TavilySearch and return source URLs/snippets."""
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

        time_range = (os.getenv("TAVILY_TIME_RANGE", "") or "").strip().lower()
        include_domains = [d.strip() for d in (os.getenv("TAVILY_INCLUDE_DOMAINS", "") or "").split(",") if d.strip()]
        exclude_domains = [d.strip() for d in (os.getenv("TAVILY_EXCLUDE_DOMAINS", "") or "").split(",") if d.strip()]
        max_results = max(1, min(int(os.getenv("WEB_SEARCH_MAX_RESULTS", str(max_search_results))), 10))

        tavily_tool = TavilySearch(
            max_results=max_results,
            topic=topic,
            search_depth=search_depth,
            include_answer=bool(include_model_answer),
            include_raw_content=False,
        )
        tool_input: Dict[str, Any] = {"query": query}
        if time_range:
            tool_input["time_range"] = time_range
        if include_domains:
            tool_input["include_domains"] = include_domains
        if exclude_domains:
            tool_input["exclude_domains"] = exclude_domains

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
        lines = [f"query={query}", "sources:"]
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

        if include_model_answer:
            answer = " ".join(str(response_obj.get("answer", "")).split()).strip()
            if answer:
                lines.append("\nmodel_answer:")
                lines.append(answer[:3000])

        lines.append("\nNote: Use parse_websites on selected URLs for grounded summaries.")
        return "\n".join(lines)

    return web_search

