"""
app.py
Unified Automation, Stream Extraction, and Web Server for Cricfy TV wrapper.
"""

import asyncio
import os
import re
from urllib.parse import urlparse
from typing import Dict, Any
import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Response as PlaywrightResponse, Request
from fastapi import FastAPI, HTTPException, Response as FastAPIResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import uvicorn

# =====================================================================
# COMPONENT 1: FRONTEND HTML PLAYER (Served from memory)
# =====================================================================

HTML_FRONTEND = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cricfy TV Web Wrapper</title>
    <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background-color: #0d1117; color: #c9d1d9; font-family: -apple-system, sans-serif; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; padding: 20px; }
        .container { width: 100%; max-width: 960px; background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
        .header { padding: 16px 24px; background: #21262d; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #30363d; }
        .header h1 { font-size: 1.2rem; color: #58a6ff; }
        .status-badge { font-size: 0.85rem; padding: 4px 12px; border-radius: 12px; background: #238636; color: #ffffff; font-weight: 600; }
        .status-badge.reconnecting { background: #d29922; }
        .video-wrapper { position: relative; width: 100%; padding-top: 56.25%; background: #000; }
        video { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
        .controls { padding: 16px 24px; display: flex; gap: 12px; background: #161b22; }
        button { background: #238636; color: white; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; }
        button:hover { background: #2ea043; }
        button.secondary { background: #21262d; border: 1px solid #30363d; }
        .log-output { padding: 12px 24px; font-family: monospace; font-size: 0.8rem; color: #8b949e; background: #0d1117; border-top: 1px solid #30363d; }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <h1>Cricfy TV Web Player</h1>
        <span id="statusBadge" class="status-badge">Connecting...</span>
    </div>
    <div class="video-wrapper">
        <video id="videoPlayer" controls autoplay playsinline></video>
    </div>
    <div class="controls">
        <button onclick="reloadStream()">Force Token Refresh</button>
        <button class="secondary" onclick="document.getElementById('videoPlayer').muted = !document.getElementById('videoPlayer').muted">Toggle Audio</button>
    </div>
    <div class="log-output" id="logOutput">System initialized. Awaiting manifest fetch...</div>
</div>
<script>
    const video = document.getElementById('videoPlayer');
    const statusBadge = document.getElementById('statusBadge');
    const logOutput = document.getElementById('logOutput');
    let hls = null;

    function log(msg) {
        logOutput.innerText = `[${new Date().toLocaleTimeString()}] ${msg}`;
        console.log(`[Player] ${msg}`);
    }
    function setStatus(text, isReconn = false) {
        statusBadge.innerText = text;
        isReconn ? statusBadge.classList.add('reconnecting') : statusBadge.classList.remove('reconnecting');
    }

    async function initializePlayer() {
        log("Fetching active stream tokens...");
        setStatus("Fetching Stream...", true);
        try {
            const res = await fetch('/api/stream');
            const data = await res.json();
            if (!data.m3u8_url) { log("No manifest. Requesting extraction..."); return forceRefresh(); }
            setupHlsPlayer(data.m3u8_url);
        } catch (err) {
            log(`Error: ${err.message}. Retrying in 5s...`);
            setTimeout(initializePlayer, 5000);
        }
    }

    function setupHlsPlayer(manifestUrl) {
        if (hls) hls.destroy();
        if (Hls.isSupported()) {
            hls = new Hls({ enableWorker: true, lowLatencyMode: true });
            hls.loadSource('/proxy/m3u8');
            hls.attachMedia(video);
            hls.on(Hls.Events.MANIFEST_PARSED, () => { setStatus("LIVE"); log("Playback started."); video.play().catch(e=>{}); });
            hls.on(Hls.Events.ERROR, (e, data) => {
                if (data.fatal && data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                    log("Network error. Healing stream..."); setStatus("Healing...", true); hls.startLoad(); forceRefresh();
                } else if (data.fatal && data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                    hls.recoverMediaError();
                }
            });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = manifestUrl;
            video.addEventListener('loadedmetadata', () => { setStatus("LIVE"); video.play(); });
        }
    }

    async function forceRefresh() {
        setStatus("Refreshing Tokens...", true);
        try {
            const res = await fetch('/api/refresh');
            const data = await res.json();
            if (data.success && data.data.m3u8_url) { log("Tokens renewed."); setupHlsPlayer(data.data.m3u8_url); }
        } catch (e) { log(`Refresh Failed: ${e.message}`); }
    }
    function reloadStream() { forceRefresh(); }
    setInterval(() => { forceRefresh(); }, 570000); // 9.5 min loop
    window.addEventListener('DOMContentLoaded', initializePlayer);
</script>
</body>
</html>
"""

# =====================================================================
# COMPONENT 2: CLOUD EMULATOR AUTOMATOR (Ported to Python)
# =====================================================================

async def download_apk(url: str, output_path: str):
    print(f"[+] Downloading APK from {url}")
    async with httpx.AsyncClient(follow_redirects=True) as client:
        with open(output_path, 'wb') as f:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                async for chunk in response.aiter_bytes():
                    f.write(chunk)
    return output_path

async def run_cloud_emulator():
    """Automates an online Android sandbox, uploads APK, and waits for initialization."""
    sandbox_url = os.getenv("SANDBOX_URL", "https://www.apkonline.net/runapk/start-apk.html")
    apk_url = os.getenv("APK_URL", "https://cricfytv.su/downloads/cricfytv.apk")
    temp_apk = "temp_app.apk"
    
    try:
        await download_apk(apk_url, temp_apk)
    except Exception as e:
        print(f"[-] Could not download APK: {e}")

    print("[+] Launching Emulator Automation...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-web-security"])
        page = await browser.new_page(viewport={"width": 1280, "height": 800})
        
        try:
            print(f"[+] Navigating to Sandbox: {sandbox_url}")
            await page.goto(sandbox_url, wait_until="networkidle", timeout=60000)
            
            # Handle File Upload
            file_input = await page.query_selector('input[type="file"]')
            if file_input and os.path.exists(temp_apk):
                print("[+] Uploading APK...")
                await file_input.set_input_files(temp_apk)
                
                # Click upload/start button
                start_btn = await page.query_selector('button#upload_button, button.btn-run, input[type="submit"]')
                if start_btn:
                    await start_btn.click()
            
            # Wait for Android canvas viewport
            print("[+] Waiting for Android sandbox viewport...")
            await page.wait_for_selector('canvas#emulator_canvas, iframe#emulator_frame, #emulator', timeout=90000)
            print("[✔] Sandbox interface loaded successfully.")
            
        except Exception as e:
            print(f"[❌] Sandbox Automation Error: {e}")
        finally:
            if os.path.exists(temp_apk):
                os.remove(temp_apk)
            await browser.close()

# =====================================================================
# COMPONENT 3: DYNAMIC STREAM EXTRACTOR 
# =====================================================================

class StreamExtractor:
    def __init__(self, target_url: str = "https://cricfytv.su"):
        self.target_url = target_url
        self.extracted_data: Dict[str, Any] = {
            "m3u8_url": None,
            "headers": {},
            "status": "idle"
        }
        self._m3u8_found_event = asyncio.Event()

    async def _handle_response(self, response: PlaywrightResponse):
        url = response.url
        content_type = response.headers.get("content-type", "").lower()
        is_m3u8 = bool(re.search(r'\.m3u8(\?.*)?$', url, re.IGNORECASE))
        is_hls_type = "application/x-mpegurl" in content_type or "application/vnd.apple.mpegurl" in content_type

        if (is_m3u8 or is_hls_type) and not self.extracted_data["m3u8_url"]:
            print(f"[✔] Stream Manifest Intercepted: {url}")
            req: Request = response.request
            
            captured_headers = {
                "User-Agent": await req.header_value("user-agent") or "",
                "Referer": await req.header_value("referer") or self.target_url,
                "Origin": await req.header_value("origin") or f"{urlparse(self.target_url).scheme}://{urlparse(self.target_url).netloc}",
                "Authorization": await req.header_value("authorization") or ""
            }

            self.extracted_data["m3u8_url"] = url
            self.extracted_data["headers"] = {k: v for k, v in captured_headers.items() if v}
            self.extracted_data["status"] = "success"
            self._m3u8_found_event.set()

    async def extract(self, timeout_seconds: int = 30) -> Dict[str, Any]:
        self.extracted_data["status"] = "extracting"
        self._m3u8_found_event.clear()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-web-security"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)")
            page = await context.new_page()
            page.on("response", self._handle_response)

            try:
                print(f"[+] Navigating to container: {self.target_url}")
                await page.goto(self.target_url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)

                try:
                    await asyncio.wait_for(self._m3u8_found_event.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    print("[-] Network wait timeout. Triggering interactions...")
                    play_buttons = await page.query_selector_all("button, .play-btn, .player, video")
                    for btn in play_buttons[:3]:
                        try:
                            await btn.click(force=True)
                            await asyncio.sleep(2)
                        except: pass

            except Exception as e:
                print(f"[❌] Extraction Error: {str(e)}")
                self.extracted_data["status"] = f"error: {str(e)}"
            finally:
                await browser.close()

        return self.extracted_data

# =====================================================================
# COMPONENT 4: FASTAPI SERVER & SELF-HEALING ENGINE
# =====================================================================

app = FastAPI(title="Cricfy TV Web Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

STREAM_CACHE: Dict[str, Any] = {"m3u8_url": None, "headers": {}, "status": "uninitialized"}
TARGET_SOURCE_URL = "https://cricfytv.su"

async def refresh_stream_job():
    """Background task to extract fresh M3U8 links every 10 mins"""
    global STREAM_CACHE
    while True:
        print("\n[🔄] Self-Healing Scheduler: Refreshing stream manifest & session tokens...")
        try:
            extractor = StreamExtractor(TARGET_SOURCE_URL)
            result = await extractor.extract()
            if result.get("m3u8_url"):
                STREAM_CACHE.update({"m3u8_url": result["m3u8_url"], "headers": result["headers"], "status": "healthy"})
                print(f"[✔] Cache Updated Successfully")
            else:
                print("[-] Refresh failed. Retaining current cache.")
        except Exception as e:
            print(f"[❌] Refresh Job Error: {e}")
        await asyncio.sleep(600) # 10 Minutes

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(refresh_stream_job())

@app.get("/api/stream")
async def get_stream_info():
    if not STREAM_CACHE.get("m3u8_url"):
        extractor = StreamExtractor(TARGET_SOURCE_URL)
        res = await extractor.extract()
        if res.get("m3u8_url"):
            STREAM_CACHE.update({"m3u8_url": res["m3u8_url"], "headers": res["headers"], "status": "healthy"})
    return STREAM_CACHE

@app.get("/api/refresh")
async def trigger_manual_refresh():
    extractor = StreamExtractor(TARGET_SOURCE_URL)
    res = await extractor.extract()
    if res.get("m3u8_url"):
        STREAM_CACHE.update({"m3u8_url": res["m3u8_url"], "headers": res["headers"], "status": "healthy"})
        return {"success": True, "data": STREAM_CACHE}
    raise HTTPException(status_code=502, detail="Stream token extraction failed.")

@app.get("/proxy/m3u8")
async def proxy_m3u8_stream():
    m3u8_url = STREAM_CACHE.get("m3u8_url")
    if not m3u8_url: raise HTTPException(status_code=404, detail="No active stream")
    
    async with httpx.AsyncClient(verify=False) as client:
        try:
            resp = await client.get(m3u8_url, headers=STREAM_CACHE.get("headers", {}), timeout=10.0)
            return FastAPIResponse(content=resp.content, status_code=resp.status_code, media_type="application/vnd.apple.mpegurl")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Proxy Error: {str(e)}")

@app.get("/", response_class=HTMLResponse)
async def serve_player():
    """Serves the HLS Player HTML defined at the top of the file."""
    return HTMLResponse(content=HTML_FRONTEND)

# =====================================================================
# ENTRY POINT
# =====================================================================
if __name__ == "__main__":
    print("[*] Starting Unified Engine...")
    # Optional: If you want to trigger the sandbox automation immediately on boot, uncomment the line below.
    # asyncio.run(run_cloud_emulator())
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
