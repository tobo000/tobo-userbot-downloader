import os
import asyncio
import aiohttp
import aiofiles
import time
import subprocess
import json
import sqlite3
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import InputMediaPhoto, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
API_ID = int(os.getenv("API_ID", "34684478"))
API_HASH = os.getenv("API_HASH", "3ee498f0d6b06bf3fa8a5b102af12942")
sudo_raw = os.getenv("SUDO_USERS", "")
SUDO_USERS = [int(x.strip()) for x in sudo_raw.split(",") if x.strip().isdigit()]

app = Client("tobo_pro_session", api_id=API_ID, api_hash=API_HASH)
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

cancel_tasks = {}

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect("bot_archive.db")
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS processed (url TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

def is_processed(url):
    conn = sqlite3.connect("bot_archive.db")
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM processed WHERE url = ?", (url,))
    res = cursor.fetchone()
    conn.close()
    return res is not None

def mark_processed(url):
    conn = sqlite3.connect("bot_archive.db")
    cursor = conn.cursor()
    try:
        cursor.execute("INSERT INTO processed (url) VALUES (?)", (url,))
        conn.commit()
    except: pass
    conn.close()

# --- PROGRESS BAR HELPERS (Your Animation) ---
def create_progress_bar(current, total):
    if total == 0: return "[░░░░░░░░░░] 0%"
    pct = current * 100 / total
    return f"[{'█' * int(pct/10)}{'░' * (10 - int(pct/10))}] {pct:.1f}%"

def get_human_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0: return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

async def progress_callback(current, total, client, message, start_time, action_text):
    now = time.time()
    if now - start_time[0] > 4: # Update every 4 seconds to avoid FloodWait
        bar = create_progress_bar(current, total)
        try:
            await message.edit_text(f"🚀 **{action_text}**\n\n{bar}\n📦 {get_human_size(current)} / {get_human_size(total)}")
            start_time[0] = now
        except: pass

# --- DOWNLOADING ENGINE ---
async def download_file(url, path):
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.erome.com/'}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=600) as r:
                if r.status == 200:
                    async with aiofiles.open(path, mode='wb') as f:
                        async for chunk in r.content.iter_chunked(1024*1024):
                            await f.write(chunk)
                    return True
    except: return False
    return False

def get_video_meta(video_path):
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        res = subprocess.check_output(cmd).decode('utf-8')
        data = json.loads(res)
        duration = int(float(data['format']['duration']))
        v = next(s for s in data['streams'] if s['codec_type'] == 'video')
        return duration, v['width'], v['height']
    except: return 0, 0, 0

# --- SCRAPER ---
async def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0'}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as r:
            soup = BeautifulSoup(await r.text(), 'html.parser')
            title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
            v_l = []
            for v in soup.find_all('video'):
                src = v.get('src') or v.get('data-src')
                if not src:
                    st = v.find('source'); src = st.get('src') if st else None
                if src: v_l.append(src if src.startswith('http') else 'https:' + src)
            p_l = [i.get('data-src') or i.get('src') for i in soup.select('div.img img') if "erome.com" in (i.get('data-src') or i.get('src', ''))]
            return title, list(dict.fromkeys(p_l)), list(dict.fromkeys(v_l))

async def get_all_profile_links(username, status_msg):
    all_links = []
    async with aiohttp.ClientSession() as session:
        for sub in ["", "/reposts"]:
            page = 1
            while True:
                await status_msg.edit_text(f"🕵️‍♂️ Scanning: `{username}` | Page: {page}\nFound: {len(all_links)} links")
                url = f"https://www.erome.com/{username}{sub}?page={page}"
                async with session.get(url) as r:
                    if r.status != 200: break
                    soup = BeautifulSoup(await r.text(), 'html.parser')
                    links = [a['href'] for a in soup.find_all("a", href=True) if "/a/" in a['href'] and "erome.com" not in a['href']]
                    if not links: break
                    for l in links:
                        full = 'https://www.erome.com' + l
                        if full not in all_links: all_links.append(full)
                    if not soup.find("a", string="Next"): break
                    page += 1
    return all_links

# --- DELIVERY ---
async def process_single_album(client, message, url, topic_id):
    if is_processed(url): return
    title, photos, videos = await scrape_album_details(url)
    if not photos and not videos: return
    
    album_id = url.rstrip('/').split('/')[-1]
    status = await client.send_message(message.chat.id, f"📥 **Preparing:** `{title}`", message_thread_id=topic_id)

    # Upload Photos
    if photos:
        photo_paths = []
        for i, p_url in enumerate(photos, 1):
            path = os.path.join(DOWNLOAD_DIR, f"{album_id}_p{i}.jpg")
            if await download_file(p_url, path): photo_paths.append(path)
            if len(photo_paths) == 10 or i == len(photos):
                if photo_paths:
                    await client.send_media_group(message.chat.id, [InputMediaPhoto(p, caption=f"🖼 {title}") for p in photo_paths], message_thread_id=topic_id)
                    for p in photo_paths: os.remove(p)
                    photo_paths = []

    # Upload Videos (With Progress Bar Animation)
    for i, v_url in enumerate(videos, 1):
        path = os.path.join(DOWNLOAD_DIR, f"{album_id}_v{i}.mp4")
        if await download_file(v_url, path):
            dur, w, h = get_video_meta(path)
            thumb = path + ".jpg"
            subprocess.run(['ffmpeg', '-ss', '00:00:01', '-i', path, '-vframes', '1', thumb, '-y'], stdout=subprocess.DEVNULL, stderr=subprocess.STNULL)
            
            # Start Animation Logic
            start_time = [time.time()]
            await client.send_video(
                chat_id=message.chat.id,
                video=path,
                thumb=thumb,
                duration=dur,
                width=w,
                height=h,
                caption=f"🎬 {title}",
                supports_streaming=True,
                message_thread_id=topic_id,
                progress=progress_callback,
                progress_args=(client, status, start_time, f"Uploading Video {i}/{len(videos)}")
            )
            os.remove(path); os.remove(thumb)

    mark_processed(url)
    await status.delete()

# --- COMMANDS ---
@app.on_message(filters.command("user", prefixes=".") & filters.user(SUDO_USERS))
async def user_cmd(client, message):
    if len(message.command) < 2: return
    username = message.command[1]
    chat_id = message.chat.id
    cancel_tasks[chat_id] = False
    topic_id = getattr(message, "message_thread_id", None)

    status = await message.reply(f"🕵️‍♂️ **Initializing Scanner...**", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_query_data=f"stop_{chat_id}")]]))
    urls = await get_all_profile_links(username, status)
    
    for url in urls:
        if cancel_tasks.get(chat_id): break
        await process_single_album(client, message, url, topic_id)
        await asyncio.sleep(1)
    await status.delete()

@app.on_callback_query(filters.regex("^stop_"))
async def stop_callback(client, callback_query: CallbackQuery):
    cancel_tasks[int(callback_query.data.split("_")[1])] = True
    await callback_query.answer("Stopping...", show_alert=True)

async def main():
    init_db()
    async with app:
        print("Bot is Online with Progress Bar Animation!")
        await idle()

if __name__ == "__main__":
    app.run(main())
