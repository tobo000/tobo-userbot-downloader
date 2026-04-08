import os
import asyncio
import aiohttp
import aiofiles
import time
import subprocess
import json
import re
import sqlite3
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle, errors
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified, FloodWait
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# --- 1. CONFIGURATION ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
sudo_raw = os.getenv("SUDO_USERS", "")
SUDO_USERS = [int(x.strip()) for x in sudo_raw.split(",") if x.strip().isdigit()]

app = Client("tobo_pro_session", api_id=int(API_ID), api_hash=API_HASH, sleep_threshold=180)
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

cancel_tasks = {}
# [RAM SURVIVAL] Strictly 1 worker for 1GB VPS
executor = ThreadPoolExecutor(max_workers=1)
sync_counter = 0

# --- 2. DATABASE & SYNC ---
DB_NAME = "bot_archive.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS processed (url TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def is_processed(url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed WHERE url = ?", (url,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def git_sync_heavy():
    """Infrequent sync to prevent network hangs"""
    global sync_counter
    sync_counter += 1
    if sync_counter % 20 == 0:
        try:
            subprocess.run(["git", "add", DB_NAME], capture_output=True)
            subprocess.run(["git", "commit", "-m", "Sync Memory"], capture_output=True)
            subprocess.run(["git", "push"], capture_output=True)
            print("LOG: Cloud backup done.")
        except: pass

def mark_processed(url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO processed (url) VALUES (?)", (url,))
        conn.commit()
        git_sync_heavy()
    except: pass
    conn.close()

# --- 3. HELPERS ---
def create_progress_bar(current, total):
    if total <= 0: return "[░░░░░░░░░░] 0%"
    pct = min(100, (current / total) * 100)
    return f"[{'█' * int(pct/10)}{'░' * (10 - int(pct/10))}] {pct:.1f}%"

def get_human_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0: return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

async def edit_status_safe(message, text):
    try: await message.edit_text(text)
    except (MessageNotModified, FloodWait): pass
    except: pass

async def progress_callback(current, total, client, status_msg, start_time, action_text):
    now = time.time()
    if now - start_time[0] > 10: # Long interval to prevent FloodWait
        bar = create_progress_bar(current, total)
        await edit_status_safe(status_msg, f"🚀 **{action_text}**\n\n{bar}\n📦 **Size:** {get_human_size(current)} / {get_human_size(total)}")
        start_time[0] = now

async def async_download_img(url, path):
    """Non-blocking image download"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as r:
                if r.status == 200:
                    async with aiofiles.open(path, mode='wb') as f:
                        await f.write(await r.read())
                    return True
    except: return False

def get_video_meta(video_path):
    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        return 0, 1280, 720, False
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        res = subprocess.check_output(cmd).decode('utf-8')
        data = json.loads(res)
        duration = int(float(data.get('format', {}).get('duration', 0)))
        v = next((s for s in data.get('streams', []) if s['codec_type'] == 'video'), {})
        return duration, int(v.get('width', 1280)), int(v.get('height', 720)), any(s['codec_type'] == 'audio' for s in data['streams'])
    except: return 0, 1280, 720, False

# ==========================================
# SCRAPER ENGINE
# ==========================================
def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/123.0.0.0', 'Referer': 'https://www.erome.com/'}
    try:
        import requests
        res = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
        p_l = [x if x.startswith('http') else 'https:' + x for x in [i.get('data-src') or i.get('src') for i in soup.select('div.img img') if "erome.com" in (i.get('data-src') or i.get('src', ''))] if x]
        raw_vids = re.findall(r'https?://[^\s"\'\\>]+erome\.com[^\s"\'\\>]+\.mp4', res.text)
        v_l = list(dict.fromkeys(raw_vids))
        v_l.sort(key=lambda x: (re.search(r'(1080|720|high)', x.lower()) is None, x))
        return title, p_l, v_l
    except: return "Error", [], []

def get_all_links(username):
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/122.0.0.0'}
    all_links = []
    import requests
    for tab in ["", "/reposts"]:
        page = 1
        while True:
            url = f"https://www.erome.com/{username}{tab}?page={page}"
            try:
                res = requests.get(url, headers=headers, timeout=20)
                album_ids = re.findall(r'href=["\'](/a/[a-zA-Z0-9]+)["\']', res.text)
                if not album_ids: break
                found_new = 0
                for aid in album_ids:
                    f_url = "https://www.erome.com" + aid
                    if f_url not in all_links: all_links.append(f_url); found_new += 1
                if found_new == 0 or "Next" not in res.text: break
                page += 1
            except: break
    return all_links

# ==========================================
# DELIVERY ENGINE (The Survival Logic)
# ==========================================
async def process_album(client, message, url, username, current, total):
    album_id = url.rstrip('/').split('/')[-1]
    if is_processed(url): return True

    title, photos, videos = scrape_album_details(url)
    if not photos and not videos: return False
    
    user_folder = os.path.join(DOWNLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)
    
    status = await message.reply_text(f"📥 **[{current}/{total}]** Archive: `{title}`")
    
    if photos:
        p_paths = []
        for i, p_url in enumerate(photos, 1):
            p = os.path.join(user_folder, f"img_{album_id}_{i}.jpg")
            if await async_download_img(p_url, p): p_paths.append(p)
            if len(p_paths) == 10 or i == len(photos):
                try: await message.reply_media_group([InputMediaPhoto(pf, caption=f"🖼 {title}") for pf in p_paths])
                except: pass
                for pf in p_paths: 
                    if os.path.exists(pf): os.remove(pf)
                p_paths = []

    if videos:
        for i, v_url in enumerate(videos, 1):
            filepath = os.path.join(user_folder, f"{album_id}_v{i}.mp4")
            try:
                # Direct download without nitro to save RAM
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(v_url, timeout=600) as r:
                        async with aiofiles.open(filepath, mode='wb') as f:
                            await f.write(await r.read())
                
                dur, w, h, audio = get_video_meta(filepath)
                if not audio:
                    temp = filepath + ".fix.mp4"
                    subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100', '-i', filepath, '-c:v', 'copy', '-c:a', 'aac', '-shortest', temp, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.remove(filepath); os.rename(temp, filepath)
                
                thumb = filepath + ".jpg"
                subprocess.run(['ffmpeg', '-ss', '00:00:01', '-i', filepath, '-vf', 'scale=320:-1', '-vframes', '1', thumb, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                
                await message.reply_video(filepath, thumb=thumb, duration=dur, width=w, height=h, caption=f"🎬 {title}", supports_streaming=True)
                if os.path.exists(filepath): os.remove(filepath)
                if os.path.exists(thumb): os.remove(thumb)
            except: pass
    
    mark_processed(url)
    await status.delete()
    return True

# ==========================================
# COMMAND HANDLERS
# ==========================================
@app.on_message(filters.command("user", prefixes=".") & filters.user(SUDO_USERS))
async def user_cmd(client, message):
    username = message.command[1].strip().split('/')[-1]
    msg = await message.reply("🛰 **Deep Scanning Profile...**")
    
    loop = asyncio.get_event_loop()
    all_urls = await loop.run_in_executor(None, get_all_links, username)
    
    if not all_urls: return await msg.edit_text("❌ No items.")
    
    total = len(all_urls)
    await msg.edit_text(f"✅ Found: `{total}`. Archiving in Survival Mode...")
    
    for i, url in enumerate(all_urls, 1):
        if cancel_tasks.get(message.chat.id): break
        await process_album(client, message, url, username, i, total)
        await asyncio.sleep(3) # Heavy rest for VPS

    await msg.delete(); await message.reply(f"🏆 Finished `{username}`!")

@app.on_message(filters.command("dl", prefixes=".") & filters.user(SUDO_USERS))
async def dl_handler(client, message):
    urls = list(dict.fromkeys([u.strip() for u in message.text.split('\n') if "erome.com/a/" in u]))
    for i, url in enumerate(urls, 1): await process_album(client, message, url, "single", i, len(urls))
    try: await message.delete()
    except: pass

async def main():
    init_db()
    async with app:
        print("LOG: V9.06 Rock Stable Online!")
        await idle()

if __name__ == "__main__":
    app.run(main())
