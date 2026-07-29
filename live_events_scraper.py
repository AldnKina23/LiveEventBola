import asyncio
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, quote
import aiohttp
from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

DYNAMIC_WAIT_TIMEOUT = 15000
GAME_TABLE_WAIT_TIMEOUT = 25000

STREAM_PATTERN = re.compile(r"\.m3u8($|\?)", re.IGNORECASE)

# Tentukan folder output dan file target
OUTPUT_DIR = Path("LiveEvents")
OUTPUT_FILE = OUTPUT_DIR / "LiveSportsEvents.m3u8"
TIVIMATE_OUTPUT_FILE = OUTPUT_DIR / "LiveSportsEvents_TiviMate.m3u8"

# Target Sites Configuration
TARGET_SITES = [
    {
        "name": "Xoilac XTX",
        "base_url": "https://xoilacxtx.tv/",
        "group": "Xoilac XTX - Live",
        "default_logo": "https://i.postimg.cc/B6WMnCRT/basketball-sport-logo-minimalist-style-600nw-2484656797.jpg",
    },
    {
        "name": "Xoilac ZZG",
        "base_url": "https://xoilaczzg.com/",
        "group": "Xoilac ZZG - Live",
        "default_logo": "https://i.postimg.cc/B6WMnCRT/basketball-sport-logo-minimalist-style-600nw-2484656797.jpg",
    },
    {
        "name": "RestNinja",
        "base_url": "https://restninja.io/",
        "group": "RestNinja - Live Events",
        "default_logo": "https://i.postimg.cc/CLDMZMZC/nfl-logo-png-seeklogo-520492.png",
    },
    {
        "name": "Trackey",
        "base_url": "https://www.trackey.io/",
        "group": "Trackey - Live Events",
        "default_logo": "https://i.postimg.cc/wBBdk9Bc/mlb-logo-png-seeklogo-250501.png",
    },
]

# --------------------------------------------------------------------------------
# TIVIMATE & STANDARD PLAYLIST GENERATORS
# --------------------------------------------------------------------------------
def write_playlist_tivimate(streams: List[Dict], filepath: Path):
    if not streams:
        print("❌ No streams found to write to TiviMate playlist.")
        return

    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")

        for entry in streams:
            extinf_line = (
                f'#EXTINF:-1 tvg-id="{entry["tvg_id"]}" '
                f'tvg-name="{entry["name"]}" '
                f'tvg-logo="{entry["tvg_logo"]}" '
                f'group-title="{entry["group"]}",{entry["name"]}\n'
            )
            f.write(extinf_line)

            base_url = entry.get("ref", "")
            pipe = ""

            if "custom_headers" in entry:
                ch = entry["custom_headers"]
                pipe += f'|referer={ch.get("referrer","")}'
                pipe += f'|origin={ch.get("origin","")}'
                pipe += f'|user-agent={quote(ch.get("user_agent",""), safe="")}'
            else:
                pipe += f"|referer={base_url}"
                pipe += f"|origin={base_url}"
                pipe += f"|user-agent={quote(USER_AGENT, safe='')}"

            f.write(entry["url"] + pipe + "\n")

    print(f"✅ TiviMate playlist saved to: {filepath}")

def write_playlist(streams: List[Dict], filepath: Path):
    if not streams:
        print("❌ No streams found to write to standard playlist.")
        return

    filepath.parent.mkdir(parents=True, exist_ok=True)

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for entry in streams:
            extinf_line = (
                f'#EXTINF:-1 tvg-id="{entry["tvg_id"]}" '
                f'tvg-name="{entry["name"]}" '
                f'tvg-logo="{entry["tvg_logo"]}" '
                f'group-title="{entry["group"]}",{entry["name"]}\n'
            )
            f.write(extinf_line)

            if "custom_headers" in entry:
                headers = entry["custom_headers"]
                f.write(f'#EXTVLCOPT:http-origin={headers["origin"]}\n')
                f.write(f'#EXTVLCOPT:http-referrer={headers["referrer"]}\n')
                f.write(f'#EXTVLCOPT:http-user-agent={headers["user_agent"]}\n')
            else:
                f.write(f'#EXTVLCOPT:http-origin={entry["ref"]}\n')
                f.write(f'#EXTVLCOPT:http-referrer={entry["ref"]}\n')
                f.write(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")

            f.write(entry["url"] + "\n")

    print(f"✅ Standard playlist saved successfully to: {filepath}")

# --------------------------------------------------------------------------------
# NETWORK & SCRAPING HELPERS
# --------------------------------------------------------------------------------
def clean_match_title(title: str) -> str:
    cleaned = " ".join(title.splitlines()).strip()
    return " ".join(cleaned.split())

async def verify_stream_url(session: aiohttp.ClientSession, url: str, headers: Optional[Dict[str, str]] = None) -> bool:
    request_headers = headers if headers else {}
    if "User-Agent" not in request_headers:
        request_headers["User-Agent"] = session.headers.get("User-Agent", USER_AGENT)

    try:
        async with session.get(url, timeout=10, allow_redirects=True, headers=request_headers) as response:
            if response.status == 200:
                print(f"   -> Stream Verified (200 OK): {url[:80]}...")
                return True
            else:
                print(f"   -> Stream HTTP Failed ({response.status}): {url[:80]}...")
                return False
    except Exception as e:
        print(f"   -> Stream Verification Error ({type(e).__name__})")
        return False

async def find_stream_from_page(context: BrowserContext, page_url: str, base_url: str, session: aiohttp.ClientSession) -> Optional[str]:
    verification_headers = {
        "Origin": base_url.rstrip('/'),
        "Referer": base_url
    }
    page = await context.new_page()
    candidate_urls: List[str] = []

    def handle_request(request):
        if STREAM_PATTERN.search(request.url) and request.url not in candidate_urls:
            print(f"   [Sniffer] Captured potential M3U8: {request.url[:90]}...")
            candidate_urls.append(request.url)

    page.on("request", handle_request)

    try:
        print(f"   ↳ Navigating to match page: {page_url}")
        await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
        
        # Tunggu jaringan tenang / pemutar video memuat m3u8
        try:
            await page.wait_for_load_state('networkidle', timeout=DYNAMIC_WAIT_TIMEOUT)
        except Exception:
            pass

        # Cek apakah stream terdeteksi saat halaman dimuat
        for stream_url in reversed(candidate_urls):
            if await verify_stream_url(session, stream_url, headers=verification_headers):
                return stream_url

        # Jika belum dapat, coba trigger tombol/player di dalam iframe atau div player
        clickable_elements = page.locator("iframe, button:has-text('Server'), button:has-text('HD'), div.player, div#player")
        count = await clickable_elements.count()

        for i in range(min(count, 5)):
            try:
                elem = clickable_elements.nth(i)
                if await elem.is_visible():
                    await elem.click(timeout=3000)
                    await page.wait_for_timeout(2000)
            except Exception:
                continue

            for stream_url in reversed(candidate_urls):
                if await verify_stream_url(session, stream_url, headers=verification_headers):
                    return stream_url

    except Exception as e:
        print(f"   ❌ Error sniffing {page_url}: {e}")
    finally:
        if not page.is_closed():
            page.remove_listener("request", handle_request)
            await page.close()

    return None

# --------------------------------------------------------------------------------
# MAIN SITE SCRAPER
# --------------------------------------------------------------------------------
async def scrape_site_events(site_info: Dict) -> List[Dict]:
    site_name = site_info["name"]
    base_url = site_info["base_url"]
    group_name = site_info["group"]
    default_logo = site_info["default_logo"]

    print(f"\n==========================================")
    print(f"🌐 Scraping Events from: {site_name} ({base_url})")
    print(f"==========================================")

    results: List[Dict] = []
    
    async with async_playwright() as p, aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as session:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT)
        
        try:
            page = await context.new_page()
            await page.goto(base_url, wait_until="domcontentloaded", timeout=45000)
            
            # Cari link pertandingan (Xoilac dan situs sports biasanya memakai pola link /truc-tiep/, /match/, /live/, dll)
            match_links = []
            
            # Tunggu elemen dasar muncul
            await page.wait_for_timeout(3000)

            # Selector fleksibel untuk menangkap link pertandingan
            selectors = [
                "a[href*='/truc-tiep/']",
                "a[href*='/match/']",
                "a[href*='/live/']",
                "a[href*='/watch/']",
                "div.match-item a",
                "div.item-match a",
                "a.match-link"
            ]

            found_elements = page.locator(", ".join(selectors))
            count = await found_elements.count()

            if count == 0:
                # Fallback: Ambil semua link internal yang berpotensi match
                found_elements = page.locator("a[href]")
                count = await found_elements.count()

            visited_hrefs = set()

            for i in range(count):
                elem = found_elements.nth(i)
                href = await elem.get_attribute("href")
                title = await elem.inner_text()

                if href and title and len(title.strip()) > 3:
                    full_url = urljoin(base_url, href)
                    
                    # Hindari link halaman utama atau link yang duplikat
                    if full_url not in visited_hrefs and full_url != base_url:
                        # Filter sederhana untuk memastikan itu link pertandingan
                        if any(k in full_url.lower() for k in ["truc-tiep", "match", "live", "watch", "event", "stream"]):
                            visited_hrefs.add(full_url)
                            match_links.append({
                                "title": clean_match_title(title),
                                "url": full_url
                            })

            await page.close()
            print(f"📌 Found {len(match_links)} potential live match links on {site_name}.")

            # Proses setiap link pertandingan untuk mengekstrak stream M3U8
            for idx, match in enumerate(match_links[:15]): # Batasi max 15 match per situs agar cepat
                print(f"\n[{idx+1}/{len(match_links[:15])}] Checking Match: {match['title']}")
                stream_url = await find_stream_from_page(context, match["url"], base_url, session)

                if stream_url:
                    results.append({
                        "name": match["title"],
                        "url": stream_url,
                        "tvg_id": f"{site_name.replace(' ', '')}.Live.{idx+1}",
                        "tvg_logo": default_logo,
                        "group": group_name,
                        "ref": base_url
                    })
                    print(f"   🎉 Stream FOUND for: {match['title']}")
                else:
                    print(f"   ⚠️ No active stream found.")

        except Exception as e:
            print(f"❌ Error scraping {site_name}: {e}")
        finally:
            await browser.close()

    return results

# --------------------------------------------------------------------------------
# EXECUTION ENTRY POINT
# --------------------------------------------------------------------------------
async def main():
    print("🚀 Starting Multi-Site Live Events Scraper...")

    tasks = [scrape_site_events(site) for site in TARGET_SITES]
    site_results = await asyncio.gather(*tasks)

    # Gabungkan seluruh stream dari semua situs
    all_streams = [stream for result in site_results for stream in result]

    print(f"\n==========================================")
    print(f"📊 Total Active Streams Found: {len(all_streams)}")
    print(f"==========================================")

    # Simpan ke format Standard (VLC)
    write_playlist(all_streams, OUTPUT_FILE)

    # Simpan ke format TiviMate Pipe
    write_playlist_tivimate(all_streams, TIVIMATE_OUTPUT_FILE)

if __name__ == "__main__":
    asyncio.run(main())
