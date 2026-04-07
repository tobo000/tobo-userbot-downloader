import os
import asyncio
import requests
import time
import subprocess
import json
import re
import sqlite3
import random
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import MessageNotModified
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
sudo_raw = os.getenv("SUDO_USERS", "")
SUDO_USERS = [int(x.strip()) for x in sudo_raw.split(",") if x.strip().isdigit()]

app = Client("tobo_pro_session", api_id=int(API_ID), api_hash=API_HASH)
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
session = requests.Session()

cancel_tasks = {}

# --- 1. DATABASE & AUTO-SYNC ---
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

def mark_processed(url):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO processed (url) VALUES (?)", (url,))
        conn.commit()
        # Auto Sync to GitHub
        subprocess.run(["git", "add", DB_NAME], check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Sync DB"], check=True, capture_output=True)
        subprocess.run(["git", "push"], check=True, capture_output=True)
    except: pass
    conn.close()

# --- 2. HELPERS ---
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
    except MessageNotModified: pass
    except: pass

async def progress_callback(current, total, client, status_msg, start_time, action_text):
    now = time.time()
    if now - start_time[0] > 5: 
        bar = create_progress_bar(current, total)
        await edit_status_safe(status_msg, f"🚀 **{action_text}**\n\n{bar}\n📦 **Size:** {get_human_size(current)} / {get_human_size(total)}")
        start_time[0] = now

def get_video_meta(video_path):
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        res = subprocess.check_output(cmd).decode('utf-8')
        data = json.loads(res)
        duration = int(float(data.get('format', {}).get('duration', 0)))
        v = next((s for s in data['streams'] if s['codec_type'] == 'video'), {})
        return duration, int(v.get('width', 1280)), int(v.get('height', 720)), any(s['codec_type'] == 'audio' for s in data['streams'])
    except: return 0, 1280, 720, False

async def download_with_progress(url, path, headers, size, status_msg, action_text):
    last_update = [time.time()]; downloaded = 0
    try:
        with requests.get(url, headers=headers, stream=True, timeout=60) as r:
            with open(path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk:
                        f.write(chunk); downloaded += len(chunk)
                        now = time.time()
                        if now - last_update[0] > 5:
                            bar = create_progress_bar(downloaded, size)
                            await edit_status_safe(status_msg, f"📥 **{action_text}**\n\n{bar}\n📦 **Progress:** {get_human_size(downloaded)} / {get_human_size(size)}")
                            last_update[0] = now
        return True
    except: return False

def download_nitro(url, path, headers, size, segs=4):
    chunk = size // segs
    def dl_part(s, e, n):
        pp = f"{path}.p{n}"; h = headers.copy(); h['Range'] = f'bytes={s}-{e}'
        with session.get(url, headers=h, stream=True) as r:
            with open(pp, 'wb') as f:
                for chk in r.iter_content(chunk_size=1024*1024): f.write(chk)
    with ThreadPoolExecutor(max_workers=segs) as ex:
        for i in range(segs): ex.submit(dl_part, i*chunk, (i+1)*chunk-1 if i < size-1 else size-1, i)
    with open(path, 'wb') as f:
        for i in range(segs):
            pp = f"{path}.p{i}"
            if os.path.exists(pp):
                with open(pp, 'rb') as pf: f.write(pf.read()); os.remove(pp)

# ==========================================
# SUPER STEALTH SCRAPER (V8.98)
# ==========================================
def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36', 'Referer': 'https://www.erome.com/'}
    try:
        res = session.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
        p_l = list(dict.fromkeys([x if x.startswith('http') else 'https:' + x for x in [i.get('data-src') or i.get('src') for i in soup.select('div.img img') if "erome.com" in (i.get('data-src') or i.get('src', ''))] if x]))
        v_l = []
        for tag in soup.find_all(['video', 'source']):
            src = tag.get('src') or tag.get('data-src')
            if src:
                if src.startswith('//'): src = 'https:' + src
                if not src.startswith('http'): src = 'https://www.erome.com' + src
                if ".mp4" in src.lower(): v_l.append(src)
        raw_vids = re.findall(r'https?://[^\s"\'\\>]+erome\.com[^\s"\'\\>]+\.mp4', res.text)
        v_l.extend(raw_vids); v_l = list(dict.fromkeys(v_l))
        v_l.sort(key=lambda x: (re.search(r'(1080|720|high)', x.lower()) is None, x))
        return title, p_l, v_l
    except: return "Error", [], []

async def scan_all_content(username, status_msg):
    username = username.split('/')[-1].split('?')[0]
    all_links = []
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ]
    
    for tab in ["", "/reposts"]:
        page = 1
        while True:
            await edit_status_safe(status_msg, f"🔍 **Scanning `{username}`...**\n🚀 Found: `{len(all_links)}` items\n📄 Page: {page}")
            url = f"https://www.erome.com/{username}{tab}?page={page}"
            headers = {'User-Agent': random.choice(user_agents), 'Referer': 'https://www.google.com/'}
            try:
                res = session.get(url, headers=headers, timeout=20)
                print(f"LOG: Scanning {url} - Status: {res.status_code}") # មើលក្នុង Terminal
                
                if res.status_code == 403:
                    await status_msg.reply_text("❌ **Access Denied (403)!**\nErome blocked your IP. Please try again later.")
                    return all_links
                
                if res.status_code != 200: break
                
                # Use REGEX to find all album IDs
                album_ids = re.findall(r'/a/([a-zA-Z0-9]+)', res.text)
                if not album_ids: break
                
                found_new = 0
                for aid in album_ids:
                    f_url = f"https://www.erome.com/a/{aid}"
                    if f_url not in all_links:
                        all_links.append(f_url); found_new += 1
                
                if found_new == 0 or "Next" not in res.text: break
                page += 1
                await asyncio.sleep(1) # Slow down to stay stealthy
            except: break
    return all_links

# ==========================================
# DELIVERY ENGINE
# ==========================================
async def process_album(client, message, url, username, current, total):
    album_id = url.rstrip('/').split('/')[-1]
    if is_processed(url): return True
    title, photos, videos = scrape_album_details(url)
    if not photos and not videos: return False
    user_folder = os.path.join(DOWNLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)
    
    status = await message.reply_text(f"📥 **[{current}/{total}]** Preparing: `{title}`")
    if photos:
        p_paths = []
        for i, p_url in enumerate(photos, 1):
            p = os.path.join(user_folder, f"img_{album_id}_{i}.jpg")
            r = requests.get(p_url); open(p, 'wb').write(r.content); p_paths.append(p)
            if len(p_paths) == 10 or i == len(photos):
                await message.reply_media_group([InputMediaPhoto(pf, caption=f"🖼 {title}") for pf in p_paths])
                for pf in p_paths: os.remove(pf)
                p_paths = []
    if videos:
        for i, v_url in enumerate(videos, 1):
            filepath = os.path.join(user_folder, f"{album_id}_v{i}.mp4")
            try:
                headers = {'User-Agent': 'Mozilla/5.0', 'Referer': url}
                with requests.get(v_url, headers=headers, stream=True, timeout=15) as r:
                    size = int(r.headers.get('content-length', 0))
                if size > 15*1024*1024:
                    await status.edit_text(f"📥 **[{current}/{total}]** Nitro Downloading..."); download_nitro(v_url, filepath, headers, size)
                else:
                    await download_with_progress(v_url, filepath, headers, size, status, f"Downloading Video {i}")
                dur, w, h, audio = get_video_meta(filepath)
                if not audio:
                    temp = filepath + ".fix.mp4"
                    subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100', '-i', filepath, '-c:v', 'copy', '-c:a', 'aac', '-shortest', temp, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.remove(filepath); os.rename(temp, filepath)
                thumb = filepath + ".jpg"
                subprocess.run(['ffmpeg', '-ss', '00:00:01', '-i', filepath, '-vframes', '1', thumb, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                await message.reply_video(filepath, thumb=thumb, duration=dur, width=w, height=h, caption=f"🎬 {title}", supports_streaming=True, progress=progress_callback, progress_args=(client, status, [time.time()], f"Uploading Video {i}"))
                os.remove(filepath); os.remove(thumb)
            except: pass
    mark_processed(url); await status.delete(); return True

# ==========================================
# COMMAND HANDLERS
# ==========================================
@app.on_message(filters.command("user", prefixes=".") & filters.user(SUDO_USERS))
async def user_cmd(client, message):
    if len(message.command) < 2: return
    username = message.command[1].strip()
    msg = await message.reply(f"🛰 **Initializing Stealth Scanner: {username}...**")
    all_urls = await scan_all_content(username, msg)
    if not all_urls: return await msg.edit_text("❌ No items found. Check Terminal for Status Code.")
    await msg.edit_text(f"✅ Found: `{len(all_urls)}`. Archiving...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 STOP", callback_data=f"stop_task|{message.chat.id}")]]))
    for i, url in enumerate(all_urls, 1):
        if cancel_tasks.get(message.chat.id): break
        await process_album(client, message, url, username, i, len(all_urls))
    await msg.delete(); await message.reply(f"🏆 Done for `{username}`!")

@app.on_callback_query(filters.regex(r"^stop_task\|"))
async def handle_stop(client, callback: CallbackQuery):
    cancel_tasks[int(callback.data.split("|")[1])] = True
    await callback.answer("Stopping.")

@app.on_message(filters.command("dl", prefixes=".") & filters.user(SUDO_USERS))
async def dl_handler(client, message):
    urls = list(dict.fromkeys([u.strip() for u in message.text.split('\n') if "erome.com/a/" in u]))
    for i, url in enumerate(urls, 1): await process_album(client, message, url, "single", i, len(urls))
    try: await message.delete()
    except: pass

async def main():
    init_db()
    async with app:
        print("LOG: V8.98 Stealth Hunter Online!")
        await idle()

if __name__ == "__main__":
    app.run(main())
