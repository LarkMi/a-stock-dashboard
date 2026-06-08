#!/usr/bin/env python3
"""
ngrok_watchdog.py — ngrok 守护进程
- 监控 ngrok 是否存活
- 挂了自动重启
- URL 变化时推送到微信
- 写入 url.txt 供其他脚本读取
"""
import subprocess, time, json, requests, os, re

NGROK = r"C:\Users\LarkMi\AppData\Local\hermes\bin\ngrok.exe"
URL_FILE = r"C:\Users\LarkMi\AppData\Local\hermes\data\ngrok_url.txt"
API_URL = "http://localhost:4040/api/tunnels"
PORT = 8888

# 持久化HTTP会话避免TIME_WAIT堆积（复用TCP连接）
_session = requests.Session()
_session.headers.update({"Connection": "keep-alive"})

def get_current_url():
    """从 ngrok API 获取当前公网 URL（复用TCP连接避免TIME_WAIT）"""
    try:
        r = _session.get(API_URL, timeout=3)
        data = r.json()
        for t in data.get("tunnels", []):
            url = t.get("public_url", "")
            if url.startswith("https://"):
                return url
    except Exception:
        pass
    return None

def save_url(url):
    with open(URL_FILE, "w") as f:
        f.write(url)

def read_saved_url():
    try:
        with open(URL_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return None

def start_ngrok():
    """启动 ngrok 进程"""
    return subprocess.Popen(
        [NGROK, "http", str(PORT), "--log=stdout"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

def main():
    print("🚀 ngrok 守护进程启动")
    
    # 首次启动
    proc = start_ngrok()
    print("   ngrok 已启动，等待就绪...")
    time.sleep(5)
    
    last_url = None
    
    while True:
        url = get_current_url()
        
        if url:
            if url != last_url:
                print(f"   🌐 公网地址: {url}")
                save_url(url)
                
                # 检查是否是新地址
                saved = read_saved_url()
                if last_url is not None and url != last_url:
                    print(f"   ⚠️ 地址已变更: {last_url} → {url}")
                last_url = url
        
        # 检查进程是否存活
        if proc.poll() is not None:
            print(f"   ❌ ngrok 已退出 (code={proc.returncode})，重新启动...")
            proc = start_ngrok()
            time.sleep(5)
            continue
        
        time.sleep(30)

if __name__ == "__main__":
    main()
