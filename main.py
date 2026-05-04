import os, asyncio, requests, time, subprocess, json, re, sqlite3, base64, hashlib, pickle, logging
from pathlib import Path
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional
from bs4 import BeautifulSoup
from pyrogram import Client, filters, idle
from pyrogram.types import InputMediaPhoto, InputMediaVideo, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.errors import FloodWait, RPCError
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv

# ============================================
# CONFIGURATION
# ============================================
load_dotenv()
API_ID = int(os.getenv("API_ID", 0))
API_HASH = os.getenv("API_HASH", "")
ADMIN_IDS = [5549600755, 7010218617]
GH_TOKEN = os.getenv("GH_TOKEN", "")
GH_REPO = os.getenv("GH_REPO", "")
GH_FILE_PATH = "bot_archive.db"

if not API_ID or not API_HASH: raise ValueError("Missing API_ID or API_HASH")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(), logging.FileHandler('bot_full.log'), logging.FileHandler('bot_errors.log')])
logger = logging.getLogger(__name__)
for h in logger.handlers:
    if h.baseFilename and 'errors' in h.baseFilename: h.setLevel(logging.ERROR)

app = Client("tobo_pro_session", api_id=API_ID, api_hash=API_HASH, sleep_threshold=600, max_concurrent_transmissions=1, workers=4)

DOWNLOAD_DIR, DB_NAME, CACHE_DIR, CHECKPOINT_DIR = "downloads", "bot_archive.db", "cache", "checkpoints"
for d in [DOWNLOAD_DIR, CACHE_DIR, CHECKPOINT_DIR]:
    if not os.path.exists(d): os.makedirs(d)

session = requests.Session(); executor = ThreadPoolExecutor(max_workers=8)
cancel_tasks = {}; chat_locks = {}

# ============================================
# OPTIMIZED RATE LIMITER
# ============================================
class RateLimitOptimizer:
    def __init__(self):
        self.delay = 12; self.success = 0; self.floods = 0; self.total = 0; self.ftotal = 0
    def record_success(self, sz): self.total += 1; self.success += 1
    def record_flood(self, wt, sz): self.total += 1; self.ftotal += 1; self.success = 0; self.delay = min(60, self.delay + 5)
    def get_delay(self, sz=0):
        mb = sz / 1048576 if sz > 0 else 0
        if mb > 500: return self.delay + 12
        if mb > 200: return self.delay + 6
        if mb > 100: return self.delay + 3
        return self.delay
    def get_buffer(self, wt): return wt + 8
    def dash(self):
        r = self.ftotal / max(self.total, 1)
        return f"⚡ **Rate** | Delay: `{self.delay}s` | Floods: `{self.ftotal}` ({r:.1%})\n"

rate_optimizer = RateLimitOptimizer()

# ============================================
# ERROR NOTIFIER
# ============================================
class ErrorNotifier:
    def __init__(self): self.c = 0
    def notify(self, t, m, d=""): self.c += 1; print(f"\n{'='*50}\n⚠️  ERROR #{self.c} | {t}\n{m[:150]}\n{'='*50}\n")
    def report(self, aid, title, p, v, dp, up, dv, uv, mp, mv, ok, gh):
        s = "✅" if (ok and mp==0 and mv==0 and gh) else "❌"
        print(f"\n{'='*50}\n📊 {aid} | {s}\nP: {dp}/{p} | V: {dv}/{v}\nMissing: {mp}p,{mv}v | GH: {gh}\n{'='*50}\n")

error_notifier = ErrorNotifier()

# ============================================
# MEDIA TRACKER
# ============================================
class MediaTracker:
    def __init__(self): self.a = {}
    def reg(self, aid, t, p, v): self.a[aid] = {'t': t, 'p': p, 'v': v, 'd': {'p': [], 'v': []}, 'u': {'p': [], 'v': []}}
    def md(self, aid, mt, url):
        if aid in self.a: self.a[aid]['d'][mt].append(url)
    def mu(self, aid, mt, url):
        if aid in self.a: self.a[aid]['u'][mt].append(url)
    def miss(self, aid):
        if aid not in self.a: return {'p': [], 'v': []}
        a = self.a[aid]; m = {'p': [], 'v': []}
        for mt in ['p', 'v']: m[mt] = [u for u in a['d'][mt] if u not in a['u'][mt]]
        return m
    def clean(self, aid):
        if aid in self.a: del self.a[aid]

media_tracker = MediaTracker()

def get_chat_lock(cid):
    if cid not in chat_locks: chat_locks[cid] = asyncio.Lock()
    return chat_locks[cid]

# ============================================
# SMART QUEUE (max_concurrent=1 for big files)
# ============================================
class SmartQueue:
    def __init__(self):
        self.q = asyncio.Queue(); self.active = {}; self.done = set(); self.fail = {}; self.max = 1; self.paused = False
    async def add(self, tid, fn, pri=0, **kw):
        await self.q.put((pri, tid, fn, kw)); print(f"📥 {tid}")
    async def process(self):
        while not self.q.empty():
            if self.paused: await asyncio.sleep(1); continue
            done = [t for t, tk in self.active.items() if tk.done()]
            for t in done: del self.active[t]
            while len(self.active) >= self.max: await asyncio.sleep(0.3)
            _, tid, fn, kw = await self.q.get()
            tk = asyncio.create_task(fn(**kw)); self.active[tid] = tk
            tk.add_done_callback(lambda t, i=tid: self._cb(i, t))
        if self.active: await asyncio.gather(*self.active.values(), return_exceptions=True)
    def _cb(self, tid, tk):
        try: self.done.add(tid) if tk.result() else self.fail.update({tid: "Failed"})
        except Exception as e: self.fail[tid] = str(e)
    def stats(self): return {'q': self.q.qsize(), 'a': len(self.active), 'd': len(self.done), 'f': len(self.fail), 'p': self.paused}
    def pause(self): self.paused = True
    def resume(self): self.paused = False
    def cancel(self):
        while not self.q.empty():
            try: self.q.get_nowait()
            except: pass
        for t in list(self.active.values()): t.cancel()

smart_queue = SmartQueue()

# ============================================
# CHECKPOINT & CACHE
# ============================================
class CheckpointManager:
    def __init__(self): self.d = Path(CHECKPOINT_DIR)
    def save(self, aid, s):
        f = self.d / f"{aid}.json"; s['ts'] = time.time()
        try: 
            with open(f, 'w') as fp: json.dump(s, fp)
        except: pass
    def load(self, aid):
        f = self.d / f"{aid}.json"
        if f.exists():
            try:
                with open(f) as fp:
                    s = json.load(fp)
                    if time.time() - s.get('ts', 0) < 86400: return s
            except: pass
    def clear(self, aid):
        f = self.d / f"{aid}.json"
        if f.exists():
            try: f.unlink()
            except: pass

checkpoint_manager = CheckpointManager()

class SmartCache:
    def __init__(self, d=CACHE_DIR, ttl=1):
        self.d = Path(d); self.d.mkdir(exist_ok=True); self.ttl = timedelta(hours=ttl)
    def _k(self, data): return hashlib.md5(data.encode()).hexdigest()
    def get(self, url):
        f = self.d / f"a_{self._k(url)}.pickle"
        if f.exists():
            try:
                with open(f, 'rb') as fp:
                    d = pickle.load(fp)
                    if datetime.now() - d['ts'] < self.ttl: return d['c']
            except: pass
    def put(self, url, c):
        f = self.d / f"a_{self._k(url)}.pickle"
        try:
            with open(f, 'wb') as fp: pickle.dump({'ts': datetime.now(), 'c': c}, fp)
        except: pass
    def clean(self):
        n = 0
        for f in self.d.glob("*.pickle"):
            try:
                with open(f, 'rb') as fp:
                    if datetime.now() - pickle.load(fp)['ts'] > timedelta(hours=24): f.unlink(); n += 1
            except:
                try: f.unlink(); n += 1
                except: pass
        return n

smart_cache = SmartCache()

# ============================================
# LIVE DASHBOARD
# ============================================
class LiveDashboard:
    def __init__(self): self.st = time.time(); self.td = 0; self.sp = 0; self.lu = time.time()
    def up(self, b):
        n = time.time(); d = n - self.lu
        if d > 0: self.sp = b / d
        self.td += b; self.lu = n
    def text(self, qs=None):
        e = time.strftime('%H:%M:%S', time.gmtime(time.time() - self.st))
        t = f"**Dashboard**\nUptime: `{e}` | DL: `{get_human_size(self.td)}` | Speed: `{get_human_size(self.sp)}`/s\n"
        if qs:
            st = "⏸" if qs.get('p') else "▶️"
            t += f"Status: `{st}` | Q: `{qs.get('q',0)}` | Active: `{qs.get('a',0)}` | Done: `{qs.get('d',0)}` | Failed: `{qs.get('f',0)}`\n"
        return t

live_dashboard = LiveDashboard()

# ============================================
# SMART COMPRESSOR
# ============================================
class SmartCompressor:
    P = {'l': {'c': 23, 'p': 'fast'}, 'm': {'c': 28, 'p': 'medium'}, 'x': {'c': 32, 'p': 'slow'}}
    def sc(self, fp): return os.path.exists(fp) and os.path.getsize(fp) / 1048576 > 100
    def ap(self, fp):
        mb = os.path.getsize(fp) / 1048576
        if mb > 500: return self.P['x']
        if mb > 200: return self.P['m']
        return self.P['l']
    def cv(self, inp, prof=None):
        if not self.sc(inp): return
        if not prof: prof = self.ap(inp)
        out = f"{inp}.c.mp4"
        try:
            osz = os.path.getsize(inp)
            subprocess.run(['ffmpeg', '-i', inp, '-c:v', 'libx264', '-crf', str(prof['c']), '-preset', prof['p'], '-c:a', 'aac', '-b:a', '128k', '-movflags', 'faststart', '-y', out], stderr=subprocess.DEVNULL, timeout=300)
            if os.path.exists(out): print(f"📦 {(1-os.path.getsize(out)/osz)*100:.0f}%"); os.remove(inp); os.rename(out, inp)
        except: pass
    def g2m(self, gif):
        mp4 = gif.replace('.gif', '.mp4')
        try:
            subprocess.run(['ffmpeg', '-i', gif, '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', 'faststart', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-y', mp4], stderr=subprocess.DEVNULL, timeout=120)
            if os.path.exists(mp4) and os.path.getsize(mp4) > 0: os.remove(gif); return mp4
        except: pass

smart_compressor = SmartCompressor()

# ============================================
# GITHUB SYNC
# ============================================
def backup_to_github():
    if not GH_TOKEN or not GH_REPO: return False
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE_PATH}"
        h = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(url, headers=h)
        sha = r.json().get('sha') if r.status_code == 200 else None
        with open(DB_NAME, "rb") as f: c = base64.b64encode(f.read()).decode()
        sz = get_human_size(os.path.getsize(DB_NAME))
        d = {"message": f"Sync: {time.strftime('%H:%M:%S')} | {sz}", "content": c, "branch": "main"}
        if sha: d["sha"] = sha
        pr = requests.put(url, headers=h, data=json.dumps(d))
        if pr.status_code in [200, 201]: print(f"☁️  GH Sync: {sz}"); return True
    except: pass
    return False

def download_from_github():
    if not GH_TOKEN or not GH_REPO: return
    try:
        url = f"https://api.github.com/repos/{GH_REPO}/contents/{GH_FILE_PATH}"
        h = {"Authorization": f"token {GH_TOKEN}", "Accept": "application/vnd.github.v3+json"}
        r = requests.get(url, headers=h)
        if r.status_code == 200:
            c = base64.b64decode(r.json()['content'])
            with open(DB_NAME, "wb") as f: f.write(c)
            print(f"✅ GH Restore: {get_human_size(len(c))}")
    except: pass

# ============================================
# DATABASE
# ============================================
def init_db():
    restored = download_from_github()
    conn = sqlite3.connect(DB_NAME)
    conn.execute("CREATE TABLE IF NOT EXISTS fp (id TEXT PRIMARY KEY, t TEXT, p INTEGER, v INTEGER, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS err (id INTEGER PRIMARY KEY AUTOINCREMENT, aid TEXT, et TEXT, em TEXT, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("CREATE TABLE IF NOT EXISTS fail (id TEXT PRIMARY KEY, url TEXT, t TEXT, p INTEGER, v INTEGER, et TEXT, em TEXT, rc INTEGER DEFAULT 0, mr INTEGER DEFAULT 3, ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.commit(); conn.close()
    print("✅ DB Ready")
    if not restored: print("☁️  Uploading fresh DB..."); backup_to_github()

def is_processed(aid):
    conn = sqlite3.connect(DB_NAME)
    r = conn.execute("SELECT 1 FROM fp WHERE id = ?", (aid,)).fetchone(); conn.close()
    return r is not None

def mark_processed(aid, t, p, v):
    try:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT OR IGNORE INTO fp (id, t, p, v) VALUES (?,?,?,?)", (aid, t, p, v))
        conn.execute("DELETE FROM fail WHERE id = ?", (aid,)); conn.commit(); conn.close()
        return backup_to_github()
    except: return False

def mark_failed(aid, url, t, p, v, et, em):
    try:
        conn = sqlite3.connect(DB_NAME)
        ex = conn.execute("SELECT rc FROM fail WHERE id = ?", (aid,)).fetchone()
        if ex: conn.execute("UPDATE fail SET et=?, em=?, rc=rc+1, ts=CURRENT_TIMESTAMP WHERE id=?", (et, em, aid))
        else: conn.execute("INSERT INTO fail (id, url, t, p, v, et, em) VALUES (?,?,?,?,?,?,?)", (aid, url, t, p, v, et, em))
        conn.commit(); conn.close(); backup_to_github()
    except: pass

def get_failed():
    conn = sqlite3.connect(DB_NAME)
    rows = conn.execute("SELECT id, url, t, p, v, et, rc, mr FROM fail WHERE rc < mr ORDER BY rc ASC").fetchall(); conn.close()
    return [{'id': r[0], 'url': r[1], 't': r[2], 'p': r[3], 'v': r[4], 'et': r[5], 'rc': r[6], 'mr': r[7]} for r in rows]

def log_error(aid, et, em):
    try:
        conn = sqlite3.connect(DB_NAME); conn.execute("INSERT INTO err (aid, et, em) VALUES (?,?,?)", (aid, et, em)); conn.commit(); conn.close()
    except: pass

# ============================================
# HELPERS
# ============================================
def bar(c, t):
    if t <= 0: return "[░░░░░░░░░░] 0%"
    p = min(100, (c/t)*100)
    return f"[{'█'*int(p/10)}{'░'*(10-int(p/10))}] {p:.1f}%"

def get_human_size(n):
    for u in ['B', 'KB', 'MB', 'GB']:
        if abs(n) < 1024.0: return f"{n:3.1f} {u}"
        n /= 1024.0
    return f"{n:.1f} TB"

async def safe_edit(m, t):
    try: await m.edit_text(t)
    except: pass

async def pyro_progress(c, t, sm, st, a, tp=""):
    now = time.time()
    if now - st[0] > 3:
        anims = ["🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"]
        anim = anims[int(now) % len(anims)]
        try:
            await sm.edit_text(f"{anim} **{a}**\nTopic: `{tp[:30]}...`\n\n{bar(c, t)}\n📦 {get_human_size(c)} / {get_human_size(t)}")
            st[0] = now
        except: pass

def get_video_meta(fp):
    try:
        cmd = ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_streams', '-show_format', fp]
        r = subprocess.check_output(cmd).decode('utf-8'); d = json.loads(r)
        dur = int(float(d.get('format', {}).get('duration', 0)))
        vs = next((s for s in d.get('streams', []) if s['codec_type'] == 'video'), {})
        return dur, int(vs.get('width', 1280)), int(vs.get('height', 720))
    except: return 1, 1280, 720

def is_gif(u): return '.gif' in u.lower()

# ============================================
# PLATFORM
# ============================================
def detect_platform(u):
    ul = u.lower()
    if 'erome.com' in ul: return 'erome'
    if 'mega.nz' in ul: return 'mega'
    return 'unknown'

# ============================================
# DOWNLOAD ENGINES
# ============================================
def download_nitro(url, path, size, sm, loop, a, tp, segs=4):
    ch = size // segs; ds = [0]; st = [time.time()]
    h = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.erome.com/'}
    def dp(s, e, n):
        pp = f"{path}.p{n}"; hd = h.copy(); hd['Range'] = f'bytes={s}-{e}'
        try:
            with requests.get(url, headers=hd, stream=True, timeout=60) as r:
                with open(pp, 'wb') as f:
                    for ck in r.iter_content(chunk_size=1048576):
                        if ck: f.write(ck); ds[0] += len(ck); live_dashboard.up(len(ck))
                        if sm and ds[0] % 5242880 == 0:
                            asyncio.run_coroutine_threadsafe(pyro_progress(ds[0], size, sm, st, a, tp), loop)
        except: pass
    with ThreadPoolExecutor(max_workers=segs) as ex:
        futs = [ex.submit(dp, i*ch, ((i+1)*ch-1 if i<segs-1 else size-1), i) for i in range(segs)]
        for f in futs: f.result()
    with open(path, 'wb') as f:
        for i in range(segs):
            pp = f"{path}.p{i}"
            if os.path.exists(pp):
                with open(pp, 'rb') as pf: f.write(pf.read())
                os.remove(pp)

def download_simple(url, path):
    try:
        r = session.get(url, headers={'Referer': 'https://www.erome.com/'}, timeout=30)
        with open(path, 'wb') as f: f.write(r.content)
        return True
    except: return False

def download_mega(url, path):
    try:
        print(f"   📥 Mega: {os.path.basename(path)}")
        dd = os.path.dirname(path)
        r = subprocess.run(['megadl', '--path', dd, url], capture_output=True, text=True, timeout=600)
        if r.returncode == 0:
            for f in os.listdir(dd):
                fp = os.path.join(dd, f)
                if os.path.isfile(fp) and f != os.path.basename(path):
                    if os.path.getsize(fp) > 0: os.rename(fp, path); break
            if os.path.exists(path) and os.path.getsize(path) > 0: return True
        return False
    except: return False

# ============================================
# SCRAPERS
# ============================================
def scrape_mega(url):
    cached = smart_cache.get(url)
    if cached: return cached
    try:
        r = subprocess.run(['megals', '--export', url], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip():
            lines = r.stdout.strip().split('\n'); t = "Mega"; p, v = [], []
            for l in lines:
                pts = l.strip().split(' ', 1)
                if len(pts) == 2:
                    fu, fn = pts
                    if any(fn.lower().endswith(e) for e in ['.mp4','.webm','.mov']): v.append(fu)
                    elif any(fn.lower().endswith(e) for e in ['.jpg','.png','.gif']): p.append(fu)
            if not p and not v: v = [url]
        else: t, p, v = "Mega", [], [url]
        res = (t, p, v); smart_cache.put(url, res); return res
    except: t, p, v = "Mega", [], [url]; res = (t, p, v); smart_cache.put(url, res); return res

def scrape_erome(url):
    cached = smart_cache.get(url)
    if cached: return cached
    h = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.erome.com/'}
    try:
        r = session.get(url, headers=h, timeout=20)
        s = BeautifulSoup(r.text, 'html.parser')
        t = s.find("h1").get_text(strip=True) if s.find("h1") else "Untitled"
        am = []
        for img in s.select('div.img img'):
            src = img.get('data-src') or img.get('src')
            if src:
                if src.startswith('//'): src = 'https:' + src
                am.append(src)
        gifs = [x for x in am if '.gif' in x.lower()]
        photos = [x for x in am if '.gif' not in x.lower()]
        vl = []
        for vt in s.find_all(['source', 'video']):
            vs = vt.get('src') or vt.get('data-src')
            if vs and ".mp4" in vs:
                if vs.startswith('//'): vs = 'https:' + vs
                vl.append(vs)
        vl.extend(re.findall(r'https?://[^\s"\'>]+.mp4', r.text))
        vl = list(dict.fromkeys([v for v in vl if "erome.com" in v]))
        res = (t, list(dict.fromkeys(photos)), list(dict.fromkeys(gifs + vl)))
        smart_cache.put(url, res); return res
    except: return "Error", [], []

def scrape_album_details(url):
    cached = smart_cache.get(url)
    if cached: return cached
    p = detect_platform(url)
    if p == 'erome': return scrape_erome(url)
    if p == 'mega': return scrape_mega(url)
    return "Error", [], []

# ============================================
# 🔥 CORE DELIVERY (OPTIMIZED)
# ============================================
async def process_album(client, chat_id, reply_id, url, username, current, total):
    album_id = url.rstrip('/').split('/')[-1]
    if is_processed(album_id): print(f"⏭️  Skip: {album_id}"); return True

    platform = detect_platform(url)
    title, photos, videos = await asyncio.get_event_loop().run_in_executor(executor, scrape_album_details, url)
    if not photos and not videos: return False

    media_tracker.reg(album_id, title, photos, videos)
    print(f"\n📊 [{current}/{total}] [{platform.upper()}] {title[:40]}")
    print(f"   P: {len(photos)} | V: {len(videos)}")

    uf = os.path.join(DOWNLOAD_DIR, f"{chat_id}_{album_id}")
    os.makedirs(uf, exist_ok=True)
    loop = asyncio.get_event_loop()

    # Status for animations
    status = await client.send_message(chat_id, f"📡 **[{current}/{total}] Preparing...**", reply_to_message_id=reply_id)

    dp_list, dv_list = [], []

    # Download Photos
    if photos:
        for i, pu in enumerate(photos, 1):
            if cancel_tasks.get(chat_id): break
            path = os.path.join(uf, f"p_{i}.jpg")
            try:
                if platform == 'mega':
                    if not await loop.run_in_executor(executor, download_mega, pu, path): continue
                else:
                    def dl(): 
                        r = session.get(pu, headers={'Referer': 'https://www.erome.com/'}, timeout=15)
                        with open(path, 'wb') as f: f.write(r.content)
                    await loop.run_in_executor(executor, dl)
                media_tracker.md(album_id, 'p', pu); live_dashboard.up(os.path.getsize(path))
                sz = get_human_size(os.path.getsize(path))
                await safe_edit(status, f"🌑 **Photos...**\n`{title[:25]}`\n{bar(i, len(photos))}\n📸 `{i}/{len(photos)}` | `{sz}`")
                dp_list.append((path, f"📸 `{i}/{len(photos)}` | `{sz}`", pu))
            except Exception as e: error_notifier.notify("P-DL", str(e), album_id); log_error(album_id, "pdl", str(e))

    # Download Videos
    if videos:
        for vi, vu in enumerate(videos, 1):
            if cancel_tasks.get(chat_id): break
            gif = is_gif(vu); fp = os.path.join(uf, f"v_{vi}.{'gif' if gif else 'mp4'}")
            try:
                if platform == 'mega':
                    if not await loop.run_in_executor(executor, download_mega, vu, fp): continue
                elif gif:
                    if not await loop.run_in_executor(executor, download_simple, vu, fp): continue
                    cv = smart_compressor.g2m(fp)
                    if cv: fp = cv; gif = False
                    else: continue
                else:
                    rh = session.head(vu, headers={'Referer': 'https://www.erome.com/'}); sz = int(rh.headers.get('content-length', 0))
                    if sz == 0: continue
                    await loop.run_in_executor(executor, download_nitro, vu, fp, sz, status, loop, f"DL V {vi}", title)
                    fv = fp + ".s.mp4"
                    subprocess.run(['ffmpeg', '-i', fp, '-c', 'copy', '-movflags', 'faststart', fv, '-y'], stderr=subprocess.DEVNULL)
                    if os.path.exists(fv): os.remove(fp); os.rename(fv, fp)
                media_tracker.md(album_id, 'v', vu)
                smart_compressor.cv(fp)
                dur, w, h = get_video_meta(fp); sz = get_human_size(os.path.getsize(fp))
                thumb = fp + ".jpg"
                subprocess.run(['ffmpeg', '-ss', '1', '-i', fp, '-vframes', '1', thumb, '-y'], stderr=subprocess.DEVNULL)
                mt = "GIF" if gif else "🎬"
                caption = f"{mt} `{vi}/{len(videos)}` | `{time.strftime('%M:%S', time.gmtime(dur))}` | `{sz}`"
                dv_list.append((fp, thumb, w, h, dur, caption, gif, vu))
            except Exception as e: error_notifier.notify("V-DL", str(e), album_id); log_error(album_id, "vdl", str(e))

    print(f"   ✅ DL: {len(dp_list)}p, {len(dv_list)}v")

    # Upload Phase
    chat_lock = get_chat_lock(chat_id); up_p, up_v = 0, 0; all_ok = True

    async with chat_lock:
        gc = sum(1 for v in dv_list if v[6])
        mc = f"**{title}**\n━━━━━━━━━━\nAlbum: `{current}/{total}`\nContent: `{len(dp_list)}` 📸 | `{len(dv_list)}` 🎬{' | '+str(gc)+' GIFs' if gc else ''}\nPlatform: `{platform.upper()}`\n👤 `{username.upper()}`\n📦 Original\n━━━━━━━━━━"
        mcs = False

        # Upload Photos
        if dp_list:
            pm = []
            for idx, (path, cap, pu) in enumerate(dp_list, 1):
                fc = mc + f"\n\n{cap}" if not mcs else cap
                if idx == 1: mcs = True
                pm.append(InputMediaPhoto(path, caption=fc))
            tc = (len(pm) + 9) // 10
            for i in range(0, len(pm), 10):
                cn = i // 10 + 1; chunk = pm[i:i+10]
                await safe_edit(status, f"📤 **Photos {cn}/{tc}**\n`{title[:25]}`\n{bar(cn, tc)}")
                for _ in range(3):
                    try: await client.send_media_group(chat_id, chunk, reply_to_message_id=reply_id); await asyncio.sleep(3); break
                    except FloodWait as e: wt = e.value if hasattr(e,'value') else 15; print(f"   ⏳ FW: {wt}s"); await asyncio.sleep(wt + 5)
                    except: await asyncio.sleep(5)
            for path, cap, pu in dp_list: media_tracker.mu(album_id, 'p', pu); up_p += 1
            for path, cap, pu in dp_list:
                try: 
                    if os.path.exists(path): os.remove(path)
                except: pass

        # Upload Videos
        if dv_list:
            for idx, (fp, thumb, w, h, dur, cap, gif, vu) in enumerate(dv_list, 1):
                mt = "GIF" if gif else "🎬"
                fc = mc + f"\n\n{cap}" if not mcs else cap
                if idx == 1: mcs = True
                fsz = os.path.getsize(fp); ok = False; pd = rate_optimizer.get_delay(fsz)
                for _ in range(3):
                    try:
                        if idx > 1: await asyncio.sleep(pd)
                        st_up = [time.time()]
                        await client.send_video(chat_id=chat_id, video=fp, thumb=thumb if os.path.exists(thumb) else None, width=w, height=h, duration=dur, supports_streaming=True, caption=fc, reply_to_message_id=reply_id, progress=pyro_progress, progress_args=(status, st_up, f"📤 {mt} {idx}/{len(dv_list)}", title))
                        ok = True; media_tracker.mu(album_id, 'v', vu); up_v += 1
                        rate_optimizer.record_success(fsz); print(f"   ✅ {idx}"); break
                    except FloodWait as e:
                        wt = e.value if hasattr(e,'value') else 15; rate_optimizer.record_flood(wt, fsz)
                        fb = rate_optimizer.get_buffer(wt)
                        print(f"   ⏳ FW: {wt}s (buf: {fb}s)"); await safe_edit(status, f"⏳ **FloodWait {wt}s**\n`{title[:25]}`\n📦 `{get_human_size(fsz)}`")
                        await asyncio.sleep(fb)
                        try:
                            if not app.is_connected: await app.start()
                        except: pass
                    except RPCError as e:
                        if "FILE_PART_X_MISSING" in str(e): await asyncio.sleep(30)
                        elif _ < 2: await asyncio.sleep(20)
                    except: await asyncio.sleep(20)
                if not ok: all_ok = False; error_notifier.notify("V-UP", f"Fail: {idx}", album_id)
                try:
                    if os.path.exists(fp): os.remove(fp)
                    if thumb and os.path.exists(thumb): os.remove(thumb)
                except: pass

    try: await status.delete()
    except: pass

    # Verify
    miss = media_tracker.miss(album_id); mp, mv = len(miss['p']), len(miss['v'])
    if all_ok and mp == 0 and mv == 0: gh = mark_processed(album_id, title, len(photos), len(videos)); ok_final = True
    else: mark_failed(album_id, url, title, len(photos), len(videos), "inc", f"M:{mp}p,{mv}v"); gh = False; ok_final = False
    error_notifier.report(album_id, title, len(photos), len(videos), len(dp_list), up_p, len(dv_list), up_v, mp, mv, ok_final, gh)
    media_tracker.clean(album_id)
    return ok_final

# ============================================
# RETRY
# ============================================
async def retry_failed(client, cid, rid):
    failed = get_failed()
    if failed:
        print(f"\n🔄 RETRY {len(failed)}")
        for a in failed:
            if is_processed(a['id']): continue
            print(f"   🔄 {a['id']} ({a['rc']+1}/{a['mr']})")
            await smart_queue.add(f"r_{a['id']}", process_album, pri=0, client=client, chat_id=cid, reply_id=rid, url=a['url'], username="retry", current=a['rc']+1, total=a['mr'])
        await smart_queue.process()
        print("✅ Retry done\n")

# ============================================
# COMMANDS
# ============================================
@app.on_message(filters.command("start", prefixes=".") & filters.user(ADMIN_IDS))
async def start_cmd(c, m):
    await m.reply("**🤖 Bot**\n.user `<u>` - Scan\n.user `<u> <s>-<e>` - Range\n.user `<u> all` - All\n.retry | .failed | .rate | .dashboard | .errors | .missing | .cancel | .stats | .reset")

@app.on_message(filters.command("retry", prefixes=".") & filters.user(ADMIN_IDS))
async def retry_cmd(c, m):
    f = get_failed()
    if f: await m.reply(f"🔄 {len(f)}..."); await retry_failed(c, m.chat.id, m.id); await m.reply("✅")
    else: await m.reply("✅ None")

@app.on_message(filters.command("failed", prefixes=".") & filters.user(ADMIN_IDS))
async def failed_cmd(c, m):
    f = get_failed()
    if f: await m.reply("**Failed:**\n" + "\n".join([f"- `{x['id']}`: {x['et']} ({x['rc']}/{x['mr']})" for x in f[:20]]))
    else: await m.reply("✅ None")

@app.on_message(filters.command("rate", prefixes=".") & filters.user(ADMIN_IDS))
async def rate_cmd(c, m):
    if len(m.command) > 1:
        try: d = int(m.command[1]); old = rate_optimizer.delay; rate_optimizer.delay = max(5, min(60, d)); await m.reply(f"⚡ `{old}s` → `{rate_optimizer.delay}s`")
        except: await m.reply(".rate <s>")
    else: await m.reply(rate_optimizer.dash())

@app.on_message(filters.command("cancel", prefixes=".") & filters.user(ADMIN_IDS))
async def cancel_cmd(c, m): cancel_tasks[m.chat.id] = True; smart_queue.cancel(); await m.reply("🛑")

@app.on_message(filters.command("dashboard", prefixes=".") & filters.user(ADMIN_IDS))
async def dashboard_cmd(c, m): await m.reply(live_dashboard.text(smart_queue.stats()) + "\n" + rate_optimizer.dash())

@app.on_message(filters.command("errors", prefixes=".") & filters.user(ADMIN_IDS))
async def errors_cmd(c, m):
    try:
        conn = sqlite3.connect(DB_NAME); rows = conn.execute("SELECT aid, et, em, ts FROM err ORDER BY ts DESC LIMIT 20").fetchall(); conn.close()
        if rows: await m.reply("**Errors:**\n" + "\n".join([f"- `{r[0]}`: {r[1]}" for r in rows]))
        else: await m.reply("✅ None")
    except: pass

@app.on_message(filters.command("missing", prefixes=".") & filters.user(ADMIN_IDS))
async def missing_cmd(c, m):
    if media_tracker.a:
        t = "**Pending:**\n"
        for aid in list(media_tracker.a.keys())[:10]: ms = media_tracker.miss(aid); t += f"- `{aid}`: {len(ms['p'])}p, {len(ms['v'])}v\n"
        await m.reply(t)
    else: await m.reply("✅ None")

@app.on_message(filters.command("user", prefixes=".") & filters.user(ADMIN_IDS))
async def user_cmd(c, m):
    if len(m.command) < 2: await m.reply(".user `<u/url>`\n.user Ashpaul69 1-50\n.user Ashpaul69 all"); return
    cid = m.chat.id; cancel_tasks[cid] = False
    args = m.command[1:]; ri = args[0].strip(); rp = args[1].strip() if len(args) > 1 else None
    if not ri.startswith('http'): pf = 'erome'; q = ri
    else: pf = detect_platform(ri); q = ri.split("erome.com/")[-1].split('/')[0] if pf == 'erome' else ri
    print(f"   🔍 {pf.upper()} | {ri} | Range: {rp}")
    if pf in ['erome', 'mega'] and ('/a/' in ri or '/folder/' in ri or '/file/' in ri): await process_album(c, cid, m.id, ri, "direct", 1, 1); return
    if pf == 'mega': await process_album(c, cid, m.id, ri, "mega", 1, 1); return
    if pf == 'erome':
        msg = await m.reply(f"**🔍 Scanning `{q}`...**")
        allu = []; h = {'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.erome.com/'}
        pg = 1
        while True:
            if cancel_tasks.get(cid): break
            try:
                r = session.get(f"https://www.erome.com/{q}?page={pg}", headers=h, timeout=15)
                if r.status_code != 200: break
                ids = list(dict.fromkeys(re.findall(r'/a/([a-zA-Z0-9]{8})', r.text)))
                if not ids: break
                for aid in ids:
                    fu = f"https://www.erome.com/a/{aid}"
                    if fu not in allu: allu.append(fu)
                await safe_edit(msg, f"🔍 `{q}` Pg:`{pg}` | `{len(allu)}`")
                if "Next" not in r.text: break
                pg += 1; await asyncio.sleep(0.3)
            except: break
        pp = pg; sp = 1
        while True:
            if cancel_tasks.get(cid): break
            try:
                r = session.get(f"https://www.erome.com/search?v={q}&page={sp}", headers=h, timeout=15)
                if r.status_code != 200: break
                ids = list(dict.fromkeys(re.findall(r'/a/([a-zA-Z0-9]{8})', r.text)))
                if not ids: break
                for aid in ids:
                    fu = f"https://www.erome.com/a/{aid}"
                    if fu not in allu: allu.append(fu)
                if "Next" not in r.text: break
                sp += 1; await asyncio.sleep(0.3)
            except: break
        if not allu: return await msg.edit_text(f"❌ None for `{q}`")
        print(f"\n✅ SCAN: {q} | {len(allu)} | {pp}+{sp}p\n")
        if rp:
            if rp.lower() == 'all':
                await msg.edit_text(f"✅ ALL {len(allu)}")
                for i, u in enumerate(allu, 1):
                    if cancel_tasks.get(cid): break
                    await smart_queue.add(f"{q}_{i}", process_album, pri=i, client=c, chat_id=cid, reply_id=m.id, url=u, username=q, current=i, total=len(allu))
                await smart_queue.process()
            elif '-' in rp:
                try:
                    s, e = rp.split('-'); s, e = int(s.strip()), int(e.strip())
                    s = max(1, s); e = min(len(allu), e)
                    if s > e: s, e = e, s
                    sel = allu[s-1:e]
                    await msg.edit_text(f"✅ {s}-{e} ({len(sel)})")
                    for i, u in enumerate(sel, 1):
                        if cancel_tasks.get(cid): break
                        await smart_queue.add(f"{q}_{s+i-1}", process_album, pri=i, client=c, chat_id=cid, reply_id=m.id, url=u, username=q, current=s+i-1, total=len(sel))
                    await smart_queue.process()
                except: await msg.edit_text("❌ Range err")
        else:
            await msg.edit_text(f"**✅ Scan Done!**\n━━━━━━━━\n👤 `{q}`\n📦 `{len(allu)}` albums\n📄 `{pp}`+`{sp}` pages\n━━━━━━━━\n\n_`.user {q} 1-50`_\n_`.user {q} all`_")
        if rp:
            st = smart_queue.stats(); f = get_failed()
            await m.reply(f"**✅** Success: `{st['d']}` | Failed: `{st['f']}` | Retry: `{len(f)}`")

@app.on_message(filters.command("reset", prefixes=".") & filters.user(ADMIN_IDS))
async def reset_cmd(c, m):
    if os.path.exists(DB_NAME): os.remove(DB_NAME)
    init_db(); backup_to_github(); await m.reply("🧹")

@app.on_message(filters.command("stats", prefixes=".") & filters.user(ADMIN_IDS))
async def stats_cmd(c, m):
    conn = sqlite3.connect(DB_NAME)
    pr = conn.execute("SELECT COUNT(*) FROM fp").fetchone()[0]; er = conn.execute("SELECT COUNT(*) FROM err").fetchone()[0]; fl = conn.execute("SELECT COUNT(*) FROM fail WHERE rc < mr").fetchone()[0]; conn.close()
    await m.reply(f"📊 **Stats**\n✅ `{pr}` | ❌ `{er}` | 🔄 `{fl}`\n\n{live_dashboard.text(smart_queue.stats())}\n{rate_optimizer.dash()}")

# ============================================
# MAIN
# ============================================
async def main():
    init_db()
    async with app:
        print("\n" + "="*50 + "\n🚀 BOT STARTED\n" + "="*50 + "\n✅ Optimized | Animations | GH Sync\n✅ .user Ashpaul69 1-50\n" + "="*50 + "\n")
        await retry_failed(app, ADMIN_IDS[0], 0)
        await idle()

if __name__ == "__main__":
    try: app.run(main())
    except KeyboardInterrupt: print("\n👋")
    except Exception as e: print(f"\n❌ {e}")
