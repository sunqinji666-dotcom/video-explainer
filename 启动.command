#!/bin/bash
# 影片解释器 - 一键启动
cd "$(dirname "$0")"
export NO_PROXY="*"
export no_proxy="*"
echo "🎬 影片解释器 正在启动..."
echo "如果窗口未弹出，请手动打开浏览器访问 http://127.0.0.1:5199"
echo "按 Ctrl+C 退出"
python3 -c "
import os
os.environ['NO_PROXY'] = '*'
os.environ['no_proxy'] = '*'
from main import main
main()
"
