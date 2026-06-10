#!/usr/bin/env python3
"""
server.py — 静态文件服务 + 锁定标的管理API + 美股隔夜数据 + 自定义分组 + 因子数据
v2: 新增 /api/us-overnight, /api/custom-groups, /api/custom-stocks, /api/evolution-log, /api/factors
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import json, os, mimetypes, urllib.parse, traceback, sqlite3, sys
from datetime import datetime

# 加载scripts目录（market_watcher等模块位置）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts"))

PORT = 8888
BASE = r"C:\Users\LarkMi\AppData\Local\hermes"
LOCKED_FILE = os.path.join(BASE, "data", "custom_stocks.json")
GROUPS_FILE = os.path.join(BASE, "data", "custom_groups.json")
LOOKUP_FILE = os.path.join(BASE, "data", "stock_lookup.json")
STATE_DB = os.path.join(BASE, "data", "market_watcher_state.db")

# 启动时加载股票查找库
_stock_lookup = {}
def load_lookup():
    global _stock_lookup
    if os.path.exists(LOOKUP_FILE):
        with open(LOOKUP_FILE, "r", encoding="utf-8") as f:
            _stock_lookup = json.load(f)

def search_stocks(q):
    """搜索股票，返回 [{code, name}] 最多20条"""
    q = q.strip().upper()
    if not q:
        return []
    results = []
    for code, name in _stock_lookup.items():
        if q in code or q in name:
            results.append({"code": code, "name": name})
        if len(results) >= 20:
            break
    return results

def get_history(code, limit=50):
    """获取某只股票的历史预测（仅已验证的）"""
    if not os.path.exists(STATE_DB):
        return []
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT ts_code, stock_name, direction as pred, pred_time as time, confidence,
               pred_note, actual_close as actual, actual_time, correct, spot_correct
        FROM predictions WHERE ts_code=?
          AND actual_close IS NOT NULL
        ORDER BY pred_time DESC LIMIT ?
    """, (code, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_stock_stats(code):
    """获取单只标的准确率统计"""
    if not os.path.exists(STATE_DB):
        return {"code": code, "total": 0, "correct": 0, "accuracy": None,
                "spot_total": 0, "spot_correct": 0, "spot_accuracy": None}
    conn = sqlite3.connect(STATE_DB)
    c = conn.cursor()
    short_code = code.split('.')[0]
    c.execute("""SELECT total_preds, correct_preds, accuracy,
                        COALESCE(spot_total,0), COALESCE(spot_correct,0),
                        COALESCE(spot_accuracy,0)
                 FROM stock_performance WHERE ts_code=?""", (short_code,))
    r = c.fetchone()
    c.execute("""SELECT COUNT(*) FROM predictions 
                 WHERE ts_code=? AND pred_time >= datetime('now','localtime','-7 days')""",
              (code,))
    recent = c.fetchone()[0]
    conn.close()
    if r:
        return {"code": code, "total": r[0], "correct": r[1], "accuracy": r[2],
                "spot_total": r[3], "spot_correct": r[4], "spot_accuracy": r[5],
                "recent_7d": recent}
    return {"code": code, "total": 0, "correct": 0, "accuracy": None,
            "spot_total": 0, "spot_correct": 0, "spot_accuracy": None, "recent_7d": recent}

def read_locked():
    if not os.path.exists(LOCKED_FILE):
        return {"locked": []}
    with open(LOCKED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def write_locked(data):
    os.makedirs(os.path.dirname(LOCKED_FILE), exist_ok=True)
    with open(LOCKED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# ===== 🆕 自定义分组管理 =====
def read_groups():
    if not os.path.exists(GROUPS_FILE):
        return {"groups": []}
    with open(GROUPS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def write_groups(data):
    os.makedirs(os.path.dirname(GROUPS_FILE), exist_ok=True)
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def upsert_group(name, stocks):
    """创建或更新分组。stocks: [{code, name}]"""
    data = read_groups()
    for g in data["groups"]:
        if g["name"] == name:
            g["stocks"] = stocks
            g["updated_at"] = datetime.now().isoformat()
            write_groups(data)
            return {"ok": True, "group": g}
    # 新建
    new_group = {"name": name, "stocks": stocks, "created_at": datetime.now().isoformat()}
    data["groups"].append(new_group)
    write_groups(data)
    return {"ok": True, "group": new_group}

def delete_group(name):
    data = read_groups()
    before = len(data["groups"])
    data["groups"] = [g for g in data["groups"] if g["name"] != name]
    if len(data["groups"]) == before:
        return {"ok": False, "error": f"分组 '{name}' 不存在"}
    write_groups(data)
    return {"ok": True}

# ===== 🆕 美股隔夜数据 =====
def init_overnight_table():
    """确保 us_overnight_cache 表存在"""
    if not os.path.exists(STATE_DB):
        return
    conn = sqlite3.connect(STATE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS us_overnight_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sp500_change REAL,
            nasdaq_change REAL,
            usdcny REAL,
            vix REAL,
            sp500_close REAL,
            nasdaq_close REAL,
            update_time TEXT,
            source TEXT DEFAULT 'web_search'
        )
    """)
    conn.commit()
    conn.close()

def get_us_overnight():
    """读取最新美股隔夜数据，返回 dict 或 None"""
    if not os.path.exists(STATE_DB):
        return None
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT sp500_change, nasdaq_change, usdcny, vix,
               sp500_close, nasdaq_close, update_time
        FROM us_overnight_cache ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()
    if row:
        d = dict(row)
        # 全部为NULL则返回None
        if all(v is None for k, v in d.items() if k != 'update_time'):
            return None
        return d
    return None

# ===== 🆕 因子快照数据 =====
def get_factor_snapshots():
    """读取最新一批因子快照（用于看板因子拆解面板）"""
    if not os.path.exists(STATE_DB):
        return []
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    # 先检查表是否存在
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='factor_snapshots'"
    ).fetchone()
    if not exists:
        conn.close()
        return []
    # 获取最新一批快照的时间戳
    latest_time = conn.execute(
        "SELECT MAX(snapshot_time) FROM factor_snapshots"
    ).fetchone()[0]
    if not latest_time:
        conn.close()
        return []
    rows = conn.execute("""
        SELECT ts_code, stock_name, tech_score, message_score, sentiment_score, macro_score,
               total_score, pred_direction, confidence, snapshot_time,
               tech_factors, message_factors, sentiment_factors, macro_factors
        FROM factor_snapshots WHERE snapshot_time = ?
        ORDER BY ABS(total_score) DESC
    """, (latest_time,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ===== 🆕 自进化日志 =====
def get_evolution_log(limit=20):
    if not os.path.exists(STATE_DB):
        return []
    conn = sqlite3.connect(STATE_DB)
    conn.row_factory = sqlite3.Row
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='evolution_log'"
    ).fetchone()
    if not exists:
        conn.close()
        return []
    rows = conn.execute(
        "SELECT * FROM evolution_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


class Handler(BaseHTTPRequestHandler):
    def cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def _json(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.cors()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _file(self, path):
        full = os.path.join(BASE, path.lstrip("/"))
        full = os.path.normpath(full)
        if not full.lower().startswith(BASE.lower()):
            self.send_error(403)
            return
        if not os.path.isfile(full):
            self.send_error(404)
            return
        mime, _ = mimetypes.guess_type(full)
        if mime is None:
            mime = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.cors()
        self.end_headers()
        with open(full, "rb") as f:
            self.wfile.write(f.read())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        if path == "/api/locked":
            self._json(200, read_locked())
        elif path == "/api/search":
            q = qs.get("q", [""])[0].strip()
            self._json(200, search_stocks(q))
        elif path == "/api/skill-log":
            from market_watcher import get_evolution_log as mw_evo_log
            limit = int(qs.get("limit", [20])[0])
            self._json(200, mw_evo_log(limit))
        elif path == "/api/debug":
            self._json(200, {"lookup_count": len(_stock_lookup), "locked_file": LOCKED_FILE, "lookup_file": LOOKUP_FILE})
        elif path == "/api/history":
            code = qs.get("code", [""])[0].strip()
            if not code:
                self._json(400, {"error": "code required"})
                return
            self._json(200, get_history(code))
        elif path == "/api/stock-stats":
            code = qs.get("code", [""])[0].strip()
            if not code:
                self._json(400, {"error": "code required"})
                return
            self._json(200, get_stock_stats(code))
        # ===== 🆕 新增 GET 端点 =====
        elif path == "/api/us-overnight":
            data = get_us_overnight()
            self._json(200, {"ok": True, "data": data})
        elif path == "/api/custom-groups":
            self._json(200, read_groups())
        elif path == "/api/evolution-log":
            limit = int(qs.get("limit", [20])[0])
            self._json(200, get_evolution_log(limit))
        elif path == "/api/factors":
            self._json(200, {"ok": True, "snapshots": get_factor_snapshots()})
        elif path == "/" or path == "":
            self._file("/dashboard.html")
        else:
            self._file(path)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        if path == "/api/refresh":
            try:
                import subprocess
                result = subprocess.run(
                    ["python", r"C:\Users\LarkMi\AppData\Local\hermes\scripts\gen_dashboard.py"],
                    capture_output=True, text=True, timeout=60, cwd=r"C:\Users\LarkMi\AppData\Local\hermes\scripts"
                )
                ok = result.returncode == 0
                from market_watcher import get_evolution_log as mw_evo_log
                evo_log = mw_evo_log(5)
                self._json(200, {"ok": ok, "output": result.stdout.strip(),
                                 "error": result.stderr.strip() if not ok else None,
                                 "skill_log": evo_log})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif path == "/api/analyze-custom":
            code = body.get("code", "").strip()
            if not code:
                self._json(400, {"error": "code required"})
                return
            try:
                from market_watcher import analyze_single_stock
                result = analyze_single_stock(code)
                self._json(200, {"ok": True, "analysis": result})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
        elif path == "/api/locked":
            code = body.get("code", "").strip()
            name = body.get("name", "").strip()
            reason = body.get("reason", "").strip()
            if not code:
                self._json(400, {"error": "code required"})
                return
            data = read_locked()
            for s in data["locked"]:
                if s["code"] == code:
                    self._json(409, {"error": f"{code} already locked"})
                    return
            data["locked"].append({"code": code, "name": name, "reason": reason})
            write_locked(data)
            analysis = None
            try:
                from market_watcher import analyze_single_stock
                analysis = analyze_single_stock(code)
            except Exception:
                pass
            self._json(200, {"ok": True, "locked": data["locked"], "analysis": analysis})
        # ===== 🆕 新增 POST 端点 =====
        elif path == "/api/custom-groups":
            name = body.get("name", "").strip()
            stocks = body.get("stocks", [])
            if not name:
                self._json(400, {"error": "group name required"})
                return
            result = upsert_group(name, stocks)
            self._json(200, result)
        elif path == "/api/custom-stocks":
            # 批量添加自定义标的: {stocks: [{code, name, group?}]}
            stocks = body.get("stocks", [])
            if not stocks:
                self._json(400, {"error": "stocks array required"})
                return
            data = read_locked()
            added, skipped = 0, 0
            existing_codes = {s["code"] for s in data["locked"]}
            for s in stocks:
                code = s.get("code", "").strip()
                if not code:
                    continue
                if code in existing_codes:
                    skipped += 1
                    continue
                data["locked"].append({
                    "code": code,
                    "name": s.get("name", code),
                    "reason": s.get("reason", ""),
                    "group": s.get("group", "")
                })
                existing_codes.add(code)
                added += 1
            write_locked(data)
            # 如果指定了分组，同时更新分组
            group_name = body.get("group", "").strip()
            if group_name and stocks:
                groups_data = read_groups()
                group_stocks = []
                for g in groups_data.get("groups", []):
                    if g["name"] == group_name:
                        group_stocks = g.get("stocks", [])
                        break
                for s in stocks:
                    if s.get("code", "").strip():
                        group_stocks.append({"code": s["code"], "name": s.get("name", s["code"])})
                upsert_group(group_name, group_stocks)
            self._json(200, {"ok": True, "added": added, "skipped": skipped, "total": len(data["locked"])})
        else:
            self.send_error(404)

    def do_DELETE(self):
        try:
            self._do_DELETE()
        except Exception as e:
            self.send_error(500, str(e))
            traceback.print_exc()
    
    def _do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/api/locked":
            code = qs.get("code", [""])[0].strip()
            if not code:
                self._json(400, {"error": "code required"})
                return
            data = read_locked()
            before = len(data["locked"])
            data["locked"] = [s for s in data["locked"] if s["code"] != code]
            if len(data["locked"]) == before:
                self._json(404, {"error": f"{code} not found"})
                return
            write_locked(data)
            self._json(200, {"ok": True, "locked": data["locked"]})
        # ===== 🆕 新增 DELETE 端点 =====
        elif parsed.path == "/api/custom-groups":
            name = qs.get("name", [""])[0].strip()
            if not name:
                self._json(400, {"error": "group name required"})
                return
            result = delete_group(name)
            self._json(200 if result["ok"] else 404, result)
        else:
            self.send_error(404)

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    load_lookup()
    init_overnight_table()
    print(f"📡 服务启动: http://localhost:{PORT}")
    print(f"📁 根目录: {BASE}")
    print(f"📊 股票库: {len(_stock_lookup)} 只")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
