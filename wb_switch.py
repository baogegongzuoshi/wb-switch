# -*- coding: utf-8 -*-
"""
WB Switch v2.18 — WorkBuddy 供应商管理器 + 用量监控（跨平台：Windows / macOS）
样式仿 cc-switch：侧边栏 + 供应商卡片 + 启用按钮 + 用量多维筛选。

功能:
  1. 供应商预设管理: 多套配置保存/切换；「所有模型」开关可把全部第三方模型
     同时注入 WorkBuddy 模型列表（同名模型 ID 会互相覆盖，WorkBuddy 限制）
  2. 启动 / 重启 WorkBuddy 一键按钮，实时显示 WorkBuddy 运行状态
  3. 守护模式: 自动备份 models.json；丢失/损坏自动恢复；启动自动接管端口
  4. 用量监控: 时间 / 类别 / 供应商 / 模型筛选；缓存命中率趋势；
     供应商命中率汇总；费用按官方价计（国内模型 ¥ / 国外模型 $，价格表内置
     model_pricing.json，共 201 个模型）
  5. 内置使用说明弹窗（右上角「? 使用说明」）；功能更新时同步更新该说明
  6. 面板: http://127.0.0.1:5276
     启动: Windows 双击 WB Switch.bat；macOS 运行 ./wb-switch.command
零依赖，仅用 Python 标准库（Python 3.8+）。
"""

import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# ---------------------------------------------------------------- 路径配置
HOME = os.path.expanduser("~")
WB_DIR = os.path.join(HOME, ".workbuddy")
MODELS_JSON = os.path.join(WB_DIR, "models.json")
WB_DB = os.path.join(WB_DIR, "workbuddy.db")
DATA_DIR = os.path.join(WB_DIR, "wb-switch")
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
EVENT_LOG = os.path.join(DATA_DIR, "guardian.log")
STATE_FILE = os.path.join(DATA_DIR, "state.json")

# WorkBuddy 可执行文件：按平台探测常见安装位置
if IS_WIN:
    _WB_CANDIDATES = [
        r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
        r"C:\Program Files (x86)\WorkBuddy\WorkBuddy.exe",
        os.path.join(HOME, r"AppData\Local\Programs\WorkBuddy\WorkBuddy.exe"),
    ]
    _WB_PROC_NAME = "WorkBuddy.exe"
elif IS_MAC:
    _WB_CANDIDATES = [
        "/Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy",
        os.path.join(HOME, "Applications/WorkBuddy.app/Contents/MacOS/WorkBuddy"),
    ]
    _WB_PROC_NAME = "WorkBuddy"
else:  # Linux 兜底
    _WB_CANDIDATES = ["/opt/WorkBuddy/workbuddy", "/usr/local/bin/workbuddy"]
    _WB_PROC_NAME = "workbuddy"
WB_EXE = next((c for c in _WB_CANDIDATES if os.path.exists(c)), _WB_CANDIDATES[0])

MAX_BACKUPS = 60
POLL_INTERVAL = 2.0
USAGE_CACHE_TTL = 20

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)

_state_lock = threading.Lock()
_state = {
    "last_good": None, "last_good_time": None,
    "last_content": None, "stable_count": 0,
    "events": [],
}


def log_event(kind, msg):
    entry = {"ts": datetime.now().strftime("%m-%d %H:%M:%S"), "kind": kind, "msg": msg}
    with _state_lock:
        _state["events"].insert(0, entry)
        del _state["events"][200:]
    try:
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] [%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, msg))
    except OSError:
        pass


# ---------------------------------------------------------------- 守护: 备份与恢复
def _read_models_raw():
    try:
        with open(MODELS_JSON, "r", encoding="utf-8") as f:
            return f.read()
    except (FileNotFoundError, OSError):
        return None


def _is_valid_models(text):
    if text is None:
        return False
    try:
        return isinstance(json.loads(text), list)
    except (ValueError, TypeError):
        return False


def _write_models(text):
    tmp = MODELS_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    shutil.move(tmp, MODELS_JSON)


def _save_snapshot(text, source):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = "models_%s.json" % ts
    try:
        with open(os.path.join(BACKUP_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
        with _state_lock:
            _state["last_good"] = text
            _state["last_good_time"] = ts
        log_event("backup", "快照已保存 (%s): %s" % (source, name))
    except OSError as e:
        log_event("error", "快照保存失败: %r" % e)
    try:
        snaps = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("models_") and f.endswith(".json"))
        for old in snaps[:-MAX_BACKUPS]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except OSError:
        pass


def _latest_snapshot():
    try:
        snaps = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith("models_") and f.endswith(".json"))
    except OSError:
        return None, None
    if not snaps:
        return None, None
    name = snaps[-1]
    try:
        with open(os.path.join(BACKUP_DIR, name), "r", encoding="utf-8") as f:
            return f.read(), name
    except OSError:
        return None, None


def guardian_loop():
    log_event("info", "守护启动，轮询间隔 %.0fs" % POLL_INTERVAL)
    last_backup_ts = 0.0
    while True:
        try:
            raw = _read_models_raw()
            if raw is None:
                snap, name = _latest_snapshot()
                if snap and _is_valid_models(snap):
                    _write_models(snap)
                    log_event("restore", "models.json 丢失，已从快照自动恢复: %s" % name)
                else:
                    log_event("error", "models.json 丢失且没有可用快照！")
            elif not _is_valid_models(raw):
                snap, name = _latest_snapshot()
                if snap and _is_valid_models(snap):
                    _write_models(snap)
                    log_event("restore", "models.json 损坏，已从快照自动恢复: %s" % name)
                else:
                    log_event("error", "models.json 损坏且没有可用快照！")
            else:
                if raw != _state.get("last_content"):
                    with _state_lock:
                        _state["stable_count"] = 0
                        _state["last_content"] = raw
                else:
                    with _state_lock:
                        _state["stable_count"] += 1
                now = time.time()
                with _state_lock:
                    stable = _state["stable_count"]
                    last_good = _state["last_good"]
                if stable >= 2 and raw != last_good and (now - last_backup_ts) > 4:
                    _save_snapshot(raw, "内容变化")
                    last_backup_ts = now
        except Exception as e:
            log_event("error", "守护循环异常: %r" % e)
        time.sleep(POLL_INTERVAL)


def guardian_init():
    raw = _read_models_raw()
    if _is_valid_models(raw):
        _save_snapshot(raw, "启动基线")
        with _state_lock:
            _state["last_content"] = raw
            _state["stable_count"] = 2
    else:
        log_event("error", "启动时 models.json 缺失或损坏，等待恢复机制处理")


# ---------------------------------------------------------------- 供应商预设
def load_presets():
    try:
        with open(PRESETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_presets(presets):
    tmp = PRESETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)
    shutil.move(tmp, PRESETS_FILE)


# ---------------------------------------------------------------- 官方内置模型目录
# 官方模型清单由 WorkBuddy 云端配置下发，应用会自动刷新本地缓存
# （~/.workbuddy/cache/acc-product-config-v3*.json）。这里直接读最新缓存，
# 保证清单永远跟随官方（自己加载，不写死）。
OFFICIAL_FILE = os.path.join(DATA_DIR, "official_selected.json")


def load_official_catalog():
    """读取最新云端配置缓存里的官方模型目录。返回 (models|None, 来源文件或错误)"""
    import glob
    cands = [os.path.join(WB_DIR, "cache", "acc-product-config-v3.json")]
    cands += glob.glob(os.path.join(WB_DIR, "cache", "conversation-product-spill",
                                    "acc-product-config-v3-*.json"))
    cands = [c for c in cands if os.path.isfile(c)]
    if not cands:
        return None, "未找到云端配置缓存（先打开一次 WorkBuddy）"
    cands.sort(key=os.path.getmtime, reverse=True)
    for p in cands:
        try:
            with open(p, "r", encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        raw = j.get("models") if isinstance(j, dict) else None
        if isinstance(raw, list) and raw:
            models = []
            for m in raw:
                if not isinstance(m, dict) or not m.get("id"):
                    continue
                models.append({
                    "id": m["id"],
                    "name": m.get("name") or m["id"],
                    "credits": m.get("credits"),
                    "ctx": m.get("maxInputTokens"),
                    "vision": bool(m.get("supportsImages")),
                    "tools": bool(m.get("supportsToolCall")),
                    "reasoning": bool(m.get("supportsReasoning")),
                    "default": bool(m.get("isDefault")),
                })
            return models, p
    return None, "云端缓存里没有模型目录"


def load_official_ids():
    try:
        with open(OFFICIAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("ids", [])
    except (OSError, ValueError):
        return []


def save_official_ids(ids):
    tmp = OFFICIAL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"ids": ids, "ts": time.time()}, f, ensure_ascii=False)
    shutil.move(tmp, OFFICIAL_FILE)


def current_models():
    raw = _read_models_raw()
    if raw and _is_valid_models(raw):
        try:
            return json.loads(raw)
        except ValueError:
            return []
    return []


def _load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    shutil.move(tmp, STATE_FILE)


CLI_SETTINGS_FILE = os.path.join(os.path.expanduser("~"), ".workbuddy", "settings.json")


def set_cli_default_model(model_id):
    """把默认模型写入 CLI settings.json 的 model 字段（官方机制，最权威的一层）。

    反编译 + 日志实测结论（v2.8 定稿）：
    - CLI 初始化快照: currentModelId = process.env.CODEBUDDY_MODEL || settings.model || 第一个可用模型
    - 桌面主进程把 CODEBUDDY_CONFIG_DIR 指向 ~/.workbuddy，所以 CLI settings 就是 ~/.workbuddy/settings.json
    - CLI 的模型列表里自定义模型 id 带 custom-local: 前缀（AgentModelResolver 日志实测），
      裸 id 匹配不上会回落 fast-model（"快速"）——2026-09-18 16:4x 切小忆实测踩坑。
    - 运行时 id 全链统一带前缀：settings.json / 会话 JSONL providerData 都是 custom-local:xxx；
      只有 workbuddy.db sessions.model 列存裸 id（原生惯例）。方舟之前"碰巧能用"是因为
      glm-5.3-flash 与官方内置模型同 id，裸 id 撞上了官方条目。
    官方模式（model_id=None）移除该键回落官方默认。
    """
    try:
        with open(CLI_SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        changed = False
        if model_id:
            runtime_id = model_id if model_id.startswith("custom-local:") else "custom-local:" + model_id
            if data.get("model") != runtime_id:
                data["model"] = runtime_id
                changed = True
        elif "model" in data:
            data.pop("model")
            changed = True
        if changed:
            tmp = CLI_SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            shutil.move(tmp, CLI_SETTINGS_FILE)
        return changed
    except (OSError, ValueError):
        return False


def set_default_model(model_id):
    """把模型写入 WorkBuddy 偏好 defaultModelId（新会话默认模型，双保险层）。

    真正的主力机制是 set_cli_default_model（settings.json model 字段）和
    models.json 的 isDefault 标记；这里照写不误，老版本可能还会读。
    官方模式下（model_id=None）移除该键，回落官方默认。
    """
    import glob
    pattern = os.path.join(os.path.expanduser("~"), ".workbuddy",
                           "storage", "user-*-personal", "scoped", "*", "preferences.json")
    changed = 0
    for p in glob.glob(pattern):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                continue
            if model_id:
                # 运行时模型列表里自定义模型 id 带 custom-local: 前缀（AgentModelResolver 实测），
                # defaultModelId 也是精确匹配，写裸 id 匹配不上会回落官方 isDefault 模型
                rid = model_id if model_id.startswith("custom-local:") else "custom-local:" + model_id
                if data.get("defaultModelId") == rid:
                    continue
                data["defaultModelId"] = rid
            else:
                if "defaultModelId" not in data:
                    continue
                data.pop("defaultModelId")
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            shutil.move(tmp, p)
            changed += 1
        except (OSError, ValueError):
            pass
    return changed


def _mark_default(models):
    """给第一个自定义模型打 isDefault: true（其余清除）。

    UI 渲染层 resolveModel 优先级③：userChoice > remoteCurrentModelId/settings.model > isDefault > 列表第一个。
    自定义模型的 isDefault 字段会通过 toModelInfoFromProduct 透传进运行时模型列表。"""
    first = True
    for m in models:
        if not isinstance(m, dict):
            continue
        if first:
            m["isDefault"] = True
            first = False
        else:
            m.pop("isDefault", None)


def infer_active_preset_index(presets):
    """从当前 models.json 推断正在使用的预设，供旧 state 首次升级时使用。"""
    cur = current_models()
    if not cur or not isinstance(cur[0], dict):
        return 0
    cm = cur[0]
    for i, p in enumerate(presets):
        for m in (p.get("models", []) if isinstance(p, dict) else []):
            if not isinstance(m, dict):
                continue
            if (m.get("id") == cm.get("id") and m.get("url") == cm.get("url") and
                    m.get("apiKey") == cm.get("apiKey")):
                return i
    return 0


def all_preset_models(presets, active_index=None):
    """全模型模式：把所有第三方预设的模型全部注入，不做去重、不改名。

    顺序：当前供应商排最前（其第一个模型作为默认），其余按预设顺序追加。
    """
    models = []
    if isinstance(active_index, int) and 0 <= active_index < len(presets):
        p = presets[active_index]
        if isinstance(p, dict):
            models += [dict(m) for m in (p.get("models") or []) if isinstance(m, dict)]
    for i, p in enumerate(presets):
        if i == active_index or not isinstance(p, dict):
            continue
        models += [dict(m) for m in (p.get("models") or []) if isinstance(m, dict)]
    return models


def apply_preset(preset_models, preset_name="", mode="single"):
    raw = _read_models_raw()
    if raw and _is_valid_models(raw) and raw.strip():
        _save_snapshot(raw, "切换前备份")
    _mark_default(preset_models)
    text = json.dumps(preset_models, ensure_ascii=False, indent=2)
    _write_models(text)
    first_id = None
    if preset_models and isinstance(preset_models[0], dict):
        first_id = preset_models[0].get("id")
    # 三层默认模型机制：CLI settings.json model（主力）+ isDefault 标记 + defaultModelId 偏好（双保险）
    set_cli_default_model(first_id)
    n = set_default_model(first_id)
    st = _load_state()
    st["last_switch_ts"] = int(time.time() * 1000)
    st["last_switch_name"] = preset_name
    st["last_default_model"] = first_id
    st["model_mode"] = mode
    _save_state(st)
    if first_id:
        log_event("info", "已切换供应商 -> %s（CLI settings.model=%s，isDefault 已标记，%d 个偏好文件）" % (preset_name, first_id, n))
    else:
        log_event("info", "已切换供应商 -> %s（已移除默认模型设置，%d 个偏好文件）" % (preset_name, n))


# ---------------------------------------------------------------- WorkBuddy 进程控制
_wb_status_cache = {"ts": 0.0, "running": False}


def wb_running():
    """检测 WorkBuddy 是否运行。Windows 用进程快照 API；macOS/Linux 用 pgrep。"""
    now = time.time()
    if now - _wb_status_cache["ts"] < 4:
        return _wb_status_cache["running"]
    running = False
    try:
        if IS_WIN:
            import ctypes
            TH32CS_SNAPPROCESS = 0x2
            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                            ("th32ProcessID", ctypes.c_ulong),
                            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                            ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                            ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_wchar * 260)]
            k32 = ctypes.windll.kernel32
            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap != -1:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                ok = k32.Process32FirstW(snap, ctypes.byref(entry))
                while ok:
                    if entry.szExeFile == _WB_PROC_NAME:
                        running = True
                        break
                    ok = k32.Process32NextW(snap, ctypes.byref(entry))
                k32.CloseHandle(snap)
        else:
            r = subprocess.run(["pgrep", "-x", _WB_PROC_NAME], capture_output=True)
            running = r.returncode == 0
    except Exception as e:
        log_event("error", "进程检测失败: %r" % e)
    _wb_status_cache["ts"] = now
    _wb_status_cache["running"] = running
    return running


def _kill_wb():
    """强制结束 WorkBuddy 进程（跨平台）。"""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/IM", _WB_PROC_NAME], capture_output=True, timeout=10)
        else:
            subprocess.run(["pkill", "-x", _WB_PROC_NAME], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def launch_wb():
    if not os.path.exists(WB_EXE):
        return False, "未找到 WorkBuddy: " + WB_EXE
    kwargs = {}
    if IS_WIN:
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([WB_EXE], cwd=os.path.dirname(WB_EXE), **kwargs)
    log_event("info", "已启动 WorkBuddy")
    return True, "已启动"


def restart_wb():
    """通过分离的助手进程执行重启（WorkBuddy 是本面板的宿主，杀死后面板可能一起退出，
    所以重启序列必须跑在独立进程里）"""
    helper = os.path.join(DATA_DIR, "restart_helper.py")
    # 关键：默认模型 id 直接从当前 models.json 计算，不依赖 state 缓存。
    # （旧版面板写的 state 可能缺 last_default_model，会导致重启后不补写默认模型 → 切换"无效"）
    models_text = ""
    mid = None
    try:
        models_text = _read_models_raw() or ""
        cur = json.loads(models_text) if models_text.strip() else []
        if isinstance(cur, list) and cur and isinstance(cur[0], dict):
            mid = cur[0].get("id")
    except ValueError:
        pass
    # 杀掉 WorkBuddy 后、拉起前：重写 models.json（防 WB 退出时回写旧配置，
    # 并确保第一个模型带 isDefault 标记）+ 补写 defaultModelId 偏好（双保险）
    rewrite = (
        "import json, glob, os\n"
        "raw = %r\n"
        "mid = %r\n"
        "mp = os.path.join(os.path.expanduser('~'), '.workbuddy', 'models.json')\n"
        "try:\n"
        "    if raw:\n"
        "        arr = json.loads(raw)\n"
        "        if isinstance(arr, list) and arr and isinstance(arr[0], dict):\n"
        "            arr[0]['isDefault'] = True\n"
        "            for m in arr[1:]:\n"
        "                if isinstance(m, dict):\n"
        "                    m.pop('isDefault', None)\n"
        "            raw = json.dumps(arr, ensure_ascii=False, indent=2)\n"
        "        open(mp + '.tmp', 'w', encoding='utf-8').write(raw)\n"
        "        os.replace(mp + '.tmp', mp)\n"
        "except Exception:\n"
        "    pass\n"
        "for p in glob.glob(os.path.join(os.path.expanduser('~'), '.workbuddy',\n"
        "    'storage', 'user-*-personal', 'scoped', '*', 'preferences.json')):\n"
        "    try:\n"
        "        d = json.load(open(p, encoding='utf-8'))\n"
        "        if not isinstance(d, dict):\n"
        "            continue\n"
        "        if mid is not None:\n"
        "            d['defaultModelId'] = 'custom-local:' + mid if not mid.startswith('custom-local:') else mid\n"
        "        else:\n"
        "            d.pop('defaultModelId', None)\n"
        "        json.dump(d, open(p + '.tmp', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)\n"
        "        os.replace(p + '.tmp', p)\n"
        "    except Exception:\n"
        "        pass\n"
        "sp = os.path.join(os.path.expanduser('~'), '.workbuddy', 'settings.json')\n"
        "try:\n"
        "    d = json.load(open(sp, encoding='utf-8'))\n"
        "    if isinstance(d, dict):\n"
        "        if mid is not None:\n"
        # CLI 模型列表里自定义模型 id 带 custom-local: 前缀，裸 id 匹配不上会回落 fast-model（快速）
        "            d['model'] = 'custom-local:' + mid if not mid.startswith('custom-local:') else mid\n"
        "        else:\n"
        "            d.pop('model', None)\n"
        "        json.dump(d, open(sp + '.tmp', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)\n"
        "        os.replace(sp + '.tmp', sp)\n"
        "except Exception:\n"
        "    pass\n"
        # 关键：WB 启动时自动恢复最近会话，把 DB sessions.model 作为 optModel 传给 CLI，
        # CLI 原样返回作为 currentModelId，桌面 resolveModel 再拿它和模型列表精确匹配。
        # 列表里自定义模型的 id 是 custom-local:xxx（AgentModelResolver 日志实测），裸 id 匹配不上
        # 就会落到优先级③，而官方云端把 fast-model（快速）标了 isDefault:true —— 这就是"变快速"的根因。
        # 所以 DB 也必须写带前缀的运行时 id（rid），对齐最近 3 个会话保证恢复哪个都命中。
        "if mid is not None:\n"
        "    rid = mid if mid.startswith('custom-local:') else 'custom-local:' + mid\n"
        "    import sqlite3\n"
        "    dbp = os.path.join(os.path.expanduser('~'), '.workbuddy', 'workbuddy.db')\n"
        "    sids = []\n"
        "    try:\n"
        "        con = sqlite3.connect(dbp)\n"
        "        c = con.cursor()\n"
        "        c.execute('SELECT id FROM sessions ORDER BY created_at DESC LIMIT 3')\n"
        "        sids = [r[0] for r in c.fetchall()]\n"
        "        for s in sids:\n"
        "            c.execute('UPDATE sessions SET model=? WHERE id=?', (rid, s))\n"
        "        con.commit()\n"
        "        con.close()\n"
        "    except Exception:\n"
        "        pass\n"
        "    for sid in sids:\n"
        "        import glob as _g\n"
        "        for jp in _g.glob(os.path.join(os.path.expanduser('~'), '.workbuddy',\n"
        "            'projects', '*', sid + '.jsonl')):\n"
        "            try:\n"
        "                lines = open(jp, encoding='utf-8').read().splitlines()\n"
        "                out = []\n"
        "                ch = False\n"
        "                for l in lines:\n"
        "                    try:\n"
        "                        o = json.loads(l)\n"
        "                    except ValueError:\n"
        "                        out.append(l)\n"
        "                        continue\n"
        "                    pd = o.get('providerData') if isinstance(o, dict) else None\n"
        "                    if isinstance(pd, dict):\n"
        "                        for k in ('model', 'requestModelId'):\n"
        "                            if isinstance(pd.get(k), str) and pd[k] != rid:\n"
        "                                pd[k] = rid\n"
        "                                ch = True\n"
        "                    out.append(json.dumps(o, ensure_ascii=False))\n"
        "                if ch:\n"
        "                    open(jp, 'w', encoding='utf-8').write('\\n'.join(out) + '\\n')\n"
        "            except Exception:\n"
        "                pass\n" % (models_text, mid)
    )
    helper_src = (
        "import subprocess, time, os\n"
        "import platform\n"
        "_win = platform.system() == 'Windows'\n"
        "subprocess.run(['taskkill', '/F', '/IM', %r] if _win else ['pkill', '-x', %r], capture_output=True)\n" % (_WB_PROC_NAME, _WB_PROC_NAME) +
        "time.sleep(3)\n"
        + rewrite +
        "_kw = {'creationflags': 0x00000008} if _win else {'start_new_session': True}\n"
        "subprocess.Popen([%r], cwd=%r, **_kw)\n" % (WB_EXE, os.path.dirname(WB_EXE))
    )
    try:
        with open(helper, "w", encoding="utf-8") as f:
            f.write(helper_src)
        flags = 0
        if IS_WIN:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([sys.executable, helper], creationflags=flags, close_fds=True,
                         **({} if IS_WIN else {"start_new_session": True}))
        log_event("info", "已下发重启指令（分离进程执行）")
        return True, "重启中"
    except OSError as e:
        # 兜底：就地重启
        _kill_wb()
        time.sleep(2)
        ok, msg = launch_wb()
        return ok, msg


# ---------------------------------------------------------------- 请求日志 / 缓存命中率
# 数据源：~/.workbuddy/projects/*/<sessionId>.jsonl，每条 AI 回复行带 providerData.usage：
#   inputTokens / outputTokens / inputTokensDetails[].cached_tokens（cached 是 input 的子集）
# 命中率 = cached_tokens / inputTokens
_req_cache = {"files": {}, "lock": threading.Lock()}
_projects_dir = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects")


def scan_requests():
    """扫描全部会话 jsonl，返回按时间排序的请求记录（按文件 mtime 增量缓存）"""
    records = []
    with _req_cache["lock"]:
        for dirpath, _dirs, filenames in os.walk(_projects_dir):
            for fn in filenames:
                if not fn.endswith(".jsonl"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                entry = _req_cache["files"].get(p)
                if entry and entry[0] == mtime:
                    records.extend(entry[1])
                    continue
                recs = []
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if "providerData" not in line or '"usage"' not in line:
                                continue
                            try:
                                obj = json.loads(line)
                            except ValueError:
                                continue
                            pd = obj.get("providerData")
                            if not isinstance(pd, dict):
                                continue
                            u = pd.get("usage")
                            if not isinstance(u, dict):
                                continue
                            inp = u.get("inputTokens") or 0
                            out = u.get("outputTokens") or 0
                            if not (inp or out):
                                continue
                            cached = 0
                            for d in (u.get("inputTokensDetails") or []):
                                if isinstance(d, dict):
                                    cached += d.get("cached_tokens") or 0
                            recs.append({
                                "ts": obj.get("timestamp") or 0,
                                "session": str(obj.get("sessionId") or fn[:-6])[:8],
                                "model": pd.get("requestModelId") or pd.get("model") or "未知",
                                "inp": inp, "out": out, "cached": cached,
                            })
                except OSError:
                    pass
                _req_cache["files"][p] = (mtime, recs)
                records.extend(recs)
    records.sort(key=lambda r: r["ts"])
    return records


def get_requests(days=30, frm=None, to=None):
    recs = scan_requests()
    now_ms = int(time.time() * 1000)
    lo = frm if frm is not None else now_ms - days * 86400000
    hi = to if to is not None else now_ms + 86400000
    pmap = _provider_map()
    pricing = _load_pricing()
    win = [r for r in recs if lo <= r["ts"] <= hi]
    out = []
    for r in win:
        r2 = dict(r)
        r2["provider"] = pmap.get(r["model"], "官方内置")
        r2["cls"] = "third" if r["model"] in pmap else "official"
        r2["hit"] = round(r["cached"] * 100.0 / r["inp"], 1) if r["inp"] else 0
        r2["cost"] = round(_est_cost(r["model"], r["inp"], r["out"], r["cached"], pricing), 5) if r2["cls"] == "third" else 0.0
        r2["currency"] = _currency_for(r["model"])
        out.append(r2)
    # 按模型聚合统计
    agg = {}
    for r in out:
        a = agg.setdefault(r["model"], {"model": r["model"], "provider": r["provider"],
                                        "reqs": 0, "inp": 0, "cached": 0, "out": 0,
                                        "cost": 0.0, "currency": r["currency"]})
        a["reqs"] += 1
        a["inp"] += r["inp"]
        a["cached"] += r["cached"]
        a["out"] += r["out"]
        a["cost"] += r.get("cost", 0.0)
    stats = []
    for a in agg.values():
        a["hit"] = round(a["cached"] * 100.0 / a["inp"], 1) if a["inp"] else 0
        a["cost"] = round(a["cost"], 4)
        stats.append(a)
    stats.sort(key=lambda a: -(a["inp"] + a["out"]))
    return {"requests": out, "stats": stats}


# ---------------------------------------------------------------- 用量统计
# 供应商识别：自定义模型按 API Key 前缀 + 接口域名判断真实厂商，
# 识别不出时用预设名/模型名兜底；官方模型不在映射里，显示"官方内置"。
_ARK_KEY_RE = re.compile(r"^ark-[0-9a-f]{8}", re.IGNORECASE)
_VENDOR_BY_DOMAIN = [
    ("volces.com", "火山方舟"), ("chr1.com", "小忆"), ("uu6.top", "转转AI"),
    ("x5m5x.com", "SwiftAPI"), ("openrouter.ai", "OpenRouter"),
    ("deepseek.com", "DeepSeek"), ("moonshot", "月之暗面 Kimi"),
    ("bigmodel.cn", "智谱"), ("zhipuai", "智谱"), ("dashscope", "阿里云百炼"),
    ("aliyuncs", "阿里云百炼"), ("siliconflow", "硅基流动"), ("api2d", "API2D"),
    ("baichuan-ai", "百川"), ("minimax", "MiniMax"), ("laozhang", "老张API"),
]


def _vendor_label(model):
    """对单个自定义模型配置推断供应商标签。"""
    key = (model.get("apiKey") or "").strip()
    if _ARK_KEY_RE.match(key):
        return "火山方舟"
    url = (model.get("url") or "").lower()
    for dom, name in _VENDOR_BY_DOMAIN:
        if dom in url:
            return name
    if url.startswith(("http://", "https://")):
        try:
            host = re.split(r"[/:]", url.split("//", 1)[1])[0]
            parts = host.split(".")
            if len(parts) >= 2:
                return parts[-2] + "." + parts[-1]  # 未知域名显示主域名
        except Exception:
            pass
    return model.get("name") or model.get("id") or "自定义"


def _provider_map():
    """model_id -> 供应商标签。当前 models.json 优先（正在生效的配置），
    其次各预设（历史用量按预设识别）。不在映射里的视为官方内置模型。
    请求日志里模型 id 可能带 custom-local: 前缀（运行时防冲突加的），一并映射。"""
    pmap = {}

    def put(mid, label):
        if mid and mid not in pmap and label:
            pmap[mid] = label
            pmap["custom-local:" + mid] = label

    for m in current_models():
        put(m.get("id"), _vendor_label(m))
    for p in load_presets():
        for m in p.get("models", []):
            put(m.get("id"), _vendor_label(m) or p.get("name") or m.get("name") or m.get("id"))
    return pmap


_usage_cache = {"ts": 0.0, "key": None, "data": None}
_usage_lock = threading.Lock()

CCS_PRICING_FILE = os.path.join(DATA_DIR, "model_pricing.json")
_CC_PRICING = {"ts": 0.0, "data": None}

# 币种判定：国内厂商模型 → ¥，国外模型 → $（计价单位与官方定价一致）
_CNY_FAMILIES = ("glm", "doubao", "qwen", "kimi", "minimax", "hunyuan",
                 "deepseek", "baichuan", "step", "ernie", "spark", "yi-")
_USD_FAMILIES = ("gpt", "o1", "o3", "o4", "claude", "gemini", "grok",
                 "codex", "command", "mistral", "codestral", "devstral", "llama")


def _bare_model_id(model_id):
    """去掉运行时前缀，拿裸模型 id（价格匹配 / 币种判定用）。"""
    mid = (model_id or "").lower()
    return mid[len("custom-local:"):] if mid.startswith("custom-local:") else mid


def _currency_for(model_id):
    mid = _bare_model_id(model_id)
    if mid.startswith(_CNY_FAMILIES) or any("/" + f in mid for f in _CNY_FAMILIES):
        return "CNY"
    if mid.startswith(_USD_FAMILIES):
        return "USD"
    return "CNY"  # 默认按国内定价


def _load_pricing():
    """读模型单价表（每百万 token，官方价）。本地 wb-switch/model_pricing.json 优先，
    缺失时回落 cc-switch 数据库；都没有则返回空表（费用显示 0）。"""
    now = time.time()
    if _CC_PRICING["data"] is not None and now - _CC_PRICING["ts"] < 3600:
        return _CC_PRICING["data"]
    data = {}
    try:
        with open(CCS_PRICING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except (OSError, ValueError):
        db = os.path.join(HOME, ".cc-switch", "cc-switch.db")
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
            try:
                for mid, ip, op, cp in con.execute(
                        "SELECT model_id, input_cost_per_million, output_cost_per_million, "
                        "cache_read_cost_per_million FROM model_pricing"):
                    if mid:
                        data[mid.lower()] = {"name": mid, "in": float(ip or 0),
                                             "out": float(op or 0), "cache": float(cp or 0)}
            finally:
                con.close()
        except sqlite3.Error:
            pass
    _CC_PRICING.update(ts=now, data=data)
    return data


def _price_for(model_id, pricing):
    """精确匹配，再按最长前缀回落（如 gpt-5.6-sol 命中 gpt-5.6）。"""
    key = _bare_model_id(model_id)
    p = pricing.get(key)
    if p:
        return p
    best = None
    for pid in pricing:
        if key.startswith(pid) and (best is None or len(pid) > len(best)):
            best = pid
    return pricing.get(best)


def _est_cost(model_id, inp, out, cached, pricing):
    """按官方价分别计输入/输出/缓存费用。无价格返回 0。"""
    p = _price_for(model_id, pricing)
    if not p:
        return 0.0
    cached = min(cached or 0, inp or 0)
    plain_inp = max((inp or 0) - cached, 0)
    return (plain_inp / 1e6 * p.get("in", 0)
            + (out or 0) / 1e6 * p.get("out", 0)
            + cached / 1e6 * p.get("cache", 0))


def get_usage(days=30, frm=None, to=None):
    key = "%s_%s_%s" % (days, frm, to)
    with _usage_lock:
        if _usage_cache["data"] and _usage_cache["key"] == key and (time.time() - _usage_cache["ts"]) < USAGE_CACHE_TTL:
            return _usage_cache["data"]
    if frm:
        since = int(frm)
    else:
        since = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    until = int(to) if to else int(time.time() * 1000)
    con = sqlite3.connect("file:%s?mode=ro" % WB_DB.replace("\\", "/"), uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT s.id, s.title, s.model, s.created_at, u.used, u.size, u.credit_json "
            "FROM session_usage u LEFT JOIN sessions s ON s.id = u.session_id "
            "WHERE COALESCE(s.created_at, 0) >= ? AND COALESCE(s.created_at, 0) <= ? "
            "ORDER BY s.created_at DESC", (since, until))
        rows = cur.fetchall()
    finally:
        con.close()
    pmap = _provider_map()
    third_ids = set()
    for p in load_presets():
        for m in p.get("models", []):
            if m.get("id"):
                third_ids.add(m["id"])
    pricing = _load_pricing()
    sessions = []
    for sid, title, model, created_at, used, size, credit_json in rows:
        used = used or 0
        model_id = model or ""
        provider = pmap.get(model_id, "官方内置")
        cls = "third" if model_id in third_ids else "official"
        cost = _est_cost(model_id, used, 0, 0, pricing) if cls == "third" else 0.0
        sessions.append({
            "id": (sid or "")[:8],
            "title": (title or "未命名会话")[:40],
            "model": model_id or "默认模型",
            "provider": provider,
            "cls": cls,
            "used": used,
            "size": size or 0,
            "pct": round(used * 100.0 / size, 1) if size else 0,
            "ts": int(created_at or 0),
            "credit": (sum(float(v) for v in json.loads(credit_json).values())
                       if credit_json else 0.0),
            "cost": round(cost, 4),
            "currency": _currency_for(model_id),
        })
    data = {"sessions": sessions, "provider_map": pmap}
    with _usage_lock:
        _usage_cache.update(ts=time.time(), key=key, data=data)
    return data


# ---------------------------------------------------------------- 拉取供应商模型列表
# 火山方舟 Agent Plan：plan 端点没有动态 /models 接口（实测 404，plan Key 也不被
# /api/v3/models 接受），模型清单由官方文档页发布（docs: 82379/2666474）。
# 这里在运行时实时抓取该页面并解析（官方支持的模型会变，不写死清单），
# 解析结果缓存 6 小时；抓取失败自动回落到最近一次缓存。
ARK_DOCS_URL = "https://www.volcengine.com/docs/82379/2666474"
ARK_CACHE_FILE = os.path.join(DATA_DIR, "ark_models_cache.json")
ARK_CACHE_TTL = 6 * 3600.0
_ark_state = {"ts": 0.0, "models": None, "lock": threading.Lock()}


def _extract_ark_models_from_html(html):
    """从文档页内嵌的双重转义 JSON 里提取全部 models 对象。返回 {id: {ctx,max,vision}}"""
    models = {}
    n = 0
    # 形如 \\\"models\\\": [ ... ]（也可能是单层转义 \"models\": [ ... ]）
    for m in re.finditer(r'\\{1,2}?"models\\{1,2}?"\s*:\s*\[', html):
        if n >= 20:
            break
        n += 1
        start = m.end() - 1  # 指向 '['
        depth, end = 0, -1
        for i in range(start, min(len(html), start + 30000)):
            c = html[i]
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end < 0:
            continue
        raw = html[start:end]
        try:
            txt = raw.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
            if '\\"' in txt or "\\n" in txt:
                txt = txt.encode("utf-8", "ignore").decode("unicode_escape", "ignore")
        except Exception:
            continue
        for om in re.finditer(r"\{[^{}]*\}", txt):
            try:
                obj = json.loads(om.group(0))
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("id"):
                models[obj["id"]] = {
                    "ctx": obj.get("contextWindow"),
                    "max": obj.get("maxTokens"),
                    "vision": "image" in (obj.get("input") or []),
                }
    return models


def fetch_ark_plan_models():
    """运行时抓取方舟官方文档中的 Agent Plan 模型清单。返回 (models|None, 来源说明)"""
    with _ark_state["lock"]:
        now = time.time()
        if _ark_state["models"] and now - _ark_state["ts"] < ARK_CACHE_TTL:
            return _ark_state["models"], "内存缓存"
        disk = None
        try:
            with open(ARK_CACHE_FILE, "r", encoding="utf-8") as f:
                disk = json.load(f)
        except Exception:
            disk = None
        if disk and disk.get("models") and now - disk.get("ts", 0) < ARK_CACHE_TTL:
            _ark_state.update(ts=disk.get("ts", now), models=disk["models"])
            return _ark_state["models"], "缓存（6h 内）"
        html = None
        for use_proxy in (True, False):
            try:
                req = urllib.request.Request(ARK_DOCS_URL)
                req.add_header("User-Agent",
                               "Mozilla/5.0 (Windows NT 10.0; Win64; x64) WB-Switch/2.2")
                if use_proxy:
                    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
                else:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    html = opener.open(req, timeout=20).read().decode("utf-8", "replace")
                if html:
                    break
            except Exception:
                continue
        models = _extract_ark_models_from_html(html) if html else {}
        if models:
            _ark_state.update(ts=now, models=models)
            try:
                with open(ARK_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"ts": now, "models": models}, f, ensure_ascii=False)
            except OSError:
                pass
            return models, "官方文档实时抓取"
        if disk and disk.get("models"):
            _ark_state.update(ts=now, models=disk["models"])
            return _ark_state["models"], "最近缓存（官方页面本次抓取失败）"
        return None, "方舟官方清单抓取失败且无可用缓存"


def _http_get_json(url, key, timeout=12):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer %s" % key)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "WB-Switch/2.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def fetch_provider_models(url, key):
    """拉取供应商模型列表。返回 (ids, details, info) 或 (None, None, err)"""
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return None, None, "API 地址不合法"
    # 火山方舟 Agent Plan：无动态接口，运行时抓官方文档（失败回落缓存）
    if "volces.com" in url and "/api/plan/" in url:
        models, src = fetch_ark_plan_models()
        if models:
            return sorted(models.keys()), models, "火山方舟 Agent Plan · " + src
        return None, None, src
    candidates = []
    if url.endswith("/v1") or url.endswith("/v2") or url.endswith("/v3"):
        candidates.append(url + "/models")
    else:
        candidates.append(url + "/v1/models")
        candidates.append(url + "/models")
    last_err = None
    for u in candidates:
        for use_proxy in (True, False):
            try:
                if use_proxy:
                    data = _http_get_json(u, key)
                else:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    req = urllib.request.Request(u)
                    req.add_header("Authorization", "Bearer %s" % key)
                    req.add_header("Accept", "application/json")
                    with opener.open(req, timeout=12) as resp:
                        data = json.loads(resp.read().decode("utf-8", "replace"))
                ids, details = [], {}
                if isinstance(data, dict) and isinstance(data.get("data"), list):
                    for m in data["data"]:
                        if isinstance(m, dict) and m.get("id"):
                            ids.append(m["id"])
                            details[m["id"]] = {
                                "ctx": m.get("context_window") or m.get("max_context_window_tokens"),
                                "vision": bool(m.get("input") and "image" in m["input"]),
                                "max": m.get("max_output_tokens"),
                            }
                elif isinstance(data, list):
                    for m in data:
                        mid = m.get("id") if isinstance(m, dict) else str(m)
                        ids.append(mid)
                ids = sorted(set(ids))
                if ids:
                    return ids, details, u
            except Exception as e:
                last_err = "%s -> %r" % (u, e)
    return None, None, (last_err or "未获取到模型")


# ---------------------------------------------------------------- Web UI
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WB Switch</title>
<style>
:root{
  --bg:#eef0f4;--panel:#fff;--ink:#1b2233;--sub:#6b7490;--line:#e3e7ef;
  --pri:#3f66f0;--pri2:#5b8cff;--ok:#15a35f;--warn:#e08a00;--err:#d84b42;
  --side:#ffffff;--chip:#eef2ff;--shadow:0 1px 4px rgba(25,35,65,.08)
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{font:14px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);display:flex;flex-direction:column;overflow:hidden}

/* ── 标题栏（模拟桌面窗口） ── */
#titlebar{height:46px;background:#1e2536;color:#dfe5f3;display:flex;align-items:center;padding:0 14px;gap:12px;flex-shrink:0;-webkit-app-region:drag}
#titlebar .logo{width:22px;height:22px;border-radius:6px;background:linear-gradient(135deg,#5b8cff,#3f66f0);display:flex;align-items:center;justify-content:center;font-size:13px}
#titlebar b{font-size:13.5px;font-weight:600}
#titlebar .ver{font-size:11px;color:#8a93ad}
#titlebar .spacer{flex:1}
.wb-status{display:flex;align-items:center;gap:6px;font-size:12px;background:#2a3348;padding:4px 12px;border-radius:14px}
.dot{width:8px;height:8px;border-radius:50%;background:#5b6178}
.dot.on{background:#31d98c;box-shadow:0 0 6px #31d98c88}
.tb-btn{border:none;border-radius:7px;padding:5px 14px;font-size:12px;cursor:pointer;font-weight:600;-webkit-app-region:no-drag}
.tb-btn.launch{background:#15a35f;color:#fff}
.tb-btn.restart{background:#e08a00;color:#fff}
.tb-btn:disabled{opacity:.45;cursor:not-allowed}

/* ── 布局 ── */
#app{flex:1;display:flex;min-height:0}
aside{width:196px;background:var(--side);border-right:1px solid var(--line);padding:14px 10px;display:flex;flex-direction:column;gap:4px;flex-shrink:0}
.nav{display:flex;align-items:center;gap:10px;padding:9px 14px;border-radius:9px;cursor:pointer;font-size:13.5px;color:#3a4460}
.nav:hover{background:#f2f5fb}
.nav.on{background:var(--chip);color:var(--pri);font-weight:600}
.nav .ico{font-size:16px}
aside .foot{margin-top:auto;font-size:11px;color:var(--sub);padding:8px 12px;line-height:1.5}
main{flex:1;overflow-y:auto;padding:18px 22px}
.page{display:none}
.page.on{display:block}
h2.pt{font-size:16px;margin-bottom:4px}
p.pd{font-size:12.5px;color:var(--sub);margin-bottom:16px}

/* ── 重启提示横幅 ── */
#restartBanner{display:none;background:#fff7e8;border:1px solid #f2d9a4;color:#8a5a00;border-radius:10px;padding:10px 16px;margin-bottom:14px;font-size:13px;align-items:center;gap:10px}
#restartBanner.show{display:flex}
#restartBanner b{color:#c07700}
#restartBanner button{margin-left:auto}

/* ── 供应商卡片 ── */
.toolbar{display:flex;gap:10px;margin-bottom:16px;align-items:center}
.btn{border:none;border-radius:8px;padding:7px 16px;font-size:13px;cursor:pointer;background:var(--pri);color:#fff;font-weight:500}
.btn.ghost{background:#fff;color:var(--pri);border:1px solid #c9d6ff}
.btn.sm{padding:4px 12px;font-size:12px}
.btn.ok{background:var(--ok)}
.btn.danger{background:#fdecec;color:var(--err)}
.btn:hover{filter:brightness(1.05)}
.cardgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
.pcard{background:var(--panel);border:1.5px solid var(--line);border-radius:12px;padding:16px;box-shadow:var(--shadow);position:relative}
.pcard.active{border-color:var(--ok);background:#f4fcf8}
.pcard .badge{position:absolute;top:12px;right:12px;font-size:11px;background:#e0f7ec;color:var(--ok);padding:2px 10px;border-radius:12px;font-weight:600}
.pcard .pname{font-size:14.5px;font-weight:700;margin-bottom:6px;max-width:80%}
.pcard .pline{font-size:12px;color:var(--sub);display:flex;gap:6px;margin-bottom:3px}
.pcard .pline .k{color:#9aa3bd;flex-shrink:0}
.pcard .pline .v{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pcard .acts{display:flex;gap:8px;margin-top:12px}
.emptybox{grid-column:1/-1;text-align:center;color:var(--sub);padding:50px 0;background:var(--panel);border:1px dashed var(--line);border-radius:12px}

/* ── 用量统计 ── */
.filters{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-bottom:14px;box-shadow:var(--shadow)}
.fgroup{display:flex;align-items:center;gap:8px}
.fgroup label{font-size:12.5px;color:var(--sub);font-weight:600}
.seg{display:flex;background:#f0f2f8;border-radius:8px;padding:3px;gap:2px}
.seg span{padding:4px 13px;border-radius:6px;font-size:12.5px;cursor:pointer;color:#4a5578}
.seg span.on{background:#fff;color:var(--pri);font-weight:600;box-shadow:0 1px 3px rgba(0,0,0,.1)}
select,input[type=date]{border:1px solid var(--line);border-radius:8px;padding:5px 10px;font-size:12.5px;background:#fff;color:var(--ink);outline:none}
.statgrid{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:14px}
.biggrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
.bigstat{background:linear-gradient(135deg,#f8faff,#eef3fc);border:1px solid var(--line);border-radius:14px;padding:18px 12px;text-align:center;box-shadow:var(--shadow)}
.bigstat .bv{font-size:24px;font-weight:700;color:var(--pri);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bigstat .bl{font-size:12px;color:var(--sub);margin-top:5px}
.surank{display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border-radius:50%;background:#f0f2f6;margin-right:8px;font-size:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
.stat .v{font-size:21px;font-weight:700;color:var(--pri)}
.stat .l{font-size:12px;color:var(--sub)}
.chartcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:var(--shadow);margin-bottom:14px}
.chartcard h3{font-size:13.5px;margin-bottom:10px;color:#2a3352}
.bars{display:flex;align-items:flex-end;gap:5px;height:130px;padding-top:4px}
.bar{flex:1;background:linear-gradient(180deg,#6c93ff,#3f66f0);border-radius:4px 4px 0 0;position:relative;min-width:8px}
.bar:hover::after{content:attr(data-tip);position:absolute;bottom:105%;left:50%;transform:translateX(-50%);background:#1b2233;color:#fff;font-size:11px;padding:3px 9px;border-radius:6px;white-space:nowrap;z-index:5}
.bar .d{position:absolute;top:100%;left:50%;transform:translateX(-50%);font-size:9px;color:var(--sub);white-space:nowrap;margin-top:3px}
.mrow{display:flex;align-items:center;gap:10px;margin-bottom:9px;font-size:13px}
.mrow .mname{width:200px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mrow .mbarwrap{flex:1;background:#eef1f6;border-radius:5px;height:15px;overflow:hidden}
.mrow .mbar{height:100%;border-radius:5px;background:linear-gradient(90deg,#43b58c,#15a35f)}
.mrow.m2 .mbar{background:linear-gradient(90deg,#7c93ff,#4a6cf0)}
.mrow .mval{width:150px;text-align:right;color:var(--sub);font-size:12px;flex-shrink:0}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line)}
th{color:var(--sub);font-weight:500;font-size:11.5px}
.tag{font-size:11px;padding:2px 9px;border-radius:10px;background:var(--chip);color:var(--pri)}
.tag.builtin{background:#f0f2f6;color:#5a6584}
.tag.t3{background:#fdf3e3;color:#b0741a}
.bar i{position:absolute;bottom:0;left:0;width:100%;background:linear-gradient(180deg,#f2b866,#e8a23f);border-radius:4px 4px 0 0;display:block}
.clsbadge{font-style:normal;font-size:9px;padding:1px 5px;border-radius:8px;margin-left:6px;vertical-align:1px;background:#e9f7f0;color:#0f8a55}
.clsbadge.t3{background:#fdf3e3;color:#b0741a}
.omrow{display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:6px;font-size:12px;cursor:pointer;flex-wrap:wrap}
.omrow:hover{background:#f4f6fb}
.omrow .omid{color:var(--sub);font-size:11px}
.omtag{font-size:10px;padding:1px 6px;border-radius:8px;background:#eef1f6;color:#5a6584}
.omtag.def{background:#e9f7f0;color:#0f8a55;font-weight:600}
.pct{height:6px;background:#eef1f6;border-radius:3px;overflow:hidden;width:80px;display:inline-block;vertical-align:middle;margin-right:6px}
.pct i{display:block;height:100%;background:var(--pri)}

/* ── 日志 ── */
#evtlist{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;box-shadow:var(--shadow);max-height:70vh;overflow:auto;font-size:12.5px}
#evtlist div{padding:5px 0;border-bottom:1px dashed var(--line)}
.k-b{color:var(--pri)}.k-r{color:var(--ok)}.k-e{color:var(--err)}.k-i{color:var(--sub)}

/* ── 所有模型开关 ── */
.modeswitch{display:inline-flex;align-items:center;gap:8px;cursor:pointer;user-select:none;margin-left:4px}
.modeswitch input{display:none}
.modeswitch .track{width:40px;height:22px;border-radius:11px;background:#d5dae4;position:relative;transition:.2s;flex-shrink:0}
.modeswitch .thumb{position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.25);transition:.2s}
.modeswitch input:checked+.track{background:var(--pri)}
.modeswitch input:checked+.track .thumb{left:20px}
.modeswitch .mlabel{font-size:13px;color:var(--ink)}

/* ── 弹窗 ── */
dialog{border:none;border-radius:14px;box-shadow:0 16px 60px rgba(10,20,50,.25);padding:22px;width:520px}
dialog::backdrop{background:rgba(15,20,40,.45)}
dialog h3{margin-bottom:14px;font-size:15px}
dialog label{display:block;font-size:12px;color:var(--sub);margin:10px 0 4px}
dialog input{width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 11px;font-size:13px}
dialog .checks{display:flex;gap:16px;margin-top:12px;font-size:13px}
dialog .checks label{display:flex;align-items:center;gap:5px;margin:0;color:var(--ink);cursor:pointer}
.dlgacts{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
#toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%);background:#1b2233;color:#fff;padding:10px 22px;border-radius:10px;font-size:13px;opacity:0;transition:.3s;pointer-events:none;z-index:99}
#toast.show{opacity:.95}
#helpdlg{width:640px;max-height:80vh;overflow:auto}
.helpbody h4{margin:14px 0 6px;font-size:13.5px;color:var(--pri)}
.helpbody p{font-size:13px;line-height:1.7;margin:4px 0}
.helpbody ul{margin:4px 0 8px;padding-left:20px}
.helpbody li{font-size:13px;line-height:1.7;margin:3px 0}
.helpbody .disclaim{font-size:12px;color:var(--sub);background:var(--chip);border-radius:8px;padding:10px 12px}
.helpbody .verline{font-size:11px;color:var(--sub);text-align:right;margin-top:8px}
</style>
</head>
<body>

<div id="titlebar">
  <div class="logo">⚡</div><b>WB Switch</b><span class="ver">v2.17 · WorkBuddy 供应商管理器</span>
  <div class="spacer"></div>
  <button class="tb-btn" onclick="openHelp()">? 使用说明</button>
  <div class="wb-status"><span class="dot" id="wbdot"></span><span id="wbtext">检测中…</span></div>
  <button class="tb-btn launch" id="btnLaunch" onclick="launchWB()">▶ 启动 WorkBuddy</button>
  <button class="tb-btn restart" id="btnRestart" onclick="restartWB()" style="display:none">↻ 重启使配置生效</button>
</div>

<div id="app">
  <aside>
    <div class="nav on" data-page="providers" onclick="go('providers',this)"><span class="ico">🔌</span>供应商</div>
    <div class="nav" data-page="usage" onclick="go('usage',this)"><span class="ico">📊</span>用量统计</div>
    <div class="nav" data-page="summary" onclick="go('summary',this)"><span class="ico">🏆</span>汇总</div>
    <div class="nav" data-page="logs" onclick="go('logs',this)"><span class="ico">🛡️</span>守护日志</div>
    <div class="foot">守护运行中 · 每 2s 巡检<br>models.json 丢失自动恢复<br>数据目录 ~/.workbuddy/wb-switch</div>
  </aside>

  <main>
    <!-- 供应商页 -->
    <section class="page on" id="page-providers">
      <h2 class="pt">供应商管理</h2>
      <p class="pd">管理 WorkBuddy 自定义供应商，一键切换（写入 ~/.workbuddy/models.json）。切换后需重启 WorkBuddy 生效。</p>
      <div id="restartBanner">⚠️ <span id="rbText">配置已切换，重启 WorkBuddy 后生效</span>
        <button class="btn sm" onclick="restartWB()">立即重启</button>
        <button class="btn sm ghost" onclick="dismissRestart()">我已重启，关闭提示</button>
      </div>
      <div class="toolbar">
        <button class="btn" onclick="openEdit()">＋ 新增供应商</button>
        <button class="btn ghost" onclick="importCurrent()">从当前配置导入</button>
        <label class="modeswitch" title="开启后把所有第三方模型同时注入 WorkBuddy 模型列表">
          <input type="checkbox" id="chkAllModels" onchange="setModelMode(this.checked?'all':'single')">
          <span class="track"><span class="thumb"></span></span>
          <span class="mlabel">所有模型</span>
        </label>
        <span id="modelModeText" style="font-size:12px;color:var(--sub)">单模型模式</span>
      </div>
      <div class="cardgrid" id="presets"></div>
    </section>

    <!-- 用量统计页 -->
    <section class="page" id="page-usage">
      <h2 class="pt">用量统计</h2>
      <p class="pd">数据来源 WorkBuddy 本地数据库与会话记录（只读），支持 时间 / 供应商 / 模型 筛选联动。</p>
      <div class="filters">
        <div class="fgroup"><label>时间</label>
          <div class="seg" id="segTime">
            <span data-d="1" onclick="setTime(1,this)">今天</span>
            <span data-d="7" class="on" onclick="setTime(7,this)">近7天</span>
            <span data-d="30" onclick="setTime(30,this)">近30天</span>
            <span data-d="90" onclick="setTime(90,this)">近90天</span>
            <span data-d="3650" onclick="setTime(3650,this)">全部</span>
          </div>
          <input type="date" id="f-from" onchange="customRange()"> <span style="color:var(--sub);font-size:12px">至</span>
          <input type="date" id="f-to" onchange="customRange()">
        </div>
        <div class="fgroup"><label>类别</label>
          <div class="seg" id="segCls">
            <span class="on" onclick="setCls('',this)">全部</span>
            <span onclick="setCls('official',this)">官方·积分</span>
            <span onclick="setCls('third',this)">第三方·Token</span>
          </div>
        </div>
        <div class="fgroup"><label>供应商</label><select id="f-prov" onchange="fillFilterOptions();renderUsage()"><option value="">全部</option></select></div>
        <div class="fgroup"><label>模型</label><select id="f-model" onchange="renderUsage()"><option value="">全部</option></select></div>
      </div>
      <div class="statgrid">
        <div class="stat"><div class="v" id="st-total">-</div><div class="l">总用量</div></div>
        <div class="stat"><div class="v" id="st-sess">-</div><div class="l">会话数</div></div>
        <div class="stat"><div class="v" id="st-hit">-</div><div class="l">缓存命中率</div></div>
        <div class="stat"><div class="v" id="st-credit">-</div><div class="l">官方积分</div></div>
        <div class="stat"><div class="v" id="st-cost">-</div><div class="l">第三方费用（官方价）</div></div>
        <div class="stat"><div class="v" id="st-avg">-</div><div class="l">日均用量</div></div>
      </div>
      <div class="chartcard"><h3>每日 Token 用量</h3>
        <div style="font-size:12px;color:var(--sub);margin:2px 0 6px">
          <span style="display:inline-block;width:10px;height:10px;background:#6c93ff;border-radius:2px;margin-right:4px"></span>官方（积分计费）
          <span style="display:inline-block;width:10px;height:10px;background:#e8a23f;border-radius:2px;margin:0 4px 0 14px"></span>第三方（Token 计费）
        </div>
        <div class="bars" id="daybars"></div><div style="height:20px"></div></div>
      <div class="chartcard"><h3>按模型分布</h3><div id="modelrows"></div>
        <h3 style="margin-top:14px">按供应商分布</h3><div id="provrows"></div></div>
      <div class="chartcard"><h3>缓存命中率趋势（每日命中率 %，折线）</h3>
        <div id="hitline"></div></div>
      <div class="chartcard"><h3>供应商命中率汇总（按请求，随筛选联动）</h3>
        <table><thead><tr><th>供应商</th><th>请求数</th><th>输入</th><th>缓存命中</th><th>输出</th><th>命中率</th><th>费用</th></tr></thead>
        <tbody id="provhit"></tbody></table></div>
      <div class="chartcard"><h3>模型统计（按请求）</h3>
        <table><thead><tr><th>模型</th><th>供应商</th><th>请求数</th><th>输入</th><th>缓存命中</th><th>输出</th><th>命中率</th><th>费用</th></tr></thead>
        <tbody id="reqstats"></tbody></table></div>
      <div class="chartcard"><h3>请求日志（最近 200 条，随筛选联动）</h3>
        <table><thead><tr><th>时间</th><th>供应商</th><th>模型</th><th>输入</th><th>命中</th><th>输出</th><th>命中率</th><th>费用</th></tr></thead>
        <tbody id="reqrows"></tbody></table></div>
      <div class="chartcard"><h3>会话明细</h3>
        <table><thead><tr><th>会话</th><th>供应商</th><th>模型</th><th>Token</th><th>计费</th><th>上下文</th><th>时间</th></tr></thead>
        <tbody id="sessrows"></tbody></table></div>
    </section>

    <!-- 汇总页 -->
    <section class="page" id="page-summary">
      <h2 class="pt">汇总战报</h2>
      <p class="pd">纯展示汇总——只放拿得出手的数字，不统计积分和金额。</p>
      <div class="filters">
        <div class="fgroup"><label>范围</label>
          <div class="seg" id="segSum">
            <span data-s="7" onclick="setSum(7,this)">近7天</span>
            <span data-s="30" onclick="setSum(30,this)">近30天</span>
            <span data-s="3650" class="on" onclick="setSum(3650,this)">全部</span>
          </div>
        </div>
        <div class="fgroup"><button class="btn" onclick="copyReport()">📋 复制战报文本</button></div>
      </div>
      <div class="biggrid">
        <div class="bigstat"><div class="bv" id="su-token">-</div><div class="bl">累计 Token</div></div>
        <div class="bigstat"><div class="bv" id="su-sess">-</div><div class="bl">总会话数</div></div>
        <div class="bigstat"><div class="bv" id="su-active">-</div><div class="bl">活跃天数</div></div>
        <div class="bigstat"><div class="bv" id="su-models">-</div><div class="bl">用过的模型</div></div>
        <div class="bigstat"><div class="bv" id="su-peak">-</div><div class="bl">单日最高</div></div>
        <div class="bigstat"><div class="bv" id="su-grow">-</div><div class="bl">近7天环比</div></div>
      </div>
      <div class="chartcard"><h3>🥇 模型排行 Top 5</h3><div id="su-modelrows"></div></div>
      <div class="chartcard"><h3>每日趋势</h3><div class="bars" id="su-trend"></div><div style="height:20px"></div></div>
    </section>

    <!-- 日志页 -->
    <section class="page" id="page-logs">
      <h2 class="pt">守护日志</h2>
      <p class="pd">快照备份 / 自动恢复 / 切换记录。models.json 一旦丢失或损坏，守护将在 2 秒内自动从快照恢复。</p>
      <div id="evtlist"></div>
    </section>
  </main>
</div>

<dialog id="dlg">
  <h3 id="dlgtitle">新增供应商</h3>
  <div id="normalRows">
    <label>供应商名称</label><input id="f-name" placeholder="例如：方舟 Agent Plan">
    <label>API 地址 (Base URL)</label><input id="f-url" placeholder="https://...（填写到 /v1 或根路径均可）">
    <label>API Key</label>
    <div style="display:flex;gap:8px">
      <input id="f-key" type="password" placeholder="sk-..." style="flex:1">
      <button class="btn ghost" style="flex-shrink:0" id="btnFetch" onclick="fetchModels()">⟳ 加载全部模型</button>
    </div>
    <div id="modelPickWrap" style="display:none">
      <label>选择模型（共 <span id="modelCount">0</span> 个，来自该供应商模型列表）</label>
      <select id="f-models" style="width:100%;border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-size:13px" onchange="onModelPick(this)"></select>
    </div>
    <label>模型 ID</label><input id="f-id" placeholder="加载失败时也可手动填写，例如：ark-code-latest">
    <div class="checks">
      <label><input type="checkbox" id="f-tools" checked>支持工具调用</label>
      <label><input type="checkbox" id="f-img">支持图片</label>
      <label><input type="checkbox" id="f-reason">支持推理</label>
    </div>
  </div>
  <div id="officialWrap" style="display:none">
    <p style="font-size:12px;color:var(--sub);margin:4px 0 8px">勾选你的官方常用模型（清单来自 WorkBuddy 云端配置，应用会自动保持最新）。官方模式下的模型切换在 WorkBuddy 会话里选择，这里是你的常用清单展示。</p>
    <div style="display:flex;gap:8px;margin-bottom:8px">
      <button class="btn ghost" id="btnOfficial" onclick="loadOfficialList()">⟳ 加载官方模型</button>
    </div>
    <div id="officialList" style="max-height:340px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:8px"></div>
  </div>
  <div class="dlgacts">
    <button class="btn ghost" onclick="dlg.close()">取消</button>
    <button class="btn" onclick="savePreset()">保存</button>
  </div>
</dialog>
<div id="toast"></div>

<dialog id="helpdlg">
  <h3>📖 使用说明</h3>
  <div class="helpbody">
    <h4>一、这个软件是干什么的</h4>
    <p>WorkBuddy 更新后第三方模型容易丢失。WB Switch 把供应商配置独立保存为预设，一键恢复、一键切换，顺便把模型用量（token、缓存命中率、按官方价的费用）统计清楚。</p>

    <h4>二、供应商页怎么用</h4>
    <ul>
      <li><b>新增供应商</b>：填 API 地址和 Key，「加载全部模型」可直接从供应商拉取模型清单（如小忆有 168 个可选）。</li>
      <li><b>启用某个预设</b>：写入 WorkBuddy 配置（models.json / settings.json / 偏好 / 会话记录四层一次写对），点右上角「重启使配置生效」后新会话默认用它。</li>
      <li><b>所有模型开关</b>：开启后全部供应商的模型同时注入 WorkBuddy 模型列表，不用来回切换；关闭恢复单模型。注意：WorkBuddy 按<b>模型 ID</b> 识别模型，不同供应商用了相同 ID（如三家都用 glm-5.3-flash）时同名条目会互相覆盖，只有排最前的（当前供应商）生效——这是 WorkBuddy 的机制限制。想四家都独立可用，给每家选不同的模型 ID 即可。</li>
    </ul>

    <h4>三、用量统计页看什么</h4>
    <ul>
      <li><b>总用量</b>：token 总数（过万显示 x.x万，过亿显示 x.x亿）。</li>
      <li><b>缓存命中率</b>：命中率越高越省钱——同样的上下文，命中缓存的部分按缓存价计费（通常只有正价的 5%~20%）。供应商命中率汇总表可对比各家的命中率，>=60% 绿 / >=30% 橙 / &lt;30% 红。</li>
      <li><b>费用</b>：第三方模型按官方价计（价格表内置 model_pricing.json，201 个模型）。国内模型显示 ¥，国外模型显示 $，与官方计价币种一致。官方模型按 credits 显示。</li>
      <li>支持 时间 / 类别 / 供应商 / 模型 四维筛选，全部图表联动。</li>
    </ul>

    <h4>四、日常使用</h4>
    <ul>
      <li>Windows 双击桌面 <b>WB Switch.bat</b>；macOS 在终端运行 <b>./WB Switch.command</b>（或双击）。面板自动接管端口加载最新版。</li>
      <li>WorkBuddy 更新后模型丢了 → 打开面板 → 点对应预设「启用」→ 重启 WorkBuddy，恢复。</li>
      <li>守护进程每 2 秒巡检，models.json 丢失或损坏会自动从快照恢复。</li>
    </ul>

    <h4>五、免责声明</h4>
    <p class="disclaim">本工具为第三方开源辅助工具，与 WorkBuddy 官方无关。它只读写本机 WorkBuddy 配置文件与本地数据库（只读统计，不上传任何数据）。费用为按公开官方价的估算值，实际以各供应商账单为准。使用本工具产生的一切后果由使用者自行承担。</p>
    <p class="verline">WB Switch v2.17 · 零依赖单文件 · 数据目录 ~/.workbuddy/wb-switch</p>
  </div>
  <div class="dlgacts"><button class="btn" onclick="helpdlg.close()">我知道了</button></div>
</dialog>

<script>
let presets=[], allSessions=[], providerMap={}, officialIds=[], reqAll=[], reqStats=[];
let curDays=7, curFrom=null, curTo=null;

const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtTok=n=>{n=+n||0;return n>=1e6?(n/1e6).toFixed(2)+'M':n>=1e3?(n/1e3).toFixed(1)+'k':n};
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200)}
function go(p,el){document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('on',n===el));
  document.querySelectorAll('.page').forEach(pg=>pg.classList.toggle('on',pg.id==='page-'+p));
  if(p==='usage')loadUsage(); if(p==='summary')loadSummary(); if(p==='logs')loadEvents();}

/* ---------- WorkBuddy 进程控制 ---------- */
let deadCount=0;
async function pollStatus(){
  try{const d=await (await fetch('/api/wb-status')).json();
    deadCount=0;
    $('wbdot').classList.toggle('on',d.running);
    $('wbtext').textContent=d.running?'WorkBuddy 运行中':'WorkBuddy 未运行';
    $('btnLaunch').style.display=d.running?'none':'';
    $('btnRestart').style.display=d.running?'':'none';
  }catch(e){
    deadCount++;
    if(deadCount===3){$('wbtext').textContent='面板服务已断开';toast('⚠ 面板服务已断开，请关闭本窗口后重新双击桌面 WB Switch.bat');}
  }
}
async function launchWB(){
  $('btnLaunch').disabled=true;
  const d=await (await fetch('/api/launch',{method:'POST'})).json();
  $('btnLaunch').disabled=false;
  toast(d.ok?'WorkBuddy 启动中…':'启动失败: '+d.error); setTimeout(pollStatus,3000);
}
async function restartWB(){
  if(!confirm('将强制关闭并重新启动 WorkBuddy，确认继续？'))return;
  $('btnRestart').disabled=true; toast('正在重启 WorkBuddy…');
  try{
    const d=await (await fetch('/api/restart',{method:'POST'})).json();
    if(d.ok===false)toast('重启失败: '+(d.error||'未知错误'));
    else toast('WorkBuddy 重启中，约 10 秒后回来');
  }catch(e){toast('⚠ 面板服务已断开，请重新双击桌面 WB Switch.bat')}
  $('btnRestart').disabled=false; setTimeout(pollStatus,6000); dismissRestart(true);
}

/* ---------- 供应商 ---------- */
async function loadPresets(){
  const d=await (await fetch('/api/presets')).json();
  presets=d.presets||[]; officialIds=d.official_ids||[]; renderPresets(d.current||[]);
  const mode=d.model_mode||'single';
  $('modelModeText').textContent=mode==='all'?'所有模型模式 · 已注入 '+(d.current||[]).length+' 个':'单模型模式';
  $('chkAllModels').checked=mode==='all';
  const st=d.pending_switch;
  $('restartBanner').classList.toggle('show',!!st);
  if(st)$('rbText').innerHTML='配置已切换为 <b>'+esc(st.name)+'</b>（'+esc(st.time)+'），重启 WorkBuddy 后生效';
}
function sig(models){return (models||[]).map(m=>[m.id,m.apiKey,m.url].join('|')).join(';&')}
function renderPresets(cur){
  const el=$('presets');
  const officialActive = !(cur||[]).length;
  let html = `<div class="pcard${officialActive?' active':''}">
    ${officialActive?'<span class="badge">✓ 使用中</span>':''}
    <div class="pname">🏢 官方内置模型</div>
    <div class="pline"><span class="v">${officialIds.length?'常用：'+esc(officialIds.join('、')):'停用自定义供应商，使用 WorkBuddy 官方模型'}</span></div>
    <div class="acts">
      ${officialActive?'<button class="btn sm ok" disabled>已启用</button>':'<button class="btn sm" onclick="switchOfficial()">启用官方</button>'}
      <button class="btn sm ghost" onclick="openOfficialEdit()">编辑</button>
    </div></div>`;
  html += presets.map((p,i)=>{
    const models=p.models||[];
    const on = !officialActive && sig(models)===sig(cur);
    const m0 = models[0]||{};
    return `<div class="pcard${on?' active':''}">
      ${on?'<span class="badge">✓ 使用中</span>':''}
      <div class="pname">${esc(p.name)}</div>
      <div class="pline"><span class="k">模型</span><span class="v">${esc(models.map(m=>m.id).join(', ')||'-')}</span></div>
      ${m0.url?`<div class="pline"><span class="k">地址</span><span class="v">${esc(m0.url)}</span></div>`:''}
      ${m0.apiKey?`<div class="pline"><span class="k">Key</span><span class="v">${esc(m0.apiKey.slice(0,8))}••••••</span></div>`:''}
      <div class="acts">
        ${on?'<button class="btn sm" onclick="switchOfficial()">停用（切回官方）</button>':'<button class="btn sm" onclick="switchTo('+i+')">启用</button>'}
        <button class="btn sm ghost" onclick="openEdit(${i})">编辑</button>
        <button class="btn sm danger" onclick="delPreset(${i})">删除</button>
      </div></div>`}).join('');
  el.innerHTML=html;
}
async function switchOfficial(){
  try{
    const d=await (await fetch('/api/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({official:true})})).json();
    if(d.ok){toast('已切回官方内置模型，点右上角「重启」后生效');loadPresets()}else toast('切换失败: '+d.error);
  }catch(e){toast('⚠ 面板服务已断开，请重新双击桌面 WB Switch.bat')}
}
async function switchTo(i){
  try{
    const d=await (await fetch('/api/switch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i})})).json();
    if(d.ok){toast('已切换为「'+presets[i].name+'」，点右上角「重启」后生效');loadPresets()}else toast('切换失败: '+d.error);
  }catch(e){toast('⚠ 面板服务已断开，请重新双击桌面 WB Switch.bat')}
}
async function setModelMode(mode){
  if(mode==='all' && !confirm('开启「所有模型」：全部第三方供应商的模型都会注入 WorkBuddy 模型列表（当前供应商的模型为默认），重启 WorkBuddy 后生效。\\n\\n注意：不同供应商使用相同模型 ID 时（如多家都用 glm-5.3-flash），WorkBuddy 按模型 ID 匹配，同名条目会互相覆盖、只有当前供应商（排最前）的生效；不同 ID 的模型都能正常显示和使用。')){ $('chkAllModels').checked=false; return; }
  try{
    const d=await (await fetch('/api/model-mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})})).json();
    if(d.ok){toast(d.message||'模型模式已更新，重启 WorkBuddy 后生效');loadPresets()}
    else{toast('操作失败: '+(d.error||'未知错误')); $('chkAllModels').checked=(mode!=='all');}
  }catch(e){toast('⚠ 面板服务已断开，请重新双击桌面 WB Switch.bat'); $('chkAllModels').checked=(mode!=='all');}
}
async function importCurrent(){
  const d=await (await fetch('/api/import-current',{method:'POST'})).json();
  if(d.ok){toast('已导入当前配置为预设');loadPresets()}else toast('导入失败: '+d.error);
}
function openEdit(i){
  const p=i===undefined?null:presets[i];
  const m0=(p&&p.models&&p.models[0])||{};
  $('dlgtitle').textContent=p?'编辑供应商':'新增供应商';
  $('normalRows').style.display=''; $('officialWrap').style.display='none';
  dlg.dataset.official='';
  $('f-name').value=p?(p.name||''):'';
  $('f-url').value=m0.url||'';
  $('f-key').value=m0.apiKey||'';
  $('f-id').value=m0.id||'';
  $('modelPickWrap').style.display='none';
  $('f-tools').checked=m0.supportsToolCall!==undefined?!!m0.supportsToolCall:true;
  $('f-img').checked=!!m0.supportsImages;
  $('f-reason').checked=!!m0.supportsReasoning;
  dlg.dataset.index=i===undefined?'':i; dlg.showModal();
}
async function openOfficialEdit(){
  $('dlgtitle').textContent='编辑官方模型';
  $('normalRows').style.display='none'; $('officialWrap').style.display='';
  dlg.dataset.index=''; dlg.dataset.official='1';
  dlg.showModal();
  loadOfficialList();
}
async function loadOfficialList(){
  const box=$('officialList');
  box.innerHTML='<span style="color:var(--sub)">加载中…</span>';
  try{
    const d=await (await fetch('/api/official-models')).json();
    if(d.ok){
      const sel=new Set(officialIds);
      box.innerHTML=d.models.map(m=>`<label class="omrow"><input type="checkbox" value="${esc(m.id)}"${sel.has(m.id)?' checked':''}>
        <b>${esc(m.name)}</b><span class="omid">${esc(m.id)}</span>
        ${m.credits?`<span class="omtag">${esc(m.credits)}</span>`:''}
        ${m.ctx?`<span class="omtag">${fmtCtx(m.ctx)}</span>`:''}
        ${m.vision?'<span class="omtag">图</span>':''}
        ${m.reasoning?'<span class="omtag">推理</span>':''}
        ${m.default?'<span class="omtag def">官方默认</span>':''}</label>`).join('')
        +'<div style="height:4px"></div>';
    }else box.innerHTML='<span style="color:var(--sub)">'+esc(d.error||'加载失败')+'</span>';
  }catch(e){box.innerHTML='<span style="color:var(--sub)">加载失败</span>'}
}
async function saveOfficial(){
  const ids=[...document.querySelectorAll('#officialList input:checked')].map(i=>i.value);
  const d=await (await fetch('/api/save-official',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ids})})).json();
  if(d.ok){dlg.close();officialIds=ids;renderPresets((await (await fetch('/api/presets')).json()).current||[]);toast('已保存官方常用模型')}
  else toast('保存失败: '+d.error);
}
let modelDetails={};
function fmtCtx(n){n=+n||0;return n>=1048576?(n/1048576).toFixed(0)+'M':n>=1024?(n/1024).toFixed(0)+'k':n}
async function fetchModels(){
  const url=$('f-url').value.trim(), key=$('f-key').value.trim();
  if(!url){toast('请先填写 API 地址');return}
  $('btnFetch').disabled=true; $('btnFetch').textContent='⟳ 加载中…';
  try{
    const d=await (await fetch('/api/fetch-models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,key})})).json();
    if(d.ok&&d.models.length){
      modelDetails=d.details||{};
      $('modelCount').textContent=d.models.length;
      const cur=$('f-id').value.trim();
      $('f-models').innerHTML='<option value="">— 请选择模型 —</option>'+d.models.map(m=>{
        const dt=modelDetails[m]||{};
        let tag='';
        if(dt.ctx)tag+=' · '+fmtCtx(dt.ctx)+' 上下文';
        if(dt.vision)tag+=' · 可看图';
        return `<option value="${esc(m)}"${m===cur?' selected':''}>${esc(m)}${tag}</option>`;
      }).join('');
      $('modelPickWrap').style.display='';
      toast('已加载 '+d.models.length+' 个模型'+(d.via?('（'+d.via+'）'):''));
    }else{toast('加载失败，可手动填写模型 ID')}
  }catch(e){toast('加载失败，可手动填写模型 ID')}
  $('btnFetch').disabled=false; $('btnFetch').textContent='⟳ 加载全部模型';
}
function onModelPick(sel){
  const m=sel.value; if(!m)return;
  $('f-id').value=m;
  const dt=modelDetails[m]||{};
  if(dt.vision!==undefined)$('f-img').checked=!!dt.vision;
}
async function savePreset(){
  if(dlg.dataset.official==='1'){dlg.dataset.official='';return saveOfficial()}
  const idx=dlg.dataset.index;
  const body={index:idx===''?null:+idx,name:$('f-name').value.trim()||'未命名',
    model:{id:$('f-id').value.trim(),name:$('f-name').value.trim(),vendor:'Custom',url:$('f-url').value.trim(),apiKey:$('f-key').value.trim(),
      supportsToolCall:$('f-tools').checked,supportsImages:$('f-img').checked,supportsReasoning:$('f-reason').checked,useCustomProtocol:false}};
  const d=await (await fetch('/api/save-preset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  if(d.ok){dlg.close();loadPresets();toast('已保存')}else toast('保存失败: '+d.error);
}
async function delPreset(i){
  if(!confirm('删除预设「'+presets[i].name+'」？'))return;
  await fetch('/api/del-preset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i})});
  loadPresets();toast('已删除');
}
async function dismissRestart(silent){
  await fetch('/api/dismiss-restart',{method:'POST'});
  if(!silent){$('restartBanner').classList.remove('show');toast('已确认，不再提示')}
}

/* ---------- 用量统计 ---------- */
// token 总量展示：>=1 万 → x.x万，>=1 亿 → x.x亿，其余原样
function fmtTotal(n){n=+n||0;
  if(n>=1e8)return (n/1e8).toFixed(2)+'亿';
  if(n>=1e4)return (n/1e4).toFixed(1)+'万';
  return String(Math.round(n));}
// 费用格式化：按模型官方计价币种显示 ¥ / $；官方模型返回 credit 金额
function fmtCost(c,cur){if(!c)return '';return (cur==='USD'?'$':'¥')+c.toFixed(2);}
let curCls='';
function setCls(v,el){curCls=v;
  document.querySelectorAll('#segCls span').forEach(s=>s.classList.toggle('on',s===el));
  renderUsage();}
function setTime(d,el){curDays=d;curFrom=null;curTo=null;
  document.querySelectorAll('#segTime span').forEach(s=>s.classList.toggle('on',s===el));
  $('f-from').value='';$('f-to').value='';loadUsage();}
function customRange(){
  const a=$('f-from').value,b=$('f-to').value;
  if(a||b){curFrom=a?new Date(a+'T00:00:00').getTime():null;
    curTo=b?new Date(b+'T23:59:59').getTime():null;
    document.querySelectorAll('#segTime span').forEach(s=>s.classList.remove('on'));}
  loadUsage();
}
async function loadUsage(){
  let url='/api/usage?days='+curDays;
  if(curFrom)url+='&from='+curFrom; if(curTo)url+='&to='+curTo;
  const d=await (await fetch(url)).json();
  allSessions=d.sessions||[]; providerMap=d.provider_map||{};
  try{
    let rurl='/api/requests?days='+curDays;
    if(curFrom)rurl+='&from='+curFrom; if(curTo)rurl+='&to='+curTo;
    const rq=await (await fetch(rurl)).json();
    reqAll=rq.requests||[]; reqStats=rq.stats||[];
  }catch(e){reqAll=[];reqStats=[]}
  fillFilterOptions(); renderUsage();
}
function fillFilterOptions(){
  // 选项来源 = 会话 + 请求（有些模型只有请求记录没有会话）
  const pool=[...allSessions.map(s=>({p:s.provider,m:s.model})),...reqAll.map(r=>({p:r.provider,m:r.model}))];
  const p=$('f-prov').value;
  const provs=[...new Set(pool.map(x=>x.p))].filter(Boolean).sort();
  // 模型列表随供应商联动：选了供应商就只列该供应商的模型
  const models=[...new Set(pool.filter(x=>!p||x.p===p).map(x=>x.m))].filter(Boolean).sort();
  const keep=(sel,list)=>{const v=sel.value;sel.innerHTML='<option value="">全部</option>'+
    list.map(x=>`<option${x===v?' selected':''}>${esc(x)}</option>`).join('');
    if(v&&!list.includes(v))sel.value='';};
  keep($('f-prov'),provs); keep($('f-model'),models);
}
function filtered(){
  const p=$('f-prov').value,m=$('f-model').value;
  return allSessions.filter(s=>(!p||s.provider===p)&&(!m||s.model===m)&&(!curCls||s.cls===curCls));
}
function renderUsage(){
  const ss=filtered();
  const total=ss.reduce((a,s)=>a+s.used,0);
  const credit=ss.reduce((a,s)=>a+(s.credit||0),0);
  // 费用按币种分开汇总（官方价分币种，不混算）
  const costCNY=ss.reduce((a,s)=>a+(s.cls==='third'&&s.currency!=='USD'?s.cost||0:0),0);
  const costUSD=ss.reduce((a,s)=>a+(s.cls==='third'&&s.currency==='USD'?s.cost||0:0),0);
  $('st-total').textContent=fmtTotal(total);
  $('st-sess').textContent=ss.length;
  $('st-credit').textContent=credit>0?credit.toFixed(2)+' credits':'-';
  $('st-cost').textContent=(costCNY>0?fmtCost(costCNY,'CNY')+' ':'')+(costUSD>0?fmtCost(costUSD,'USD'):'')||(costCNY+costUSD>0?'':'-');
  const dayset=[...new Set(allSessions.map(s=>s.ts?new Date(s.ts).toDateString():null))].filter(Boolean);
  const span=Math.max(1,curFrom&&curTo?Math.ceil((curTo-curFrom)/864e5):curDays>365?3650:curDays);
  $('st-avg').textContent=fmtTotal(total/Math.min(span,Math.max(dayset.length,1)));
  // 每日（官方/第三方双色堆叠）
  const days={}; ss.forEach(s=>{if(!s.ts)return;const k=new Date(s.ts).toLocaleDateString('sv-SE');
    days[k]=days[k]||{o:0,t:0,n:0}; if(s.cls==='third')days[k].t+=s.used; else days[k].o+=s.used; days[k].n++});
  const darr=Object.entries(days).sort().slice(-31);
  const dmax=Math.max(1,...darr.map(d=>d[1].o+d[1].t));
  $('daybars').innerHTML=darr.map(d=>{
    const sum=d[1].o+d[1].t, thirdPct=sum?d[1].t/sum*100:0;
    return `<div class="bar" style="height:${Math.max(3,sum/dmax*100)}%" data-tip="${d[0]}：官方 ${fmtTok(d[1].o)} · 第三方 ${fmtTok(d[1].t)} / ${d[1].n} 会话">`+
      `<i style="height:${thirdPct}%"></i><span class="d">${d[0].slice(5)}</span></div>`}).join('')
    ||'<span style="color:var(--sub)">暂无数据</span>';
  // 按模型 / 按供应商
  const agg=(key,cls)=>{
    const g={}; ss.forEach(s=>{const k=s[key];g[k]=g[k]||{t:0,n:0,cny:0,usd:0,third:false};
      g[k].t+=s.used; g[k].n++;
      if(s.cls==='third'){g[k].third=true; if(s.currency==='USD')g[k].usd+=s.cost||0; else g[k].cny+=s.cost||0;}
    });
    const arr=Object.entries(g).sort((a,b)=>b[1].t-a[1].t).slice(0,10);
    const mx=Math.max(1,...arr.map(a=>a[1].t));
    return arr.map(a=>`<div class="mrow ${cls}"><span class="mname" title="${esc(a[0])}">${esc(a[0])}<i class="clsbadge ${a[1].third?'t3':''}">${a[1].third?'第三方':'官方'}</i></span><span class="mbarwrap"><span class="mbar" style="display:block;width:${a[1].t/mx*100}%"></span></span><span class="mval">${fmtTok(a[1].t)} · ${a[1].n} 会话${a[1].cny>0?' · '+fmtCost(a[1].cny,'CNY'):''}${a[1].usd>0?' · '+fmtCost(a[1].usd,'USD'):''}</span></div>`).join('')||'<span style="color:var(--sub);font-size:12px">暂无数据</span>'};
  $('modelrows').innerHTML=agg('model','');
  $('provrows').innerHTML=agg('provider','m2');
  // 明细
  $('sessrows').innerHTML=ss.slice(0,50).map(s=>{
    const bill=s.cls==='third'
      ?(s.cost>0?`<span class="tag t3">${fmtCost(s.cost,s.currency)}</span>`:'<span class="tag t3">Token 计费</span>')
      :(s.credit>0?`<span class="tag builtin">${s.credit.toFixed(2)} credit</span>`:'<span style="color:var(--sub)">-</span>');
    return `<tr>
    <td>${esc(s.title)}</td>
    <td><span class="tag ${providerMap[s.model]?'t3':'builtin'}">${esc(s.provider)}</span></td>
    <td>${esc(s.model)}</td><td>${fmtTok(s.used)}</td>
    <td>${bill}</td>
    <td><span class="pct"><i style="width:${Math.min(s.pct,100)}%"></i></span>${s.pct}%</td>
    <td>${s.ts?new Date(s.ts).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'-'}</td></tr>`}).join('')
    ||'<tr><td colspan="7" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
  // ---- 请求维度：命中率 / 模型统计 / 请求日志 ----
  const p=$('f-prov').value,m=$('f-model').value;
  const rr=reqAll.filter(r=>(!p||r.provider===p)&&(!m||r.model===m)&&(!curCls||r.cls===curCls));
  const inpS=rr.reduce((a,r)=>a+r.inp,0), cacheS=rr.reduce((a,r)=>a+r.cached,0);
  $('st-hit').textContent=inpS?(cacheS*100/inpS).toFixed(1)+'%':'-';
  // 每日命中率折线
  const hd={}; rr.forEach(r=>{if(!r.ts)return;const k=new Date(r.ts).toLocaleDateString('sv-SE');
    const o=hd[k]=hd[k]||{i:0,c:0};o.i+=r.inp;o.c+=r.cached});
  const harr=Object.entries(hd).sort().map(d=>({k:d[0],i:d[1].i?d[1].c*100/d[1].i:0})).slice(-30);
  $('hitline').innerHTML=harr.length>=2?hitLineSVG(harr):'<span style="color:var(--sub);font-size:12px">数据点不足（至少需要 2 天的请求记录）</span>';
  // 供应商命中率汇总（每家供应商：请求数 / token / 缓存命中率 / 费用）
  const pg={}; rr.forEach(r=>{const g=pg[r.provider]=pg[r.provider]||{reqs:0,inp:0,cached:0,out:0,third:false,cny:0,usd:0};
    g.reqs++;g.inp+=r.inp;g.cached+=r.cached;g.out+=r.out;if(r.cls==='third')g.third=true;
    if(r.cls==='third'){if(r.currency==='USD')g.usd+=r.cost||0;else g.cny+=r.cost||0;}});
  const parr=Object.entries(pg).sort((a,b)=>b[1].inp-a[1].inp);
  $('provhit').innerHTML=parr.map(([pr,g])=>{
    const hit=g.inp?Math.round(g.cached*1000/g.inp)/10:0;
    const hcol=hit>=60?'#1a9d5c':hit>=30?'#b0741a':'#c2504d';
    const cost=(g.cny>0?fmtCost(g.cny,'CNY'):'')+(g.usd>0?(g.cny>0?' + ':'')+fmtCost(g.usd,'USD'):'');
    return `<tr>
    <td><span class="tag ${g.third?'t3':'builtin'}">${esc(pr)}</span></td>
    <td>${g.reqs}</td><td>${fmtTok(g.inp)}</td><td>${fmtTok(g.cached)}</td><td>${fmtTok(g.out)}</td>
    <td><b style="color:${hcol}">${hit}%</b></td>
    <td>${cost||'-'}</td></tr>`}).join('')
    ||'<tr><td colspan="7" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
  // 模型统计
  const rs=reqStats.filter(a=>(!p||a.provider===p)&&(!m||a.model===m)
    &&(!curCls||(curCls==='third')===(!!providerMap[a.model])));
  $('reqstats').innerHTML=rs.map(a=>{
    const cost=a.cost>0?fmtCost(a.cost,a.currency):'-';
    return `<tr>
    <td>${esc(a.model)}</td>
    <td><span class="tag ${providerMap[a.model]?'t3':'builtin'}">${esc(a.provider)}</span></td>
    <td>${a.reqs}</td><td>${fmtTok(a.inp)}</td><td>${fmtTok(a.cached)}</td><td>${fmtTok(a.out)}</td>
    <td><b style="color:${a.hit>=60?'#1a9d5c':a.hit>=30?'#b0741a':'#c2504d'}">${a.hit}%</b></td>
    <td>${cost}</td></tr>`}).join('')
    ||'<tr><td colspan="8" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
  // 请求日志（最新在前，最多 200 条）
  $('reqrows').innerHTML=rr.slice(-200).reverse().map(r=>`<tr>
    <td>${new Date(r.ts).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</td>
    <td><span class="tag ${providerMap[r.model]?'t3':'builtin'}">${esc(r.provider)}</span></td>
    <td>${esc(r.model)}</td><td>${fmtTok(r.inp)}</td>
    <td>${fmtTok(r.cached)}</td><td>${fmtTok(r.out)}</td>
    <td>${r.hit}%</td>
    <td>${r.cost>0?fmtCost(r.cost,r.currency):'-'}</td></tr>`).join('')
    ||'<tr><td colspan="8" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
}
function hitLineSVG(arr){
  const W=660,H=170,P=40;
  const xs=i=>P+i*(W-2*P)/(arr.length-1);
  const ys=v=>H-P-(v/100)*(H-2*P-10);
  const pts=arr.map((d,i)=>xs(i).toFixed(1)+','+ys(d.i).toFixed(1)).join(' ');
  let inner='';
  [0,25,50,75,100].forEach(v=>{inner+=`<line x1="${P}" y1="${ys(v)}" x2="${W-P}" y2="${ys(v)}" stroke="#e4e8f0"/>`+
    `<text x="${P-6}" y="${ys(v)+4}" text-anchor="end" font-size="10" fill="#8a93ab">${v}%</text>`});
  const lstep=Math.ceil(arr.length/8);
  arr.forEach((d,i)=>{if(i%lstep===0||i===arr.length-1)
    inner+=`<text x="${xs(i)}" y="${H-8}" text-anchor="middle" font-size="10" fill="#8a93ab">${d.k.slice(5)}</text>`});
  const dots=arr.map((d,i)=>`<circle cx="${xs(i).toFixed(1)}" cy="${ys(d.i).toFixed(1)}" r="3.5" fill="#6c93ff"><title>${d.k}：${d.i.toFixed(1)}%</title></circle>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto">
    ${inner}<polygon points="${P},${H-P} ${pts} ${W-P},${H-P}" fill="rgba(108,147,255,.10)"/>
    <polyline points="${pts}" fill="none" stroke="#6c93ff" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>${dots}</svg>`;
}

/* ---------- 汇总战报 ---------- */
let sumDays=3650, sumData=null;
function setSum(d,el){sumDays=d;
  document.querySelectorAll('#segSum span').forEach(s=>s.classList.toggle('on',s===el));
  loadSummary();}
async function loadSummary(){
  try{
    const d=await (await fetch('/api/usage?days='+sumDays)).json();
    const ss=d.sessions||[];
    const total=ss.reduce((a,s)=>a+s.used,0);
    $('su-token').textContent=fmtTotal(total);
    $('su-sess').textContent=ss.length;
    const dayset=new Set(ss.filter(s=>s.ts).map(s=>new Date(s.ts).toDateString()));
    $('su-active').textContent=dayset.size;
    const g={}; ss.forEach(s=>{g[s.model]=(g[s.model]||0)+s.used});
    const marr=Object.entries(g).sort((a,b)=>b[1]-a[1]);
    $('su-models').textContent=marr.length;
    const days={}; ss.forEach(s=>{if(!s.ts)return;
      const k=new Date(s.ts).toLocaleDateString('sv-SE');days[k]=(days[k]||0)+s.used});
    const darr=Object.entries(days).sort();
    let peak=null; darr.forEach(x=>{if(!peak||x[1]>peak[1])peak=x});
    $('su-peak').textContent=peak?(peak[0].slice(5)+' · '+fmtTok(peak[1])):'-';
    // 近7天 vs 前7天
    const now=Date.now();
    const l7=ss.filter(s=>s.ts>now-7*864e5).reduce((a,s)=>a+s.used,0);
    const p7=ss.filter(s=>s.ts<=now-7*864e5&&s.ts>now-14*864e5).reduce((a,s)=>a+s.used,0);
    let gt='-', grow=0;
    if(p7>0){grow=Math.round((l7-p7)/p7*100);gt=(grow>=0?'+':'')+grow+'%'}
    else if(l7>0){gt='新纪录';grow=999}
    $('su-grow').textContent=gt;
    $('su-grow').style.color=grow>0?'#0f8a55':(grow<0?'#c0564f':'var(--pri)');
    // Top5 排行
    const medals=['🥇','🥈','🥉','4️⃣','5️⃣'];
    const top=marr.slice(0,5);
    const mx=Math.max(1,...top.map(a=>a[1]));
    $('su-modelrows').innerHTML=top.map((a,i)=>`<div class="mrow"><span class="mname"><span class="surank">${medals[i]||i+1}</span>${esc(a[0])}</span><span class="mbarwrap"><span class="mbar" style="display:block;width:${a[1]/mx*100}%"></span></span><span class="mval">${fmtTok(a[1])}</span></div>`).join('')
      ||'<span style="color:var(--sub);font-size:12px">暂无数据</span>';
    // 趋势
    const tarr=darr.slice(-30);
    const dmax=Math.max(1,...tarr.map(a=>a[1]));
    $('su-trend').innerHTML=tarr.map(a=>`<div class="bar" style="height:${Math.max(3,a[1]/dmax*100)}%" data-tip="${a[0]}：${fmtTok(a[1])}"><span class="d">${a[0].slice(5)}</span></div>`).join('')
      ||'<span style="color:var(--sub)">暂无数据</span>';
    sumData={range:sumDays>=3650?'全部':('近'+sumDays+'天'),total,sess:ss.length,
      active:dayset.size,models:marr.length,peak:peak?peak[0]+'（'+fmtTok(peak[1])+'）':'无',
      grow:gt,top:marr.slice(0,3)};
  }catch(e){toast('汇总加载失败')}
}
function copyReport(){
  if(!sumData){toast('数据还没加载好');return}
  const d=sumData;
  const lines=[
    '🔥 我的 WorkBuddy 使用战报（'+d.range+'）',
    '━━━━━━━━━━━━━━━',
    '⚡ 累计干掉 '+fmtTok(d.total)+' Tokens',
    '💬 一共 '+d.sess+' 个会话，活跃 '+d.active+' 天',
    '🤖 用过 '+d.models+' 个模型',
    '📈 单日最高峰：'+d.peak,
    '🚀 近7天环比：'+d.grow,
  ];
  if(d.top&&d.top.length){
    lines.push('━━━━━━━━━━━━━━━');
    d.top.forEach((a,i)=>lines.push((['🥇','🥈','🥉'][i]||'·')+' '+a[0]+'：'+fmtTok(a[1])+' Tokens'));
  }
  navigator.clipboard.writeText(lines.join('\n')).then(()=>toast('战报已复制，去吹牛逼吧'))
    .catch(()=>toast('复制失败，浏览器不支持剪贴板'));
}

/* ---------- 使用说明 ---------- */
function openHelp(){helpdlg.showModal()}

/* ---------- 日志 ---------- */
async function loadEvents(){
  const d=await (await fetch('/api/events')).json();
  $('evtlist').innerHTML=(d.events||[]).map(e=>
    `<div><span class="k-${e.kind[0]==='b'?'b':e.kind[0]==='r'?'r':e.kind[0]==='e'?'e':'i'}">[${esc(e.ts)} ${esc(e.kind)}]</span> ${esc(e.msg)}</div>`).join('')
    ||'<div style="text-align:center;color:var(--sub);padding:20px">暂无事件</div>';
}

loadPresets(); pollStatus();
setInterval(pollStatus,6000);
setInterval(()=>{if($('page-usage').classList.contains('on'))loadUsage()},30000);
</script>
</body>
</html>"""


# ---------------------------------------------------------------- HTTP 服务
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/presets":
            st = _load_state()
            pend = None
            lts = st.get("last_switch_ts")
            if lts and not st.get("switch_ack"):
                pend = {"name": st.get("last_switch_name", ""),
                        "time": datetime.fromtimestamp(lts / 1000).strftime("%H:%M:%S")}
            with _state_lock:
                snap_t = _state.get("last_good_time")
            self._json({"presets": load_presets(), "current": current_models(),
                        "last_snapshot": snap_t, "pending_switch": pend,
                        "official_ids": load_official_ids(),
                        "model_mode": st.get("model_mode", "single")})
        elif path == "/api/official-models":
            models, info = load_official_catalog()
            if models:
                self._json({"ok": True, "models": models, "via": "WorkBuddy 云端配置缓存"})
            else:
                self._json({"ok": False, "error": info})
        elif path == "/api/events":
            with _state_lock:
                evs = list(_state["events"])
            self._json({"events": evs})
        elif path == "/api/wb-status":
            self._json({"running": wb_running()})
        elif path == "/api/usage":
            q = {}
            for part in self.path.split("?")[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    q[k] = v
            try:
                days = min(max(int(q.get("days", 30)), 1), 3650)
            except ValueError:
                days = 30
            frm = int(q["from"]) if q.get("from", "").isdigit() else None
            to = int(q["to"]) if q.get("to", "").isdigit() else None
            try:
                self._json(get_usage(days=days, frm=frm, to=to))
            except sqlite3.Error as e:
                self._json({"error": "数据库读取失败: %r" % e}, 500)
        elif path == "/api/requests":
            q = {}
            for part in self.path.split("?")[1:]:
                if "=" in part:
                    k, v = part.split("=", 1)
                    q[k] = v
            try:
                days = min(max(int(q.get("days", 30)), 1), 3650)
            except ValueError:
                days = 30
            frm = int(q["from"]) if q.get("from", "").isdigit() else None
            to = int(q["to"]) if q.get("to", "").isdigit() else None
            self._json(get_requests(days=days, frm=frm, to=to))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        payload = self._body()
        path = self.path.split("?")[0]
        if path == "/api/save-official":
            ids = payload.get("ids")
            if not isinstance(ids, list):
                self._json({"ok": False, "error": "参数错误"})
            else:
                save_official_ids([str(i) for i in ids])
                log_event("info", "已更新官方常用模型（%d 个）" % len(ids))
                self._json({"ok": True})
        elif path == "/api/switch":
            if payload.get("official"):
                # 切回官方：清空自定义模型列表，WorkBuddy 自动回落到内置模型
                apply_preset([], "官方内置模型")
                self._json({"ok": True})
                return
            presets = load_presets()
            i = payload.get("index")
            if not isinstance(i, int) or not (0 <= i < len(presets)):
                self._json({"error": "预设不存在"}, 400)
                return
            try:
                st = _load_state()
                mode = st.get("model_mode", "single")
                models = all_preset_models(presets, i) if mode == "all" else presets[i].get("models", [])
                apply_preset(models, presets[i].get("name", ""), mode)
                st = _load_state()
                st["active_preset_index"] = i
                _save_state(st)
                self._json({"ok": True, "mode": mode, "models": len(models)})
            except OSError as e:
                self._json({"error": repr(e)}, 500)
        elif path == "/api/model-mode":
            mode = payload.get("mode")
            if mode not in ("all", "single"):
                self._json({"ok": False, "error": "mode 必须是 all 或 single"}, 400)
                return
            presets = load_presets()
            if not presets:
                self._json({"ok": False, "error": "没有可注入的第三方预设"}, 400)
                return
            st = _load_state()
            i = st.get("active_preset_index")
            if not isinstance(i, int) or not (0 <= i < len(presets)):
                i = infer_active_preset_index(presets)
            models = all_preset_models(presets, i) if mode == "all" else [dict(m) for m in presets[i].get("models", [])]
            try:
                name = ("所有第三方模型（默认：%s）" if mode == "all" else "%s（单模型）") % presets[i].get("name", "")
                apply_preset(models, name, mode)
                st = _load_state()
                st["active_preset_index"] = i
                _save_state(st)
                msg = "已注入 %d 个第三方模型" % len(models) if mode == "all" else "已恢复单模型：%s" % presets[i].get("name", "")
                self._json({"ok": True, "mode": mode, "models": len(models), "message": msg})
            except OSError as e:
                self._json({"ok": False, "error": repr(e)}, 500)
        elif path == "/api/import-current":
            cur = current_models()
            if not cur:
                self._json({"error": "当前配置为空"}, 400)
                return
            presets = load_presets()
            name = "当前配置 " + datetime.now().strftime("%m-%d %H:%M")
            presets.append({"name": name, "models": cur})
            save_presets(presets)
            log_event("info", "当前配置已导入为预设: %s" % name)
            self._json({"ok": True})
        elif path == "/api/save-preset":
            presets = load_presets()
            entry = {"name": payload.get("name", "未命名"), "models": [payload.get("model", {})]}
            idx = payload.get("index")
            if isinstance(idx, int) and 0 <= idx < len(presets):
                presets[idx] = entry
            else:
                presets.append(entry)
            try:
                save_presets(presets)
                st = _load_state()
                if st.get("model_mode") == "all":
                    active = st.get("active_preset_index", 0)
                    if not isinstance(active, int) or not (0 <= active < len(presets)):
                        active = 0
                    models = all_preset_models(presets, active)
                    apply_preset(models, "所有第三方模型（已刷新）", "all")
                    st = _load_state(); st["active_preset_index"] = active; _save_state(st)
                self._json({"ok": True})
            except OSError as e:
                self._json({"error": repr(e)}, 500)
        elif path == "/api/del-preset":
            presets = load_presets()
            i = payload.get("index")
            if isinstance(i, int) and 0 <= i < len(presets):
                presets.pop(i)
            save_presets(presets)
            self._json({"ok": True})
        elif path == "/api/fetch-models":
            ids, details, info = fetch_provider_models(payload.get("url", ""), payload.get("key", ""))
            if ids:
                self._json({"ok": True, "models": ids, "details": details, "via": info})
            else:
                self._json({"ok": False, "error": info})
        elif path == "/api/launch":
            ok, msg = launch_wb()
            self._json({"ok": ok, "error": None if ok else msg})
        elif path == "/api/restart":
            ok, msg = restart_wb()
            self._json({"ok": ok, "error": None if ok else msg})
        elif path == "/api/dismiss-restart":
            st = _load_state()
            st["switch_ack"] = True
            _save_state(st)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)


def _kill_old_panel(listen_port=5276):
    """杀掉占用 listen_port 的旧面板实例（仅限 python 进程）。

    端口被占会让新代码无法生效——"改了代码面板却不更新"的根源。
    启动时先接管端口，保证启动脚本永远加载最新代码。
    """
    me = os.getpid()
    pids = set()
    if IS_WIN:
        import ctypes
        TCP_TABLE_OWNER_PID_LISTENER = 3

        class ROW(ctypes.Structure):
            _fields_ = [("dwState", ctypes.c_ulong), ("dwLocalAddr", ctypes.c_ulong),
                        ("dwLocalPort", ctypes.c_ulong), ("dwRemoteAddr", ctypes.c_ulong),
                        ("dwRemotePort", ctypes.c_ulong), ("dwOwningPid", ctypes.c_ulong)]

        class TABLE(ctypes.Structure):
            _fields_ = [("dwNumEntries", ctypes.c_ulong), ("table", ROW * 512)]

        # 端口在网络字节序：5276 = 0x14A4 -> htons 0xA414
        want = ((listen_port & 0xFF) << 8) | ((listen_port >> 8) & 0xFF)
        size = ctypes.c_ulong(ctypes.sizeof(TABLE))
        tab = TABLE()
        iphlp = ctypes.windll.iphlpapi
        if iphlp.GetExtendedTcpTable(ctypes.byref(tab), ctypes.byref(size), 0,
                                     2, TCP_TABLE_OWNER_PID_LISTENER, 0) != 0:
            return None
        for i in range(tab.dwNumEntries):
            r = tab.table[i]
            if (r.dwLocalPort & 0xFFFF) == want:
                pids.add(r.dwOwningPid)

        def _is_python(pid):
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            buf = ctypes.create_unicode_buffer(520)
            ln = ctypes.c_ulong(520)
            name = ""
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(ln)):
                name = os.path.basename(buf.value or "").lower()
            k32.CloseHandle(h)
            return name.startswith("python")
    else:
        # macOS/Linux：lsof 找监听进程
        try:
            r = subprocess.run(["lsof", "-t", "-iTCP:%d" % listen_port, "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=8)
            pids = {int(x) for x in r.stdout.split() if x.strip().isdigit()}
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

        def _is_python(pid):
            try:
                r = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                                   capture_output=True, text=True, timeout=5)
                return "python" in (r.stdout or "").lower()
            except (OSError, subprocess.SubprocessError):
                return False

    for pid in pids:
        if pid == me or not pid:
            continue
        # 只杀 python（旧面板），避免误伤其他软件
        if _is_python(pid):
            try:
                if IS_WIN:
                    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
                else:
                    subprocess.run(["kill", "-9", str(pid)], capture_output=True)
                log_event("info", "已自动结束旧面板进程 pid=%d（端口接管）" % pid)
                return pid
            except OSError:
                pass
    return None


def main():
    guardian_init()
    threading.Thread(target=guardian_loop, daemon=True).start()
    port = 5276
    server = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        # 端口被旧实例占用：杀掉旧面板后重试（只杀 python 系进程）
        _kill_old_panel(port)
        time.sleep(1.0)
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            print("端口 5276 接管失败，退出")
            return
    url = "http://127.0.0.1:%d" % port
    log_event("info", "面板已启动: %s" % url)
    print("WB Switch 面板: %s  (Ctrl+C 退出)" % url)
    try:
        # 优先用 Chrome --app 模式打开，呈现独立桌面窗口效果
        chrome_candidates = []
        if IS_WIN:
            chrome_candidates = [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                os.path.join(HOME, r"AppData\Local\Google\Chrome\Application\chrome.exe"),
            ]
        elif IS_MAC:
            chrome_candidates = [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                os.path.join(HOME, "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            ]
        opened = False
        for c in chrome_candidates:
            if os.path.exists(c):
                subprocess.Popen([c, "--app=" + url, "--window-size=1180,760"],
                                 **({"start_new_session": True} if not IS_WIN else {}))
                opened = True
                break
        if not opened:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
