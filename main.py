#!/usr/bin/env python3
import logging
import re
import urllib.parse
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
import httpx
from urllib.parse import urljoin

# Setup logging
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("mersin_proxy.log"), logging.StreamHandler()]
)
logger = logging.getLogger()

# Constants
SOURCE_URL = "https://fishy.streamed.su/"
COOKIE_URL = "https://fishy.streamed.su/api/event"
TIMEOUT = 30
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Mobile Safari/537.36",
    "Referer": "https://embedstreams.top/",
    "Origin": "https://embedstreams.top",
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Sec-Ch-Ua": '"Not A(Brand";v="8", "Chromium";v="132"',
    "Sec-Ch-Ua-Mobile": "?1",
    "Sec-Ch-Ua-Platform": '"Android"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
    "Content-Type": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "X-Forwarded-For": "127.0.0.1"
}

SEGMENT_MAP = {}
CACHED_COOKIES = None

app = FastAPI()

@app.get("/ping")
async def ping():
    logger.info("Ping request received")
    return {"status": "ok"}

async def fetch_cookies():
    global CACHED_COOKIES
    if CACHED_COOKIES:
        logger.debug("Using cached cookies")
        return CACHED_COOKIES
    logger.debug(f"Fetching cookies from {COOKIE_URL}")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            payload = {"event": "pageview"}
            response = await client.post(COOKIE_URL, json=payload, headers=HEADERS)
            logger.debug(f"Cookie response: {response.status_code}, headers: {response.headers}")
            cookies = response.headers.get("set-cookie")
            if not cookies:
                logger.error("No cookies received")
                return None
            cookie_dict = {}
            for cookie in cookies.split(","):
                parts = cookie.split(";")[0].strip().split("=")
                if len(parts) == 2:
                    name, value = parts
                    name = name.strip().lstrip("_")
                    cookie_dict[name] = value.strip()
            required_cookies = ["ddg8_", "ddg10_", "ddg9_", "ddg1_"]
            formatted_cookies = "; ".join(
                f"{key}={cookie_dict.get(key)}" for key in required_cookies if key in cookie_dict
            )
            if not formatted_cookies:
                logger.error("No required cookies found")
                return None
            CACHED_COOKIES = formatted_cookies
            logger.debug(f"Formatted cookies: {formatted_cookies}")
            return formatted_cookies
        except httpx.HTTPError as e:
            logger.error(f"Error fetching cookies: {str(e)}")
            return None

async def fetch_m3u8_url():
    logger.debug(f"Fetching M3U8 URL from {SOURCE_URL}")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        try:
            response = await client.get(SOURCE_URL, headers=HEADERS)
            content = response.text
            m3u8_match = re.search(r'https?://[^\s"]+\.m3u8(?:\?[^"\s]*)?', content)
            if m3u8_match:
                m3u8_url = m3u8_match.group(0)
                logger.debug(f"Found M3U8 URL: {m3u8_url}")
                return m3u8_url
            logger.error("No M3U8 URL found in page content")
            return None
        except httpx.HTTPError as e:
            logger.error(f"Error fetching M3U8 URL: {str(e)}")
            return None

async def fetch_resource(url, cookies, retries=3):
    sources = [
        url,
        url.replace("rr.buytommy.top", "p2-panel.streamed.su"),
        url.replace("rr.buytommy.top", "flu.streamed.su")
    ]
    headers = HEADERS.copy()
    headers["Cookie"] = cookies
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        for source_url in sources:
            for attempt in range(1, retries + 1):
                logger.debug(f"Fetching (attempt {attempt}): {source_url}")
                try:
                    response = await client.get(source_url, headers=headers)
                    response.raise_for_status()
                    content = response.content
                    content_type = response.headers.get("content-type", "application/octet-stream")
                    if source_url.endswith(".ts") or source_url.endswith(".js"):
                        content_type = "video/mp2t"
                    logger.debug(f"Success: {source_url} - Status: {response.status_code}, Content-Type: {content_type}, Size: {len(content)} bytes")
                    return content, content_type
                except httpx.HTTPError as e:
                    logger.error(f"Failed: {source_url} - Error: {str(e)}")
                    if attempt == retries:
                        break
        logger.error(f"All attempts failed for {url}")
        return None

@app.get("/playlist.m3u8")
async def get_playlist(url: str, cookies: str, request: Request):
    logger.debug(f"Received playlist request: url={url}, cookies={cookies}, headers={request.headers}")
    try:
        # Normalize cookies
        cookies = urllib.parse.unquote(cookies).replace("%3B+", "; ").replace("%3D", "=")
        cookies = "; ".join(
            f"{pair.split('=')[0].lstrip('_')}={pair.split('=')[1]}"
            for pair in cookies.split("; ")
            if pair
        )
        logger.debug(f"Normalized cookies: {cookies}")

        # Try provided M3U8 URL
        result = await fetch_resource(url, cookies)
        if not result:
            logger.info("Provided M3U8 failed, fetching fresh URL and cookies")
            m3u8_url = await fetch_m3u8_url()
            cookies = await fetch_cookies()
            if not m3u8_url or not cookies:
                logger.error("Could not fetch M3U8 URL or cookies")
                raise HTTPException(status_code=500, detail="Could not fetch M3U8 URL or cookies")
            result = await fetch_resource(m3u8_url, cookies)
            if not result:
                logger.error("Error fetching M3U8")
                raise HTTPException(status_code=500, detail="Error fetching M3U8")

        m3u8_content, _ = result
        m3u8_content = m3u8_content.decode("utf-8")
        if "#EXTM3U" not in m3u8_content:
            logger.error("Invalid M3U8 content")
            raise HTTPException(status_code=500, detail="Invalid M3U8 content")

        SEGMENT_MAP.clear()
        m3u8_lines = m3u8_content.splitlines()
        base_url = urllib.parse.urlparse(url).scheme + "://" + urllib.parse.urlparse(url).netloc
        for i, line in enumerate(m3u8_lines):
            if line.startswith("#EXT-X-KEY") and "URI=" in line:
                original_uri = line.split('URI="')[1].split('"')[0]
                key_path = original_uri.lstrip("/")
                new_uri = f"/{key_path}"
                m3u8_lines[i] = line.replace(original_uri, new_uri)
                full_key_url = urljoin(url, original_uri)
                SEGMENT_MAP[key_path] = full_key_url
                logger.debug(f"Mapping key {key_path} to {full_key_url}")
            elif line.startswith("https://"):
                original_url = line.strip()
                segment_name = original_url.split("/")[-1].replace(".js", ".ts")
                new_url = f"/{segment_name}"
                m3u8_lines[i] = new_url
                direct_url = original_url.replace("https://corsproxy.io/?url=", "")
                if direct_url.startswith("https://flu.streamed.su"):
                    SEGMENT_MAP[segment_name] = direct_url
                else:
                    segment_name_base = segment_name.replace(".ts", ".js")
                    direct_url = f"https://p2-panel.streamed.su/bucket-44677-gjnru5ktoa/{segment_name_base}"
                    SEGMENT_MAP[segment_name] = direct_url
                logger.debug(f"Mapping segment {segment_name} to {direct_url}")
        m3u8_content = "\n".join(m3u8_lines)
        logger.debug(f"Rewritten M3U8 content:\n{m3u8_content}")

        return Response(
            content=m3u8_content,
            media_type="application/vnd.apple.mpegurl",
            headers={"Access-Control-Allow-Origin": "*"}
        )
    except Exception as e:
        logger.error(f"Error processing M3U8: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching M3U8: {str(e)}")

@app.get("/{path:path}")
async def get_resource(path: str, request: Request):
    logger.debug(f"Received resource request: path={path}, query={request.query_params}, headers={request.headers}")
    try:
        cookies = request.query_params.get("cookies", "")
        resource_url = SEGMENT_MAP.get(path)
        if not resource_url:
            resource_url = f"https://rr.buytommy.top/{path.replace('.ts', '.js')}"
            logger.info(f"Unmapped request, trying: {resource_url}")
        range_header = request.headers.get("Range")
        headers = HEADERS.copy()
        if cookies:
            headers["Cookie"] = urllib.parse.unquote(cookies)
        if range_header:
            headers["Range"] = range_header
            logger.debug(f"Range request: {range_header}")
        result = await fetch_resource(resource_url, cookies)
        if not result:
            logger.error(f"Failed to fetch resource: {resource_url}")
            raise HTTPException(status_code=500, detail="Error fetching resource")
        content, content_type = result
        response_headers = {
            "Access-Control-Allow-Origin": "*",
            "Accept-Ranges": "bytes"
        }
        if range_header:
            response_headers["Content-Range"] = f"bytes 0-{len(content)-1}/{len(content)}"
            return Response(content=content, status_code=206, media_type=content_type, headers=response_headers)
        return Response(content=content, media_type=content_type, headers=response_headers)
    except Exception as e:
        logger.error(f"Error fetching resource: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error fetching resource: {str(e)}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
