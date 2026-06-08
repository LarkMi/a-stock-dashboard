#!/usr/bin/env python3
"""推送最新 dashboard.html 到 GitHub — 供 cron 定时调用"""
import subprocess, sys, os

REPO_DIR = os.path.expanduser("~/projects/a-stock-dashboard")
DASHBOARD_SRC = os.path.expanduser("~/AppData/Local/hermes/dashboard.html")

# 1. 复制最新看板
import shutil
shutil.copy2(DASHBOARD_SRC, os.path.join(REPO_DIR, "dashboard.html"))
print(f"Copied dashboard.html")

# 2. Git commit & push
os.chdir(REPO_DIR)
subprocess.run(["git", "add", "dashboard.html"], check=True)
result = subprocess.run(["git", "diff", "--staged", "--quiet"])
if result.returncode == 0:
    print("No changes to push")
    sys.exit(0)

subprocess.run(["git", "commit", "-m", f"Auto-update dashboard {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}"], check=True)
subprocess.run(["git", "push"], check=True)
print("Pushed to GitHub ✅")
