import os
import asyncio
import requests
import time
import subprocess
import json
import re
import sqlite3
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# --- CONFIGURATION (Keep your original Setup) ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

app = Client("tobo_pro_session", api_id=int(API_ID), api_hash=API_HASH)
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
session = requests.Session()

# Cache for button selection and stop task
data_cache = {}
cancel_tasks = {}

# --- [NEW] DATABASE SYSTEM (Anti-Duplicate) ---
def init_db():
    conn = sqlite3.connect("bot_memory.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS processed (album_id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def is_processed(album_id):
    conn = sqlite3.connect("bot_memory.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed WHERE album_id = ?", (album_id,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def mark_processed(album_id):
    conn = sqlite3.connect("bot_memory.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO processed (album_id) VALUES (?)", (album_id,))
        conn.commit()
    except: pass
    conn.close()

# --- [KEEP] YOUR ORIGINAL HELPERS ---
def create_progress_bar(current, total):
    if total <= 0: return "[░░░░░░░░░░] 0%"
    pct = min(100, current * 100 / total)
    return f"[{'█' * int(pct/10)}{'░' * (10 - int(pct/10))}] {pct:.1f}%"

def get_human_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0: return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

async def edit_status(message, text, last_update_time, force=False):
    now = time.time()
    if force or (now - last_update_time[0] > 4):
        try:
            await message.edit_text(text)
            last_update_time[0] = now
        except: pass

def get_video_meta(video_path):
    if not os.path.exists(video_path) or os.path.getsize(video_path) == 0:
        return 0, 1280, 720, False
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        res = subprocess.check_output(cmd).decode('utf-8')
        data = json.loads(res)
        duration = int(float(data['format']['duration']))
        v = next(s for s in data['streams'] if s['codec_type'] == 'video')
        has_audio = any(s['codec_type'] == 'audio' for s in data['streams'])
        return duration, v['width'], v['height'], has_audio
    except: return 0, 1280, 720, False

# --- [KEEP] YOUR ORIGINAL NITRO DOWNLOADER ---
def download_nitro(url, path, headers, size, segs=4):
    chunk = size // segs
    def dl_part(s, e, n):
        pp = f"{path}.p{n}"; h = headers.copy(); h['Range'] = f'bytes={s}-{e}'
        try:
            with session.get(url, headers=h, stream=True, timeout=60) as r:
                with open(pp, 'wb') as f:
                    for chk in r.iter_content(chunk_size=1024*1024): f.write(chk)
        except: pass
    with ThreadPoolExecutor(max_workers=segs) as ex:
        for i in range(segs):
            start = i * chunk
            end = (i + 1) * chunk - 1 if i < size - 1 else size - 1
            ex.submit(dl_part, start, end, i)
    with open(path, 'wb') as f:
        for i in range(segs):
            pp = f"{path}.p{i}"
            if os.path.exists(pp):
                with open(pp, 'rb') as pf: f.write(pf.read())
                os.remove(pp)

# ==========================================
# [UPDATE] SCRAPER ENGINE (V8.70: High-Res Fix)
# ==========================================
def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/123.0.0.0'}
    try:
        res = session.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
        p_l = [x if x.startswith('http') else 'https:' + x for x in [i.get('data-src') or i.get('src') for i in soup.select('div.img img') if "erome.com" in (i.get('data-src') or i.get('src', ''))] if x]
        
        v_candidates = []
        for tag in soup.find_all(['video', 'source', 'a']):
            src = tag.get('src') or tag.get('data-src') or tag.get('href')
            if src and ".mp4" in src.lower(): v_candidates.append(src if src.startswith('http') else 'https:' + src)
        v_candidates.extend(re.findall(r'https?://[^\s"\'>]+\.mp4', res.text))
        
        # [UPDATE] High Resolution Selection logic
        v_l = list(dict.fromkeys([v for v in v_candidates if "erome.com" in v]))
        v_l.sort(key=lambda x: (re.search(r'(1080|720|high)', x.lower()) is None, x))
        
        return title, p_l, v_l
    except: return "Error", [], []

async def full_scan_profile(username, status_msg):
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.erome.com/'}
    results = {"posts": [], "reposts": []}
    icons = ["🔍", "🔎", "📡", "⚡"]
    for tab in ["", "/reposts"]:
        page = 1
        key = "posts" if tab == "" else "reposts"
        while True:
            icon = icons[page % len(icons)]
            await status_msg.edit_text(f"{icon} **Scanning {username}...**\n\n📝 Posts: `{len(results['posts'])}` \n🔁 Reposts: `{len(results['reposts'])}` \n\n*Reading {key} page {page}...*")
            url = f"https://www.erome.com/{username}{tab}?page={page}"
            try:
                res = session.get(url, headers=headers, timeout=20)
                if res.status_code != 200: break
                soup = BeautifulSoup(res.text, 'html.parser')
                links = [ 'https://www.erome.com' + a['href'] if a['href'].startswith('/') else a['href'] 
                         for a in soup.find_all("a", href=True) if "/a/" in a['href'] and "erome.com" not in a['href'] ]
                if not links: break
                added = 0
                for l in links:
                    if l not in results[key]: results[key].append(l); added += 1
                if added == 0 or not soup.find("a", string=re.compile(r"Next", re.I)): break
                page += 1
                await asyncio.sleep(0.3)
            except: break
    return results

# ==========================================
# [KEEP] ORIGINAL DELIVERY + [UPDATE] SMART FOLDER
# ==========================================
async def process_album(client, message, url, username):
    album_id = url.rstrip('/').split('/')[-1]
    
    # Anti-Duplicate Check
    if is_processed(album_id): return

    title, photos, videos = scrape_album_details(url)
    if not photos and not videos: return
    
    # [NEW] Smart Folder Management
    user_folder = os.path.join(DOWNLOAD_DIR, username)
    if not os.path.exists(user_folder): os.makedirs(user_folder)
    
    status = await client.send_message(message.chat.id, f"📥 **Archiving:** `{title}`", reply_to_message_id=message.id)
    last_edit = [0]

    # [KEEP] Photo delivery logic
    if photos:
        p_files = []
        for p_idx, p_url in enumerate(photos, 1):
            filepath = os.path.join(user_folder, f"img_{album_id}_{p_idx}.jpg")
            try:
                r = session.get(p_url, timeout=30)
                with open(filepath, 'wb') as f: f.write(r.content)
                if os.path.exists(filepath): p_files.append(filepath)
                if len(p_files) == 10 or p_idx == len(photos):
                    await client.send_media_group(message.chat.id, [InputMediaPhoto(pf, caption=f"🖼 **{title}**") for pf in p_files], reply_to_message_id=message.id)
                    for pf in p_files: os.remove(pf)
                    p_files = []
            except: pass

    # [KEEP] Video delivery logic
    if videos:
        for v_idx, v_url in enumerate(videos, 1):
            v_name = v_url.split('/')[-1].split('?')[0]
            if ".mp4" not in v_name.lower(): v_name += ".mp4"
            filepath = os.path.join(user_folder, f"{album_id}_{v_name}")
            try:
                head = session.head(v_url, allow_redirects=True, timeout=15)
                size = int(head.headers.get('content-length', 0))
                await edit_status(status, f"📥 **Downloading HD Video {v_idx}/{len(videos)}**\nSize: {get_human_size(size)}", last_edit, force=True)
                
                # Use Nitro for Large, Requests for Small
                if size > 15*1024*1024: download_nitro(v_url, filepath, {'User-Agent': 'Mozilla/5.0', 'Referer': url}, size)
                else:
                    with session.get(v_url, stream=True, timeout=60) as r:
                        with open(filepath, 'wb') as f:
                            for chunk in r.iter_content(chunk_size=1024*1024): f.write(chunk)
                
                if not os.path.exists(filepath): continue
                dur, w, h, has_audio = get_video_meta(filepath)
                if not has_audio:
                    temp = filepath + ".fix.mp4"
                    subprocess.run(['ffmpeg', '-f', 'lavfi', '-i', 'anullsrc=channel_layout=stereo:sample_rate=44100', '-i', filepath, '-c:v', 'copy', '-c:a', 'aac', '-shortest', temp, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if os.path.exists(temp): os.remove(filepath); os.rename(temp, filepath)
                
                thumb = filepath + ".jpg"
                subprocess.run(['ffmpeg', '-ss', '00:00:01', '-i', filepath, '-vframes', '1', '-q:v', '2', thumb, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                await client.send_video(message.chat.id, video=filepath, thumb=thumb if os.path.exists(thumb) else None, width=w, height=h, duration=dur, caption=f"🎬 **{title}**\n📦 {get_human_size(size)}", supports_streaming=True, reply_to_message_id=message.id)
                os.remove(filepath); os.remove(thumb) if os.path.exists(thumb) else None
            except: pass
    
    mark_processed(album_id) 
    await status.delete()

# ==========================================
# HANDLERS (Stop Button Support)
# ==========================================
@app.on_message(filters.command("user", prefixes="."))
async def user_cmd(client, message):
    if len(message.command) < 2: return
    input_data = message.command[1].strip()
    username = input_data.split("erome.com/")[-1].split('/')[0].split('?')[0] if "erome.com/" in input_data else input_data
    chat_id = message.chat.id
    cancel_tasks[chat_id] = False 

    msg = await message.reply(f"🛰 **Initializing Scanner...**")
    results = await full_scan_profile(username, msg)
    data_cache[username] = results
    
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Download Posts ({len(results['posts'])})", callback_data=f"dl_p|{username}")],
        [InlineKeyboardButton(f"🔁 Download Reposts ({len(results['reposts'])})", callback_data=f"dl_r|{username}")]
    ])
    await msg.edit_text(f"👤 **User Profile:** `{username}`\n\n📝 Posts: `{len(results['posts'])}` | 🔁 Reposts: `{len(results['reposts'])}`", reply_markup=buttons)

@app.on_callback_query(filters.regex(r"^dl_(p|r)\|"))
async def handle_choice(client, callback: CallbackQuery):
    action, username = callback.data.split("|")
    chat_id = callback.message.chat.id
    key = "posts" if action == "dl_p" else "reposts"
    urls = data_cache.get(username, {}).get(key, [])
    
    if not urls: return await callback.answer("❌ Empty.")
    
    # Show Stop Button
    stop_btn = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 STOP ARCHIVING", callback_data=f"stop_task|{chat_id}")]])
    await callback.message.edit_text(f"🚀 **Archiving `{len(urls)}` {key}...**", reply_markup=stop_btn)

    for url in urls:
        if cancel_tasks.get(chat_id): 
            await callback.message.reply("🛑 **Stopped by user.**")
            break
        await process_album(client, callback.message, url, username)
        await asyncio.sleep(1.5)
    await callback.message.reply(f"🏆 Completed archive for {username}!")

@app.on_callback_query(filters.regex(r"^stop_task\|"))
async def handle_stop(client, callback: CallbackQuery):
    chat_id = int(callback.data.split("|")[1])
    cancel_tasks[chat_id] = True
    await callback.answer("Stopping task...", show_alert=True)

@app.on_message(filters.command("dl", prefixes="."))
async def dl_handler(client, message):
    urls = list(dict.fromkeys([u.strip() for u in message.text.split('\n') if "erome.com/a/" in u]))
    for url in urls: await process_album(client, message, url, "general")
    try: await message.delete()
    except: pass

async def main():
    init_db()
    async with app:
        print("LOG: V8.70 Combined Edition is Online!")
        await idle()

if __name__ == "__main__":
    app.run(main())
