#!/usr/bin/env python3
"""Screenshot the earnings radar report via Playwright (full page)."""
import asyncio

from playwright.async_api import async_playwright

HTML = "/opt/data/finance_work/earnings-radar/samples/earnings_radar_demo.html"
OUT = "/opt/data/finance_work/earnings-radar/samples/earnings_radar_demo.png"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"])
        page = await b.new_page(viewport={"width": 1280, "height": 1000}, device_scale_factor=1)
        await page.goto("file://" + HTML)
        await page.wait_for_timeout(900)
        await page.screenshot(path=OUT, full_page=True)
        h = await page.evaluate("document.documentElement.scrollHeight")
        print("scrollHeight:", h)
        await b.close()
    print("SAVED:", OUT)


if __name__ == "__main__":
    asyncio.run(main())
