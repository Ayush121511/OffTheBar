import asyncio
from crawl4ai import AsyncWebCrawler

async def crawl_page_async(url):
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(url=url)
        return result.markdown

def crawl_page(url):
    return asyncio.run(crawl_page_async(url))