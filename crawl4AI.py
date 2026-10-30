import asyncio
from crawl4ai import AsyncWebCrawler

CRAWL_TIMEOUT_SECONDS = 12


async def crawl_page_async(url):
    async with AsyncWebCrawler() as crawler:
        result = await asyncio.wait_for(crawler.arun(url=url), timeout=CRAWL_TIMEOUT_SECONDS)
        return result.markdown

def crawl_page(url):
    try:
        return asyncio.run(crawl_page_async(url))
    except asyncio.TimeoutError:
        print(f"Crawl timed out after {CRAWL_TIMEOUT_SECONDS}s: {url}")
        return ""