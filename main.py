import os
import asyncio
import aiohttp
import aiofiles
import requests
import time
import subprocess
import json
import re
import sqlite3
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

app = Client("tobo_pro_session", api_id=int(API_ID), api_hash=API_HASH)
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

cancel_tasks = {}
executor = ThreadPoolExecutor(max_workers=4)

# --- 1. DATABASE ---
def init_db():
    conn = sqlite3.connect("bot_archive.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS processed (album_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def is_processed(album_id):
    conn = sqlite3.connect("bot_archive.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed WHERE album_id = ?", (album_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def mark_processed(album_id):
    conn = sqlite3.connect("bot_archive.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO processed (album_id) VALUES (?)", (album_id,))
        conn.commit()
    except: pass
    conn.close()

# --- 2. HELPERS (Animation & Metadata) ---
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
    try:
        await message.edit_text(text)
    except MessageNotModified: pass
    except Exception: pass

async def progress_callback(current, total, status_msg, start_time, action_text):
    """Animation for UPLOADING"""
    now = time.time()
    if now - start_time[0] > 6:
        bar = create_progress_bar(current, total)
        await edit_status_safe(status_msg, f"🚀 **{action_text}**\n\n{bar}\n📦 **Size:** {get_human_size(current)} / {get_human_size(total)}")
        start_time[0] = now

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

# --- 3. ASYNC DOWNLOAD ENGINE (V8.88 Fix) ---
async def download_with_progress(url, path, status_msg, action_text):
    """Downloads with real-time animation without freezing the bot"""
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/121.0.0.0', 'Referer': 'https://www.erome.com/'}
    last_update = [time.time()]
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, timeout=600) as r:
                if r.status != 200: return False
                total_size = int(r.headers.get('content-length', 0))
                downloaded = 0
                async with aiofiles.open(path, mode='wb') as f:
                    async for chunk in r.content.iter_chunked(1024*1024):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        # Update Animation Bar
                        if total_size > 0:
                            await edit_progress_msg_async(downloaded, total_size, status_msg, last_update, action_text)
                return True
    except: return False

async def edit_progress_msg_async(current, total, status_msg, last_update, action_text):
    now = time.time()
    if now - last_update[0] > 6:
        bar = create_progress_bar(current, total)
        await edit_status_safe(status_msg, f"📥 **{action_text}**\n\n{bar}\n📦 **Progress:** {get_human_size(current)} / {get_human_size(total)}")
        last_update[0] = now

# ==========================================
# SCRAPER ENGINE (Improved Discovery)
# ==========================================
def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/123.0.0.0', 'Referer': 'https://www.erome.com/'}
    try:
        res = requests.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
        
        # Photos
        p_l = list(dict.fromkeys([x if x.startswith('http') else 'https:' + x for x in [i.get('data-src') or i.get('src') for i in soup.select('div.img img') if "erome.com" in (i.get('data-src') or i.get('src', ''))] if x]))
        
        # Videos (Aggressive Discovery)
        v_candidates = []
        for tag in soup.find_all(['video', 'source', 'a']):
            src = tag.get('src') or tag.get('data-src') or tag.get('href')
            if src and ".mp4" in src.lower(): v_candidates.append(src if src.startswith('http') else 'https:' + src)
        v_candidates.extend(re.findall(r'https?://[^\s"\'>]+\.mp4', res.text))
        v_l = list(dict.fromkeys([v for v in v_candidates if "erome.com" in v]))
        v_l.sort(key=lambda x: (re.search(r'(1080|720|high)', x.lower()) is None, x))
        
        return title, p_l, v_l
    except: return "Error", [], []

async def scan_all_content(username, status_msg):
    all_urls = []
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/121.0.0.0'}
    for tab in ["", "/reposts"]:
        page = 1
        while True:
            await edit_status_safe(status_msg, f"🔍 **Scanning `{username}`...**\n🚀 Found: `{len(all_urls)}` items")
            url = f"https://www.erome.com/{username}{tab}?page={page}"
            try:
                res = requests.get(url, headers=headers, timeout=20)
                if res.status_code != 200: break
                html = res.text
                album_ids = re.findall(r'/a/([a-zA-Z0-9]+)', html)
                if not album_ids: break
                new = 0
                for aid in album_ids:
                    f_url = f"https://www.erome.com/a/{aid}"
                    if f_url not in all_urls: all_urls.append(f_url); new += 1
                if new == 0 or "Next" not in html: break
                page += 1
                await asyncio.sleep(0.5)
            except: break
    return all_urls

# ==========================================
# DELIVERY ENGINE
# ==========================================
async def process_album(client, chat_id, reply_id, url, username, current, total):
    album_id = url.rstrip('/').split('/')[-1]
    if is_processed(album_id): return True

    title, photos, videos = scrape_album_details(url)
    if not photos and not videos: return False
    
    user_folder = os.path.join(DOWNLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)
    
    status = await client.send_message(chat_id, f"📥 **[{current}/{total}]** Preparing: `{title}`", reply_to_message_id=reply_id)

    # 1. Photos
    if photos:
        p_paths = []
        for i, p_url in enumerate(photos, 1):
            path = os.path.join(user_folder, f"img_{album_id}_{i}.jpg")
            try:
                # Fast download for photos
                r = requests.get(p_url, timeout=30)
                with open(path, 'wb') as f: f.write(r.content)
                if os.path.exists(path): p_paths.append(path)
                if len(p_paths) == 10 or i == len(photos):
                    await client.send_media_group(chat_id, [InputMediaPhoto(pf, caption=f"🖼 {title}") for pf in p_paths], reply_to_message_id=reply_id)
                    for pf in p_paths: os.remove(pf)
                    p_paths = []
            except: pass

    # 2. Videos (With Async Animation)
    if videos:
        for v_idx, v_url in enumerate(videos, 1):
            v_name = f"{album_id}_v{v_idx}.mp4"
            filepath = os.path.join(user_folder, v_name)
            
            # --- START ASYNC DOWNLOAD WITH BAR ---
            success = await download_with_progress(v_url, filepath, status, f"Downloading Video {v_idx}")
            
            if not success or not os.path.exists(filepath) or os.path.getsize(filepath) == 0: continue

            # Audio Repair & Thumbnail
            dur, w, h, has_audio = get_video_meta(filepath)
            if not has_audio:
                temp_f = filepath + ".fix.mp4"
                subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100', '-i', filepath, '-c:v', 'copy', '-c:a', 'aac', '-shortest', temp_f, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if os.path.exists(temp_f): os.remove(filepath); os.rename(temp_f, filepath)
            
            thumb = filepath + ".jpg"
            subprocess.run(['ffmpeg', '-ss', '00:00:01', '-i', filepath, '-vframes', '1', '-q:v', '2', thumb, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # --- START UPLOAD WITH BAR ---
            start_time = [time.time()]
            await client.send_video(
                chat_id=chat_id, video=filepath, thumb=thumb if os.path.exists(thumb) else None,
                width=w, height=h, duration=dur, caption=f"🎬 {title}",
                supports_streaming=True, reply_to_message_id=reply_id,
                progress=progress_callback, progress_args=(status, start_time, f"Uploading Video {v_idx}")
            )
            if os.path.exists(filepath): os.remove(filepath)
            if os.path.exists(thumb): os.remove(thumb)
    
    mark_processed(album_id)
    await status.delete()
    return True

# ==========================================
# COMMAND HANDLERS
# ==========================================
@app.on_message(filters.command("user", prefixes="."))
async def user_cmd(client, message):
    if len(message.command) < 2: return
    username = message.command[1].strip()
    username = username.split("erome.com/")[-1].split('/')[0]
    chat_id = message.chat.id
    cancel_tasks[chat_id] = False 

    msg = await message.reply_text(f"🛰 **Scanning profile: {username}...**")
    all_urls = await scan_all_content(username, msg)
    if not all_urls: return await msg.edit_text("❌ No items found.")

    total = len(all_urls)
    await msg.edit_text(f"✅ Found: `{total}` items. Archiving...", 
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 STOP", callback_data=f"stop_task|{chat_id}")]]))

    for i, url in enumerate(all_urls, 1):
        if cancel_tasks.get(chat_id): break
        await process_album(client, chat_id, message.id, url, username, i, total)
        # Prevent VPS Overheat
        await asyncio.sleep(2) if i % 5 == 0 else await asyncio.sleep(0.5)

    await msg.delete(); await message.reply_text(f"🏆 Done for `{username}`!")

@app.on_callback_query(filters.regex(r"^stop_task\|"))
async def handle_stop(client, callback: CallbackQuery):
    cancel_tasks[int(callback.data.split("|")[1])] = True
    await callback.answer("🛑 Stopping...", show_alert=True)

@app.on_message(filters.command("dl", prefixes="."))
async def dl_handler(client, message):
    urls = list(dict.fromkeys([u.strip() for u in message.text.split('\n') if "erome.com/a/" in u]))
    for i, url in enumerate(urls, 1): 
        await process_album(client, message.chat.id, message.id, url, "general", i, len(urls))
    try: await message.delete()
    except: pass

async def main():
    init_db()
    async with app:
        print("LOG: V8.88 perfección Edition Ready!")
        await idle()

if __name__ == "__main__":
    app.run(main())
