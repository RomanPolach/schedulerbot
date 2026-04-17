from __future__ import annotations

import asyncio
import atexit
import json
import os
import re
import threading
import traceback
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from langchain.tools import tool
from playwright.async_api import Browser
from playwright.async_api import BrowserContext
from playwright.async_api import Playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from ..runtime_config import MAX_SITE_CONTENT_CHARS

_EVENT_LOOP_POLICY_LOCK = threading.Lock()
_INLINE_URL_AT_END_RE = re.compile(r"\((https?://[^()\s]+)\)$")
_VALID_WAIT_UNTIL = {"commit", "domcontentloaded", "load", "networkidle"}
_ALLOWED_RESOURCE_TYPES = {
    "document",
    "stylesheet",
    "image",
    "media",
    "font",
    "script",
    "xhr",
    "fetch",
    "eventsource",
    "websocket",
    "manifest",
    "texttrack",
    "other",
}
_PREFERRED_COLLECTION_KEYS = ("member", "items", "results", "data")


def _parse_reuse_browser_enabled() -> bool:
    return (os.getenv("PLAYWRIGHT_REUSE_BROWSER", "true") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_render_wait_ms() -> int:
    return max(0, min(int(os.getenv("PLAYWRIGHT_RENDER_WAIT_MS", "900")), 15_000))


def _resolve_page_timeout_ms() -> int:
    timeout_seconds = float(os.getenv("PLAYWRIGHT_PAGE_TIMEOUT_SECONDS", "15"))
    return max(3_000, min(int(timeout_seconds * 1000), 180_000))


def _resolve_max_concurrency() -> int:
    return max(1, min(int(os.getenv("PLAYWRIGHT_MAX_CONCURRENCY", "4")), 8))


def _resolve_warmup_timeout_seconds() -> float:
    return max(5.0, min(float(os.getenv("PLAYWRIGHT_STARTUP_TIMEOUT_SECONDS", "30")), 120.0))


def _resolve_wait_until() -> str:
    raw = (os.getenv("PLAYWRIGHT_WAIT_UNTIL", "domcontentloaded") or "").strip().lower()
    return raw if raw in _VALID_WAIT_UNTIL else "domcontentloaded"


def _resolve_block_resource_types() -> set[str]:
    raw = (os.getenv("PLAYWRIGHT_BLOCK_RESOURCE_TYPES", "image,media,font") or "").strip().lower()
    if not raw:
        return set()
    resource_types = {part.strip() for part in raw.split(",") if part.strip()}
    return {item for item in resource_types if item in _ALLOWED_RESOURCE_TYPES}


def _looks_like_low_value_tail_line(line: str) -> bool:
    text = " ".join((line or "").split()).strip()
    if not text:
        return True

    lowered = text.lower()
    if lowered in {"o nás", "blog", "cookies", "kontakt", "osobní údaje", "pro média"}:
        return True

    match = _INLINE_URL_AT_END_RE.search(text)
    if match:
        label = text[: match.start()].strip()
        parsed = urlparse(match.group(1))
        depth = len([segment for segment in parsed.path.split("/") if segment])
        return len(label) <= 40 or (len(label) <= 60 and depth <= 1)

    return len(text) <= 60


def _trim_low_value_tail_lines(text: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 4:
        return "\n".join(lines)

    trimmed = list(lines)
    removed = 0
    while trimmed and _looks_like_low_value_tail_line(trimmed[-1]):
        trimmed.pop()
        removed += 1

    # Avoid stripping a legitimate short ending line unless it is clearly a footer cluster.
    if removed < 3:
        return "\n".join(lines)
    return "\n".join(trimmed)


def _root_domain(host: str | None) -> str:
    value = str(host or "").strip().lower()
    if not value:
        return ""
    parts = [part for part in value.split(".") if part]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return value


def _hosts_related(host_a: str | None, host_b: str | None) -> bool:
    a = str(host_a or "").strip().lower()
    b = str(host_b or "").strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    if a.endswith(f".{b}") or b.endswith(f".{a}"):
        return True
    return _root_domain(a) == _root_domain(b)


def _find_json_collection(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    max_depth: int = 4,
) -> tuple[str, list[Any], Optional[int], bool] | None:
    if depth > max_depth:
        return None

    if isinstance(value, list):
        if value and any(isinstance(item, dict) for item in value):
            return path, value, len(value), False
        return None

    if not isinstance(value, dict):
        return None

    for key in _PREFERRED_COLLECTION_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, list) and candidate and any(isinstance(item, dict) for item in candidate):
            total_items = value.get("totalItems")
            has_explicit_total = isinstance(total_items, int)
            total = int(total_items) if has_explicit_total else len(candidate)
            return f"{path}.{key}", candidate, total, has_explicit_total

    for key, candidate in value.items():
        if not isinstance(candidate, (dict, list)):
            continue
        found = _find_json_collection(
            candidate,
            path=f"{path}.{key}",
            depth=depth + 1,
            max_depth=max_depth,
        )
        if found is not None:
            return found

    return None


def _serialize_preview_item(item: Any, max_chars: int = 520) -> str:
    try:
        raw = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        raw = str(item)
    compact = " ".join(raw.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[: max_chars - 3].rstrip()}..."


def _summarize_json_collection(source_url: str, payload: Any) -> str | None:
    found = _find_json_collection(payload)
    if found is None:
        return None

    path, items, total, has_explicit_total = found
    if not has_explicit_total:
        return None
    dict_items = [item for item in items if isinstance(item, dict)]
    if not dict_items:
        return None

    previews = dict_items[: min(8, len(dict_items))]
    lines = [
        f"Source: {source_url}",
        f"Collection path: {path}",
        f"Total items: {total}",
        "Preview items:",
    ]
    for idx, item in enumerate(previews, start=1):
        lines.append(f"{idx}. {_serialize_preview_item(item)}")
    return "\n".join(lines)


class _PlaywrightBrowserService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ready_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._startup_error: Optional[str] = None
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

    def start(self, timeout_seconds: float) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                pass
            else:
                self._ready_event.clear()
                self._startup_error = None
                self._thread = threading.Thread(
                    target=self._thread_main,
                    name="playwright-browser-service",
                    daemon=True,
                )
                self._thread.start()

        if not self._ready_event.wait(timeout=timeout_seconds):
            raise RuntimeError("Timed out while starting shared Playwright browser.")
        if self._startup_error:
            raise RuntimeError(self._startup_error)

    def run(self, coro: Any, timeout_seconds: float) -> Any:
        self.start(timeout_seconds=timeout_seconds)
        if not self._loop:
            raise RuntimeError("Shared Playwright event loop is unavailable.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError("Playwright operation timed out.") from exc

    def warmup(self, timeout_seconds: float) -> None:
        self.run(self.ensure_browser_context(), timeout_seconds=timeout_seconds)

    def shutdown(self) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread

        if loop and loop.is_running():
            try:
                close_future = asyncio.run_coroutine_threadsafe(self._close_resources(), loop)
                close_future.result(timeout=10)
            except Exception:
                pass
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

        if thread and thread.is_alive():
            thread.join(timeout=2)

        with self._lock:
            self._loop = None
            self._thread = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._startup_error = None
            self._ready_event.clear()

    async def ensure_browser_context(self) -> BrowserContext:
        if self._context is not None:
            return self._context
        await self._close_resources()
        await self._initialize_resources()
        if self._context is None:
            raise RuntimeError("Shared Playwright browser context initialization failed.")
        return self._context

    async def new_isolated_context(self) -> BrowserContext:
        if self._browser is None:
            await self.ensure_browser_context()
        if self._browser is None:
            raise RuntimeError("Shared Playwright browser initialization failed.")
        return await self._browser.new_context(
            user_agent=self._user_agent,
            ignore_https_errors=True,
        )

    async def reset_browser_context(self) -> BrowserContext:
        await self._close_resources()
        await self._initialize_resources()
        if self._context is None:
            raise RuntimeError("Shared Playwright browser context re-initialization failed.")
        return self._context

    def _thread_main(self) -> None:
        try:
            if os.name == "nt" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
                with _EVENT_LOOP_POLICY_LOCK:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.create_task(self._startup())
            loop.run_forever()
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc!r}"
            self._ready_event.set()
        finally:
            if self._loop:
                try:
                    self._loop.close()
                except Exception:
                    pass

    async def _startup(self) -> None:
        try:
            await self._initialize_resources()
        except Exception as exc:
            self._startup_error = f"{type(exc).__name__}: {exc!r}"
        finally:
            self._ready_event.set()

    async def _initialize_resources(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self._user_agent,
            ignore_https_errors=True,
        )

    async def _close_resources(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._context = None
        self._browser = None
        self._playwright = None


_BROWSER_SERVICE = _PlaywrightBrowserService()
atexit.register(_BROWSER_SERVICE.shutdown)
_SHARED_PARSE_SESSION_LOCK = threading.Lock()


def _run_function_in_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
    result: Dict[str, Any] = {}
    err: Dict[str, str] = {}

    def _runner() -> None:
        previous_policy = None
        policy_changed = False
        try:
            if os.name == "nt" and hasattr(asyncio, "WindowsProactorEventLoopPolicy"):
                with _EVENT_LOOP_POLICY_LOCK:
                    previous_policy = asyncio.get_event_loop_policy()
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                    policy_changed = True
            result["value"] = func(*args, **kwargs)
        except Exception as exc:
            err["message"] = f"{type(exc).__name__}: {exc!r}"
            err["trace"] = traceback.format_exc(limit=6)
        finally:
            if policy_changed and previous_policy is not None:
                try:
                    with _EVENT_LOOP_POLICY_LOCK:
                        asyncio.set_event_loop_policy(previous_policy)
                except Exception:
                    pass

    thread = threading.Thread(target=_runner, name="playwright-parser", daemon=True)
    thread.start()
    thread.join()

    if err:
        raise RuntimeError(f"{err['message']}\n{err['trace']}")
    return result.get("value")


async def _parse_single_website_with_playwright(
    context: Any,
    url: str,
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    wait_until: str,
) -> str:
    normalized = url if url.startswith(("http://", "https://")) else f"https://{url}"
    target_host = (urlparse(normalized).hostname or "").strip().lower()
    response_tasks: list[asyncio.Task[Any]] = []
    response_summaries: list[str] = []
    seen_summary_sources: set[str] = set()
    xhr_fetch_response_count = 0

    page = await context.new_page()
    try:
        async def _capture_json_response(response: Any) -> None:
            try:
                request = response.request
                if request.resource_type not in {"xhr", "fetch"}:
                    return
                if response.status >= 400:
                    return

                response_url = str(response.url or "")
                parsed = urlparse(response_url)
                response_host = (parsed.hostname or "").strip().lower()
                if not _hosts_related(target_host, response_host):
                    return

                response_path = (parsed.path or "").lower()
                content_type = str(response.headers.get("content-type", "")).lower()
                is_json_like = (
                    "json" in content_type
                    or "/api/" in response_path
                    or response_path.endswith(".json")
                )
                if not is_json_like:
                    return

                payload_text = await response.text()
                payload = json.loads(payload_text)
                summary = _summarize_json_collection(response_url, payload)
                if not summary:
                    return

                source_key = f"{response_url}|{hash(summary)}"
                if source_key in seen_summary_sources:
                    return
                seen_summary_sources.add(source_key)
                response_summaries.append(summary)
            except Exception:
                return

        def _on_response(response: Any) -> None:
            nonlocal xhr_fetch_response_count
            try:
                request = response.request
                if request.resource_type not in {"xhr", "fetch"}:
                    return
            except Exception:
                return
            xhr_fetch_response_count += 1
            if len(response_tasks) >= 48:
                return
            response_tasks.append(asyncio.create_task(_capture_json_response(response)))

        page.on("response", _on_response)

        try:
            await page.goto(normalized, wait_until=wait_until, timeout=timeout_ms)
        except PlaywrightTimeoutError:
            # Some pages keep background requests open after the usable content is visible.
            # Continue with the DOM that already loaded instead of discarding the page.
            if page.url == "about:blank":
                return (
                    f"URL: {normalized}\n"
                    f"Error: Timeout while waiting for page load within {timeout_ms} ms."
                )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=min(timeout_ms, max(1200, min(render_wait_ms * 8, 3000))),
            )
        except Exception:
            # Keep fast-path behavior: use whatever loaded so far.
            pass

        if render_wait_ms:
            await page.wait_for_timeout(render_wait_ms)
        if response_tasks:
            await asyncio.gather(*response_tasks, return_exceptions=True)

        final_url = page.url
        title = (await page.title()) or "(no title)"

        async def _extract_visible_content() -> tuple[str, int]:
            extraction = await page.evaluate(
                """() => {
                const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
                const blockTags = new Set([
                    'article', 'section', 'div', 'p', 'li', 'ul', 'ol', 'table', 'tr',
                    'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'main', 'body',
                    'header', 'footer', 'nav', 'aside', 'br'
                ]);

                const isHidden = (el) => {
                    if (!(el instanceof Element)) {
                        return false;
                    }
                    if (el.hidden || el.getAttribute('aria-hidden') === 'true') {
                        return true;
                    }
                    const style = window.getComputedStyle(el);
                    return style.display === 'none' || style.visibility === 'hidden';
                };

                const shouldDropElement = (el) => {
                    if (!(el instanceof Element)) {
                        return false;
                    }
                    const tag = el.tagName.toLowerCase();
                    return ['script', 'style', 'noscript', 'svg', 'canvas', 'iframe'].includes(tag);
                };

                const root = document.body;
                let keptLinks = 0;
                const currentUrl = (() => {
                    try {
                        const value = new URL(window.location.href);
                        value.hash = '';
                        return value.toString();
                    } catch (error) {
                        return window.location.href;
                    }
                })();

                const escapeTableCell = (value) => normalize(value).replace(/[|]/g, '\\\\|');

                const renderTable = (table) => {
                    const rowSelector = table.tagName.toLowerCase() === 'table'
                        ? ':scope > thead > tr, :scope > tbody > tr, :scope > tfoot > tr, :scope > tr'
                        : ':scope > [role="row"], :scope [role="row"]';
                    const cellSelector = 'th, td, [role="columnheader"], [role="rowheader"], [role="cell"]';
                    const spanValue = (cell, propertyName, attributeName) => {
                        const parsed = Number.parseInt(cell[propertyName] || cell.getAttribute(attributeName) || '1', 10);
                        if (!Number.isFinite(parsed) || parsed < 1) {
                            return 1;
                        }
                        return Math.min(parsed, 50);
                    };
                    const sourceRows = Array.from(table.querySelectorAll(rowSelector))
                        .map((row) => {
                            const cells = Array.from(row.querySelectorAll(cellSelector))
                                .filter((cell) => cell.closest('tr, [role="row"]') === row)
                                .map((cell) => {
                                    const rendered = Array.from(cell.childNodes)
                                        .map((child) => renderNode(child))
                                        .filter(Boolean)
                                        .join(' ');
                                    return {
                                        text: escapeTableCell(rendered || cell.innerText || cell.textContent || ''),
                                        colSpan: spanValue(cell, 'colSpan', 'colspan'),
                                        rowSpan: spanValue(cell, 'rowSpan', 'rowspan')
                                    };
                                });
                            return {
                                cells,
                                isHeader: cells.length > 0 && Array.from(row.children).some((cell) => {
                                    const tag = cell.tagName.toLowerCase();
                                    const role = cell.getAttribute('role');
                                    return tag === 'th' || role === 'columnheader' || role === 'rowheader';
                                })
                            };
                        })
                        .filter((row) => row.cells.length > 0);

                    const activeRowSpans = [];
                    const rows = sourceRows.map((row) => {
                        const cells = [];
                        let column = 0;

                        const applyActiveRowSpan = () => {
                            while (activeRowSpans[column] && activeRowSpans[column].remainingRows > 0) {
                                const span = activeRowSpans[column];
                                cells[column] = span.text;
                                span.remainingRows -= 1;
                                if (span.remainingRows <= 0) {
                                    activeRowSpans[column] = undefined;
                                }
                                column += 1;
                            }
                        };

                        applyActiveRowSpan();
                        for (const cell of row.cells) {
                            applyActiveRowSpan();
                            for (let offset = 0; offset < cell.colSpan; offset += 1) {
                                const targetColumn = column + offset;
                                const text = offset === 0 ? cell.text : '';
                                cells[targetColumn] = text;
                                if (cell.rowSpan > 1) {
                                    activeRowSpans[targetColumn] = {
                                        text,
                                        remainingRows: cell.rowSpan - 1
                                    };
                                }
                            }
                            column += cell.colSpan;
                        }

                        while (column < activeRowSpans.length) {
                            applyActiveRowSpan();
                            column += 1;
                        }

                        return {
                            cells,
                            isHeader: row.isHeader
                        };
                    });

                    if (!rows.length) {
                        return '';
                    }

                    const maxColumns = Math.max(...rows.map((row) => row.cells.length));
                    if (!maxColumns) {
                        return '';
                    }

                    const normalizedRows = rows.map((row) => ({
                        ...row,
                        cells: row.cells.concat(Array(Math.max(0, maxColumns - row.cells.length)).fill(''))
                    }));
                    const hasHeader = normalizedRows[0].isHeader;
                    const output = [];

                    for (let index = 0; index < normalizedRows.length; index += 1) {
                        const row = normalizedRows[index];
                        output.push(`| ${row.cells.join(' | ')} |`);
                        if (index === 0 && hasHeader) {
                            output.push(`| ${Array(maxColumns).fill('---').join(' | ')} |`);
                        }
                    }

                    return '\\n' + output.join('\\n') + '\\n';
                };

                const renderNode = (node) => {
                    if (!node) {
                        return '';
                    }

                    if (node.nodeType === Node.TEXT_NODE) {
                        return normalize(node.textContent || '');
                    }

                    if (!(node instanceof Element)) {
                        return '';
                    }

                    if (isHidden(node) || shouldDropElement(node)) {
                        return '';
                    }

                    const tag = node.tagName.toLowerCase();
                    if (tag === 'a') {
                        const href = normalize(node.href || node.getAttribute('href') || '');
                        const label = normalize(node.innerText || node.textContent || '');
                        if (!href) {
                            return label;
                        }
                        try {
                            const target = new URL(href, window.location.href);
                            target.hash = '';
                            if (target.toString() === currentUrl && label.length <= 80) {
                                // Ignore control links that point back to the same page.
                                return '';
                            }
                        } catch (error) {}
                        keptLinks += 1;
                        return label ? `${label} (${href})` : `(${href})`;
                    }

                    const role = node.getAttribute('role');
                    if (tag === 'table' || role === 'table' || role === 'grid') {
                        return renderTable(node);
                    }

                    if (tag === 'br') {
                        return '\\n';
                    }

                    const parts = [];
                    for (const child of Array.from(node.childNodes)) {
                        const rendered = renderNode(child);
                        if (rendered) {
                            parts.push(rendered);
                        }
                    }

                    const joined = parts.join(blockTags.has(tag) ? '\\n' : ' ');
                    const cleaned = joined
                        .replace(/[ \\t]*\\n[ \\t]*/g, '\\n')
                        .replace(/\\n{3,}/g, '\\n\\n')
                        .replace(/[ \\t]{2,}/g, ' ')
                        .trim();

                    if (!cleaned) {
                        return '';
                    }

                    return blockTags.has(tag) ? `\\n${cleaned}\\n` : cleaned;
                };

                const rawLines = renderNode(root)
                    .split(/\\n+/)
                    .map((line) => normalize(line))
                    .filter(Boolean);

                const lines = [];
                for (const line of rawLines) {
                    if (!lines.length || lines[lines.length - 1] !== line) {
                        lines.push(line);
                    }
                }

                return {
                    content: lines.join('\\n'),
                    kept_links: keptLinks
                };
            }"""
            )
            content_text = str((extraction or {}).get("content", "")).strip()
            kept_links = int((extraction or {}).get("kept_links", 0) or 0)
            return content_text, kept_links

        text, kept_links = await _extract_visible_content()

        if xhr_fetch_response_count > 0:
            second_pass_wait_ms = min(timeout_ms // 4, max(900, min(render_wait_ms * 3, 3200)))
            if second_pass_wait_ms > 0:
                try:
                    await page.wait_for_load_state(
                        "networkidle",
                        timeout=min(timeout_ms, max(900, second_pass_wait_ms)),
                    )
                except Exception:
                    pass
                await page.wait_for_timeout(second_pass_wait_ms)
                if response_tasks:
                    await asyncio.gather(*response_tasks, return_exceptions=True)
                retry_text, retry_kept_links = await _extract_visible_content()
                if len(retry_text) > len(text):
                    text = retry_text
                    kept_links = retry_kept_links

        should_append_api_snapshots = bool(response_summaries)
        if should_append_api_snapshots:
            api_snapshot = "\n\n".join(response_summaries[:3])
            text = (
                f"{text}\n\nAPI data snapshots:\n{api_snapshot}".strip()
                if text
                else f"API data snapshots:\n{api_snapshot}"
            )

        if len(text) > content_limit:
            text = f"{text[:content_limit].rstrip()}..."

        if not text:
            text = "(no text extracted)"

        return f"URL: {final_url}\nTitle: {title}\nContent:\n{text}"
    except Exception as exc:
        return f"URL: {normalized}\nError: {type(exc).__name__}: {exc!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _parse_websites_with_playwright_async(
    create_context: Any,
    items: List[str],
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    wait_until: str,
    blocked_resource_types: set[str],
    max_concurrency: int,
) -> List[str]:
    semaphore = asyncio.Semaphore(max_concurrency)
    targets = list(items[:10])

    async def _prepare_context(context: BrowserContext) -> None:
        context.set_default_navigation_timeout(timeout_ms)
        context.set_default_timeout(timeout_ms)
        if blocked_resource_types:
            async def _route_handler(route: Any) -> None:
                if route.request.resource_type in blocked_resource_types:
                    await route.abort()
                else:
                    await route.continue_()

            await context.route("**/*", _route_handler)

    async def _worker(index: int, target_url: str) -> tuple[int, str]:
        async with semaphore:
            context = await create_context()
            try:
                await _prepare_context(context)
                chunk = await _parse_single_website_with_playwright(
                    context=context,
                    url=target_url,
                    content_limit=content_limit,
                    timeout_ms=timeout_ms,
                    render_wait_ms=render_wait_ms,
                    wait_until=wait_until,
                )
                return index, chunk
            finally:
                try:
                    await context.close()
                except Exception:
                    pass

    results = await asyncio.gather(*[_worker(i, target) for i, target in enumerate(targets)])
    results.sort(key=lambda x: x[0])
    return [chunk for _, chunk in results]


async def _parse_websites_with_playwright_ephemeral_async(
    items: List[str],
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    wait_until: str,
    blocked_resource_types: set[str],
    max_concurrency: int,
) -> List[str]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            async def _create_context() -> BrowserContext:
                return await browser.new_context(ignore_https_errors=True)

            return await _parse_websites_with_playwright_async(
                create_context=_create_context,
                items=items,
                content_limit=content_limit,
                timeout_ms=timeout_ms,
                render_wait_ms=render_wait_ms,
                wait_until=wait_until,
                blocked_resource_types=blocked_resource_types,
                max_concurrency=max_concurrency,
            )
        finally:
            await browser.close()


def _parse_websites_with_shared_browser(
    items: List[str],
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    wait_until: str,
    blocked_resource_types: set[str],
    max_concurrency: int,
) -> str:
    timeout_seconds = max(10.0, min(float(timeout_ms) / 1000.0 + 30.0, 300.0))
    with _SHARED_PARSE_SESSION_LOCK:
        # Ensure the shared browser process is healthy before creating per-page
        # isolated contexts for this parse request.
        _BROWSER_SERVICE.run(
            _BROWSER_SERVICE.ensure_browser_context(),
            timeout_seconds=timeout_seconds,
        )
        try:
            async def _create_context() -> BrowserContext:
                return await _BROWSER_SERVICE.new_isolated_context()

            chunks = _BROWSER_SERVICE.run(
                _parse_websites_with_playwright_async(
                    create_context=_create_context,
                    items=items,
                    content_limit=content_limit,
                    timeout_ms=timeout_ms,
                    render_wait_ms=render_wait_ms,
                    wait_until=wait_until,
                    blocked_resource_types=blocked_resource_types,
                    max_concurrency=max_concurrency,
                ),
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            # Browser/context can occasionally become invalid after crashes or OOM.
            # Recreate once and retry this parse request from clean per-page contexts.
            _BROWSER_SERVICE.run(
                _BROWSER_SERVICE.reset_browser_context(),
                timeout_seconds=timeout_seconds,
            )
            async def _create_context() -> BrowserContext:
                return await _BROWSER_SERVICE.new_isolated_context()
            chunks = _BROWSER_SERVICE.run(
                _parse_websites_with_playwright_async(
                    create_context=_create_context,
                    items=items,
                    content_limit=content_limit,
                    timeout_ms=timeout_ms,
                    render_wait_ms=render_wait_ms,
                    wait_until=wait_until,
                    blocked_resource_types=blocked_resource_types,
                    max_concurrency=max_concurrency,
                ),
                timeout_seconds=timeout_seconds,
            )
        return "\n\n".join(chunks)


def _parse_websites_with_ephemeral_browser(
    items: List[str],
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    wait_until: str,
    blocked_resource_types: set[str],
    max_concurrency: int,
) -> str:
    chunks = asyncio.run(
        _parse_websites_with_playwright_ephemeral_async(
            items=items,
            content_limit=content_limit,
            timeout_ms=timeout_ms,
            render_wait_ms=render_wait_ms,
            wait_until=wait_until,
            blocked_resource_types=blocked_resource_types,
            max_concurrency=max_concurrency,
        )
    )
    return "\n\n".join(chunks)


def warmup_parse_websites_browser() -> str:
    if not _parse_reuse_browser_enabled():
        return "disabled"
    timeout_seconds = _resolve_warmup_timeout_seconds()
    try:
        _BROWSER_SERVICE.warmup(timeout_seconds=timeout_seconds)
        return "ready"
    except Exception as exc:
        return f"failed: {type(exc).__name__}: {exc}"


def create_parse_websites_tool() -> Any:
    @tool
    def parse_websites(urls: str, max_chars_per_site: int = MAX_SITE_CONTENT_CHARS) -> str:
        """Fetch and parse web pages from multiple URLs.

        Required args:
        - urls: comma-separated or newline-separated URL list.

        Optional args:
        - max_chars_per_site: max extracted characters per site (clamped to configured global limit).

        Returns:
        - per-URL block containing final URL, page title, and extracted text with kept links inline.

        Notes:
        - maximum 10 URLs per call.
        - pages may be partially loaded or truncated on timeout/size limits.

        Examples:
        - parse_websites(urls="https://example.com, https://news.ycombinator.com")
        - parse_websites(urls="https://example.com\\nhttps://another.com", max_chars_per_site=12000)
        """
        raw_items = re.split(r"[\n,]+", urls)
        items = [u.strip() for u in raw_items if u.strip()]
        if not items:
            return "No URLs provided."

        content_limit = max(500, min(max_chars_per_site, MAX_SITE_CONTENT_CHARS))
        timeout_ms = _resolve_page_timeout_ms()
        render_wait_ms = _resolve_render_wait_ms()
        wait_until = _resolve_wait_until()
        blocked_resource_types = _resolve_block_resource_types()
        max_concurrency = _resolve_max_concurrency()

        try:
            if _parse_reuse_browser_enabled():
                return _parse_websites_with_shared_browser(
                    items=items,
                    content_limit=content_limit,
                    timeout_ms=timeout_ms,
                    render_wait_ms=render_wait_ms,
                    wait_until=wait_until,
                    blocked_resource_types=blocked_resource_types,
                    max_concurrency=max_concurrency,
                )

            return _run_function_in_thread(
                _parse_websites_with_ephemeral_browser,
                items,
                content_limit,
                timeout_ms,
                render_wait_ms,
                wait_until,
                blocked_resource_types,
                max_concurrency,
            )
        except Exception as exc:
            return (
                "Playwright parser initialization failed. "
                "Ensure playwright is installed and Chromium is available.\n"
                f"Error: {type(exc).__name__}: {exc!r}"
            )

    return parse_websites
