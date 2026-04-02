import os
import asyncio
import requests
import time
import subprocess
import json
import re
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# --- CONFIGURATION ---
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

app = Client("tobo_pro_session", api_id=int(API_ID), api_hash=API_HASH)
DOWNLOAD_DIR = "downloads"
if not os.path.exists(DOWNLOAD_DIR): os.makedirs(DOWNLOAD_DIR)
session = requests.Session()

data_cache = {}

# --- HELPERS ---
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

# ==========================================
# ADVANCED SCANNER WITH ANIMATION
# ==========================================
async def full_scan_profile(username, status_msg):
    headers = {'User-Agent': 'Mozilla/5.0 Chrome/123.0.0.0', 'Referer': 'https://www.erome.com/'}
    results = {"posts": [], "reposts": []}
    
    # Animation frames
    frames = ["🔍", "🔎", "⚡", "✨", "📡", "🚀"]
    frame_idx = 0
    
    for tab in ["", "/reposts"]:
        page = 1
        key = "posts" if tab == "" else "reposts"
        while True:
            # Update Animation
            icon = frames[frame_idx % len(frames)]
            frame_idx += 1
            
            await status_msg.edit_text(
                f"{icon} **Scanning {username}...**\n\n"
                f"📝 Posts found: `{len(results['posts'])}` \n"
                f"🔁 Reposts found: `{len(results['reposts'])}` \n\n"
                f"*Currently: Analyzing {key} (Page {page})*"
            )
            
            url = f"https://www.erome.com/{username}{tab}?page={page}"
            try:
                res = session.get(url, headers=headers, timeout=20)
                if res.status_code != 200: break
                soup = BeautifulSoup(res.text, 'html.parser')
                
                found_links = []
                for a in soup.find_all("a", href=True):
                    href = a['href']
                    if "/a/" in href and "erome.com" not in href:
                        found_links.append('https://www.erome.com' + href if href.startswith('/') else href)
                
                if not found_links: break
                
                added = 0
                for link in found_links:
                    if link not in results[key]:
                        results[key].append(link); added += 1
                
                if added == 0: break
                if not soup.find("a", string=re.compile(r"Next", re.I)): break
                page += 1
                await asyncio.sleep(0.3)
            except: break
    return results

# ==========================================
# DELIVERY ENGINE
# ==========================================
def scrape_album_details(url):
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        res = session.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
        p_l = list(dict.fromkeys([i.get('data-src') or i.get('src') for i in soup.select('div.img img') if "erome.com" in (i.get('data-src') or i.get('src', ''))]))
        v_candidates = []
        for tag in soup.find_all(['video', 'source', 'a']):
            src = tag.get('src') or tag.get('data-src') or tag.get('href')
            if src and ".mp4" in src.lower(): v_candidates.append(src if src.startswith('http') else 'https:' + src)
        v_candidates.extend(re.findall(r'https?://[^\s"\'>]+\.mp4', res.text))
        v_l = list(dict.fromkeys([v for v in v_candidates if "erome.com" in v]))
        v_l.sort(key=lambda x: (re.search(r'(1080|720|high)', x.lower()) is None, x))
        return title, p_l, v_l
    except: return "Error", [], []

async def process_album(client, message, url):
    title, photos, videos = scrape_album_details(url)
    if not photos and not videos: return
    album_id = url.rstrip('/').split('/')[-1]
    status = await client.send_message(message.chat.id, f"📥 **Analyzing:** `{title}`", reply_to_message_id=message.id)
    last_edit = [0]

    if photos:
        p_files = []
        for p_idx, p_url in enumerate(photos, 1):
            filepath = os.path.join(DOWNLOAD_DIR, f"img_{album_id}_{p_idx}.jpg")
            try:
                r = session.get(p_url, timeout=30)
                with open(filepath, 'wb') as f: f.write(r.content)
                if os.path.exists(filepath): p_files.append(filepath)
                if len(p_files) == 10 or p_idx == len(photos):
                    await client.send_media_group(message.chat.id, [InputMediaPhoto(pf, caption=f"🖼 **{title}**") for pf in p_files], reply_to_message_id=message.id)
                    for pf in p_files: os.remove(pf)
                    p_files = []
            except: pass

    if videos:
        for v_idx, v_url in enumerate(videos, 1):
            v_name = v_url.split('/')[-1].split('?')[0]
            if ".mp4" not in v_name.lower(): v_name += ".mp4"
            filepath = os.path.join(DOWNLOAD_DIR, f"{album_id}_{v_name}")
            try:
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
                await client.send_video(message.chat.id, video=filepath, thumb=thumb if os.path.exists(thumb) else None, width=w, height=h, duration=dur, caption=f"🎬 **{title}**", supports_streaming=True, reply_to_message_id=message.id)
                os.remove(filepath); os.remove(thumb) if os.path.exists(thumb) else None
            except: pass
    await status.delete()

# ==========================================
# HANDLERS
# ==========================================
@app.on_message(filters.command("user", prefixes="."))
async def user_cmd(client, message):
    if len(message.command) < 2: return
    
    # Smart Link Parser
    input_data = message.command[1].strip()
    if "erome.com/" in input_data:
        username = input_data.split("erome.com/")[-1].split('/')[0].split('?')[0]
    else:
        username = input_data

    msg = await message.reply(f"🛰 **Connecting to profile:** `{username}`...")
    
    # Full Scan with Animation
    results = await full_scan_profile(username, msg)
    data_cache[username] = results
    
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Posts ({len(results['posts'])})", callback_data=f"dl_p|{username}")],
        [InlineKeyboardButton(f"🔁 Reposts ({len(results['reposts'])})", callback_data=f"dl_r|{username}")]
    ])
    
    await msg.edit_text(
        f"👤 **User Profile:** `{username}`\n\n"
        f"📝 **Original Posts:** `{len(results['posts'])}` items\n"
        f"🔁 **Reposted Albums:** `{len(results['reposts'])}` items\n\n"
        f"Select what to archive:",
        reply_markup=buttons
    )

@app.on_callback_query(filters.regex(r"^dl_(p|r)\|"))
async def handle_dl(client, callback: CallbackQuery):
    action, username = callback.data.split("|")
    key = "posts" if action == "dl_p" else "reposts"
    urls = data_cache.get(username, {}).get(key, [])
    
    if not urls:
        return await callback.answer("❌ No items found.", show_alert=True)
    
    await callback.message.edit_text(f"🚀 **Archiving {len(urls)} {key}...**")
    for url in urls:
        await process_album(client, callback.message, url)
        await asyncio.sleep(2)
    await callback.message.reply(f"🏆 Completed {key} archive for `{username}`!")

async def main():
    async with app:
        print("LOG: Tobo Pro V8.56 Online (Scan Animation)!")
        await idle()

if __name__ == "__main__":
    app.run(main())
