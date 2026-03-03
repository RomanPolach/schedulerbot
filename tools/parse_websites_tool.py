from __future__ import annotations

import asyncio
import atexit
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

from runtime_config import MAX_SITE_CONTENT_CHARS

_EVENT_LOOP_POLICY_LOCK = threading.Lock()


def _parse_reuse_browser_enabled() -> bool:
    return (os.getenv("PLAYWRIGHT_REUSE_BROWSER", "true") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _resolve_render_wait_ms() -> int:
    return max(0, min(int(os.getenv("PLAYWRIGHT_RENDER_WAIT_MS", "350")), 15_000))

def _resolve_page_timeout_ms() -> int:
    timeout_seconds = float(os.getenv("PLAYWRIGHT_PAGE_TIMEOUT_SECONDS", "30"))
    return max(5_000, min(int(timeout_seconds * 1000), 180_000))


def _resolve_max_concurrency() -> int:
    return max(1, min(int(os.getenv("PLAYWRIGHT_MAX_CONCURRENCY", "4")), 8))


def _resolve_warmup_timeout_seconds() -> float:
    return max(5.0, min(float(os.getenv("PLAYWRIGHT_STARTUP_TIMEOUT_SECONDS", "30")), 120.0))


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
) -> str:
    normalized = url if url.startswith(("http://", "https://")) else f"https://{url}"

    page = await context.new_page()
    try:
        try:
            await page.goto(normalized, wait_until="networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            return (
                f"URL: {normalized}\n"
                f"Error: Timeout while waiting for full page load (networkidle) "
                f"within {timeout_ms} ms."
            )

        if render_wait_ms:
            await page.wait_for_timeout(render_wait_ms)

        final_url = page.url
        title = (await page.title()) or "(no title)"
        body_text = await page.inner_text("body")
        text = " ".join(body_text.split())

        text = text[:content_limit]

        link_rows = await page.eval_on_selector_all(
            "main a[href], article a[href], [role='main'] a[href]",
            """els => els.map(e => ({
                href: e.href || e.getAttribute('href') || '',
                text: (e.innerText || '').trim()
            }))""",
        )
        if not link_rows:
            link_rows = await page.eval_on_selector_all(
                "a[href]",
                """els => els.map(e => ({
                    href: e.href || e.getAttribute('href') || '',
                    text: (e.innerText || '').trim()
                }))""",
            )

        by_href: Dict[str, str] = {}
        for row in link_rows:
            href = str((row or {}).get("href", "")).strip()
            label = " ".join(str((row or {}).get("text", "")).split())
            if not href:
                continue
            lowered_href = href.lower()
            if lowered_href.startswith(("javascript:", "mailto:", "tel:")):
                continue
            if not href.startswith(("http://", "https://")):
                continue
            if len(label) < 6:
                continue
            current = by_href.get(href, "")
            if len(label) > len(current):
                by_href[href] = label

        base_domain = urlparse(final_url).netloc.lower()
        scored_links = []
        for href, label in by_href.items():
            parsed = urlparse(href)
            score = 0
            if parsed.netloc.lower() == base_domain:
                score += 2
            if 20 <= len(label) <= 160:
                score += 1
            if parsed.fragment:
                score -= 2
            if len(parsed.query) > 160:
                score -= 1
            lowered = label.lower()
            if any(
                bad in lowered
                for bad in [
                    "cookie",
                    "privacy",
                    "terms",
                    "login",
                    "sign in",
                    "newsletter",
                    "facebook",
                    "instagram",
                    "linkedin",
                ]
            ):
                score -= 2
            scored_links.append((score, href, label))
        scored_links.sort(key=lambda x: x[0], reverse=True)

        links_block = ""
        if scored_links:
            lines = ["Detected page links:"]
            for idx, (_, href, label) in enumerate(scored_links[:20], start=1):
                lines.append(f"{idx}. {label or '(no title)'} | {href}")
            links_block = "\n" + "\n".join(lines)

        return f"URL: {final_url}\nTitle: {title}\nContent: {text or '(no text extracted)'}{links_block}"
    except Exception as exc:
        return f"URL: {normalized}\nError: {type(exc).__name__}: {exc!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _parse_websites_with_playwright_async(
    context: BrowserContext,
    items: List[str],
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    max_concurrency: int,
) -> List[str]:
    semaphore = asyncio.Semaphore(max_concurrency)
    targets = list(items[:10])

    async def _worker(index: int, target_url: str) -> tuple[int, str]:
        async with semaphore:
            chunk = await _parse_single_website_with_playwright(
                context=context,
                url=target_url,
                content_limit=content_limit,
                timeout_ms=timeout_ms,
                render_wait_ms=render_wait_ms,
            )
            return index, chunk

    results = await asyncio.gather(*[_worker(i, target) for i, target in enumerate(targets)])
    results.sort(key=lambda x: x[0])
    return [chunk for _, chunk in results]


async def _parse_websites_with_playwright_ephemeral_async(
    items: List[str],
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    max_concurrency: int,
) -> List[str]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        try:
            return await _parse_websites_with_playwright_async(
                context=context,
                items=items,
                content_limit=content_limit,
                timeout_ms=timeout_ms,
                render_wait_ms=render_wait_ms,
                max_concurrency=max_concurrency,
            )
        finally:
            await context.close()
            await browser.close()


def _parse_websites_with_shared_browser(
    items: List[str],
    content_limit: int,
    timeout_ms: int,
    render_wait_ms: int,
    max_concurrency: int,
) -> str:
    timeout_seconds = max(10.0, min(float(timeout_ms) / 1000.0 + 30.0, 300.0))
    context = _BROWSER_SERVICE.run(
        _BROWSER_SERVICE.ensure_browser_context(),
        timeout_seconds=timeout_seconds,
    )
    try:
        chunks = _BROWSER_SERVICE.run(
            _parse_websites_with_playwright_async(
                context=context,
                items=items,
                content_limit=content_limit,
                timeout_ms=timeout_ms,
                render_wait_ms=render_wait_ms,
                max_concurrency=max_concurrency,
            ),
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        # Browser/context can occasionally become invalid after crashes or OOM.
        # Recreate once and retry this parse request.
        context = _BROWSER_SERVICE.run(
            _BROWSER_SERVICE.reset_browser_context(),
            timeout_seconds=timeout_seconds,
        )
        chunks = _BROWSER_SERVICE.run(
            _parse_websites_with_playwright_async(
                context=context,
                items=items,
                content_limit=content_limit,
                timeout_ms=timeout_ms,
                render_wait_ms=render_wait_ms,
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
    max_concurrency: int,
) -> str:
    chunks = asyncio.run(
        _parse_websites_with_playwright_ephemeral_async(
            items=items,
            content_limit=content_limit,
            timeout_ms=timeout_ms,
            render_wait_ms=render_wait_ms,
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
        """Fetch and parse a comma/newline-separated list of URLs, returning title and extracted text.

        Content is truncated only if it exceeds ~10 A4 pages (MAX_SITE_CONTENT_CHARS).
        """
        raw_items = re.split(r"[\n,]+", urls)
        items = [u.strip() for u in raw_items if u.strip()]
        if not items:
            return "No URLs provided."

        content_limit = max(500, min(max_chars_per_site, MAX_SITE_CONTENT_CHARS))
        timeout_ms = _resolve_page_timeout_ms()
        render_wait_ms = _resolve_render_wait_ms()
        max_concurrency = _resolve_max_concurrency()

        try:
            if _parse_reuse_browser_enabled():
                return _parse_websites_with_shared_browser(
                    items=items,
                    content_limit=content_limit,
                    timeout_ms=timeout_ms,
                    render_wait_ms=render_wait_ms,
                    max_concurrency=max_concurrency,
                )

            return _run_function_in_thread(
                _parse_websites_with_ephemeral_browser,
                items,
                content_limit,
                timeout_ms,
                render_wait_ms,
                max_concurrency,
            )
        except Exception as exc:
            return (
                "Playwright parser initialization failed. "
                "Ensure playwright is installed and Chromium is available.\n"
                f"Error: {type(exc).__name__}: {exc!r}"
            )

    return parse_websites
