import asyncio
from crawl4ai import AsyncWebCrawler

# Pages are fetched together in one browser, and whatever hasn't loaded by
# then is given up on. One at a time, each with its own browser launch and a
# 12s timeout, three pages took ~5s even when two were anti-bot walls.
CRAWL_DEADLINE_SECONDS = 5


async def _crawl_pages_async(urls, deadline):
    async with AsyncWebCrawler() as crawler:
        async def crawl(url):
            try:
                result = await crawler.arun(url=url)
                return result.markdown or ""
            except Exception as exc:
                print(f"Crawl failed for {url}: {exc}")
                return ""

        tasks = [asyncio.create_task(crawl(url)) for url in urls]
        done, pending = await asyncio.wait(tasks, timeout=deadline)
        for task in pending:
            task.cancel()
        # Let the cancelled crawls unwind before the browser closes.
        await asyncio.gather(*pending, return_exceptions=True)
        pages = {}
        for url, task in zip(urls, tasks):
            if task in done:
                pages[url] = task.result()
            else:
                print(f"Crawl timed out after {deadline}s: {url}")
                pages[url] = ""
        return pages


def crawl_pages(urls, deadline=CRAWL_DEADLINE_SECONDS):
    """Markdown text per URL ("" for pages that failed or ran out of time)."""
    if not urls:
        return {}
    try:
        return asyncio.run(_crawl_pages_async(urls, deadline))
    except Exception as exc:
        print(f"Crawling failed: {exc}")
        return {url: "" for url in urls}
