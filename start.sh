#!/usr/bin/env bash
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[首次运行] 正在创建环境并安装依赖……"
  python -m venv .venv
  .venv/Scripts/python -m pip install -r requirements.txt
fi
echo "服务启动中，浏览器稍后自动打开 http://127.0.0.1:5000"
( sleep 2 && cmd //c start "" "http://127.0.0.1:5000" >/dev/null 2>&1 ) &
.venv/Scripts/python app.py
