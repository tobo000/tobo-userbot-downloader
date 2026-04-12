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
from pyrogram.types import InputMediaPhoto, InputMediaVideo
from pyrogram.errors import FloodWait, RPCError
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

app = Client("tobo_pro_session", api_id=int(API_ID), api_hash=API_HASH)
DOWNLOAD_DIR = "downloads"
DB_NAME = "bot_archive.db"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)

session = requests.Session()
executor = ThreadPoolExecutor(max_workers=4)
cancel_tasks = {}

# --- 1. DATABASE ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS processed (album_id TEXT PRIMARY KEY)")

def is_processed(album_id):
    with sqlite3.connect(DB_NAME) as conn:
        res = conn.execute("SELECT 1 FROM processed WHERE album_id = ?", (album_id,)).fetchone()
    return res is not None

def mark_processed(album_id):
    try:
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("INSERT INTO processed (album_id) VALUES (?)", (album_id,))
    except: pass

# --- 2. HELPERS & ANIMATIONS ---
def create_progress_bar(current, total):
    if total <= 0: return "[░░░░░░░░░░] 0%"
    pct = min(100, (current / total) * 100)
    return f"[{'█' * int(pct/10)}{'░' * (10 - int(pct/10))}] {pct:.1f}%"

def get_human_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0: return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

async def update_progress_msg(current, total, status_msg, start_time, action_text, topic=""):
    now = time.time()
    if now - start_time[0] > 8: 
        anims = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
        anim = anims[int(now % len(anims))]
        bar = create_progress_bar(current, total)
        try:
            await status_msg.edit_text(
                f"{anim} **{action_text}**\n"
                f"**Topic:** `{topic[:30]}...`\n\n"
                f"{bar}\n"
                f"📦 **Size:** {get_human_size(current)} / {get_human_size(total)}"
            )
            start_time[0] = now
        except FloodWait as e:
            await asyncio.sleep(e.value)
        except: pass

async def pyrogram_progress(current, total, status_msg, start_time, action_text, topic=""):
    await update_progress_msg(current, total, status_msg, start_time, action_text, topic)

def get_video_meta(video_path):
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        res = subprocess.check_output(cmd).decode('utf-8')
        data = json.loads(res)
        duration = int(float(data.get('format', {}).get('duration', 0)))
        video_stream = next((s for s in data.get('streams', []) if s['codec_type'] == 'video'), {})
        width, height = int(video_stream.get('width', 1280)), int(video_stream.get('height', 720))
        return duration, width, height
    except: return 0, 1280, 720

# --- 3. NITRO DOWNLOAD ENGINE ---
def download_nitro_animated(url, path, size, status_msg, loop, action, topic, segs=4):
    chunk = size // segs
    downloaded_shared = [0]
    start_time = [time.time()]
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/121.0.0.0', 'Referer': 'https://www.erome.com/'}
    
    def dl_part(s, e, n):
        pp = f"{path}.p{n}"; h = headers.copy(); h['Range'] = f'bytes={s}-{e}'
        try:
            with requests.get(url, headers=h, stream=True, timeout=60) as r:
                with open(pp, 'wb') as f:
                    for chk in r.iter_content(chunk_size=512*1024):
                        if chk:
                            f.write(chk); downloaded_shared[0] += len(chk)
                            asyncio.run_coroutine_threadsafe(update_progress_msg(downloaded_shared[0], size, status_msg, start_time, action, topic), loop)
        except: pass

    with ThreadPoolExecutor(max_workers=segs) as ex:
        futures = [ex.submit(dl_part, i*chunk, ((i+1)*chunk-1 if i<segs-1 else size-1), i) for i in range(segs)]
        for fut in futures: fut.result()

    with open(path, 'wb') as f:
        for i in range(segs):
            pp = f"{path}.p{i}"
            if os.path.exists(pp):
                with open(pp, 'rb') as pf: f.write(pf.read())
                os.remove(pp)

# --- 4. SCRAPER ---
def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/121.0.0.0', 'Referer': 'https://www.erome.com/'}
    try:
        res = session.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
        p_l = [img.get('data-src') or img.get('src') for img in soup.select('div.img img')]
        p_l = ['https:' + x if x.startswith('//') else x for x in p_l if x]
        v_l = [s.get('src') for s in soup.find_all('source') if s.get('src')]
        v_l = list(dict.fromkeys(['https:' + v if v.startswith('//') else v for v in v_l if "erome.com" in v]))
        return title, p_l, v_l
    except: return "Error", [], []

# --- 5. CORE DELIVERY (WITH ERROR CATCHING) ---
async def process_album(client, chat_id, reply_id, url, username, current, total):
    try: await client.get_chat(chat_id)
    except: pass

    album_id = url.rstrip('/').split('/')[-1]
    if is_processed(album_id): return True
    
    loop = asyncio.get_running_loop()
    title, photos, videos = await loop.run_in_executor(executor, scrape_album_details, url)
    if not photos and not videos: return False
    
    user_folder = os.path.join(DOWNLOAD_DIR, f"{chat_id}_{album_id}")
    os.makedirs(user_folder, exist_ok=True)
    status = await client.send_message(chat_id, f"📡 **[{current}/{total}] Preparing Archive...**", reply_to_message_id=reply_id)

    album_caption = (f"🎬 Topic: **{title}**\n"
                     f"📂 Album: `{current}/{total}`\n"
                     f"📊 Total: `{len(photos)}` Photo | `{len(videos)}` Video\n"
                     f"👤 User: `{username.upper()}`\n"
                     f"📦 Original Quality")

    # --- PHOTOS UPLOAD WITH ERROR CATCHING ---
    if photos:
        photo_media = []
        for i, p_url in enumerate(photos, 1):
            if cancel_tasks.get(chat_id): break
            path = os.path.join(user_folder, f"p_{i}.jpg")
            try:
                def dl_p():
                    r = session.get(p_url, headers={'Referer': 'https://www.erome.com/'}, timeout=15)
                    with open(path, 'wb') as f: f.write(r.content)
                await loop.run_in_executor(executor, dl_p)
                photo_media.append(InputMediaPhoto(path))
            except: pass
            
        for i in range(0, len(photo_media), 10):
            chunk = photo_media[i:i+10]
            if i == 0: chunk[0].caption = album_caption
            try: 
                await client.send_media_group(chat_id, chunk, reply_to_message_id=reply_id)
                await asyncio.sleep(2) 
            except Exception as e:
                print(f"Photo Upload Error: {e}")
                await asyncio.sleep(5)
        
        for f in os.listdir(user_folder):
            if f.startswith("p_"): os.remove(os.path.join(user_folder, f))

    # --- VIDEOS UPLOAD WITH ERROR CATCHING ---
    if videos:
        for v_idx, v_url in enumerate(videos, 1):
            if cancel_tasks.get(chat_id): break
            filepath = os.path.join(user_folder, f"v_{v_idx}.mp4")
            try:
                r_head = session.head(v_url, headers={'Referer': 'https://www.erome.com/'}, timeout=10)
                size = int(r_head.headers.get('content-length', 0))
                
                label = f"🎬 Downloading Video {v_idx}/{len(videos)}"
                await loop.run_in_executor(executor, download_nitro_animated, v_url, filepath, size, status, loop, label, title)

                final_v = filepath + ".stream.mp4"
                subprocess.run(['ffmpeg', '-i', filepath, '-c', 'copy', '-movflags', 'faststart', final_v, '-y'], stderr=subprocess.DEVNULL)
                if os.path.exists(final_v): os.replace(final_v, filepath)

                dur, w, h = get_video_meta(filepath)
                thumb = filepath + ".jpg"
                subprocess.run(['ffmpeg', '-ss', '1', '-i', filepath, '-vframes', '1', thumb, '-y'], stderr=subprocess.DEVNULL)
                
                start_time_up = [time.time()]
                try:
                    await client.send_video(
                        chat_id=chat_id, video=filepath, thumb=thumb if os.path.exists(thumb) else None,
                        width=w, height=h, duration=dur, supports_streaming=True, 
                        caption=album_caption if not photos and v_idx == 1 else "",
                        reply_to_message_id=reply_id, progress=pyrogram_progress, 
                        progress_args=(status, start_time_up, f"📤 Uploading {v_idx}/{len(videos)}", title)
                    )
                except Exception as e:
                    print(f"Video Upload Error: {e}")
                    await client.send_message(chat_id, f"⚠️ Upload failed for video {v_idx} in `{title}`")

                await asyncio.sleep(2) 
                if os.path.exists(filepath): os.remove(filepath)
                if os.path.exists(thumb): os.remove(thumb)
            except Exception as e: print(f"Processing Error: {e}")

    mark_processed(album_id)
    await status.delete()
    return True

# --- HANDLERS ---
@app.on_message(filters.command("cancel", prefixes="."))
async def cancel_cmd(client, message):
    cancel_tasks[message.chat.id] = True
    await message.reply("🛑 **Cancellation Sent!**")

@app.on_message(filters.command("user", prefixes="."))
async def user_cmd(client, message):
    if len(message.command) < 2: return
    chat_id = message.chat.id; raw_input = message.command[1].strip(); cancel_tasks[chat_id] = False
    
    if "/a/" in raw_input:
        await process_album(client, chat_id, message.id, raw_input, "direct", 1, 1)
        return

    query = raw_input.split("erome.com/")[-1].split('/')[0]
    msg = await message.reply(f"🛰 **Initializing Scanner for `{query}`...**")
    
    # --- MULTI-PAGE SCANNER WITH ANIMATED COUNT ---
    all_urls = []
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/121.0.0.0'}
    page = 1
    scan_anims = ["🔍", "🔎", "📡", "🛰"]
    
    while page <= 20: # Safety limit 20 pages
        if cancel_tasks.get(chat_id): break
        try:
            anim = scan_anims[page % len(scan_anims)]
            await msg.edit_text(f"{anim} **Scanning Page {page}...**\n📦 Found: `{len(all_urls)}` albums")
            
            res = session.get(f"https://www.erome.com/{query}?page={page}", headers=headers, timeout=15)
            ids = re.findall(r'/a/([a-zA-Z0-9]{8})', res.text)
            
            if not ids: break
            
            new_found = 0
            for aid in ids:
                f_url = f"https://www.erome.com/a/{aid}"
                if f_url not in all_urls:
                    all_urls.append(f_url)
                    new_found += 1
            
            if new_found == 0 or "Next" not in res.text: break
            page += 1
            await asyncio.sleep(1) # Breath between page scans
        except: break

    if not all_urls: return await msg.edit_text(f"❌ No content found for `{query}`.")
    
    await msg.edit_text(f"✅ **Scanner Complete!**\n📊 Total Albums: `{len(all_urls)}`")
    await asyncio.sleep(2)
    await msg.delete()

    for i, url in enumerate(all_urls, 1):
        if cancel_tasks.get(chat_id): break
        await process_album(client, chat_id, message.id, url, query, i, len(all_urls))

async def main():
    init_db()
    async with app:
        print("LOG: Multi-Page Scanner Fixed & Ready!")
        await idle()

if __name__ == "__main__":
    app.run(main())
