#!/usr/bin/env sh
# Builds dist/texturechanger for Linux. Run: sh build.sh
set -e
cd "$(dirname "$0")"
python3 test_texturechanger.py
python3 -m PyInstaller --noconfirm --onefile --windowed --name texturechanger --add-data 'ui.html:.' texturechanger.py
echo "Built dist/texturechanger"
