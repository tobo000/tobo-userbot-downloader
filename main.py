import os
import asyncio
import requests
import time
import subprocess
import json
import re
import sqlite3
import base64
import hashlib
import pickle
import logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import (
    InputMediaPhoto, InputMediaVideo, 
    InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery
)
from pyrogram.errors import FloodWait, RPCError
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# ============================================
# 1. CONFIGURATION
# ============================================
load_dotenv()
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
ADMIN_IDS = [5549600755, 7010218617]

# GitHub Config
GH_TOKEN = os.getenv("GH_TOKEN")
GH_REPO = os.getenv("GH_REPO") 
GH_FILE_PATH = "bot_archive.db"

if not API_ID or not API_HASH:
    raise ValueError("Missing API_ID or API_HASH in .env file")

try:
    API_ID = int(API_ID)
except ValueError:
    raise ValueError("API_ID must be an integer")

# Setup logging with file handlers
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('bot_full.log'),
        logging.FileHandler('bot_errors.log')
    ]
)
logger = logging.getLogger(__name__)

# Set error log level
for handler in logger.handlers:
    if handler.baseFilename and 'errors' in handler.baseFilename:
        handler.setLevel(logging.ERROR)

app = Client(
    "tobo_pro_session", 
    api_id=API_ID, 
    api_hash=API_HASH, 
    sleep_threshold=600,
    max_concurrent_transmissions=1,
    workers=4
)

DOWNLOAD_DIR = "downloads"
DB_NAME = "bot_archive.db"
CACHE_DIR = "cache"
CHECKPOINT_DIR = "checkpoints"

for directory in [DOWNLOAD_DIR, CACHE_DIR, CHECKPOINT_DIR]:
    if not os.path.exists(directory):
        os.makedirs(directory)

session = requests.Session()
executor = ThreadPoolExecutor(max_workers=8)
cancel_tasks = {}
chat_locks = {}

# ============================================
# ERROR NOTIFICATION SYSTEM (Terminal Only)
# ============================================
class ErrorNotifier:
    """Log errors to terminal and files - no DM"""
    def __init__(self):
        self.error_count = 0
        self.warning_count = 0
    
    def notify(self, error_type: str, message: str, details: str = ""):
        """Log error to terminal"""
        self.error_count += 1
        border = "=" * 60
        error_msg = (
            f"\n{border}\n"
            f"⚠️  BOT ERROR #{self.error_count} | {time.strftime('%H:%M:%S')}\n"
            f"{border}\n"
            f"Type: {error_type}\n"
            f"Message: {message[:200]}\n"
        )
        if details:
            error_msg += f"Details: {details[:150]}\n"
        error_msg += f"{border}\n"
        print(error_msg)
        logger.error(f"[ERROR #{self.error_count}] {error_type}: {message} | {details}")
    
    def warning(self, warning_type: str, message: str):
        """Log warning to terminal"""
        self.warning_count += 1
        print(f"⚠️  WARNING [{warning_type}]: {message[:150]}")
        logger.warning(f"[WARNING] {warning_type}: {message}")
    
    def success(self, message: str):
        """Log success to terminal"""
        print(f"✅ {message}")
        logger.info(f"[SUCCESS] {message}")
    
    def album_report(self, album_id: str, title: str, photos: int, videos: int, 
                     downloaded_p: int, uploaded_p: int, downloaded_v: int, uploaded_v: int,
                     missing_p: int, missing_v: int, success: bool):
        """Display album report in terminal"""
        border = "=" * 60
        status = "✅ COMPLETE" if (success and missing_p == 0 and missing_v == 0) else "❌ ISSUES FOUND"
        
        report = (
            f"\n{border}\n"
            f"📊 ALBUM REPORT | {status}\n"
            f"{border}\n"
            f"Album ID: {album_id}\n"
            f"Title: {title[:50]}\n"
            f"Photos: {downloaded_p}/{photos} downloaded | {uploaded_p}/{photos} uploaded"
        )
        if missing_p > 0:
            report += f" | ⚠️ {missing_p} MISSING"
        
        report += f"\nVideos: {downloaded_v}/{videos} downloaded | {uploaded_v}/{videos} uploaded"
        if missing_v > 0:
            report += f" | ⚠️ {missing_v} MISSING"
        
        report += f"\n{border}\n"
        print(report)
        logger.info(report)
    
    def get_stats(self) -> Dict:
        return {'total_errors': self.error_count, 'total_warnings': self.warning_count}

error_notifier = ErrorNotifier()

# ============================================
# MEDIA TRACKING SYSTEM
# ============================================
class MediaTracker:
    """Track every media item to ensure nothing is missed"""
    def __init__(self):
        self.pending_albums = {}
    
    def register_album(self, album_id: str, title: str, photos: List[str], videos: List[str]):
        self.pending_albums[album_id] = {
            'title': title,
            'photos': photos,
            'videos': videos,
            'downloaded': {'photos': [], 'videos': []},
            'uploaded': {'photos': [], 'videos': []},
            'timestamp': time.time()
        }
    
    def mark_downloaded(self, album_id: str, media_type: str, url: str):
        if album_id in self.pending_albums:
            self.pending_albums[album_id]['downloaded'][media_type].append(url)
    
    def mark_uploaded(self, album_id: str, media_type: str, url: str):
        if album_id in self.pending_albums:
            self.pending_albums[album_id]['uploaded'][media_type].append(url)
    
    def get_missing_media(self, album_id: str) -> Dict:
        if album_id not in self.pending_albums:
            return {'photos': [], 'videos': []}
        album = self.pending_albums[album_id]
        missing = {'photos': [], 'videos': []}
        for media_type in ['photos', 'videos']:
            missing[media_type] = [url for url in album['downloaded'][media_type] 
                                   if url not in album['uploaded'][media_type]]
        return missing
    
    def get_album_stats(self, album_id: str) -> Dict:
        if album_id not in self.pending_albums:
            return {}
        album = self.pending_albums[album_id]
        missing = self.get_missing_media(album_id)
        return {
            'title': album['title'],
            'total_photos': len(album['photos']),
            'total_videos': len(album['videos']),
            'downloaded_photos': len(album['downloaded']['photos']),
            'uploaded_photos': len(album['uploaded']['photos']),
            'downloaded_videos': len(album['downloaded']['videos']),
            'uploaded_videos': len(album['uploaded']['videos']),
            'missing_photos': len(missing['photos']),
            'missing_videos': len(missing['videos'])
        }
    
    def cleanup_album(self, album_id: str):
        if album_id in self.pending_albums:
            del self.pending_albums[album_id]

media_tracker = MediaTracker()

def get_chat_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()
    return chat_locks[chat_id]

# ============================================
# SMART QUEUE SYSTEM
# ============================================
class SmartQueue:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.active_tasks = {}
        self.completed_tasks = set()
        self.failed_tasks = {}
        self.max_concurrent = 3
        self.is_paused = False
    
    async def add_task(self, task_id: str, task_func, priority: int = 0, **kwargs):
        await self.queue.put((priority, task_id, task_func, kwargs))
        print(f"📥 Queue: {task_id}")
        logger.info(f"Task added: {task_id}")
    
    async def process_queue(self, progress_callback=None):
        while not self.queue.empty():
            if self.is_paused:
                await asyncio.sleep(1)
                continue
            completed = [tid for tid, task in self.active_tasks.items() if task.done()]
            for tid in completed:
                del self.active_tasks[tid]
            while len(self.active_tasks) >= self.max_concurrent:
                completed = [tid for tid, task in self.active_tasks.items() if task.done()]
                for tid in completed:
                    del self.active_tasks[tid]
                await asyncio.sleep(0.5)
            priority, task_id, task_func, kwargs = await self.queue.get()
            task = asyncio.create_task(task_func(**kwargs))
            self.active_tasks[task_id] = task
            task.add_done_callback(lambda t, tid=task_id: self._task_completed(tid, t))
            if progress_callback:
                await progress_callback(self.get_stats())
        if self.active_tasks:
            await asyncio.gather(*self.active_tasks.values(), return_exceptions=True)
    
    def _task_completed(self, task_id: str, task: asyncio.Task):
        try:
            result = task.result()
            if result:
                self.completed_tasks.add(task_id)
            else:
                self.failed_tasks[task_id] = "Task returned False"
        except Exception as e:
            self.failed_tasks[task_id] = str(e)
    
    def get_stats(self) -> Dict:
        return {
            'queue_size': self.queue.qsize(),
            'active_tasks': len(self.active_tasks),
            'completed': len(self.completed_tasks),
            'failed': len(self.failed_tasks),
            'is_paused': self.is_paused
        }
    def pause(self): self.is_paused = True
    def resume(self): self.is_paused = False
    def cancel_all(self):
        while not self.queue.empty():
            try: self.queue.get_nowait()
            except: pass
        for task_id, task in list(self.active_tasks.items()):
            task.cancel()

smart_queue = SmartQueue()

# ============================================
# CHECKPOINT MANAGER
# ============================================
class CheckpointManager:
    def __init__(self):
        self.checkpoint_dir = Path(CHECKPOINT_DIR)
    def save_checkpoint(self, album_id: str, state: Dict):
        checkpoint_file = self.checkpoint_dir / f"{album_id}.json"
        state['timestamp'] = time.time()
        try:
            with open(checkpoint_file, 'w') as f: json.dump(state, f)
        except: pass
    def load_checkpoint(self, album_id: str) -> Optional[Dict]:
        checkpoint_file = self.checkpoint_dir / f"{album_id}.json"
        if checkpoint_file.exists():
            try:
                with open(checkpoint_file, 'r') as f:
                    state = json.load(f)
                if time.time() - state.get('timestamp', 0) < 86400: return state
            except: pass
        return None
    def clear_checkpoint(self, album_id: str):
        checkpoint_file = self.checkpoint_dir / f"{album_id}.json"
        if checkpoint_file.exists():
            try: checkpoint_file.unlink()
            except: pass

checkpoint_manager = CheckpointManager()

# ============================================
# SMART CACHE
# ============================================
class SmartCache:
    def __init__(self, cache_dir: str = "cache", ttl_hours: int = 1):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(exist_ok=True)
        self.ttl = timedelta(hours=ttl_hours)
    def _get_cache_key(self, data: str) -> str:
        return hashlib.md5(data.encode()).hexdigest()
    def get_cached_album(self, url: str) -> Optional[Tuple]:
        cache_file = self.cache_dir / f"album_{self._get_cache_key(url)}.pickle"
        if cache_file.exists():
            try:
                with open(cache_file, 'rb') as f:
                    data = pickle.load(f)
                    if datetime.now() - data['timestamp'] < self.ttl: return data['content']
            except: pass
        return None
    def cache_album(self, url: str, content: Tuple):
        cache_file = self.cache_dir / f"album_{self._get_cache_key(url)}.pickle"
        try:
            with open(cache_file, 'wb') as f: pickle.dump({'timestamp': datetime.now(), 'content': content}, f)
        except: pass
    def clean_old_cache(self) -> int:
        count = 0
        for cache_file in self.cache_dir.glob("*.pickle"):
            try:
                with open(cache_file, 'rb') as f:
                    if datetime.now() - pickle.load(f)['timestamp'] > timedelta(hours=24):
                        cache_file.unlink(); count += 1
            except:
                try: cache_file.unlink(); count += 1
                except: pass
        return count

smart_cache = SmartCache()

# ============================================
# LIVE DASHBOARD
# ============================================
class LiveDashboard:
    def __init__(self):
        self.start_time = time.time()
        self.total_downloaded = 0
        self.current_speed = 0
        self.last_update = time.time()
    def update_speed(self, bytes_downloaded: int):
        now = time.time()
        if now - self.last_update > 0: self.current_speed = bytes_downloaded / (now - self.last_update)
        self.total_downloaded += bytes_downloaded
        self.last_update = now
    def get_dashboard_text(self, queue_stats: Dict = None) -> str:
        elapsed = time.strftime('%H:%M:%S', time.gmtime(time.time() - self.start_time))
        text = f"**Live Dashboard**\n━━━━━━━━━━━━━━━━━━━━\nUptime: `{elapsed}`\nDownloaded: `{get_human_size(self.total_downloaded)}`\nSpeed: `{get_human_size(self.current_speed)}`/s\n"
        if queue_stats:
            text += f"━━━━━━━━━━━━━━━━━━━━\nStatus: `{'PAUSED' if queue_stats.get('is_paused') else 'RUNNING'}`\nQueue: `{queue_stats.get('queue_size', 0)}`\nActive: `{queue_stats.get('active_tasks', 0)}`\nDone: `{queue_stats.get('completed', 0)}`\nFailed: `{queue_stats.get('failed', 0)}`\n"
        return text

live_dashboard = LiveDashboard()

# ============================================
# SMART COMPRESSOR
# ============================================
class SmartCompressor:
    COMPRESSION_PROFILES = {'light': {'crf': 23, 'preset': 'fast'}, 'medium': {'crf': 28, 'preset': 'medium'}, 'maximum': {'crf': 32, 'preset': 'slow'}}
    def should_compress(self, filepath: str) -> bool:
        return os.path.exists(filepath) and os.path.getsize(filepath) / (1024 * 1024) > 100
    def auto_select_profile(self, filepath: str) -> Dict:
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        if size_mb > 500: return self.COMPRESSION_PROFILES['maximum']
        elif size_mb > 200: return self.COMPRESSION_PROFILES['medium']
        return self.COMPRESSION_PROFILES['light']
    def compress_video(self, input_path: str, profile: Dict = None) -> Optional[str]:
        if not self.should_compress(input_path): return None
        if profile is None: profile = self.auto_select_profile(input_path)
        output_path = f"{input_path}.compressed.mp4"
        try:
            original_size = os.path.getsize(input_path)
            subprocess.run(['ffmpeg', '-i', input_path, '-c:v', 'libx264', '-crf', str(profile['crf']), '-preset', profile['preset'], '-c:a', 'aac', '-b:a', '128k', '-movflags', 'faststart', '-y', output_path], stderr=subprocess.DEVNULL, timeout=300)
            if os.path.exists(output_path):
                print(f"📦 Compressed: {(1 - os.path.getsize(output_path)/original_size) * 100:.1f}% reduction")
                os.remove(input_path); os.rename(output_path, input_path)
                return input_path
        except: pass
        return None
    def convert_gif_to_mp4(self, gif_path: str) -> Optional[str]:
        mp4_path = gif_path.replace('.gif', '.mp4')
        try:
            subprocess.run(['ffmpeg', '-i', gif_path, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', 'faststart', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-y', mp4_path], stderr=subprocess.DEVNULL, timeout=120)
            if os.path.exists(mp4_path) and os.path.getsize(mp4_path) > 0:
                os.remove(gif_path); return mp4_path
        except: pass
        return None

smart_compressor = SmartCompressor()

# ============================================
# KEYBOARD MANAGER
# ============================================
class KeyboardManager:
    @staticmethod
    def get_control_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Pause", callback_data="pause_all"), InlineKeyboardButton("Resume", callback_data="resume_all"), InlineKeyboardButton("Cancel", callback_data="cancel_all")],
            [InlineKeyboardButton("Dashboard", callback_data="show_dashboard"), InlineKeyboardButton("Clean Cache", callback_data="clean_cache")]
        ])

keyboard_manager = KeyboardManager()

# ============================================
# GITHUB SYNC
# ============================================
def backup_to_github():
    if not GH_TOKEN or not GH_REPO: return
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE_PATH}"
        headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        res = requests.get(url, headers=headers)
        sha = res.json().get('sha') if res.status_code == 200 else None
        with open(DB_NAME, "rb") as f: content = base64.b64encode(f.read()).decode()
        data = {"message": f"Sync: {time.ctime()}", "content": content, "branch": "main"}
        if sha: data["sha"] = sha
        requests.put(url, headers=headers, data=json.dumps(data))
    except: pass

def download_from_github():
    if not GH_TOKEN or not GH_REPO: return
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE_PATH}"
        headers = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            with open(DB_NAME, "wb") as f: f.write(base64.b64decode(res.json()['content']))
    except: pass

# ============================================
# DATABASE
# ============================================
def init_db():
    download_from_github()
    conn = sqlite3.connect(DB_NAME)
    conn.execute("CREATE TABLE IF NOT EXISTS processed (album_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS processed_media (media_id TEXT PRIMARY KEY, album_id TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS error_log (id INTEGER PRIMARY KEY AUTOINCREMENT, album_id TEXT, error_type TEXT, error_message TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit(); conn.close()

def is_processed(album_id): 
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT 1 FROM processed WHERE album_id = ?", (album_id,)).fetchone()
    conn.close(); return res is not None

def mark_processed(album_id):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT OR IGNORE INTO processed (album_id) VALUES (?)", (album_id,))
        conn.commit(); conn.close(); backup_to_github()
    except: pass

def log_error_to_db(album_id, error_type, error_message):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT INTO error_log (album_id, error_type, error_message) VALUES (?, ?, ?)", (album_id, error_type, error_message))
        conn.commit(); conn.close()
    except: pass

# ============================================
# HELPERS
# ============================================
def create_progress_bar(current, total):
    if total <= 0: return "[░░░░░░░░░░] 0%"
    pct = min(100, (current / total) * 100)
    return f"[{'█' * int(pct/10)}{'░' * (10 - int(pct/10))}] {pct:.1f}%"

def get_human_size(num):
    for unit in ['B', 'KB', 'MB', 'GB']:
        if abs(num) < 1024.0: return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"

async def safe_edit(msg, text):
    try: await msg.edit_text(text)
    except: pass

async def pyrogram_progress(current, total, status_msg, start_time, action_text, topic=""):
    now = time.time()
    if now - start_time[0] > 3:
        anims = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
        anim = anims[int(now % len(anims))]
        try:
            await status_msg.edit_text(f"{anim} **{action_text}**\nTopic: `{topic[:30]}...`\n\n{create_progress_bar(current, total)}\nOriginal Size: {get_human_size(current)} / {get_human_size(total)}")
            start_time[0] = now
        except: pass

def get_video_meta(video_path):
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', video_path]
        res = subprocess.check_output(cmd).decode('utf-8'); data = json.loads(res)
        duration = int(float(data.get('format', {}).get('duration', 0)))
        video_stream = next((s for s in data.get('streams', []) if s['codec_type'] == 'video'), {})
        return duration, int(video_stream.get('width', 1280)), int(video_stream.get('height', 720))
    except: return 1, 1280, 720

def is_gif_url(url): return '.gif' in url.lower()

# ============================================
# NITRO DOWNLOAD
# ============================================
def download_nitro_animated(url, path, size, status_msg, loop, action, topic, segs=4):
    chunk = size // segs; downloaded_shared = [0]; start_time = [time.time()]
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.erome.com/'}
    def dl_part(s, e, n):
        pp = f"{path}.p{n}"; h = headers.copy(); h['Range'] = f'bytes={s}-{e}'
        try:
            with requests.get(url, headers=h, stream=True, timeout=60) as r:
                with open(pp, 'wb') as f:
                    for chk in r.iter_content(chunk_size=1024*1024):
                        if chk:
                            f.write(chk); downloaded_shared[0] += len(chk)
                            live_dashboard.update_speed(len(chk))
                            asyncio.run_coroutine_threadsafe(pyrogram_progress(downloaded_shared[0], size, status_msg, start_time, action, topic), loop)
        except Exception as e: print(f"Part error: {e}")
    with ThreadPoolExecutor(max_workers=segs) as ex:
        futures = [ex.submit(dl_part, i*chunk, ((i+1)*chunk-1 if i < segs-1 else size-1), i) for i in range(segs)]
        for future in futures: future.result()
    with open(path, 'wb') as f:
        for i in range(segs):
            pp = f"{path}.p{i}"
            if os.path.exists(pp):
                with open(pp, 'rb') as pf: f.write(pf.read())
                os.remove(pp)

def download_simple_file(url, path):
    try:
        r = session.get(url, headers={'Referer': 'https://www.erome.com/'}, timeout=30)
        with open(path, 'wb') as f: f.write(r.content)
        return True
    except: return False

# ============================================
# SCRAPER
# ============================================
def scrape_album_details(url):
    cached = smart_cache.get_cached_album(url)
    if cached: return cached
    headers = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.erome.com/'}
    try:
        res = session.get(url, headers=headers, timeout=20)
        soup = BeautifulSoup(res.text, 'html.parser')
        title = soup.find("h1").get_text(strip=True) if soup.find("h1") else "Untitled"
        all_media = []
        for img in soup.select('div.img img'):
            src = img.get('data-src') or img.get('src')
            if src:
                if src.startswith('//'): src = 'https:' + src
                all_media.append(src)
        gifs = [x for x in all_media if '.gif' in x.lower()]
        photos = [x for x in all_media if '.gif' not in x.lower()]
        v_l = []
        for v_tag in soup.find_all(['source', 'video']):
            v_src = v_tag.get('src') or v_tag.get('data-src')
            if v_src and ".mp4" in v_src:
                if v_src.startswith('//'): v_src = 'https:' + v_src
                v_l.append(v_src)
        v_l.extend(re.findall(r'https?://[^\s"\'>]+.mp4', res.text))
        v_l = list(dict.fromkeys([v for v in v_l if "erome.com" in v]))
        result = (title, list(dict.fromkeys(photos)), list(dict.fromkeys(gifs + v_l)))
        smart_cache.cache_album(url, result)
        return result
    except: return "Error", [], []

# ============================================
# CORE DELIVERY
# ============================================
async def process_album(client, chat_id, reply_id, url, username, current, total):
    album_id = url.rstrip('/').split('/')[-1]
    if is_processed(album_id):
        print(f"⏭️  Skipping (already processed): {album_id}")
        return True
    
    title, photos, videos = await asyncio.get_event_loop().run_in_executor(executor, scrape_album_details, url)
    if not photos and not videos: return False
    
    # Register with tracker
    media_tracker.register_album(album_id, title, photos, videos)
    print(f"\n📊 [{current}/{total}] Processing: {title[:50]}")
    print(f"   Photos: {len(photos)} | Videos: {len(videos)}")
    
    user_folder = os.path.join(DOWNLOAD_DIR, f"{chat_id}_{album_id}")
    os.makedirs(user_folder, exist_ok=True)
    
    chat_lock = get_chat_lock(chat_id)
    album_success = True
    downloaded_p, uploaded_p = 0, 0
    downloaded_v, uploaded_v = 0, 0
    
    async with chat_lock:
        status = await client.send_message(chat_id, f"[{current}/{total}] Preparing Archive: {title}", reply_to_message_id=reply_id)
        
        gif_count = sum(1 for v in videos if '.gif' in v.lower())
        gif_info = f" | {gif_count} GIFs" if gif_count > 0 else ""
        master_caption = f"**{title}**\n━━━━━━━━━━━━━━━━\nAlbum: `{current}/{total}`\nContent: `{len(photos)}` Photos | `{len(videos)}` Videos{gif_info}\nUser: `{username.upper()}`\nQuality: Original\n━━━━━━━━━━━━━━━━"
        
        loop = asyncio.get_event_loop()
        master_caption_sent = False

        # Photos
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
                    media_tracker.mark_downloaded(album_id, 'photos', p_url)
                    downloaded_p += 1
                    live_dashboard.update_speed(os.path.getsize(path))
                    size_h = get_human_size(os.path.getsize(path))
                    caption = master_caption + f"\n\nPhoto `{i}/{len(photos)}` | `{size_h}`" if not master_caption_sent and i == 1 else f"Photo `{i}/{len(photos)}` | `{size_h}`"
                    if not master_caption_sent and i == 1: master_caption_sent = True
                    photo_media.append(InputMediaPhoto(path, caption=caption))
                except Exception as e:
                    error_notifier.notify("Photo Download", str(e), album_id)
                    log_error_to_db(album_id, "photo_download", str(e))
            
            for i in range(0, len(photo_media), 10):
                chunk = photo_media[i:i+10]
                for attempt in range(3):
                    try:
                        await client.send_media_group(chat_id, chunk, reply_to_message_id=reply_id)
                        await asyncio.sleep(3)
                        break
                    except FloodWait as e:
                        wait_time = e.value if hasattr(e, 'value') else 15
                        print(f"⏳ FloodWait photos: {wait_time}s")
                        await asyncio.sleep(wait_time + 5)
                    except Exception as e:
                        if attempt < 2: await asyncio.sleep(5)
            
            for p_url in photos[:downloaded_p]:
                if not cancel_tasks.get(chat_id):
                    media_tracker.mark_uploaded(album_id, 'photos', p_url)
                    uploaded_p += 1
            
            for f in os.listdir(user_folder):
                if f.startswith("p_"):
                    try: os.remove(os.path.join(user_folder, f))
                    except: pass

        # Videos
        if videos:
            for v_idx, v_url in enumerate(videos, 1):
                if cancel_tasks.get(chat_id): break
                is_gif = is_gif_url(v_url)
                filepath = os.path.join(user_folder, f"v_{v_idx}.{'gif' if is_gif else 'mp4'}")
                thumb = None
                upload_success = False
                
                try:
                    if is_gif:
                        if not await loop.run_in_executor(executor, download_simple_file, v_url, filepath): continue
                        converted = smart_compressor.convert_gif_to_mp4(filepath)
                        if converted: filepath = converted
                        else: continue
                    else:
                        r_head = session.head(v_url, headers={'Referer': 'https://www.erome.com/'})
                        size = int(r_head.headers.get('content-length', 0))
                        if size == 0: continue
                        await loop.run_in_executor(executor, download_nitro_animated, v_url, filepath, size, status, loop, f"Downloading Video {v_idx}", title)
                        final_v = filepath + ".stream.mp4"
                        subprocess.run(['ffmpeg', '-i', filepath, '-c', 'copy', '-movflags', 'faststart', final_v, '-y'], stderr=subprocess.DEVNULL)
                        if os.path.exists(final_v): os.remove(filepath); os.rename(final_v, filepath)
                    
                    media_tracker.mark_downloaded(album_id, 'videos', v_url)
                    downloaded_v += 1
                    smart_compressor.compress_video(filepath)
                    dur, w, h = get_video_meta(filepath)
                    size_h = get_human_size(os.path.getsize(filepath))
                    thumb = filepath + ".jpg"
                    subprocess.run(['ffmpeg', '-ss', '1', '-i', filepath, '-vframes', '1', thumb, '-y'], stderr=subprocess.DEVNULL)
                    
                    duration_str = time.strftime('%M:%S', time.gmtime(dur))
                    media_type = "GIF" if is_gif else "Video"
                    caption = master_caption + f"\n\n{media_type} `{v_idx}/{len(videos)}` | `{duration_str}` | `{size_h}`" if not master_caption_sent and v_idx == 1 else f"{media_type} `{v_idx}/{len(videos)}` | `{duration_str}` | `{size_h}`"
                    if not master_caption_sent and v_idx == 1: master_caption_sent = True
                    
                    for attempt in range(3):
                        try:
                            start_time_up = [time.time()]
                            await client.send_video(chat_id=chat_id, video=filepath, thumb=thumb if os.path.exists(thumb) else None, width=w, height=h, duration=dur, supports_streaming=True, caption=caption, reply_to_message_id=reply_id, progress=pyrogram_progress, progress_args=(status, start_time_up, f"Uploading {media_type} {v_idx}/{len(videos)}", title))
                            upload_success = True
                            media_tracker.mark_uploaded(album_id, 'videos', v_url)
                            uploaded_v += 1
                            await asyncio.sleep(15)
                            break
                        except FloodWait as e:
                            print(f"⏳ FloodWait: {e.value if hasattr(e, 'value') else 15}s")
                            await asyncio.sleep((e.value if hasattr(e, 'value') else 15) + 15)
                        except RPCError as e:
                            if "FILE_PART_X_MISSING" in str(e): await asyncio.sleep(25)
                            elif attempt < 2: await asyncio.sleep(15)
                        except Exception as e:
                            if attempt < 2: await asyncio.sleep(15)
                    
                    if not upload_success:
                        error_notifier.notify("Video Upload", f"Failed: {v_idx}/{len(videos)}", album_id)
                        log_error_to_db(album_id, "video_upload", f"Failed video {v_idx}")
                        album_success = False
                    
                except Exception as e:
                    error_notifier.notify("Video Download", str(e), album_id)
                    log_error_to_db(album_id, "video_download", str(e))
                    album_success = False
                finally:
                    try:
                        if os.path.exists(filepath): os.remove(filepath)
                        if thumb and os.path.exists(thumb): os.remove(thumb)
                    except: pass

        # Album Report
        missing = media_tracker.get_missing_media(album_id)
        missing_p = len(missing['photos'])
        missing_v = len(missing['videos'])
        
        error_notifier.album_report(album_id, title, len(photos), len(videos), 
                                    downloaded_p, uploaded_p, downloaded_v, uploaded_v,
                                    missing_p, missing_v, album_success)
        
        if missing_p > 0 or missing_v > 0:
            print(f"⚠️  MISSING MEDIA: {missing_p} photos, {missing_v} videos were downloaded but not uploaded!")
        
        mark_processed(album_id)
        media_tracker.cleanup_album(album_id)
        try: await status.delete()
        except: pass
    
    return album_success

# ============================================
# CALLBACK HANDLERS
# ============================================
@app.on_callback_query(filters.user(ADMIN_IDS))
async def handle_callbacks(client, callback_query):
    data = callback_query.data
    try:
        if data == "show_dashboard":
            await callback_query.message.reply(live_dashboard.get_dashboard_text(smart_queue.get_stats()), reply_markup=keyboard_manager.get_control_keyboard())
            await callback_query.answer("Dashboard updated!")
        elif data == "pause_all": smart_queue.pause(); await callback_query.answer("Queue paused!")
        elif data == "resume_all": smart_queue.resume(); await callback_query.answer("Queue resumed!")
        elif data == "cancel_all": smart_queue.cancel_all(); cancel_tasks[callback_query.message.chat.id] = True; await callback_query.answer("Cancelled!")
        elif data == "clean_cache": await callback_query.answer(f"Cleaned {smart_cache.clean_old_cache()} cache files!")
    except: pass

# ============================================
# COMMAND HANDLERS
# ============================================
@app.on_message(filters.command("start", prefixes=".") & filters.user(ADMIN_IDS))
async def start_cmd(client, message):
    await message.reply("**Bot Started!**\n\n.user `<username>` - Download\n.dashboard - Live status\n.errors - Recent errors\n.missing - Check missing media\n.cancel - Stop\n.stats - Statistics\n.reset - Reset DB", reply_markup=keyboard_manager.get_control_keyboard())

@app.on_message(filters.command("cancel", prefixes=".") & filters.user(ADMIN_IDS))
async def cancel_cmd(client, message):
    cancel_tasks[message.chat.id] = True; smart_queue.cancel_all()
    await message.reply("**Cancelled!**")

@app.on_message(filters.command("dashboard", prefixes=".") & filters.user(ADMIN_IDS))
async def dashboard_cmd(client, message):
    await message.reply(live_dashboard.get_dashboard_text(smart_queue.get_stats()), reply_markup=keyboard_manager.get_control_keyboard())

@app.on_message(filters.command("errors", prefixes=".") & filters.user(ADMIN_IDS))
async def errors_cmd(client, message):
    try:
        conn = sqlite3.connect(DB_NAME)
        rows = conn.execute("SELECT album_id, error_type, error_message, timestamp FROM error_log ORDER BY timestamp DESC LIMIT 20").fetchall()
        conn.close()
        if rows:
            text = "**Recent Errors:**\n" + "\n".join([f"- `{r[0]}`: {r[1]} - {r[2][:80]}" for r in rows])
            await message.reply(text)
        else: await message.reply("✅ No errors logged!")
    except Exception as e: await message.reply(f"Error: {e}")

@app.on_message(filters.command("missing", prefixes=".") & filters.user(ADMIN_IDS))
async def missing_cmd(client, message):
    if media_tracker.pending_albums:
        text = "**Pending Albums:**\n"
        for album_id in list(media_tracker.pending_albums.keys())[:10]:
            missing = media_tracker.get_missing_media(album_id)
            text += f"- `{album_id}`: {len(missing['photos'])}p, {len(missing['videos'])}v missing\n"
        await message.reply(text)
    else: await message.reply("✅ No pending albums!")

@app.on_message(filters.command("user", prefixes=".") & filters.user(ADMIN_IDS))
async def user_cmd(client, message):
    if len(message.command) < 2: return
    chat_id = message.chat.id; raw_input = message.command[1].strip(); cancel_tasks[chat_id] = False
    if "/a/" in raw_input:
        await process_album(client, chat_id, message.id, raw_input, "direct", 1, 1); return
    query = raw_input.split("erome.com/")[-1].split('/')[0]
    msg = await message.reply(f"**Scanning `{query}`...**")
    all_urls = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36', 'Referer': 'https://www.erome.com/'}
    
    page = 1
    while True:
        if cancel_tasks.get(chat_id): break
        try:
            res = session.get(f"https://www.erome.com/{query}?page={page}", headers=headers, timeout=15)
            if res.status_code != 200: break
            ids = list(dict.fromkeys(re.findall(r'/a/([a-zA-Z0-9]{8})', res.text)))
            if not ids: break
            for aid in ids:
                f_url = f"https://www.erome.com/a/{aid}"
                if f_url not in all_urls: all_urls.append(f_url)
            await safe_edit(msg, f"**Scanning `{query}`**\nPage: `{page}` | Found: `{len(all_urls)}` albums")
            if "Next" not in res.text: break
            page += 1; await asyncio.sleep(0.3)
        except: break
    profile_pages = page
    
    search_page = 1
    while True:
        if cancel_tasks.get(chat_id): break
        try:
            res = session.get(f"https://www.erome.com/search?v={query}&page={search_page}", headers=headers, timeout=15)
            if res.status_code != 200: break
            ids = list(dict.fromkeys(re.findall(r'/a/([a-zA-Z0-9]{8})', res.text)))
            if not ids: break
            for aid in ids:
                f_url = f"https://www.erome.com/a/{aid}"
                if f_url not in all_urls: all_urls.append(f_url)
            if "Next" not in res.text: break
            search_page += 1; await asyncio.sleep(0.3)
        except: break
    
    if not all_urls: return await msg.edit_text(f"**No content found for `{query}`**")
    print(f"\n{'='*60}\n✅ SCAN COMPLETE: {query}\n{'='*60}\nAlbums: {len(all_urls)}\nPages: {profile_pages} + {search_page}\n{'='*60}\n")
    await msg.edit_text(f"**Scan Complete!**\nUser: `{query}`\nAlbums: `{len(all_urls)}`\nPages: `{profile_pages}` + `{search_page}`\n\n_Starting downloads..._")
    
    for i, url in enumerate(all_urls, 1):
        if cancel_tasks.get(chat_id): break
        await smart_queue.add_task(f"{query}_{i}", process_album, priority=i, client=client, chat_id=chat_id, reply_id=message.id, url=url, username=query, current=i, total=len(all_urls))
    
    await smart_queue.process_queue()
    stats = smart_queue.get_stats()
    await message.reply(f"**Done!** Success: `{stats['completed']}` | Failed: `{stats['failed']}`\n\n_.errors - View errors_\n_.missing - Check missing media_")

@app.on_message(filters.command("reset", prefixes=".") & filters.user(ADMIN_IDS))
async def reset_db(client, message):
    if os.path.exists(DB_NAME): os.remove(DB_NAME)
    init_db(); backup_to_github()
    await message.reply("**Memory Cleared!**")

@app.on_message(filters.command("stats", prefixes=".") & filters.user(ADMIN_IDS))
async def stats_cmd(client, message):
    conn = sqlite3.connect(DB_NAME)
    processed_count = conn.execute("SELECT COUNT(*) FROM processed").fetchone()[0]
    error_count = conn.execute("SELECT COUNT(*) FROM error_log").fetchone()[0]
    conn.close()
    await message.reply(f"**Stats**\nProcessed: `{processed_count}`\nErrors: `{error_count}`\n\n{live_dashboard.get_dashboard_text(smart_queue.get_stats())}")

# ============================================
# MAIN
# ============================================
async def main():
    init_db()
    async with app:
        print("\n" + "=" * 60)
        print("🚀 BOT STARTED SUCCESSFULLY!")
        print("=" * 60)
        print("✅ Error Notifications: TERMINAL ONLY")
        print("✅ Media Tracking: Active")
        print("✅ Smart Queue: 3 concurrent")
        print("✅ All Features: Active")
        print("=" * 60 + "\n")
        await idle()

if __name__ == "__main__":
    try: app.run(main())
    except KeyboardInterrupt: print("\n👋 Bot stopped by user")
    except Exception as e: print(f"\n❌ Fatal error: {e}")
