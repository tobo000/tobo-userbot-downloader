#!/bin/bash

# ============================================
# Bot Start Script - Erome + Mega.nz Downloader
# ============================================

echo "========================================"
echo "🚀 BOT START SCRIPT"
echo "========================================"

# ------------------------------------------
# 1. Clean up old processes
# ------------------------------------------
echo ""
echo "[1/5] Cleaning up old processes..."
pkill -9 -f main.py 2>/dev/null
sleep 1
echo "      Done."

# ------------------------------------------
# 2. Clean session files
# ------------------------------------------
echo ""
echo "[2/5] Cleaning session files..."
rm -f *.session-journal 2>/dev/null
echo "      Done."

# ------------------------------------------
# 3. Activate virtual environment
# ------------------------------------------
echo ""
echo "[3/5] Activating virtual environment..."
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "      Virtual Environment activated."
else
    echo "      ERROR: venv folder not found!"
    exit 1
fi

# ------------------------------------------
# 4. Check dependencies
# ------------------------------------------
echo ""
echo "[4/5] Checking dependencies..."

# Check Python
if command -v python3 &> /dev/null; then
    echo "      ✅ Python: $(python3 --version)"
else
    echo "      ❌ Python3 not found!"
    exit 1
fi

# Check FFmpeg
if command -v ffmpeg &> /dev/null; then
    echo "      ✅ FFmpeg: installed"
else
    echo "      ⚠️  FFmpeg not found - video processing disabled"
fi

# Check FFprobe
if command -v ffprobe &> /dev/null; then
    echo "      ✅ FFprobe: installed"
else
    echo "      ⚠️  FFprobe not found - video metadata disabled"
fi

# Check Megatools (for Mega.nz support)
if command -v megadl &> /dev/null; then
    echo "      ✅ Megatools: installed (Mega.nz support enabled)"
else
    echo "      ⚠️  Megatools not found - Mega.nz support disabled"
    echo "      Install: sudo apt-get update && sudo apt-get install -y megatools"
fi

# Check .env file
if [ -f ".env" ]; then
    echo "      ✅ .env file: found"
else
    echo "      ❌ .env file not found!"
    echo "      Create .env with: API_ID, API_HASH, GH_TOKEN, GH_REPO"
    exit 1
fi

# ------------------------------------------
# 5. Backup database + Start bot
# ------------------------------------------
echo ""
echo "[5/5] Starting the Bot..."

# Create backups folder if not exists
mkdir -p backups

# Backup database with timestamp
if [ -f "bot_archive.db" ]; then
    DB_SIZE=$(ls -lh bot_archive.db | awk '{print $5}')
    cp bot_archive.db "backups/bot_archive_$(date +%Y%m%d_%H%M%S).db"
    echo "      Database backup created (Size: $DB_SIZE)"
    
    # Keep only last 5 backups
    ls -t backups/bot_archive_*.db 2>/dev/null | tail -n +6 | xargs rm -f 2>/dev/null
fi

echo ""
echo "========================================"
echo "   BOT IS STARTING..."
echo "========================================"
echo ""
echo "   Commands:"
echo "   .user Ashpaul69         - Scan user"
echo "   .user Ashpaul69 1-50    - Download range"
echo "   .dashboard              - Check status"
echo "   .cancel                 - Stop all"
echo ""
echo "========================================"
echo ""

# Run the bot
python3 main.py

# If bot stops, show message
echo ""
echo "========================================"
echo "   BOT STOPPED"
echo "========================================"
echo ""
echo "   To restart: ./start.sh"
echo "   To restore backup: cp backups/bot_archive_YYYYMMDD_HHMMSS.db bot_archive.db"
echo ""
