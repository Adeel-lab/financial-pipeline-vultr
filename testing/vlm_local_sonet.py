import asyncio
import urllib.parse
import tkinter as tk
import ollama
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

QUERY = "tesla global news"
MAX_URLS = 15


def extract_real_url(href: str):
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        uddg = urllib.parse.parse_qs(parsed.query).get("uddg")
        return urllib.parse.unquote(uddg[0]) if uddg else None
    if href.startswith("http") and "duckduckgo.com" not in parsed.netloc:
        return href
    return None


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1200, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)

        search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(QUERY)}"
        await page.goto(search_url)
        await page.wait_for_load_state("domcontentloaded")

        consent = page.locator("button:has-text('Accept all')")
        if await consent.count() > 0 and await consent.first.is_visible():
            await consent.first.click()

        anchors = await page.locator("a.result__a").all()
        urls = []
        for a in anchors:
            real = extract_real_url(await a.get_attribute("href"))
            if real and real not in urls:
                urls.append(real)
            if len(urls) >= MAX_URLS:
                break

        print(f"[Local Monitor] Collected {len(urls)} URLs")
        buffer = []

        for i, url in enumerate(urls, start=1):
            print(f"\n⚡ Processing [{i}/{len(urls)}] -> {url}")
            tab = None
            try:
                tab = await context.new_page()
                await tab.goto(url, timeout=30000, wait_until="domcontentloaded")
                await tab.wait_for_timeout(2500)
                shot = f"C:\\Users\\adeel\\Desktop\\notebooks\\vultr\\screenshots\\screenshot_{i}.png"
                await tab.screenshot(path=shot, full_page=False)

                resp = ollama.chat(
                    model="qwen2.5vl:7b",
                    messages=[{
                        "role": "user",
                        "content": "Summarize the key information on this webpage "
                                   "screenshot: main points, details, context.",
                        "images": [shot],
                    }],
                )
                summary = resp["message"]["content"]
                buffer.append(f"=== ENTRY #{i} | URL: {url} ===\n{summary}\n\n")
            except Exception as e:
                print(f"Skipping #{i}: {e}")
            finally:
                if tab:
                    await tab.close()

        payload = "".join(buffer)
        with open("summaries.txt", "w", encoding="utf-8") as f:
            f.write(payload)
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(payload)
        root.update()
        root.destroy()

        print(f"\n[SUCCESS] {len(buffer)} summaries copied to clipboard.")
        await context.close()
        await browser.close()


asyncio.run(main())