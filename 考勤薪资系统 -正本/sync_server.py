import argparse
import json
import socket
import sqlite3
import os   #保留
from copy import deepcopy
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
os.system("")  # 开启windows控制台ANSI颜色，保留
ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "DB"
DB_PATH = DB_DIR / "attendance.db"
shared_state = {"version": 0, "data": {}}
def ensure_db_directory():
    DB_DIR.mkdir(parents=True, exist_ok=True)
def initialize_db():
    ensure_db_directory()
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                version INTEGER NOT NULL DEFAULT 0,
                data TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                action TEXT NOT NULL,
                path TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sync_history_created_at ON sync_history(created_at DESC);
            """
        )
        conn.commit()
    finally:
        conn.close()
def migrate_legacy_json():
    legacy_file = ROOT / "sync_state.json"
    if not legacy_file.exists():
        return
    try:
        value = json.loads(legacy_file.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            shared_state["version"] = int(value.get("version", 0))
            data = value.get("data")
            if isinstance(data, dict):
                shared_state["data"] = data
            else:
                shared_state["data"] = {}
            save_state()
    except (OSError, ValueError, TypeError):
        print("无法读取旧的 sync_state.json，已忽略迁移。")
def load_state():
    initialize_db()
    conn = sqlite3.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT version, data FROM sync_state WHERE id = 1"
        ).fetchone()
        if row is not None:
            version = int(row[0])
            try:
                data = json.loads(row[1])
            except (TypeError, ValueError):
                data = {}
            if isinstance(data, dict):
                shared_state["version"] = version
                shared_state["data"] = data
                return
    except sqlite3.Error:
        print("无法读取 SQLite 数据库，将从空数据开始。")
    finally:
        conn.close()
    migrate_legacy_json()
def save_state():
    ensure_db_directory()
    conn = sqlite3.connect(DB_PATH)
    try:
        payload = json.dumps(shared_state.get("data", {}), ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO sync_state (id, version, data)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                version = excluded.version,
                data = excluded.data
            """,
            (int(shared_state.get("version", 0)), payload),
        )
        conn.commit()
    except sqlite3.Error as exc:
        print(f"保存数据到 SQLite 时出错: {exc}")
        raise
    finally:
        conn.close()
def collect_state_changes(old_data, new_data, parent_path=""):
    changes = []
    if isinstance(old_data, dict) and isinstance(new_data, dict):
        keys = sorted(set(old_data) | set(new_data))
        for key in keys:
            current_path = f"{parent_path}.{key}" if parent_path else key
            if key not in old_data:
                changes.append({
                    "action": "insert",
                    "path": current_path,
                    "old_value": None,
                    "new_value": new_data[key],
                })
            elif key not in new_data:
                changes.append({
                    "action": "delete",
                    "path": current_path,
                    "old_value": old_data[key],
                    "new_value": None,
                })
            else:
                changes.extend(collect_state_changes(old_data[key], new_data[key], current_path))
        return changes
    if old_data != new_data:
        changes.append({
            "action": "update",
            "path": parent_path,
            "old_value": old_data,
            "new_value": new_data,
        })
    return changes
def normalize_history_value(value):
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value
def save_history(old_data, new_data, pass_emp_id="", pass_emp_name=""):
    if old_data == new_data:
        return
    changes = collect_state_changes(old_data, new_data)
    if not changes:
        return
    conn = sqlite3.connect(DB_PATH)
    try:
        for change in changes:
            old_value = normalize_history_value(change.get("old_value"))
            new_value = normalize_history_value(change.get("new_value"))
            conn.execute(
                """
                INSERT INTO sync_history (action, path, old_value, new_value, detail)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    change["action"],
                    change["path"],
                    old_value,
                    new_value,
                    json.dumps({
                        "path": change["path"],
                        "old_value": change.get("old_value"),
                        "new_value": change.get("new_value"),
                    }, ensure_ascii=False),
                ),
            )
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            action = change["action"]
            path = change["path"]
            old = change["old_value"]
            new = change["new_value"]
            emp_record = find_emp_record(old_data, path)
            action_cn = {
                "insert": "【新增】",
                "update": "【修改】",
                "delete": "【删除】"
            }.get(action, f"【{action}】")
            print(f"[{now_str}] {action_cn} {path}")
            print(f"    ├─旧值: {old}")
            print(f"    └─新值: \033[92m{new}\033[0m")
            # 优先使用前端传过来的员工信息
            if pass_emp_id or pass_emp_name:
                print(f"    👉员工：{pass_emp_id} {pass_emp_name}")
            elif emp_record is not None:
                emp_id = emp_record.get("emp_id", "")
                emp_name = emp_record.get("name", "")
                print(f"    👉员工：{emp_id} {emp_name}")
            else:
                print(f"    👉⚠该考勤路径未携带员工信息")
        conn.commit()
    except sqlite3.Error as exc:
        print(f"保存变更历史时出错: {exc}")
    finally:
        conn.close()
def fetch_history(limit=50):
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, action, path, old_value, new_value, detail
            FROM sync_history
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    result = []
    for row in rows:
        result.append({
            "id": row[0],
            "created_at": row[1],
            "action": row[2],
            "path": row[3],
            "old_value": json.loads(row[4]) if row[4] else None,
            "new_value": json.loads(row[5]) if row[5] else None,
            "detail": json.loads(row[6]) if row[6] else {},
        })
    return result
def get_parent_object_by_path(root_data, path:str):
    """根据变更path，获取它所属的完整父对象（整条考勤记录）"""
    parts = path.split(".")
    if len(parts) <=1:
        return None
    parent_parts = parts[:-1] # 去掉最后一个字段，拿到整条记录的路径
    cur = root_data
    for k in parent_parts:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur
def find_emp_record(root_data, path: str):
    """逐层向上回溯路径，找到包含emp_id/name的员工对象"""
    parts = path.split(".")
    for cut in range(len(parts)-1, 0, -1):
        parent_parts = parts[:cut]
        cur = root_data
        for k in parent_parts:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                cur = None
                break
        if cur and isinstance(cur, dict) and ("emp_id" in cur or "name" in cur):
            return cur
    return None
class SyncRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)
    def send_json(self, value, status=200):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
    def do_GET(self):
        request_path, _, query = self.path.partition("?")
        if request_path == "/":
            self.send_response(302)
            target = "/attendance.html" + ("?" + query if query else "")
            self.send_header("Location", target)
            self.end_headers()
            return
        if request_path == "/api/state":
            self.send_json(shared_state)
            return
        if request_path == "/api/history":
            params = {}
            if query:
                for item in query.split("&"):
                    if "=" in item:
                        key, value = item.split("=", 1)
                        params[key] = value
            limit = int(params.get("limit", "50")) if str(params.get("limit", "50")).isdigit() else 50
            self.send_json({"history": fetch_history(limit)})
            return
        if request_path == "/api/hosts":
            hosts = []
            try:
                for address in socket.gethostbyname_ex(socket.gethostname())[2]:
                    if address.startswith(("10.", "192.168.", "172.")) and address not in hosts:
                        hosts.append(address)
            except socket.error:
                pass
            self.send_json({"hosts": hosts})
            return
        super().do_GET()

    # ========== 这里修正缩进！do_POST 和 do_GET 同级 ==========
    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/state":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length).decode("utf-8"))
            data = request.get("data")
            if not isinstance(data, dict):
                raise ValueError("data must be an object")
            # 读取前端传过来的操作员工信息
            current_emp_id = request.get("current_emp_id", "")
            current_emp_name = request.get("current_emp_name", "")
            previous_data = deepcopy(shared_state.get("data", {}))
            shared_state["version"] += 1
            shared_state["data"] = data
            save_state()
            # 把员工信息传入save_history
            save_history(previous_data, data, current_emp_id, current_emp_name)
            self.send_json({"version": shared_state["version"]})
        except (ValueError, TypeError, json.JSONDecodeError, OSError):
            self.send_json({"error": "invalid sync data"}, 400)

    def log_message(self, format_string, *args):
        print("[%s] %s" % (self.log_date_time_string(), format_string % args))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="企业考勤局域网同步服务")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    args = parser.parse_args()
    load_state()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), SyncRequestHandler)
    print("同步服务已启动: http://0.0.0.0:%d" % args.port)
    print("手机请访问电脑局域网地址，例如: http://电脑局域网IP:%d/attendance.html?mobile=1" % args.port)
    print(f"数据库文件: {DB_PATH}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n同步服务已停止")
    finally:
        server.server_close()
