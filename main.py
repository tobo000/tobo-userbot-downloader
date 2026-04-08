import os
import asyncio
import aiohttp
import time
import subprocess
import json
import re
import sqlite3
import logging
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# --- 1. CONFIGURATION ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
sudo_raw = os.getenv("SUDO_USERS", "")
SUDO_USERS = [int(x.strip()) for x in sudo_raw.split(",") if x.strip().isdigit()]

app = Client("tobo_pro_session", api_id=int(API_ID), api_hash=API_HASH, sleep_threshold=300)
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

# --- 2. DATABASE (The Brain of the Bot) ---
DB_NAME = "bot_archive.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    # status: 0=pending, 1=done, 2=failed
    cursor.execute("CREATE TABLE IF NOT EXISTS archive_queue (url TEXT PRIMARY KEY, username TEXT, chat_id INTEGER, status INTEGER DEFAULT 0)")
    conn.commit()
    conn.close()

def add_to_queue(url, username, chat_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT OR IGNORE INTO archive_queue (url, username, chat_id, status) VALUES (?, ?, ?, 0)", (url, username, chat_id))
        conn.commit()
    except: pass
    conn.close()

def get_next_task():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT url, username, chat_id FROM archive_queue WHERE status = 0 LIMIT 1")
    res = cursor.fetchone()
    conn.close()
    return res

def mark_status(url, status):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE archive_queue SET status = ? WHERE url = ?", (status, url))
    conn.commit()
    conn.close()

# --- 3. HELPERS (Keeping your Original Code) ---
def get_human_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0: return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

def get_video_meta(video_path):
    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        return 0, 1280, 720, False
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        res = subprocess.check_output(cmd).decode('utf-8')
        data = json.loads(res)
        duration = int(float(data.get('format', {}).get('duration', 0)))
        v = next((s for s in data['streams'] if s['codec_type'] == 'video'), {})
        return duration, int(v.get('width', 1280)), int(v.get('height', 720)), any(s['codec_type'] == 'audio' for s in data['streams'])
    except: return 0, 1280, 720, False

def download_nitro(url, path, headers, size):
    """Your original Nitro Downloader logic"""
    import requests
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        with open(path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024*1024): f.write(chunk)

# ==========================================
# SCRAPER ENGINE (Improved for 1000+ items)
# ==========================================
async def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/123.0.0.0'}
    try:
        async with aiohttp.ClientSession(headers=headers) as sess:
            async with sess.get(url, timeout=20) as r:
                html = await r.text()
                soup = BeautifulSoup(html, 'html.parser')
                title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
                p_l = [x if x.startswith('http') else 'https:' + x for x in [i.get('data-src') or i.get('src') for i in soup.select('div.img img') if "erome.com" in (i.get('data-src') or i.get('src', ''))] if x]
                v_l = list(dict.fromkeys(re.findall(r'https?://[^\s"\'\\>]+erome\.com[^\s"\'\\>]+\.mp4', html)))
                return title, p_l, v_l
    except: return "Error", [], []

# ==========================================
# THE WORKER (This prevents the "Stuck" issue)
# ==========================================
async def worker():
    """Background machine that processes 1034 albums one by one safely"""
    while True:
        task = get_next_task()
        if not task:
            await asyncio.sleep(10) # Wait for new links
            continue
        
        url, username, chat_id = task
        try:
            title, photos, videos = await scrape_album_details(url)
            user_folder = os.path.join(DOWNLOAD_DIR, username)
            os.makedirs(user_folder, exist_ok=True)

            # Archiving Photos
            if photos:
                p_paths = []
                for i, p_url in enumerate(photos, 1):
                    p = os.path.join(user_folder, f"img_{i}.jpg")
                    async with aiohttp.ClientSession() as s:
                        async with s.get(p_url) as r:
                            if r.status == 200: open(p, 'wb').write(await r.read()); p_paths.append(p)
                    if len(p_paths) == 10 or i == len(photos):
                        try: await app.send_media_group(chat_id, [InputMediaPhoto(pf, caption=f"🖼 {title}") for pf in p_paths])
                        except: pass
                        for pf in p_paths: os.remove(pf)
                        p_paths = []

            # Archiving Videos
            if videos:
                for i, v_url in enumerate(videos, 1):
                    filepath = os.path.join(user_folder, "bg_video.mp4")
                    import requests
                    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': url}
                    with requests.get(v_url, headers=headers, stream=True, timeout=60) as r:
                        with open(filepath, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=1024*1024): f.write(chunk)
                    
                    dur, w, h, audio = get_video_meta(filepath)
                    if not audio:
                        temp = filepath + ".fix.mp4"
                        subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100', '-i', filepath, '-c:v', 'copy', '-c:a', 'aac', '-shortest', temp, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        os.remove(filepath); os.rename(temp, filepath)
                    
                    thumb = filepath + ".jpg"
                    subprocess.run(['ffmpeg', '-ss', '00:00:01', '-i', filepath, '-vframes', '1', '-vf', 'scale=320:-1', thumb, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    await app.send_video(chat_id, filepath, thumb=thumb, duration=dur, width=w, height=h, caption=f"🎬 {title}", supports_streaming=True)
                    if os.path.exists(filepath): os.remove(filepath)
                    if os.path.exists(thumb): os.remove(thumb)

            mark_status(url, 1) # Done
            print(f"WORKER: Finished {url}")
            await asyncio.sleep(2) # Rest for 1GB RAM safety
        except Exception as e:
            print(f"WORKER ERROR: {e}")
            mark_status(url, 2) # Mark as failed to try later

# ==========================================
# COMMAND HANDLERS
# ==========================================
@app.on_message(filters.command("user", prefixes=".") & filters.user(SUDO_USERS))
async def user_cmd(client, message):
    username = message.command[1].strip().split("erome.com/")[-1].split('/')[0]
    status = await message.reply(f"🕵️‍♂️ **Harvesting all items for {username}...**\nI will process them in background. You don't need to wait!")
    
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/122.0.0.0'}
    total_found = 0
    async with aiohttp.ClientSession(headers=headers) as sess:
        for tab in ["", "/reposts"]:
            page = 1
            while True:
                url = f"https://www.erome.com/{username}{tab}?page={page}"
                async with sess.get(url) as r:
                    if r.status != 200: break
                    html = await r.text()
                    ids = list(dict.fromkeys(re.findall(r'/a/([a-zA-Z0-9]+)', html)))
                    if not ids: break
                    for aid in ids:
                        add_to_queue(f"https://www.erome.com/a/{aid}", username, message.chat.id)
                        total_found += 1
                    if "Next" not in html: break
                    page += 1
                    await asyncio.sleep(0.5)

    await status.edit_text(f"✅ **Harvesting Complete!**\nFound: `{total_found}` items.\n🚀 Background Worker has started processing them. Please wait and see your files arriving!")

@app.on_message(filters.command("dl", prefixes=".") & filters.user(SUDO_USERS))
async def dl_handler(client, message):
    urls = list(dict.fromkeys([u.strip() for u in message.text.split('\n') if "erome.com/a/" in u]))
    for url in urls:
        add_to_queue(url, "single", message.chat.id)
    await message.reply("✅ Added to queue!")

async def main():
    init_db()
    asyncio.create_task(worker()) # Start the worker in background
    async with app:
        print("LOG: V9.30 Background Worker is Online!")
        await idle()

if __name__ == "__main__":
    app.run(main())
