#!/usr/bin/env python3
"""
HHPanda Scraper → MonPlayer JSON (v2.1)
Chiến lược stream URL:
  1. AJAX player.php → parse iframe/source → lấy m3u8/mp4 thật
  2. Fallback: giữ embed URL nếu không extract được direct stream
  3. Server priority: tiktik (V1) > pro (V2) > vip4k
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

CONFIG = {
    "BASE_URL":     "https://hhpanda.st",
    "OUTPUT_DIR":   "ophim",
    "LIST_FILE":    "ophim.json",
    "MAX_MOVIES":   5,
    "MAX_EPISODES": 5,
    "TIMEOUT_NAV":  30000,
    "TIMEOUT_WAIT": 20000,
    "USER_AGENT":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "RAW_BASE":     os.getenv("RAW_BASE", "https://raw.githubusercontent.com/your-repo/hhpanda-mv/main"),
}

QUALITY_PRIORITY = ["tiktik", "pro", "vip4k", "vip4kv2", "1080", "4k", "hd"]

EXTRA_HEADERS = {
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language":           "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding":           "gzip, deflate, br",
    "Cache-Control":             "no-cache",
    "Pragma":                    "no-cache",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",
    "Sec-Fetch-User":            "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer":                   "https://hhpanda.st/",
}


def _human_delay(min_ms=300, max_ms=900):
    time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))


def _apply_stealth(page):
    if HAS_STEALTH:
        stealth_sync(page)
    else:
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins',   { get: () => [1, 2, 3] });
            Object.defineProperty(navigator, 'languages', { get: () => ['vi-VN', 'vi', 'en-US'] });
            window.chrome = { runtime: {} };
        """)


def _debug_page(page, label):
    try:
        title   = page.title()
        url_now = page.url
        html    = page.content()[:800].replace('\n', ' ')
        logger.info(f"   [DEBUG:{label}] title='{title}'")
        logger.info(f"   [DEBUG:{label}] url='{url_now}'")
        logger.info(f"   [DEBUG:{label}] html[:800]={html}")
    except Exception as e:
        logger.info(f"   [DEBUG:{label}] cannot read page: {e}")


def _wait_for_cf(page, selector, timeout):
    try:
        page.wait_for_function(
            """() => !document.title.includes('Just a moment') &&
                    !document.querySelector('#challenge-running') &&
                    document.readyState === 'complete'""",
            timeout=15000
        )
    except Exception:
        pass
    page.wait_for_selector(selector, state="attached", timeout=timeout)


# ── AJAX Player Fetcher ───────────────────────────────────────────────────────
def _fetch_player_ajax(context, post_id: str, chapter_st: str, sv: str, server_type: str) -> str | None:
    """
    Gọi AJAX endpoint player.php của hhpanda để lấy HTML chứa iframe/stream.
    """
    page = context.new_page()
    try:
        player_url = f"{CONFIG['BASE_URL']}/player/player.php"
        params = {
            "action": "dox_ajax_player",
            "post_id": post_id,
            "chapter_st": chapter_st,
            "sv": sv,
            "type": server_type,
        }
        query = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{player_url}?{query}"
        
        logger.info(f"      AJAX → {full_url[:100]}...")
        page.set_extra_http_headers({
            "Referer": f"{CONFIG['BASE_URL']}/", 
            "X-Requested-With": "XMLHttpRequest"
        })
        page.goto(full_url, wait_until="domcontentloaded", timeout=20000)
        
        html = page.content()
        return html
    except Exception as e:
        logger.warning(f"      AJAX fetch error: {e}")
        return None
    finally:
        page.close()


def _parse_stream_from_html(html: str) -> dict | None:
    """
    Parse HTML response từ player.php để extract stream URL.
    Ưu tiên: m3u8 > mp4 > iframe embed.
    """
    # 1. Tìm iframe src (ưu tiên số 1)
    iframe_match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE)
    if iframe_match:
        src = iframe_match.group(1)
        # Handle protocol-relative URL: //streamfree.vip/... → https://...
        if src.startswith('//'):
            src = 'https:' + src
        elif src.startswith('/'):
            src = 'https://streamfree.vip' + src
        if '.m3u8' in src:
            return {"url": src, "type": "hls"}
        elif any(ext in src for ext in ['.mp4', '.webm']):
            return {"url": src, "type": "mp4"}
        else:
            return {"url": src, "type": "embed"}
    
    # 2. Tìm <source> tag (jwplayer, video.js)
    source_match = re.search(
        r'<source[^>]+src=["\']([^"\']+\.(m3u8|mp4|webm)[^"\']*)["\']', 
        html, re.IGNORECASE
    )
    if source_match:
        src = source_match.group(1)
        if src.startswith('//'):
            src = 'https:' + src
        vtype = "hls" if ".m3u8" in src else "mp4"
        return {"url": src, "type": vtype}
    
    # 3. Tìm file: trong JS config (plyr, hls.js, custom player)
    js_match = re.search(
        r'''['"]?file['"]?\s*:\s*['"]([^'"]+\.(m3u8|mp4|webm)[^'"]*)['"]''', 
        html, re.IGNORECASE
    )
    if js_match:
        src = js_match.group(1)
        if src.startswith('//'):
            src = 'https:' + src
        vtype = "hls" if ".m3u8" in src else "mp4"
        return {"url": src, "type": vtype}
    
    return None


# ── Step 1: Most-viewed → movie list ──────────────────────────────────────────
def get_trending_movies(page):
    try:
        trending_url = f"{CONFIG['BASE_URL']}/most-viewed"
        page.goto(trending_url, wait_until="domcontentloaded", timeout=CONFIG["TIMEOUT_NAV"])
        _debug_page(page, "most-viewed")
        _wait_for_cf(page, ".movie-item, .halim-item", CONFIG["TIMEOUT_WAIT"])

        movies = page.evaluate("""() => {
            const results = [];
            const selectors = ['.movie-item', '.halim-item', '.film-item', '#main-contents .item'];
            let items = [];
            for (const sel of selectors) {
                items = document.querySelectorAll(sel);
                if (items.length > 0) break;
            }
            for (const item of items) {
                if (results.length >= 15) break;
                const link = item.querySelector('a[href*="/watch-"], a[href*="/the-gioi-"], .film-title a, h3 a');
                if (!link?.href) continue;
                const href = link.href;
                const match = href.match(/\/watch-([^/]+)\//) || href.match(/\/([^/]+)$/);
                const slug = match ? match[1].replace(/-tap-\d+.*$/, '').replace(/-sv\d+.*$/, '') : '';
                if (!slug || slug.includes('search') || slug.includes('category')) continue;
                const title = link.innerText.trim() || link.title || item.querySelector('.film-name, .halim-title')?.innerText || '';
                let thumb = item.querySelector('img[data-src], img[data-original], img.film-poster-img, .film-poster img')?.dataset?.src || '';
                if (!thumb) thumb = item.querySelector('img')?.src || '';
                if (thumb && !thumb.startsWith('http')) thumb = 'https://hhpanda.st' + thumb;
                const badge = item.querySelector('.tick, .label, .badge, .halim-status')?.innerText.trim() || 'Trending';
                results.push({ slug, title: title.replace(/Tập\s*\d+.*$/i, '').trim(), thumb, badge });
            }
            const seen = new Set();
            return results.filter(m => {
                if (seen.has(m.slug)) return false;
                seen.add(m.slug);
                return true;
            }).slice(0, 10);
        }""")
        return movies
    except Exception as e:
        logger.error(f"Failed to get trending movies: {e}")
        _debug_page(page, "most-viewed-error")
        return []


# ── Step 1b: Detail/Episode page → extract post_id + latest ep ────────────────
def get_series_info_and_latest_ep(page, slug):
    """
    Vào trang episode bất kỳ của series để lấy:
    - post_id (từ DoPostInfo script)
    - latest episode URL (tap số cao nhất)
    """
    # Dùng episode 1 làm fallback (luôn tồn tại)
    sample_url = f"{CONFIG['BASE_URL']}/watch-{slug}/tap-1-sv1.html"
    
    try:
        _human_delay(300, 700)
        page.goto(sample_url, wait_until="domcontentloaded", timeout=CONFIG["TIMEOUT_NAV"])
        
        # Nếu 404, thử redirect đến series page
        if "404" in page.title().lower() or page.url != sample_url:
            series_url = f"{CONFIG['BASE_URL']}/{slug}"
            page.goto(series_url, wait_until="domcontentloaded", timeout=CONFIG["TIMEOUT_NAV"])
        
        _debug_page(page, f"series-{slug}")
        _wait_for_cf(page, "#halim-list-server, .halim-episode", CONFIG["TIMEOUT_WAIT"])

        info = page.evaluate(f"""() => {{
            // Lấy post_id từ script DoPostInfo (nguồn chính xác nhất)
            let post_id = null;
            const scripts = Array.from(document.scripts);
            for (const script of scripts) {{
                if (script.textContent && script.textContent.includes('DoPostInfo')) {{
                    const match = script.textContent.match(/id:\\s*(\\d+)/);
                    if (match) {{
                        post_id = match[1];
                        break;
                    }}
                }}
            }}
            // Fallback: lấy từ data attribute
            if (!post_id) {{
                const epLink = document.querySelector('a[data-post-id]');
                if (epLink) post_id = epLink.getAttribute('data-post-id');
            }}
            
            // Lấy danh sách episodes và tìm số tap cao nhất
            const episodes = [];
            const containers = document.querySelectorAll('#halim-list-server .halim-episode a, ul.halim-list-eps a');
            for (const a of containers) {{
                const href = a.href || '';
                const epMatch = href.match(/tap-(\\d+)/);
                const svMatch = href.match(/-sv(\\d+)/);
                if (epMatch) {{
                    episodes.push({{
                        num: parseInt(epMatch[1]),
                        sv: svMatch ? parseInt(svMatch[1]) : 1,
                        href: href,
                        title: a.title || a.innerText.trim()
                    }});
                }}
            }}
            episodes.sort((a, b) => b.num - a.num);
            const latest = episodes[0] || null;
            
            return {{ post_id, latest }};
        }}""")
        
        if info["latest"]:
            logger.info(f"   Latest ep: tap-{info['latest']['num']} sv{info['latest']['sv']}")
        return info

    except PlaywrightTimeout:
        logger.warning(f"   Timeout loading series {slug}")
        return {"post_id": None, "latest": None}
    except Exception as e:
        logger.warning(f"   Error loading series {slug}: {e}")
        return {"post_id": None, "latest": None}


# ── Step 2: Episode list from series page ─────────────────────────────────────
def get_episodes(page, slug, post_id):
    """
    Lấy danh sách episodes từ #halim-list-server.
    Handle duplicate #listsv-1 IDs cho Vietsub/Thuyết minh.
    """
    try:
        sample_url = f"{CONFIG['BASE_URL']}/watch-{slug}/tap-1-sv1.html"
        _human_delay(400, 800)
        page.goto(sample_url, wait_until="domcontentloaded", timeout=CONFIG["TIMEOUT_NAV"])
        _wait_for_cf(page, "#halim-list-server", CONFIG["TIMEOUT_WAIT"])

        episodes = page.evaluate(f"""() => {{
            const results = [];
            const seen = new Set();
            
            // Query từng server container riêng biệt để tránh duplicate ID
            const serverContainers = document.querySelectorAll('#halim-list-server > div.halim-server');
            
            for (const container of serverContainers) {{
                const serverName = container.querySelector('.halim-server-name')?.innerText || '';
                const isVietsub = serverName.toLowerCase().includes('vietsub');
                const defaultSv = isVietsub ? 1 : 2;
                
                const links = container.querySelectorAll('ul.halim-list-eps a[data-post-id][data-ep]');
                for (const a of links) {{
                    const href = a.href || '';
                    const epMatch = href.match(/tap-(\\d+)/);
                    const svMatch = href.match(/-sv(\\d+)/);
                    if (!epMatch) continue;
                    
                    const num = parseInt(epMatch[1]);
                    const sv = svMatch ? parseInt(svMatch[1]) : defaultSv;
                    const key = `${{num}}-sv${{sv}}`;
                    
                    if (seen.has(key)) continue;
                    seen.add(key);
                    
                    results.push({{
                        name: `Tap ${{num}}`,
                        num: num,
                        sv: sv,
                        url: href,
                        post_id: a.getAttribute('data-post-id'),
                        ep_slug: a.getAttribute('data-ep'),
                        title: a.title || `Tập ${{num}}`
                    }});
                }}
            }}
            
            // Sort: episode number desc, then sv asc (Vietsub trước)
            return results.sort((a, b) => b.num - a.num || a.sv - b.sv);
        }}""")

        if episodes:
            logger.info(f"   Got {len(episodes)} episodes for {slug}")
        else:
            logger.warning(f"   No episodes found for {slug}")
            _debug_page(page, f"no-ep-{slug}")
        return episodes

    except Exception as e:
        logger.warning(f"   Error getting episodes for {slug}: {e}")
        return []


# ── Step 3: Stream extraction via AJAX ────────────────────────────────────────
def get_stream_url(page, context, ep_info):
    """
    Gọi player.php AJAX để lấy stream URL cho episode.
    Thử lần lượt các server type: tiktik > pro > vip4k > vip4kv2
    """
    post_id = ep_info.get("post_id")
    chapter_st = ep_info.get("ep_slug")
    sv = str(ep_info.get("sv", 1))
    
    if not post_id or not chapter_st:
        logger.warning("      Missing post_id or chapter_st")
        return None

    server_types = ["tiktik", "pro", "vip4k", "vip4kv2"]
    streams = []

    for stype in server_types:
        try:
            _human_delay(200, 400)
            html = _fetch_player_ajax(context, post_id, chapter_st, sv, stype)
            if not html:
                continue
            
            stream = _parse_stream_from_html(html)
            if stream:
                streams.append({
                    "url": stream["url"],
                    "type": stream["type"],
                    "label": f"{stype}-sv{sv}",
                })
                logger.info(f"      ✓ Stream [{stream['type']}] via {stype}: {stream['url'][:70]}...")
                break  # Found working stream, stop trying other types
        except Exception as e:
            logger.debug(f"      Error with {stype}: {e}")
            continue

    return streams if streams else None


# ── Helpers ────────────────────────────────────────────────────────────────────
def _sort_streams(stream_list):
    """Sort by quality priority and type preference"""
    def priority(s):
        type_rank = {"mp4": 0, "hls": 1, "embed": 2}.get(s.get("type", "embed"), 3)
        lbl = (s.get("label") or "").strip().lower()
        try:
            quality_rank = QUALITY_PRIORITY.index(lbl.split("-")[0])
        except ValueError:
            quality_rank = 99
        return (type_rank, quality_rank)
    return sorted(stream_list, key=priority)


def build_detail_json(slug, episodes):
    streams = []
    for i, ep in enumerate(episodes):
        raw_streams = ep.get("stream")
        if not raw_streams:
            continue
        sorted_streams = _sort_streams(raw_streams)
        stream_links = []
        for j, s in enumerate(sorted_streams):
            label = s.get("label") or f"Link {j + 1}"
            stream_links.append({
                "id":      f"{slug}--0-{i}-{j}",
                "name":    label.upper().replace("-", " "),
                "type":    s["type"],
                "default": j == 0,
                "url":     s["url"],
            })
        streams.append({
            "id":           f"{slug}--0-{i}",
            "name":         ep["name"],
            "stream_links": stream_links
        })
    return {
        "sources": [{
            "id":   f"{slug}--0",
            "name": "Thuyet Minh #1",
            "contents": [{
                "id":          f"{slug}--0",
                "name":        "",
                "grid_number": 3,
                "streams":     streams
            }]
        }],
        "subtitle": "Thuyet Minh"
    }


def build_list_item(movie):
    return {
        "id":          movie["slug"],
        "name":        movie["title"],
        "description": "",
        "image": {
            "url":    movie["thumb"],
            "type":   "cover",
            "width":  480,
            "height": 640
        },
        "type":    "playlist",
        "display": "text-below",
        "label": {
            "text":       movie["badge"] or "Trending",
            "position":   "top-left",
            "color":      "#35ba8b",
            "text_color": "#ffffff"
        },
        "remote_data": {
            "url": f"{CONFIG['RAW_BASE']}/ophim/detail/{movie['slug']}.json"
        },
        "enable_detail": True
    }


# ── Main ───────────────────────────────────────────────────────────────────────
def scrape():
    logger.info("Starting HHPanda to MonPlayer scraper (v2.1)...")
    logger.info(f"playwright-stealth: {'OK' if HAS_STEALTH else 'NOT FOUND'}")
    logger.info("Stream logic: AJAX player.php → parse iframe/source")

    channels   = []
    detail_dir = Path(CONFIG["OUTPUT_DIR"]) / "detail"
    detail_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--lang=vi-VN",
            ]
        )
        context = browser.new_context(
            user_agent=CONFIG["USER_AGENT"],
            viewport={"width": 1280, "height": 720},
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            extra_http_headers=EXTRA_HEADERS,
            java_script_enabled=True,
        )
        page = context.new_page()
        _apply_stealth(page)

        try:
            movies = get_trending_movies(page)
            if not movies:
                logger.error("No movies found. Exiting.")
                return

            limit = min(len(movies), CONFIG["MAX_MOVIES"])
            logger.info(f"Found {len(movies)} movies. Processing {limit}...")

            for idx, movie in enumerate(movies[:limit], 1):
                logger.info(f"[{idx}/{limit}] {movie['title']} ({movie['slug']})")
                try:
                    # Get post_id and latest ep info
                    series_info = get_series_info_and_latest_ep(page, movie["slug"])
                    post_id = series_info["post_id"]
                    if not post_id:
                        logger.warning(f"   Could not get post_id for {movie['slug']}")
                        continue
                    
                    # Get full episode list
                    episodes = get_episodes(page, movie["slug"], post_id)
                    if not episodes:
                        logger.warning(f"   No episodes found for {movie['slug']}")
                        continue

                    logger.info(f"   Found {len(episodes)} episodes. Extracting streams...")
                    ep_data     = []
                    crawl_limit = min(len(episodes), CONFIG["MAX_EPISODES"])

                    for i in range(crawl_limit):
                        ep     = episodes[i]
                        stream = get_stream_url(page, context, ep)
                        if stream:
                            ep_data.append({"name": ep["name"], "stream": stream})
                        else:
                            logger.warning(f"      {ep['name']}: no stream found")

                        if (i + 1) % 10 == 0:
                            logger.info(f"   Progress: {i + 1}/{crawl_limit}")

                    if ep_data:
                        detail_json = build_detail_json(movie["slug"], ep_data)
                        detail_path = detail_dir / f"{movie['slug']}.json"
                        with open(detail_path, "w", encoding="utf-8") as f:
                            json.dump(detail_json, f, ensure_ascii=False, indent=2)
                        logger.info(f"   Saved {detail_path.name} ({len(ep_data)} episodes)")
                        channels.append(build_list_item(movie))
                    else:
                        logger.warning(f"   No valid streams for {movie['slug']}")

                except Exception as e:
                    logger.error(f"   Error processing {movie['slug']}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Critical error: {e}")
        finally:
            browser.close()

    list_output = {
        "id":          "hhpanda-thuyet-minh",
        "name":        "HHPanda - Thuyet Minh",
        "url":         f"{CONFIG['RAW_BASE']}/ophim",
        "color":       "#004444",
        "image":       {"url": f"{CONFIG['BASE_URL']}/wp-content/uploads/2024/10/logo.webp", "type": "cover"},
        "description": "Phim hoat hinh 3D Trung Quoc thuyet minh chat luong cao tu HHPanda.st",
        "grid_number": 3,
        "channels":    channels,
        "sorts":       [{"text": "Moi nhat", "type": "radio", "url": f"{CONFIG['RAW_BASE']}/ophim"}],
        "meta": {
            "source":      CONFIG["BASE_URL"],
            "total_items": len(channels),
            "updated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "version":     "2.1"
        }
    }

    list_path = Path(CONFIG["LIST_FILE"])
    with open(list_path, "w", encoding="utf-8") as f:
        json.dump(list_output, f, ensure_ascii=False, indent=2)

    logger.info(f"Done! Saved {list_path} + {len(channels)} detail files.")
    return list_output


if __name__ == "__main__":
    scrape()
