import asyncio
import json
import ollama
import tkinter as tk
import re
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
import platform
import urllib.parse


'''
Initialize the async context. Keeping headless execution deactivated (headless=False) 
allows you to watch your local script physically highlight the target layout elements in
real-time.
 
 '''
async def main():
    async with async_playwright() as p:
        browser= await p.chromium.launch(headless=False)
        context=await browser.new_context( viewport={"width": 1200, "height": 800},
                                           user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
        page= await context.new_page()
        await Stealth().apply_stealth_async(page)


        '''

Step 4: Target the Search Query Pipeline

Encode your custom query and direct your browser tab to pull listings.
'''


        query= "tesla global news"
        

        search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote_plus(query)}"

        await page.goto(search_url)
        await page.wait_for_load_state("networkidle")

        consent_button=page.locator("button:has-text('Accept all'), #L2AGLb")
        if await consent_button.is_visible():
            await consent_button.click()
            await page.wait_for_load_state("networkidle")
            anchors=await page.locator("a.result__url").all()
            urls= []  
            for anchor in anchors:
                href=await anchor.get_attribute("href")
                if href and href.startswith("http")and "duckduckgo.com" not in href:
                    if href not in urls:
                        urls.append(href)
                if len(urls) >= 30:
                    break   
            print(f"[Local Monitor] Collected {len(urls)} target URLs to parse.")
            master_clipboard_buffer = []
            for index, url in enumerate(urls , start=1):
                print(f"\n⚡ Processing [{index}/15] -> {url}")
                try:
                    target_page= await context.new_page()
                    await target_page.set_viewport_size({"width": 1200, "height": 800})
                    await target_page.goto(url, timeout=30000, wait_until="networkidle")
                    await target_page.wait_for_timeout(2000)
                    screenshot_file="screenshot.png"
                    await target_page.screenshot(path=screenshot_file, full_page=True)
                    response= ollama.chat(
                        model="llama3.2-vision",
                        messages=[{
                            'role':'user',
                            'content': f"Analyze the content of this webpage screenshot and provide a concise summary of the key information. Please focus on the main points, important details, and any relevant context that can be derived from the visual elements. The screenshot is attached for your reference.",
                            'images':[screenshot_file]}])
                    
                    vlm_text_output= response['message']['content']
                    json_match = re.search(r'\{.*\}', vlm_text_output, re.DOTALL).group()
                    coords = json.loads(json_match)["box_2d"]
                    ymin, xmin, ymax, xmax = coords
                    start_x = int((xmin / 1000) * 1280)
                    start_y = int((ymin / 1000) * 720)
                    end_x = int((xmax / 1000) * 1280)
                    end_y = int((ymax / 1000) * 720)
                    await target_page.mouse.move(start_x, start_y)
                    await target_page.mouse.down()
                    await target_page.mouse.move(end_x, end_y, steps=20) # Smooth drag select
                    await target_page.mouse.up()

                    # Dynamic shortcut command selector mapped per local OS architecture
                    modifier = "Meta" if platform.system() == "Darwin" else "Control"
                    await target_page.keyboard.press(f"{modifier}+C")
                    await target_page.wait_for_timeout(600)  # Safe buffer cushion for clipboard write
                    root = tk.Tk()
                    root.withdraw()
                    local_copied_text = root.clipboard_get()
                    
                    master_clipboard_buffer.append(f"=== ENTRY #{index} | URL: {url} ===\n{local_copied_text}\n\n")
                    await target_page.close()
                    
                except Exception as loop_error:
                    print(f"Skipping index #{index} due to processing exception: {loop_error}")
                    if 'target_page' in locals(): await target_page.close()

            final_clipboard_payload = "".join(master_clipboard_buffer)
                    
                    # Flush the final aggregated results back to the system clipboard
            root = tk.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(final_clipboard_payload)
            root.update()
                    
            print("\n[SUCCESS] Content from all accessible websites compiled locally via Ollama and stored on clipboard!")
            await context.close()
            browser.close()

# Run the local automation process
asyncio.run(main())


              



asyncio.run(main())

