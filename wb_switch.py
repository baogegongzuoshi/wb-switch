# -*- coding: utf-8 -*-
"""
WB Switch v2.55 — WorkBuddy 供应商管理器 + 用量监控（跨平台：Windows / macOS）
样式仿 cc-switch：侧边栏 + 供应商卡片 + 启用按钮 + 用量多维筛选。

功能:
  1. 供应商预设管理: 多套配置保存/切换；「所有模型」开关可把全部第三方模型
     同时注入 WorkBuddy 模型列表（同名模型 ID 会互相覆盖，WorkBuddy 限制）
  2. 启动 / 重启 WorkBuddy 一键按钮，实时显示 WorkBuddy 运行状态
  3. 守护模式: 自动备份 models.json；丢失/损坏自动恢复；启动自动接管端口
  4. 用量监控: 时间 / 类别 / 供应商 / 模型筛选；缓存命中率趋势；
     供应商命中率汇总；费用按官方价计（国内模型 ¥ / 国外模型 $，价格表内置
     model_pricing.json，共 55 个官网正价模型）
  5. 内置使用说明弹窗（右上角「? 使用说明」）；功能更新时同步更新该说明
  6. 面板: http://127.0.0.1:5276
     启动: Windows 双击 WB Switch.exe（或 WB Switch.bat）；macOS 运行 ./wb-switch.command
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
# 官方模型就那么几个：写死一份快照（OFFICIAL_CATALOG_BUILTIN），启动即用；
# 「重新拉取」按钮再从 WorkBuddy 云端配置缓存（acc-product-config-v3*.json）
# 抓最新清单合并进 official_catalog.json。官方模型全部是中国模型，计费只按积分。
OFFICIAL_FILE = os.path.join(DATA_DIR, "official_selected.json")
OFFICIAL_CATALOG_FILE = os.path.join(DATA_DIR, "official_catalog.json")
OFFICIAL_CATALOG_TS = os.path.join(DATA_DIR, "official_catalog_ts.json")

# 官方模型快照（取自 WorkBuddy 云端公开配置 acc-product-config-v3；
# 已剔除 custom-local: 前缀的第三方自定义模型）。全部为中国模型，按积分计费。
# 「重新拉取官方模型」按钮可用云端缓存覆盖本清单；用户也可在官方模型页手动删除错误条目。
OFFICIAL_CATALOG_BUILTIN = [
    {"id": "fast-model", "name": "快速", "credits": "x0.21", "ctx": 300000, "vision": True, "tools": True, "reasoning": True, "default": True},
    {"id": "balanced-model", "name": "均衡", "credits": "x0.65", "ctx": 300000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "deep-model", "name": "极致", "credits": "x1.20", "ctx": 300000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "hy3", "name": "Hy3", "credits": "x0.00", "ctx": 192000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "hy3-x", "name": "Hy3", "credits": "x0.05", "ctx": 192000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "hy4-preview", "name": "Hy4 preview", "credits": "x0.29", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "hy4-preview-f", "name": "Hy4 preview", "credits": "x0.00", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "minimax-m2.5", "name": "MiniMax-M2.5", "credits": "x0.18", "ctx": 200000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-5v-turbo", "name": "GLM-5v-Turbo", "credits": "x0.71", "ctx": 200000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-5.3", "name": "GLM-5.3", "credits": "x0.79", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-5.3-flash", "name": "GLM-5.3-Flash", "credits": "x0.06", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-5.2", "name": "GLM-5.2", "credits": "x0.79", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-5.1", "name": "GLM-5.1", "credits": "x0.79", "ctx": 200000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-5.0-turbo", "name": "GLM-5.0-Turbo", "credits": "x0.95", "ctx": 200000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-4.6v", "name": "GLM-4.6V", "credits": "x0.11", "ctx": 128000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "kimi-k3-1", "name": "Kimi-K3", "credits": "x1.62", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "kimi-k2.8-preview", "name": "Kimi-K2.8-Preview", "credits": "x0.77", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "kimi-k2.7", "name": "Kimi-K2.7-Code", "credits": "x0.57", "ctx": 256000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "kimi-k2.6", "name": "Kimi-K2.6", "credits": "x0.52", "ctx": 256000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "kimi-k2.5", "name": "Kimi-K2.5", "credits": "x0.45", "ctx": 256000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "kimi-k2-thinking", "name": "Kimi-K2-Thinking", "credits": "x0.54", "ctx": 256000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "minimax-m3", "name": "MiniMax-M3", "credits": "x0.25", "ctx": 512000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "minimax-m2.7", "name": "MiniMax-M2.7", "credits": "x0.26", "ctx": 200000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "glm-4.6", "name": "GLM-4.6", "credits": "x0.23", "ctx": 168000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "deepseek-v4-flash", "name": "Deepseek-V4-Flash", "credits": "x0.17", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "deepseek-v4.1-flash", "name": "Deepseek-V4.1-Flash", "credits": "x0.03", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "deepseek-v4-pro", "name": "Deepseek-V4-Pro", "credits": "x0.51", "ctx": 1000000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "deepseek-v3-2-volc", "name": "DeepSeek-V3.2", "credits": "x0.29", "ctx": 96000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "deepseek-v3-1-volc", "name": "DeepSeek-V3-1-Terminus", "credits": "x0.52", "ctx": 96000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "deepseek-v3-1-lkeap", "name": "DeepSeek-V3-1", "credits": "x0.52", "ctx": 96000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "deepseek-v3-1", "name": "DeepSeek-V3.1", "credits": "x0.52", "ctx": 96000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "deepseek-v3-0324-lkeap", "name": "DeepSeek-V3-0324", "credits": "x0.52", "ctx": 112000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "deepseek-r1-0528-lkeap", "name": "DeepSeek-R1-0528", "credits": "", "ctx": 96000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "kimi-k2-instruct-taiji", "name": "Kimi-K2", "credits": "", "ctx": 31000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "completion-gf", "name": "completion-gf", "credits": "", "ctx": 200000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "default-1.1", "name": "Claude-3.7-Sonnet", "credits": "", "ctx": 200000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "hunyuan-3b", "name": "hunyuan-3b", "credits": "", "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "hunyuan-7b-dense", "name": "hunyuan-7b", "credits": "", "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "codewise-completions", "name": "codewise-completions", "credits": "", "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "codewise-rewrite", "name": "codewise-rewrite", "credits": "", "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "codewise-nes-a4-027-aide", "name": "codewise-nes-a4-027-aide", "credits": "", "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "codewise-jump", "name": "codewise-jump", "credits": "", "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "deepseek-r1-0528", "name": "deepseek-r1", "credits": "", "ctx": 96000, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "deepseek-v3-0324-taco-completion", "name": "deepseek-v3-0324", "credits": "", "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "deepseek-v3-0324", "name": "deepseek-v3", "credits": "", "ctx": 96000, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "default-1.2", "name": "Claude-4.0-Sonnet", "credits": "", "ctx": 200000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "hunyuan-2.0-instruct", "name": "Hunyuan-2.0-Instruct", "credits": "", "ctx": 128000, "vision": True, "tools": True, "reasoning": True, "default": False},
    {"id": "hunyuan-chat", "name": "Hunyuan-Turbos", "credits": "", "ctx": 128000, "vision": True, "tools": True, "reasoning": False, "default": False},
    {"id": "hunyuan-image-alpha", "name": "Hunyuan Image Alpha", "credits": None, "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
    {"id": "hunyuan-image-alpha-edit", "name": "Hunyuan Image Alpha Edit", "credits": None, "ctx": None, "vision": False, "tools": False, "reasoning": False, "default": False},
]


def load_official_catalog(pull=False):
    """官方模型目录：写死清单优先（pull=False 直接返回），
    pull=True 时尝试从云端配置缓存重新拉取并覆盖本地快照。
    返回 (models|None, 来源说明)。清单里排除 custom-local: 前缀（那是第三方自定义模型）。"""
    import glob
    if pull:
        cands = [os.path.join(WB_DIR, "cache", "acc-product-config-v3.json")]
        cands += glob.glob(os.path.join(WB_DIR, "cache", "conversation-product-spill",
                                        "acc-product-config-v3-*.json"))
        cands = [c for c in cands if os.path.isfile(c)]
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
                    if str(m["id"]).startswith("custom-local:"):
                        continue  # 第三方自定义模型混进云端缓存的，剔除
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
                if models:
                    try:
                        with open(OFFICIAL_CATALOG_FILE, "w", encoding="utf-8") as f:
                            json.dump(models, f, ensure_ascii=False, indent=1)
                        with open(OFFICIAL_CATALOG_TS, "w", encoding="utf-8") as f:
                            json.dump({"ts": time.time()}, f)
                    except OSError:
                        pass
                    return models, "已从云端配置重新拉取（%d 个官方模型）" % len(models)
        return None, "重新拉取失败：云端配置缓存里没有可用的官方模型目录（写死清单继续生效）"
    # 非拉取：用户手动删改过的本地快照优先，其次写死清单
    try:
        with open(OFFICIAL_CATALOG_FILE, "r", encoding="utf-8") as f:
            local = json.load(f)
        if isinstance(local, list) and local:
            return local, "本地快照"
    except (OSError, ValueError):
        pass
    return [dict(m) for m in OFFICIAL_CATALOG_BUILTIN], "内置清单（v2.50 写死）"


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


# ---------------------------------------------------------------- 在线更新价格表
def refresh_pricing_online():
    """调用当前供应商的 AI 模型查询最新官方价格，合并进本地价格表。
    返回 (ok, message)。"""
    import urllib.request
    models = current_models()
    if not models:
        return False, "失败原因：读不到当前供应商配置（models.json 为空或损坏），没有可用的模型来查询价格。"
    m = models[0]
    url, key, mid = m.get("url", ""), m.get("apiKey", ""), m.get("id", "")
    if not (url and key and mid):
        return False, "失败原因：当前供应商配置不完整（url/apiKey/模型 id 缺失）。"
    prompt = (
        "列出以下模型的官方公开价格（每百万 token，人民币或美元原币种，含缓存命中价）。"
        "只输出 JSON 数组，不要其他文字，格式："
        '[{"id":"模型id","in":输入价,"out":输出价,"cache":缓存命中价,"currency":"CNY或USD"}]。'
        "模型清单：%s。价格不确定的模型跳过，不要编造。" % ", ".join(
            sorted({r.get("name", "") for r in _load_pricing().values()})[:60]))
    body = json.dumps({"model": mid, "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": 4096}).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        # 剥掉可能的 ```json 包裹
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        arr = json.loads(text[text.index("["):text.rindex("]") + 1])
        pricing = _load_pricing()
        added = updated = 0
        for it in arr:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            k = str(it["id"]).lower()
            if not all(isinstance(it.get(f), (int, float)) and it.get(f, 0) >= 0
                       for f in ("in", "out")):
                continue
            if k in pricing:
                updated += 1
            else:
                added += 1
            pricing[k] = {"name": it.get("id"), "in": float(it["in"]),
                          "out": float(it["out"]), "cache": float(it.get("cache") or 0)}
        tmp = CCS_PRICING_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(pricing, f, ensure_ascii=False, indent=1)
        shutil.move(tmp, CCS_PRICING_FILE)
        _CC_PRICING.update(ts=0)  # 失效缓存立即重载
        return True, "已获取并合并 %d 条价格（新增 %d，更新 %d），价格表现有 %d 个模型。" % (
            len(arr), added, updated, len(pricing))
    except Exception as e:
        return False, "失败原因：%s（供应商 %s / 模型 %s 查询未成功，检查网络或供应商余额后重试）" % (
            repr(e)[:160], m.get("name", "?"), mid)


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
      裸 id 匹配不上会回落 fast-model（"快速"）。
    - 运行时 id 全链统一带前缀：settings.json / 会话 JSONL providerData 都是 custom-local:xxx；
      只有 workbuddy.db sessions.model 列存裸 id（原生惯例）。此前"碰巧能用"是因为
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
# v2.58 渠道判据（确定性，不再猜）：
#   billed  = rawUsage.credit 存在 -> WorkBuddy 官方计费网关返回的实扣积分字段，
#             纯第三方直连 API 从不返回该字段；官方模型的请求轮基本都有
#   custom  = requestModelId 带 custom-local: 前缀 -> WorkBuddy「自定义模型」注入，
#             必为第三方
_req_cache = {"files": {}, "ver": 0, "lock": threading.Lock()}
_projects_dir = os.path.join(os.path.expanduser("~"), ".workbuddy", "projects")


def scan_requests():
    """扫描全部会话 jsonl，返回按时间排序的请求记录（按文件 mtime 增量缓存）"""
    records = []
    with _req_cache["lock"]:
        # 缓存结构版本：字段集变化（如 v2.58 加 billed）时 +1，旧缓存整体失效
        if _req_cache.get("ver") != 2:
            _req_cache["files"] = {}
            _req_cache["ver"] = 2
        seen_paths = set()
        for dirpath, _dirs, filenames in os.walk(_projects_dir):
            for fn in filenames:
                if not fn.endswith(".jsonl"):
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    mtime = os.path.getmtime(p)
                except OSError:
                    continue
                seen_paths.add(p)
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
                                "pname": pd.get("requestModelName") or "",
                                "inp": inp, "out": out, "cached": cached,
                                # v2.58 渠道确定性判据：
                                # billed：rawUsage.credit 存在 = 官方计费网关实扣（第三方直连 API 无此字段）
                                # custom：模型 id 带 custom-local: 前缀 = 自定义第三方模型
                                "billed": 1 if isinstance(pd.get("rawUsage"), dict) and "credit" in pd["rawUsage"] else 0,
                                # conversationRequestId 去横线 = session_usage.credit_json 的键（实测验证），
                                # 官方请求据此精确挂上单条扣费积分
                                "creq": (pd.get("conversationRequestId") or pd.get("traceId") or "").replace("-", ""),
                                # 前端展开费用公式的稳定行 id（ messageId 去横线，缺省用 时间+token 兜底 ）
                                "rid": (pd.get("messageId") or ("%s_%s_%s" % (obj.get("timestamp") or 0, inp, out))).replace("-", "")[:24],
                            })
                except OSError:
                    pass
                # v2.58 空预设名补全（时间就近继承）：WorkBuddy 偶发漏写 requestModelName。
                # 同一会话同一模型 id 的渠道不会跳变，空名行继承「时间上最近的非空名行」
                # 的预设名（前后取时间更近者）。不能按模型 id 全局取值——长会话中途
                # 切换过供应商时，全局取值会把旧供应商的名字错安到新时段头上。
                _named = [(i, r["ts"], r["pname"]) for i, r in enumerate(recs) if r.get("pname")]
                for i, r in enumerate(recs):
                    if r.get("pname"):
                        continue
                    _prev = _next = None
                    for j, ts, pn in reversed(_named):
                        if j < i:
                            _prev = (ts, pn)
                            break
                    for j, ts, pn in _named:
                        if j > i:
                            _next = (ts, pn)
                            break
                    if _prev and _next:
                        r["pname"] = _prev[1] if (r["ts"] - _prev[0]) <= (_next[0] - r["ts"]) else _next[1]
                    elif _prev:
                        r["pname"] = _prev[1]
                    elif _next:
                        r["pname"] = _next[1]
                _req_cache["files"][p] = (mtime, recs)
                records.extend(recs)
        # 会话文件被删除后，其缓存条目永远残留（累积泄漏）；一轮扫描结束顺手清掉
        for gone in [p for p in _req_cache["files"] if p not in seen_paths]:
            del _req_cache["files"][gone]
    records.sort(key=lambda r: r["ts"])
    return records


def bare_id(model_id):
    """剥掉运行时 custom-local: 前缀，拿裸模型 id（比对/聚合/展示统一用它）。"""
    mid = model_id or ""
    return mid[len("custom-local:"):] if mid.startswith("custom-local:") else mid


# 历史供应商名关键词 -> 统一简称（jsonl 的 requestModelName 是请求当时的预设名）
_PNAME_KW = (("volces", "火山方舟"), ("openrouter", "OpenRouter"))


def _vendor_from_pname(pname):
    p = (pname or "").lower()
    for kw, name in _PNAME_KW:
        if kw.lower() in p:
            return name
    return None


def _preset_vendor_names():
    """预设名 -> 供应商标签（用预设内模型的 apiKey/url 识别厂商）。"""
    m = {}
    for p in load_presets():
        v = None
        for mod in p.get("models", []):
            v = _vendor_label(mod)
            if v:
                break
        if p.get("name"):
            m[p["name"]] = v or p["name"]
    return m


def _official_catalog_ids():
    """官方模型 id 集合（写死清单/本地快照，剔除 custom-local: 前缀）。"""
    models, _src = load_official_catalog()
    ids = set()
    for m in (models or []):
        mid = str(m.get("id") or "")
        if mid and not mid.startswith("custom-local:"):
            ids.add(mid.lower())
    return ids


# v2.63 模型名守卫：官方目录 + 所有预设/当前配置里出现过的模型 id（小写）。
# 用户会把预设命名成模型名（如 "Hy3"、"GLM-5.3-Flash"），这种名字一旦被
# 当成供应商标签就会凭空造出假厂商（倍率页冒出 "Hy3 供应商"）。守卫集合
# 缓存 5s，预设增删后自动刷新。
_mname_guard_cache = {"ts": 0.0, "ids": None}


def _model_name_guard_ids():
    now = time.time()
    if _mname_guard_cache["ids"] is None or now - _mname_guard_cache["ts"] > 5:
        ids = _official_catalog_ids()
        try:
            for m in current_models():
                if m.get("id"):
                    ids.add(str(m["id"]).lower())
        except Exception:
            pass
        try:
            for p in load_presets():
                for m in p.get("models", []):
                    if m.get("id"):
                        ids.add(str(m["id"]).lower())
        except Exception:
            pass
        _mname_guard_cache["ids"] = ids
        _mname_guard_cache["ts"] = now
    return _mname_guard_cache["ids"]


def _pname_is_model_name(pname):
    """判断预设名是否其实是模型名。模型名绝不能当供应商标签。"""
    p = bare_id(pname or "").strip().lower()
    return bool(p) and p in _model_name_guard_ids()


# _provider_map 按预设实时重建，逐请求调用太贵；缓存 5s（预设变更很快生效）
_pm_cache = {"ts": 0.0, "data": None}


def _provider_map_cached():
    now = time.time()
    if _pm_cache["data"] is None or now - _pm_cache["ts"] > 5:
        try:
            _pm_cache["data"] = _provider_map()
        except Exception:
            _pm_cache["data"] = {}
        _pm_cache["ts"] = now
    return _pm_cache["data"]


def _classify_request(pname, model_id, pname_map, aliases, billed=0, raw_model=""):
    """逐请求判定 (供应商, 官方/第三方)。

    v2.58 重构：彻底废弃「历史归属反推」（多供应商共用同一模型 id 时必然
    跨渠道污染——同一模型 id 被多个渠道使用，猜必错），
    只用 WorkBuddy 写进日志的确定性标识：

    判据优先级：
    0) raw_model 带 custom-local: 前缀 -> 自定义第三方模型，必为第三方。
       供应商标签：pname 精确匹配预设 -> 厂商关键词 -> 模型实际配置识别
       （apiKey/url）；预设名若是模型名（v2.63 守卫）严禁当厂商
    1) billed=1（rawUsage.credit 存在 = 官方计费网关实扣字段，
       第三方直连 API 从不返回）-> 官方
    2) pname 精确等于当前预设名 -> 该预设配置的厂商（按 url/key 识别）
    3) pname 含第三方厂商关键词 -> 该厂商
    4) 无任何第三方标识 -> 官方
    """
    is_custom = raw_model.startswith("custom-local:") if raw_model else (model_id or "") != bare_id(model_id)
    if is_custom:
        if pname:
            if pname in pname_map:
                return pname_map[pname], "third"
            v = _vendor_from_pname(pname)
            if v:
                return v, "third"
            # v2.63 守卫：预设名是模型名（如官方模型 hy3/glm-5.3-flash 被
            # 拿去当预设名），绝不能把模型名当供应商标签——回退到该模型
            # 实际配置（apiKey/url）识别的厂商；识别不出落"自定义"。
            if _pname_is_model_name(pname):
                pm = _provider_map_cached()
                v = pm.get(raw_model) or pm.get(bare_id(model_id))
                if v and not _pname_is_model_name(v):
                    return v, "third"
                return "自定义", "third"
            return pname, "third"  # 预设名本身即标签（自定义供应商名）
        return "未知", "third"
    if billed:
        return "官方", "official"
    if pname:
        if pname in pname_map:
            return pname_map[pname], "third"
        v = _vendor_from_pname(pname)
        if v:
            return v, "third"
    return "官方", "official"


def _learned_aliases():
    """【已废弃 v2.58】历史归属反推在多供应商共用同一模型 id 时必然跨渠道污染
    （同一模型 id 被多个渠道使用，猜必错），不再使用。
    分类改用确定性判据（custom-local: 前缀 / rawUsage.credit），本函数恒返回空表。"""
    return {}


def _req_money(inp, cached, out, pop):
    """单条请求按官方正价分项算钱（输入正价/缓存命中/输出分开，¥）。
    pop 无价格（模型无官方价）返回 None，由上层用平均单价折算。"""
    if not pop or (not pop.get("in") and not pop.get("out") and not pop.get("cache")):
        return None
    _cached = min(cached, inp)
    _plain = max(inp - _cached, 0)
    return (_plain / 1e6 * float(pop.get("in", 0))
            + out / 1e6 * float(pop.get("out", 0))
            + _cached / 1e6 * float(pop.get("cache", 0)))


def _official_money_ctx(off_items, credit_by_creq, pricing):
    """v2.69 官方积分「钱分摊」上下文（用户口径：两边都算成钱，金额占比 × 积分）。
    off_items: [(r, b)] 官方请求列表（窗口内）。
    返回 dict：
      eff      与 off_items 对齐的每条折算金额（¥）：有官方价=正价分项金额；
               无官方价=token × 窗口平均单价（简单平均，不乱算）
      avg      窗口平均单价（¥/token，仅统计有官方价的请求）
      coef     系数（元/积分）= Σ有实扣轮折算金额 ÷ Σ该轮实扣积分
      round_m  creq -> 该轮折算金额合计（全部轮）
      round_mm creq -> {model: 折算金额}（全部轮）
      round_cr creq -> 该轮实扣积分（仅有实扣轮）
    """
    # 第一遍：每条的钱 + 平均价
    eff = []
    money_sum = 0.0
    tok_sum = 0
    for r, b in off_items:
        pop = _price_for_raw(b, pricing) or {}
        m = _req_money(r["inp"], r["cached"], r["out"], pop)
        if m is None:
            eff.append(None)
        else:
            eff.append(m)
            money_sum += m
            tok_sum += r["inp"] + r["out"]
    avg = (money_sum / tok_sum) if tok_sum > 0 else 0.0
    # 第二遍：无官方价的用平均单价折算；逐轮归集
    eff2 = []
    round_m = {}
    round_mm = {}
    for (r, b), m in zip(off_items, eff):
        c = r.get("creq") or ""
        if m is None:
            # 无官方价：token × 窗口平均单价（¥/token；简单平均，不搞其他花样）
            m = (r["inp"] + r["out"]) * avg
        eff2.append(m)
        if c:
            round_m[c] = round_m.get(c, 0.0) + m
            d = round_mm.setdefault(c, {})
            d[b] = d.get(b, 0.0) + m
    # 系数：分子=有实扣轮的折算金额，分母=该轮实扣积分
    num = 0.0
    den = 0.0
    for c, cr in credit_by_creq.items():
        if c in round_m and cr > 0:
            num += round_m[c]
            den += cr
    coef = (num / den) if den > 0 else 0.07
    return {"eff": eff2, "avg": avg, "coef": coef, "round_m": round_m,
            "round_mm": round_mm}


def get_requests(days=30, frm=None, to=None):
    # 结果缓存：同一时间窗 20s 内直接返回上次结果（倍率变更会 bump 版本号）
    key = "%s_%s_%s" % (days, frm, to)
    with _result_lock:
        if (_result_cache["data"] and _result_cache["key"] == key
                and _result_cache["ver"] == _result_ver[0]
                and (time.time() - _result_cache["ts"]) < RESULT_CACHE_TTL):
            return _result_cache["data"]
    recs = scan_requests()
    now_ms = int(time.time() * 1000)
    if frm is not None:
        lo = int(frm)
    else:
        # 与 get_usage 一致：N天 = 从 N-1 天前的本地零点起算（自然日口径）
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        lo = int((midnight - timedelta(days=days - 1)).timestamp() * 1000)
    hi = int(to) if to is not None else now_ms + 86400000
    # 后端兜底钳制（旧版缓存页面会把「昨天」结束时间算成 +1000 天）：跨度超 400 天视为异常，
    # 按 from 起算的 days 个自然日窗口重算，保证旧页面打开也不会显示错误数据。
    if frm is not None and to is not None and (hi - lo) > 400 * 864e5:
        _s = datetime.fromtimestamp(lo / 1000)
        _e = _s + timedelta(days=days - 1) if days > 1 else _s
        lo = int(datetime(_s.year, _s.month, _s.day, 0, 0, 0).timestamp() * 1000)
        hi = int(datetime(_e.year, _e.month, _e.day, 23, 59, 59, 999000).timestamp() * 1000)
    pmap = _provider_map()
    pricing = _load_pricing()
    win = [r for r in recs if lo <= r["ts"] <= hi]
    # 逐请求精确积分：credit_json 键 = conversationRequestId 去横线（实测验证）。
    # 一个键 = 一轮对话（agent 多步子请求共享同一 conversationRequestId），值 = 整轮扣费。
    # 先全量扫一遍算出每个 creq 的 token 总量，再把积分按子请求 token 占比分摊到每行。
    credit_by_creq = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % WB_DB.replace("\\", "/"), uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT credit_json FROM session_usage WHERE credit_json IS NOT NULL")
            for (cj,) in cur.fetchall():
                try:
                    for k, v in json.loads(cj).items():
                        credit_by_creq[k] = credit_by_creq.get(k, 0.0) + float(v)
                except (ValueError, TypeError):
                    continue
        finally:
            con.close()
    except sqlite3.Error:
        pass
    creq_tok = {}
    for r in recs:
        c = r.get("creq") or ""
        if c in credit_by_creq:
            creq_tok[c] = creq_tok.get(c, 0) + r["inp"] + r["out"]
    pname_map = _preset_vendor_names()
    of_coef = None      # v2.69 起废弃（留名防止旧引用崩溃）
    gr_money = None     # v2.69 钱·分摊上下文（懒计算一次，与 get_official 同口径）
    gr_idx = None       # creq -> 官方请求序列中该轮首条下标（与 gr_money["eff"] 对齐，随 gr_money 懒构建）
    out = []
    for r in win:
        r2 = dict(r)
        b = bare_id(r["model"])
        r2["model"] = b
        r2["provider"], r2["cls"] = _classify_request(
            r.get("pname") or "", b, pname_map, None,
            billed=r.get("billed", 0), raw_model=r.get("model") or "")
        r2["hit"] = round(r["cached"] * 100.0 / r["inp"], 1) if r["inp"] else 0
        # 费用计算明细（点击费用单元格展开公式）：官方价 × 该「供应商+模型」倍率
        p = _price_for(r2["provider"], b, pricing) if r2["cls"] == "third" else None
        if p:
            _cached = min(r["cached"], r["inp"])
            _plain = max(r["inp"] - _cached, 0)
            r2["pin"] = float(p.get("in", 0))
            r2["pout"] = float(p.get("out", 0))
            r2["pcache"] = float(p.get("cache", 0))
            r2["plain"] = _plain
            # 官方原始单价（价格表正价）+ 实际倍率，费用公式里分开两行展示
            pop = _price_for_raw(b, pricing) or {}
            r2["opin"] = float(pop.get("in", 0))
            r2["opout"] = float(pop.get("out", 0))
            r2["opcache"] = float(pop.get("cache", 0))
            r2["omult"] = float(p.get("_mult", 1.0))
        else:
            r2["pin"] = r2["pout"] = r2["pcache"] = 0.0
            r2["plain"] = max(r["inp"] - min(r["cached"], r["inp"]), 0)
        # 官方请求：v2.69 钱·分摊口径——实扣轮积分按本条金额占轮金额比分摊；
        # 估算轮 = 本条金额 ÷ 系数（两边都算成钱，与 get_official 同口径）。
        cr = 0.0
        if r2["cls"] == "official":
            c = r.get("creq") or ""
            if c in credit_by_creq:
                r2["cr_round"] = round(credit_by_creq[c], 4)  # 该轮总积分（实扣）
                if gr_money is None:
                    _off = [(x, bare_id(x["model"])) for x in win
                            if _classify_request(x.get("pname") or "", bare_id(x["model"]),
                                                 pname_map, None, billed=x.get("billed", 0),
                                                 raw_model=x.get("model") or "")[1] == "official"]
                    gr_money = _official_money_ctx(_off, credit_by_creq, pricing)
                    gr_idx = {}
                    for _i, (_x, _b2) in enumerate(_off):
                        _c2 = _x.get("creq") or ""
                        if _c2 and _c2 not in gr_idx:
                            gr_idx[_c2] = _i
                m = gr_money["eff"][gr_idx[c]]
                rm = gr_money["round_m"].get(c, 0.0)
                cr = credit_by_creq[c] * (m / rm) if rm > 0 else 0.0
                r2["cr_money"] = round(rm, 6)
                r2["money"] = round(m, 6)
                r2["credit_est"] = False
            else:
                # 无实扣记录：本条按官方正价分项算钱，再 ÷ 数据推导系数
                if gr_money is None:
                    _off = [(x, bare_id(x["model"])) for x in win
                            if _classify_request(x.get("pname") or "", bare_id(x["model"]),
                                                 pname_map, None, billed=x.get("billed", 0),
                                                 raw_model=x.get("model") or "")[1] == "official"]
                    gr_money = _official_money_ctx(_off, credit_by_creq, pricing)
                    gr_idx = {}
                    for _i, (_x, _b2) in enumerate(_off):
                        _c2 = _x.get("creq") or ""
                        if _c2 and _c2 not in gr_idx:
                            gr_idx[_c2] = _i
                m = gr_money["eff"][gr_idx[c]]
                cr = m / gr_money["coef"] if gr_money["coef"] > 0 else 0.0
                r2["credit_est"] = True
                r2["est_coef"] = round(gr_money["coef"], 6)
                r2["money"] = round(m, 6)
        r2["credit"] = round(cr, 4)
        r2["cost"] = round(_est_cost(r2["provider"], b, r["inp"], r["out"], r["cached"], pricing, r["ts"]), 5) if r2["cls"] == "third" else 0.0
        r2["currency"] = _currency_for(b)
        out.append(r2)
    # 按模型聚合统计
    agg = {}
    for r in out:
        a = agg.setdefault((r["provider"], r["model"]),
                           {"model": r["model"], "provider": r["provider"], "cls": r["cls"],
                            "reqs": 0, "inp": 0, "cached": 0, "out": 0,
                            "cost": 0.0, "credit": 0.0, "credit_est_n": 0,
                            "credit_real_n": 0, "currency": r["currency"]})
        a["reqs"] += 1
        a["inp"] += r["inp"]
        a["cached"] += r["cached"]
        a["out"] += r["out"]
        a["cost"] += r.get("cost", 0.0)
        a["credit"] += r.get("credit", 0.0)
        if r["cls"] == "official":
            if r.get("credit_est"):
                a["credit_est_n"] += 1
            else:
                a["credit_real_n"] += 1
    stats = []
    for a in agg.values():
        a["hit"] = round(a["cached"] * 100.0 / a["inp"], 1) if a["inp"] else 0
        a["cost"] = round(a["cost"], 4)
        a["credit"] = round(a["credit"], 2)
        stats.append(a)
    stats.sort(key=lambda a: -(a["inp"] + a["out"]))
    data = {"requests": out, "stats": stats}
    with _result_lock:
        _result_cache.update(data=data, key=key, ts=time.time(), ver=_result_ver[0])
    return data


def get_requests_slim(data, limit=2000):
    """明细瘦身：日志页每页最多 100 条且最新在前，2000 条足够滚动回看；
    展开公式用的字段保留（rid/creq/cr_round/cr_tok 只在展开行需要，保留），
    删掉纯冗余的 session 字段。stats 聚合行保持全量不动。"""
    out = data.get("requests") or []
    n_total = len(out)
    if n_total > limit:
        out = out[-limit:]
    slim = [{k: v for k, v in r.items() if k != "session"} for r in out]
    return {"requests": slim, "stats": data.get("stats") or [],
            "total": n_total}


def get_official(days=30, frm=None, to=None):
    """官方模型成本一览（v2.47）：按「对话轮」汇总官方模型用量。
    - 轮 = conversationRequestId 去横线（credit_json 的键）；一轮 = 一次对话，
      agent 多步子请求共享同一轮，积分按整轮扣一次（不算分摊成每次请求）。
    - 每模型积分 = 该模型在各轮 token 占比 × 轮积分（多模型同轮时按占比归集，
      各模型积分之和 = 轮积分，不重不漏）。
    - 相当于多少钱（v2.67 数据推导系数口径）：
      ① 先按官方正价把输入/输出/缓存命中分开算出总成本（免费模型扣 0 积分，不进分子）；
      ② 系数 = 官方正价总成本 ÷ 实扣总积分（元/积分，随时间窗实测变化，不再是硬编码 0.07）；
      ③ 实际积分价值 = 实扣积分 × 系数。官方正价成本仅作参考展示。
    - 估算轮（无实扣记录）的积分 = token × 官方正价（输入/输出/缓存分列）÷ 系数。
    """
    key = "%s_%s_%s" % (days, frm, to)
    with _result_lock:
        if (_result_cache.get("data_of") and _result_cache.get("key_of") == key
                and _result_cache.get("ver_of") == _result_ver[0]
                and (time.time() - _result_cache.get("ts_of", 0)) < RESULT_CACHE_TTL):
            return _result_cache["data_of"]
    recs = scan_requests()
    if frm is not None:
        lo = int(frm)
    else:
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        lo = int((midnight - timedelta(days=days - 1)).timestamp() * 1000)
    hi = int(to) if to is not None else int(time.time() * 1000) + 86400000
    # 后端兜底钳制（同 get_requests：旧页面昨天窗口 +1000 天异常）
    if frm is not None and to is not None and (hi - lo) > 400 * 864e5:
        _s = datetime.fromtimestamp(lo / 1000)
        _e = _s + timedelta(days=days - 1) if days > 1 else _s
        lo = int(datetime(_s.year, _s.month, _s.day, 0, 0, 0).timestamp() * 1000)
        hi = int(datetime(_e.year, _e.month, _e.day, 23, 59, 59, 999000).timestamp() * 1000)
    # credit_json：键 = conversationRequestId 去横线，值 = 该轮实际扣的积分
    credit_by_creq = {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % WB_DB.replace("\\", "/"), uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT credit_json FROM session_usage WHERE credit_json IS NOT NULL")
            for (cj,) in cur.fetchall():
                try:
                    for k, v in json.loads(cj).items():
                        credit_by_creq[k] = credit_by_creq.get(k, 0.0) + float(v)
                except (ValueError, TypeError):
                    continue
        finally:
            con.close()
    except sqlite3.Error:
        pass
    pname_map = _preset_vendor_names()
    pricing = _load_pricing()
    off_items = []      # 窗口内官方请求 [(r, bare_model)]（v2.69 钱分摊口径用）
    # 逐轮逐模型聚合 token 与调用次数
    mod_creqs = {}      # model -> set(creq)
    mod_tok = {}        # model -> [inp, cached, out]
    round_tok = {}      # creq -> {model: tok}
    round_calls = {}    # creq -> {model: calls}
    req_log = []        # 官方请求日志（v2.67：扫描后统一组装）
    credit_by_creq = {}  # 重新在这里算一份（get_official 不依赖 get_requests 的缓存）
    try:
        con = sqlite3.connect("file:%s?mode=ro" % WB_DB.replace("\\", "/"), uri=True)
        try:
            cur = con.cursor()
            cur.execute("SELECT credit_json FROM session_usage WHERE credit_json IS NOT NULL")
            for (cj,) in cur.fetchall():
                try:
                    for k, v in json.loads(cj).items():
                        credit_by_creq[k] = credit_by_creq.get(k, 0.0) + float(v)
                except (ValueError, TypeError):
                    continue
        finally:
            con.close()
    except sqlite3.Error:
        pass
    for r in recs:
        if not (lo <= r["ts"] <= hi):
            continue
        b = bare_id(r["model"])
        prov, cls = _classify_request(
            r.get("pname") or "", b, pname_map, None,
            billed=r.get("billed", 0), raw_model=r.get("model") or "")
        if cls != "official":
            continue
        c = r.get("creq") or ""
        tok = r["inp"] + r["out"]
        mod_creqs.setdefault(b, set())
        mt = mod_tok.setdefault(b, [0, 0, 0])
        if c:
            mod_creqs[b].add(c)
            round_tok.setdefault(c, {})
            round_tok[c][b] = round_tok[c].get(b, 0) + tok
            rc = round_calls.setdefault(c, {})
            rc[b] = rc.get(b, 0) + 1
        mt[0] += r["inp"]; mt[1] += r["cached"]; mt[2] += r["out"]
        off_items.append((r, b))
    # ---- v2.69 钱·分摊口径（用户公式）----
    # 两边都算成钱：整轮按官方正价分项算出轮金额，本轮某条请求的积分
    # = 整轮实扣积分 ×（该条金额 ÷ 轮金额）。系数 = Σ有实扣轮金额 ÷ Σ该轮实扣积分。
    money_ctx = _official_money_ctx(off_items, credit_by_creq, pricing)
    coefficient = money_ctx["coef"]
    req_log = []
    for (r, b), m in zip(off_items, money_ctx["eff"]):
        c = r.get("creq") or ""
        pop = _price_for_raw(b, pricing) or {}
        has_price = bool(pop and (pop.get("in") or pop.get("out") or pop.get("cache")))
        cr = 0.0
        est = False
        if c and c in credit_by_creq:
            # 实扣轮：整轮积分 × 本条金额占轮金额比（钱分摊；金额即官方正价分项结果）
            rm = money_ctx["round_m"].get(c, 0.0)
            cr = credit_by_creq[c] * (m / rm) if rm > 0 else 0.0
        elif c:
            # 估算轮：本条金额 ÷ 系数
            est = True
            cr = m / coefficient if coefficient > 0 else 0.0
        req_log.append({"ts": r["ts"], "model": b, "inp": r["inp"], "cached": r["cached"],
                        "out": r["out"], "credit": round(cr, 4), "est": est,
                        "calls": round_calls.get(c, {}).get(b, 1) if c else 1,
                        "creq": c,
                        "cr_round": round(credit_by_creq[c], 4) if (c and c in credit_by_creq) else None,
                        "cr_money": round(money_ctx["round_m"].get(c, 0.0), 6) if (c and c in credit_by_creq) else None,
                        "money": round(m, 6),
                        "coef": round(coefficient, 6) if est else None,
                        "avg": round(money_ctx["avg"], 9),
                        "has_price": has_price,
                        "pin": float(pop.get("in", 0)), "pout": float(pop.get("out", 0)),
                        "pcache": float(pop.get("cache", 0))})
    rows, detail = [], {}
    all_creqs = set(round_tok.keys())
    tot = {"rounds": len(all_creqs), "credit": 0.0, "cny": 0.0, "usd": 0.0,
           "cny_free": 0.0, "usd_free": 0.0, "worth_cny": 0.0}
    for b in sorted(mod_tok.keys()):
        mt = mod_tok[b]
        cached = min(mt[1], mt[0])
        plain = max(mt[0] - cached, 0)
        # 该模型各轮积分（v2.69 钱分摊：按该模型金额占轮金额比归集；无实扣轮按系数折算）
        credit = 0.0
        for c in mod_creqs[b]:
            m_c = money_ctx["round_mm"].get(c, {}).get(b, 0.0)
            if c in credit_by_creq and credit_by_creq[c] > 0:
                rm = money_ctx["round_m"].get(c, 0.0)
                credit += credit_by_creq[c] * (m_c / rm) if rm > 0 else 0.0
            else:
                # 无实扣轮：估算积分 = 轮金额 ÷ 系数，再按金额占比归到该模型
                est_round = money_ctx["round_m"].get(c, 0.0) / coefficient if coefficient > 0 else 0.0
                credit += est_round * ((m_c / money_ctx["round_m"][c]) if money_ctx["round_m"].get(c) else 0.0)
        p = _price_for_raw(b, pricing)
        # 官方「积分倍率」（键 = 官方|模型id）：仅作判断标识（0=免费免积分，非0=消耗积分），
        # 不得参与任何费用/积分计算
        omult = _multiplier_at("官方", b)
        cost = 0.0
        if p:
            cost = (plain / 1e6 * p.get("in", 0) + mt[2] / 1e6 * p.get("out", 0)
                    + cached / 1e6 * p.get("cache", 0))
        cur = _currency_for(b)
        free = credit <= 1e-9
        # v2.67 实际积分价值 = 实扣积分 × 系数（系数 = 官方正价总成本 ÷ 实扣总积分，数据推导）
        worth = credit * coefficient
        row = {"model": b, "rounds": len(mod_creqs[b]), "credit": round(credit, 2),
               "inp": mt[0], "cached": cached, "out": mt[2],
               "cost": round(cost, 4), "worth": round(worth, 4), "currency": cur,
               "free": free, "plain": plain, "omult": omult}
        if p:
            row["pin"] = float(p.get("in", 0)); row["pout"] = float(p.get("out", 0))
            row["pcache"] = float(p.get("cache", 0))
        else:
            row["pin"] = row["pout"] = row["pcache"] = 0.0
        rows.append(row)
        # 每轮明细：该轮调了哪些模型、各多少次、整轮积分
        det = []
        for c in sorted(mod_creqs[b]):
            rc = round_calls.get(c, {})
            comps = [{"m": k, "n": v} for k, v in sorted(rc.items(), key=lambda x: -x[1])]
            det.append({"creq": c, "credit": round(credit_by_creq.get(c, 0.0), 2),
                        "comps": comps})
        detail[b] = det
        if free:
            tot["usd_free" if cur == "USD" else "cny_free"] += cost
        else:
            tot["usd" if cur == "USD" else "cny"] += cost
            tot["worth_cny"] += (worth if cur == "CNY" else worth * 7.2)
        tot["credit"] += credit
    rows.sort(key=lambda x: -(x["inp"] + x["out"]))
    tot["credit"] = round(tot["credit"], 2)
    tot["worth_cny"] = round(tot["worth_cny"], 2)
    tot["coefficient"] = round(coefficient, 6)
    for k in ("cny", "usd", "cny_free", "usd_free"):
        tot[k] = round(tot[k], 2)
    tot["cny_all"] = round(tot["cny"] + tot["usd"] * 7.2, 2)
    tot["cny_all_free"] = round(tot["cny_free"] + tot["usd_free"] * 7.2, 2)
    # 请求日志：最新在前，限 400 条（页面滚动够用）
    req_log.sort(key=lambda x: -x["ts"])
    data = {"ok": True, "rows": rows, "detail": detail, "totals": tot,
            "req_log": req_log[:400], "req_log_total": len(req_log)}
    with _result_lock:
        _result_cache.update(data_of=data, key_of=key, ts_of=time.time(), ver_of=_result_ver[0])
    return data

# ---------------------------------------------------------------- 用量统计
# 供应商识别：自定义模型按 API Key 前缀 + 接口域名判断真实厂商，
# 识别不出时用预设名/模型名兜底；官方模型不在映射里，显示"官方"。
_ARK_KEY_RE = re.compile(r"^ark-[0-9a-f]{8}", re.IGNORECASE)
_VENDOR_BY_DOMAIN = [
    ("volces.com", "火山方舟"), ("openrouter.ai", "OpenRouter"),
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
    其次各预设（历史用量按预设识别）。不在映射里的视为官方模型，显示"官方"。
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
MULTIPLIER_FILE = os.path.join(DATA_DIR, "model_multiplier.json")
_CC_PRICING = {"ts": 0.0, "data": None}
# 通用结果缓存：get_requests / get_official 等重聚合的结果按「时间窗+倍率版本」缓存。
# 历史请求数据不可变，每秒重算纯属浪费；TTL 与 usage 缓存一致（20s），
# 倍率保存时 bump 版本号使缓存立即失效。
RESULT_CACHE_TTL = 20
_result_cache = {"data": None, "key": None, "ts": 0.0, "ver": 0}
_result_ver = [0]   # 倍率/价格变更时 +1
_result_lock = threading.Lock()

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
        if not data:
            bundled = os.path.join(getattr(sys, "_MEIPASS",
                                           os.path.dirname(os.path.abspath(__file__))),
                                   "model_pricing.json")
            try:
                with open(bundled, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            except (OSError, ValueError):
                pass
    _CC_PRICING.update(ts=now, data=data)
    return data


def _mkey(provider, model_id):
    """倍率主键：供应商标签 + 裸模型 id（不同供应商同模型各算各的）。"""
    return "%s|%s" % (provider or "", _bare_model_id(model_id))


def _load_multipliers():
    """读模型倍率表。兼容两种格式：
    旧：{"供应商|模型id": float}
    新：{"供应商|模型id": {"cur": float, "hist": [[生效时间ms, 倍率], ...]}}
    hist 按 time 升序；查询时取「生效时间 <= 请求时间」的最后一段倍率（时间分段计费）。"""
    try:
        with open(MULTIPLIER_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _mult_value(v):
    """把倍率条目归一为 float（兼容旧格式 float / 新格式 dict）。"""
    if isinstance(v, dict):
        v = v.get("cur", 1.0)
    try:
        return float(v) if v is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def _mult_hist(v):
    """把倍率条目归一为时段列表 [(生效时间ms, 倍率), ...]（按时间升序）。
    旧格式（纯 float）视为从远古起一直生效的单一时段。"""
    if isinstance(v, dict):
        h = v.get("hist")
        if isinstance(h, list) and h:
            segs = []
            for it in h:
                if isinstance(it, (list, tuple)) and len(it) >= 2:
                    try:
                        segs.append((float(it[0]), float(it[1])))
                    except (TypeError, ValueError):
                        continue
            segs.sort(key=lambda x: x[0])
            return segs or [(0.0, _mult_value(v))]
    return [(0.0, _mult_value(v))]


def _multiplier_at(provider, model_id, ts_ms=None):
    """取某请求时刻生效的倍率：从时段历史中找生效时间 <= ts 的最后一段。
    ts 为空（如实时预览）取当前倍率 cur。"""
    m = _load_multipliers()
    v = m.get(_mkey(provider, model_id))
    if v is None:
        return 1.0
    if ts_ms is None:
        return _mult_value(v)
    segs = _mult_hist(v)
    mult = segs[0][1]
    for t, mv in segs:
        if t <= ts_ms:
            mult = mv
        else:
            break
    return mult


def _save_multipliers(d):
    tmp = MULTIPLIER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    shutil.move(tmp, MULTIPLIER_FILE)


def _multiplier_for(provider, model_id):
    m = _load_multipliers()
    if not m:
        return 1.0
    v = m.get(_mkey(provider, model_id))
    if v is None:
        return 1.0
    return _mult_value(v)


def _price_for_raw(model_id, pricing):
    """不带倍率的原始官方价（倍率编辑页展示用）。"""
    key = _bare_model_id(model_id)
    p = pricing.get(key)
    if p:
        return p
    norm = key.replace(".", "-").replace("_", "-")
    if norm != key:
        p = pricing.get(norm)
        if p:
            return p
    best = None
    for pid in pricing:
        if key.startswith(pid) or norm.startswith(pid):
            if best is None or len(pid) > len(best):
                best = pid
    return pricing.get(best)


def _price_for(provider, model_id, pricing, ts_ms=None):
    """在 _price_for_raw 基础上叠加该「供应商+模型」组合在 ts 时刻生效的倍率（默认 1）。
    所有费用计算统一走这里——倍率一处改、全站生效；ts 缺省取当前倍率。"""
    hit = _price_for_raw(model_id, pricing)
    if hit is not None:
        mult = _multiplier_at(provider, model_id, ts_ms)
        if mult != 1.0:
            hit = dict(hit, **{"in": hit.get("in", 0) * mult, "out": hit.get("out", 0) * mult,
                               "cache": hit.get("cache", 0) * mult, "_mult": mult})
    return hit


def _est_cost(provider, model_id, inp, out, cached, pricing, ts_ms=None):
    """按（官方价 × 该时刻倍率）分别计输入/输出/缓存费用。无价格返回 0。
    倍率按变更历史分段：改成 0（免费）之前的请求照旧计费，免费期内计 0，
    从现在起改回正倍率才开始计费（「当前修改」模式）。"""
    p = _price_for(provider, model_id, pricing, ts_ms)
    if not p:
        return 0.0
    cached = min(cached or 0, inp or 0)
    plain_inp = max((inp or 0) - cached, 0)
    return (plain_inp / 1e6 * p.get("in", 0)
            + (out or 0) / 1e6 * p.get("out", 0)
            + cached / 1e6 * p.get("cache", 0))


def get_usage(days=30, frm=None, to=None):
    """会话维度用量。数据源 = 会话 jsonl 的逐请求 usage（供应商返回的原始值），
    不再使用 DB session_usage.used（该字段不是 token，严重失真）。
    口径与官方后台一致：用量 = 输入(含缓存命中) + 输出。"""
    key = "%s_%s_%s" % (days, frm, to)
    with _usage_lock:
        if _usage_cache["data"] and _usage_cache["key"] == key and (time.time() - _usage_cache["ts"]) < USAGE_CACHE_TTL:
            return _usage_cache["data"]
    if frm:
        since = int(frm)
    else:
        # 按本地自然日口径：N天 = 从 N-1 天前的本地零点起算（与官方后台"今日消耗"一致），
        # 而非"过去 24×N 小时"（旧口径会把昨天下午的数据算进"1天"）
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        since = int((midnight - timedelta(days=days - 1)).timestamp() * 1000)
    until = int(to) if to else int(time.time() * 1000)
    # 旧版前端页面（浏览器缓存未刷新）会把「昨天」结束时间算成 +1000 天（≈+100 年），
    # 传上来昨天≈全部。后端兜底钳制：窗口跨度超过 400 天视为异常，按 from 起算的自然日
    # 窗口重算（到 23:59:59.999），保证旧页面打开也不会显示错误数据。
    _now_ms = int(time.time() * 1000)
    if frm and to and (until - since) > 400 * 864e5:
        _s = datetime.fromtimestamp(since / 1000)
        _e = _s + timedelta(days=days - 1) if days > 1 else _s
        since = int(datetime(_s.year, _s.month, _s.day, 0, 0, 0).timestamp() * 1000)
        until = int(datetime(_e.year, _e.month, _e.day, 23, 59, 59, 999000).timestamp() * 1000)
    elif frm and not to and (_now_ms - since) > 400 * 864e5:
        pass  # 只有 from 且跨度异常大：视为"从那天到现在"，合法（全部档）
    # 会话元数据（标题/上下文/积分）不过滤时间，时间窗由请求数据决定
    meta = {}
    con = sqlite3.connect("file:%s?mode=ro" % WB_DB.replace("\\", "/"), uri=True)
    try:
        cur = con.cursor()
        cur.execute(
            "SELECT s.id, s.title, s.created_at, u.size, u.credit_json "
            "FROM sessions s LEFT JOIN session_usage u ON u.session_id = s.id")
        for sid, title, created_at, size, credit_json in cur.fetchall():
            meta[(sid or "")[:8]] = {
                "title": (title or "")[:40], "created_at": created_at or 0,
                "size": size or 0,
                "credit": (sum(float(v) for v in json.loads(credit_json).values())
                           if credit_json else 0.0),
            }
    finally:
        con.close()
    pmap = _provider_map()
    third_ids = set()
    for p in load_presets():
        for m in p.get("models", []):
            if m.get("id"):
                third_ids.add(m["id"])
    pname_map = _preset_vendor_names()
    pricing = _load_pricing()
    # 按（会话, 供应商, 模型）聚合逐请求 usage——同名模型不同供应商分开算
    recs = scan_requests()
    # 全时段各会话的官方 token 总量：积分按"时间窗内官方 token / 全时段官方 token"
    # 的比例分摊（credit_json 只有会话级累计，按消息哈希记、无法对时间，
    # 但消息扣费与 token 同步累计，按 token 时间占比分摊即时间维度真实消耗）
    off_all = {}
    for r in recs:
        _b = bare_id(r["model"])
        _pv, _pc = _classify_request(
            r.get("pname") or "", _b, pname_map, None,
            billed=r.get("billed", 0), raw_model=r.get("model") or "")
        if _pc == "official":
            off_all[r["session"]] = off_all.get(r["session"], 0) + r["inp"] + r["out"]
    g = {}
    for r in recs:
        if not (since <= r["ts"] <= until):
            continue
        b = bare_id(r["model"])
        provider, cls = _classify_request(
            r.get("pname") or "", b, pname_map, None,
            billed=r.get("billed", 0), raw_model=r.get("model") or "")
        a = g.setdefault((r["session"], provider, b), {
            "inp": 0, "out": 0, "cached": 0, "n": 0, "ts": 0,
            "provider": provider, "cls": cls,
        })
        a["inp"] += r["inp"]; a["out"] += r["out"]; a["cached"] += r["cached"]
        a["n"] += 1
        a["ts"] = max(a["ts"], r["ts"])
    sessions = []
    for (sid8, _pv, b), a in g.items():
        m = meta.get(sid8, {})
        cls = a["cls"]
        used = a["inp"] + a["out"]  # 与官方后台口径一致
        cost = _est_cost(a["provider"], b, a["inp"], a["out"], a["cached"], pricing, a["ts"]) if cls == "third" else 0.0
        sessions.append({
            "id": sid8,
            "title": m.get("title") or "未命名会话",
            "model": b or "默认模型",
            "provider": a["provider"],
            "cls": cls,
            "used": used,
            "inp": a["inp"], "out": a["out"], "cached": a["cached"],
            "reqs": a["n"],
            "size": m.get("size") or 0,
            "pct": round(a["inp"] * 100.0 / m["size"], 1) if m.get("size") else 0,
            "ts": a["ts"] or m.get("created_at") or 0,
            "credit": m.get("credit") or 0.0,
            "cost": round(cost, 4),
            "currency": _currency_for(b),
        })
    # 官方费用用积分显示：积分取自 DB 会话实际扣费记录（credit_json 逐条消息累计）。
    # 第三方请求不消耗 WorkBuddy 积分，会话积分全部归该会话的官方分组；
    # 同会话有多条官方分组（多模型）时按 token 用量占比分摊。
    # 时间维度：会话积分 ×（窗口内官方 token / 全时段官方 token），保证
    # 「今天」这类短窗口只计入今天真实消耗的比例，而不是整会话累计值。
    off_rows = {}
    for s in sessions:
        if s["cls"] == "official":
            off_rows.setdefault(s["id"], []).append(s)
        else:
            s["credit"] = 0.0
    for sid8, rows in off_rows.items():
        c = meta.get(sid8, {}).get("credit") or 0.0
        if c <= 0:
            continue
        tot = sum(r["used"] for r in rows)
        all_tok = off_all.get(sid8, 0)
        if all_tok > 0 and tot < all_tok:
            c = c * float(tot) / all_tok
        if tot > 0:
            for r in rows:
                r["credit"] = round(c * r["used"] / tot, 4)
        else:
            sh = round(c / len(rows), 4)
            for r in rows:
                r["credit"] = sh
    sessions.sort(key=lambda x: -x["ts"])
    # 总命中率：后端一次算好，汇总页/战报直接用，前端不必再拉全量请求明细
    ti = sum(s["inp"] for s in sessions)
    tc = sum(s["cached"] for s in sessions)
    hit = round(tc * 100.0 / ti, 1) if ti else None
    # v2.68 按日分布：按逐条请求的真实时间聚合（不是会话最后活跃时间）。
    # 会话行 ts = 该会话最后一条请求时间，跨天大会话的 token 会被整堆到一天，
    # 导致「单日最高/活跃天数/环比/趋势」全部失真（且与「当天」档对不上）。
    # 口径：日 token = 输入(含缓存) + 输出，与会话 used 一致；credit 不按日拆
    # （credit_json 只有会话级累计，按 token 时间占比拆会与官方账单对不上，不展示）。
    daily = {}
    for r in recs:
        if not (since <= r["ts"] <= until):
            continue
        k = datetime.fromtimestamp(r["ts"] / 1000).strftime("%Y-%m-%d")
        daily[k] = daily.get(k, 0) + r["inp"] + r["out"]
    daily_arr = [{"day": k, "used": v} for k, v in sorted(daily.items())]
    data = {"sessions": sessions, "provider_map": pmap, "hit": hit,
            "daily": daily_arr}
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
    """运行时抓取火山方舟官方文档中的 Agent Plan 模型清单。返回 (models|None, 来源说明)"""
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
        return None, "火山方舟官方清单抓取失败且无可用缓存"


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
aside{width:196px;background:var(--side);border-right:1px solid var(--line);padding:18px 10px 14px;display:flex;flex-direction:column;gap:4px;flex-shrink:0}
.side-brand{display:flex;align-items:center;gap:11px;padding:8px 10px 16px;margin-bottom:8px;border-bottom:1px solid var(--line)}
.brand-badge{width:40px;height:40px;border-radius:11px;background:linear-gradient(135deg,#4f6ef7,#8b5cf6);display:flex;align-items:center;justify-content:center;flex-shrink:0;box-shadow:0 3px 10px rgba(99,102,241,.35)}
.brand-badge b{color:#fff;font-size:19px;font-weight:800;letter-spacing:1px;font-family:Georgia,'Times New Roman',serif}
.brand-name{font-size:14.5px;font-weight:800;color:var(--ink);letter-spacing:1.5px}
.brand-sub{font-size:10px;color:#9aa3bd;margin-top:2px;letter-spacing:.4px;text-transform:uppercase}
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

/* ── 价格表（v2.50 重设计） ── */
.prtable{border-collapse:separate;border-spacing:0;width:100%}
.prtable thead th{position:sticky;top:0;background:linear-gradient(180deg,#f7f9fd,#f2f5fa);border-bottom:1.5px solid var(--line);padding:10px 14px;font-size:12px;color:#5a6480;z-index:1}
.prtable tbody td{border-bottom:1px solid #eef1f7;padding:8px 14px}
.prtable tbody tr:hover{background:#f8faff}
.pr-sym{color:#9aa3bd;font-size:11px;margin-right:2px;font-weight:600}
.pr-cur{display:inline-block;font-size:11px;padding:2px 9px;border-radius:10px;font-weight:600}
.pr-cur.cny{background:#fff3e8;color:#c96a10}
.pr-cur.usd{background:#e8f1ff;color:#2b62c9}

/* ── 左右结构选项卡（subtabs + 左菜单右内容） ── */
/* v2.64：右侧内容设最小宽度，窄窗口时整块横向滚动，绝不挤压变形
   （此前 min-width:0 导致时间选择器竖排、统计卡挤扁） */
.lrwrap{display:flex;gap:14px;align-items:flex-start;overflow-x:auto;padding-bottom:4px}
.lrmenu{width:172px;flex-shrink:0;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:8px;box-shadow:var(--shadow)}
.lrmenu .lri{padding:9px 12px;border-radius:8px;cursor:pointer;font-size:13px;color:#3a4460;display:flex;justify-content:space-between;align-items:center;white-space:nowrap}
.lrmenu .lri:hover{background:#f2f5fb}
.lrmenu .lri.on{background:var(--chip);color:var(--pri);font-weight:600}
.lrmenu .lri .cnt{font-size:11px;color:#9aa3bd}
.lrcontent{flex:1;min-width:680px}

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
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-bottom:14px}
.biggrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:14px}
/* v2.66 汇总看板分组标题（账本/规模/效率三段） */
.sumgroup{margin-bottom:6px}
.sumgroup .biggrid{margin-bottom:10px}
.sumgroup-t{font-size:13px;font-weight:700;color:#2a3352;margin:0 0 8px 2px}
.bigstat{background:linear-gradient(135deg,#f8faff,#eef3fc);border:1px solid var(--line);border-radius:14px;padding:18px 8px;text-align:center;box-shadow:var(--shadow)}
.bigstat .bv{font-size:24px;font-weight:700;color:var(--pri);font-variant-numeric:tabular-nums}
.bigstat .bv .cur{font-style:normal}
.bigstat .bv .cur span{font-size:19px}
.bigstat .bl{font-size:12px;color:var(--sub);margin-top:5px}
.surank{display:inline-block;width:22px;height:22px;line-height:22px;text-align:center;border-radius:50%;background:#f0f2f6;margin-right:8px;font-size:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow);text-align:center}
.stat .v{font-size:21px;font-weight:700;color:var(--pri);font-variant-numeric:tabular-nums;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stat .l{font-size:12px;color:var(--sub);margin-top:3px}
.chartcard{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;box-shadow:var(--shadow);margin-bottom:14px}
/* 左右结构页里的宽表格：模型名不折行，卡片内横向滚动，右侧列不再被挤变形 */
.lrcontent table td{white-space:nowrap}
.lrcontent table td.tl b{display:inline-block;max-width:280px;overflow:hidden;text-overflow:ellipsis;vertical-align:bottom}
.chartcard h3{font-size:13.5px;margin-bottom:10px;color:#2a3352}
.tabs{display:flex;gap:4px;margin-bottom:12px;border-bottom:1.5px solid var(--line)}
.tabs h3{margin:0;padding:7px 18px;font-size:13px;color:var(--sub);cursor:pointer;border-bottom:2px solid transparent;user-select:none}
.tabs h3:hover{color:var(--pri)}
.tabs h3.on{color:var(--pri);border-bottom-color:var(--pri);font-weight:700}
/* CC Switch 式按钮选项卡（统计页主选项卡） */
.maintabs{display:flex;gap:8px;margin-bottom:14px;border-bottom:none;flex-wrap:wrap}
.maintabs h3{margin:0;padding:7px 20px;font-size:13.5px;color:var(--sub);cursor:pointer;user-select:none;
  border:1px solid var(--line);border-radius:8px;background:var(--panel);transition:all .15s}
.maintabs h3:hover{color:var(--pri);border-color:var(--pri)}
.maintabs h3.on{background:var(--pri);color:#fff;border-color:var(--pri);font-weight:700}
.loghead{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.loghead .rtools{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub)}
.pager{border:1px solid var(--line);background:#fff;border-radius:6px;padding:3px 10px;font-size:12px;cursor:pointer;color:#4a5578}
.pager:hover{border-color:var(--pri);color:var(--pri)}
.pager:disabled{opacity:.4;cursor:default;pointer-events:none}
/* ── CC Switch 风格总览大卡 ── */
.cccard{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:22px 24px;box-shadow:var(--shadow);margin-bottom:14px}
.ccmain{display:flex;align-items:center;gap:16px}
.ccicon{width:52px;height:52px;border-radius:14px;background:linear-gradient(135deg,#e8f0ff,#dbe7ff);display:flex;align-items:center;justify-content:center;font-size:26px;flex-shrink:0}
.ccmainnum{flex:1;min-width:0}
.cc-label{font-size:12.5px;color:var(--sub)}
.cc-big{font-size:34px;font-weight:800;line-height:1.15;color:#1c2438;font-variant-numeric:tabular-nums;letter-spacing:.5px}
.cc-big i{font-style:normal;font-size:14px;font-weight:500;color:var(--sub);margin-left:8px;letter-spacing:0}
.cc-topright{display:flex;gap:12px;flex-shrink:0}
.cc-mini{background:#f7f9fd;border:1px solid var(--line);border-radius:12px;padding:10px 18px;min-width:110px}
.cc-mini-l{font-size:11.5px;color:var(--sub);margin-bottom:3px}
.cc-mini-v{font-size:15px;font-weight:700;color:#1c2438;font-variant-numeric:tabular-nums;line-height:1.45}
.ccgrid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:16px}
.ccell{background:#fafbfd;border:1px solid var(--line);border-radius:12px;padding:14px 18px}
.ccell .cc-num{font-size:21px;font-weight:700;color:#1c2438;margin-top:4px;font-variant-numeric:tabular-nums}
.cchit{display:flex;justify-content:space-between;align-items:center;margin-top:18px}
.cchit-right b{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums}
.ccsub{font-size:12px;color:var(--sub);margin-top:10px}
/* ── CC Switch 风格命中率总览 ── */
.hitbar{height:10px;background:#eef1f6;border-radius:5px;overflow:hidden;margin-top:16px;display:flex}
.hitbar i{display:block;height:100%;border-radius:5px;background:#15a35f;transition:width .5s,background-color .3s}
.hitbar-legend{display:flex;gap:14px;margin-top:8px;font-size:11px;color:var(--sub);flex-wrap:wrap}
.hitbar-legend b{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px;vertical-align:-1px}
/* 币种/积分符号配色：¥ 橙、$ 绿、∫ 紫 */
.cur{font-style:normal;font-weight:700;margin-right:1px}
.cur span{margin-left:1px}
.refdot{width:8px;height:8px;border-radius:50%;background:#15a35f;display:inline-block;transition:opacity .3s;box-shadow:0 0 0 3px rgba(21,163,95,.15)}
.refdot.tick{opacity:.25}
/* 命中率五段配色（达到哪段整段用该色） */
.h5{color:#15a35f}   /* >=90 绿 */
.h4{color:#7cb342}   /* 80-90 黄绿 */
.h3{color:#c2891b}   /* 70-80 琥珀 */
.h2{color:#e07b28}   /* 60-70 橙 */
.h1{color:#c2504d}   /* <60 红 */
.bars{display:flex;align-items:flex-end;gap:5px;height:130px;padding-top:4px}
.bar{flex:1;background:linear-gradient(180deg,#6c93ff,#3f66f0);border-radius:4px 4px 0 0;position:relative;min-width:8px}
.bar:hover::after{content:attr(data-tip);position:absolute;bottom:105%;left:50%;transform:translateX(-50%);background:#1b2233;color:#fff;font-size:11px;padding:3px 9px;border-radius:6px;white-space:nowrap;z-index:5}
.bar .d{position:absolute;top:100%;left:50%;transform:translateX(-50%);font-size:9px;color:var(--sub);white-space:nowrap;margin-top:3px}
.mrow{display:flex;align-items:center;gap:10px;margin-bottom:9px;font-size:13px}
.mrow .mname{width:200px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mrow .mbarwrap{flex:1;background:#eef1f6;border-radius:5px;height:15px;overflow:hidden;position:relative}
.mrow .mbar{height:100%;border-radius:5px;background:linear-gradient(90deg,#43b58c,#15a35f)}
.mrow.m2 .mbar{background:linear-gradient(90deg,#7c93ff,#4a6cf0)}
/* 占比数字：居中嵌在进度条中间（白色半透明小胶囊，两种底色上都可读） */
.mrow .mpctin{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);z-index:2;line-height:1;
  font-size:10px;font-weight:700;color:#1f2937;background:rgba(255,255,255,.82);padding:1px 6px;border-radius:7px;pointer-events:none}
.mrow .mval{width:150px;text-align:right;color:var(--sub);font-size:12px;flex-shrink:0}
/* 分布行右侧三列定宽分色：Token=蓝 / 会话=橙 / 费用=符号自带色（∫紫 ¥橙 $绿），全部右对齐 */
.mrow .mtok{display:inline-block;min-width:82px;text-align:right;color:#3f66f0;font-weight:600;font-variant-numeric:tabular-nums}
.mrow .msess{display:inline-block;min-width:78px;text-align:right;color:#b0741a;font-variant-numeric:tabular-nums}
.mrow .mfee{display:inline-block;min-width:112px;text-align:right;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{padding:8px 12px;border-bottom:1px solid var(--line)}
th{text-align:center;color:var(--sub);font-weight:600;font-size:11.5px;background:#f7f9fd;white-space:nowrap}
td{text-align:center;vertical-align:middle}
td.tl{text-align:left}
td.tr{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
th.tl{text-align:left}
th.tr{text-align:right}
tbody tr:nth-child(even){background:#fafbfd}
tbody tr:hover{background:#f1f5fe}
.tag{font-size:11px;padding:2px 9px;border-radius:10px;background:var(--chip);color:var(--pri)}
.tag.builtin{background:#f0f2f6;color:#5a6584}
.tag.t3{background:#fdf3e3;color:#b0741a}
/* 费用单元格：可点击展开计算明细（再点收起，默认收起） */
.feecell{cursor:pointer;border-bottom:1px dashed #b9c4dd;user-select:none}
.feecell:hover{color:var(--pri);border-bottom-color:var(--pri)}
.feeformula{display:none;background:#f8faff;border:1px solid #dfe7f5;border-left:3px solid var(--pri);
  border-radius:8px;padding:10px 14px;margin:6px 0 10px;font-size:12px;color:#3a4460;line-height:1.9}
.feeformula.show{display:block}
.feeformula .fml{font-family:Consolas,'Courier New',monospace;background:#fff;border:1px solid #e3e9f5;
  border-radius:6px;padding:6px 10px;display:inline-block;margin-top:4px;color:#1f2937}
.feeformula .fv{font-weight:700;color:var(--pri)}
.feeformula .fk{color:#9aa3bd;margin-right:4px}
/* ── 官方模型页 ── */
.of-sum{display:flex;gap:14px;margin:14px 0;flex-wrap:nowrap}
.of-card{flex:1;min-width:220px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 18px;box-shadow:var(--shadow)}
.of-card .of-label{font-size:12px;color:var(--sub)}
.of-num{font-size:26px;font-weight:700;color:#1f2937;margin-top:4px}
.of-credit{font-size:26px;font-weight:700;color:#8b5cf6;margin-top:4px}
.of-cost{font-size:26px;font-weight:700;color:#e07b28;margin-top:4px}
.of-card .of-sub{font-size:11px;color:var(--sub);margin-top:3px}

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
dialog select{border:1px solid var(--line);border-radius:8px;padding:8px 11px;font-size:13px;background:#fff}
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

/* ── 科普栏 ── */
.kb-list{max-width:860px}
.kb-item{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);margin-bottom:12px;overflow:hidden}
.kb-title{display:flex;align-items:center;gap:10px;padding:15px 18px;cursor:pointer;user-select:none;font-size:14.5px;font-weight:600;color:var(--ink)}
.kb-title:hover{background:#f6f8fd}
.kb-title .arrow{margin-left:auto;color:var(--sub);transition:transform .25s;font-size:12px}
.kb-item.open .kb-title .arrow{transform:rotate(180deg)}
.kb-item.open .kb-title{color:var(--pri);border-bottom:1px solid var(--line)}
.kb-tag{font-size:10.5px;padding:2px 8px;border-radius:9px;background:#e9f7f0;color:#0f8a55;font-weight:500;flex-shrink:0}
.kb-body{display:none;padding:16px 22px 20px;font-size:13.5px;line-height:1.85;color:#333c55}
.kb-item.open .kb-body{display:block;animation:kbIn .25s ease}
@keyframes kbIn{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.kb-body h5{font-size:13.5px;color:var(--pri);margin:14px 0 6px}
.kb-body h5:first-child{margin-top:0}
.kb-body p{margin:6px 0}
.kb-body b{color:var(--pri)}
.kb-body .hl{background:linear-gradient(transparent 60%,#ffe9a8 60%);font-weight:600}
.kb-body ul{margin:4px 0 8px;padding-left:20px}
.kb-body li{margin:4px 0}
.kb-table{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0;max-width:420px}
.kb-table th,.kb-table td{padding:7px 12px;border-bottom:1px solid var(--line);text-align:left}
.kb-table th{color:var(--sub);font-weight:500;font-size:11.5px;background:#f8f9fc}
.kb-table td b{color:var(--pri)}
.kb-table tr.bad td{color:#d24a3e}
.kb-formula{background:var(--chip);border-radius:8px;padding:10px 14px;font-size:12.5px;color:#4a5578;margin:10px 0;font-family:Consolas,monospace}
.kb-lead{background:var(--chip);border-radius:10px;padding:12px 16px;font-size:13.5px;margin:0 0 6px}
.kb-tip{background:#fff8e8;border-left:3px solid #e8a23f;border-radius:0 8px 8px 0;padding:10px 14px;font-size:12.5px;margin:12px 0;color:#7a5b16}
.kb-duo{width:100%;border-collapse:collapse;font-size:13px;margin:10px 0}
.kb-duo th{padding:8px 12px;font-size:12px;border-bottom:2px solid var(--line);text-align:left}
.kb-duo td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:top;line-height:1.7}
.kb-duo th.good{color:#0f8a55}
.kb-duo th.bad2{color:#d24a3e}
</style>
</head>
<body>

<div id="titlebar">
  <div class="logo">⚡</div><b>WB Switch</b><span class="ver">v1.0.0 · WB Switch</span>
  <div class="spacer"></div>
  <button class="tb-btn" onclick="openHelp()">? 使用说明</button>
  <div class="wb-status"><span class="dot" id="wbdot"></span><span id="wbtext">检测中…</span></div>
  <button class="tb-btn launch" id="btnLaunch" onclick="launchWB()">▶ 启动 WorkBuddy</button>
  <button class="tb-btn restart" id="btnRestart" onclick="restartWB()" style="display:none">↻ 重启使配置生效</button>
</div>

<div id="app">
  <aside>
    <div class="side-brand">
      <div class="brand-badge"><b>T</b></div>
      <div>
        <div class="brand-name">词元管理系统</div>
        <div class="brand-sub">Token Analytics</div>
      </div>
    </div>
    <div class="nav on" data-page="providers" onclick="go('providers',this)"><span class="ico">🔌</span>供应商</div>
    <div class="nav" data-page="usage" onclick="go('usage',this)"><span class="ico">📊</span>用量统计</div>
    <div class="nav" data-page="stats" onclick="go('stats',this)"><span class="ico">📈</span>数据分析</div>
    <div class="nav" data-page="multi" onclick="go('multi',this)"><span class="ico">✖️</span>模型倍率</div>
    <div class="nav" data-page="official" onclick="go('official',this)"><span class="ico">🏛️</span>官方模型</div>
    <div class="nav" data-page="pricing" onclick="go('pricing',this)"><span class="ico">🏷️</span>价格表</div>
    <div class="nav" data-page="science" onclick="go('science',this)"><span class="ico">💡</span>科普</div>
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
            <span data-d="1" onclick="setTime(1,this)">当天</span>
            <span onclick="setYesterday(this)">昨天</span>
            <span data-d="3" onclick="setTime(3,this)">3天</span>
            <span data-d="7" class="on" onclick="setTime(7,this)">7天</span>
            <span data-d="30" onclick="setTime(30,this)">30天</span>
            <span data-d="3650" onclick="setTime(3650,this)">全部</span>
          </div>
          <input type="datetime-local" id="f-from" style="width:190px" step="60" onchange="customRange()"> <span style="color:var(--sub);font-size:12px">至</span>
          <input type="datetime-local" id="f-to" style="width:190px" step="60" onchange="customRange()">
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
        <div class="fgroup" style="margin-left:auto"><label>⟳ 自动刷新</label>
          <select id="f-refresh" onchange="setRefresh(this.value)">
            <option value="5">5 秒</option>
            <option value="10">10 秒</option>
            <option value="30" selected>30 秒</option>
            <option value="60">60 秒</option>
          </select>
          <span class="refdot" id="refdot"></span>
        </div>
      </div>
      <div id="range-hint" style="font-size:12px;color:var(--sub);margin:2px 0 8px"></div>
      <div class="cccard">
        <div class="ccmain">
          <div class="ccicon">⚡</div>
          <div class="ccmainnum">
            <div class="cc-label">真实消耗 Tokens</div>
            <div class="cc-big" id="cc-total">-<i id="cc-total-approx"></i></div>
          </div>
          <div class="cc-topright">
            <div class="cc-mini"><div class="cc-mini-l">总请求数</div><div class="cc-mini-v" id="cc-reqs">-</div></div>
            <div class="cc-mini"><div class="cc-mini-l">总成本</div><div class="cc-mini-v" id="cc-cost">-</div></div>
          </div>
        </div>
        <div class="ccgrid">
          <div class="ccell"><div class="cc-label">⬇ 新增输入</div><div class="cc-num" id="cc-inp">-</div></div>
          <div class="ccell"><div class="cc-label">⬆ Output</div><div class="cc-num" id="cc-out">-</div></div>
          <div class="ccell"><div class="cc-label">💬 会话</div><div class="cc-num" id="cc-sess">-</div></div>
          <div class="ccell"><div class="cc-label">✦ 命中</div><div class="cc-num" id="cc-cache">-</div></div>
        </div>
        <div class="cchit">
          <span class="cc-label">缓存命中率</span>
          <span class="cchit-right"><b id="cc-hitpct">-</b></span>
        </div>
        <div class="hitbar"><i id="hitbar-fill"></i></div>
        <div class="hitbar-legend" id="hitbar-legend"></div>
        <div class="ccsub" id="cc-sub"></div>
      </div>
      <div class="chartcard">
        <div class="loghead"><h3 style="margin:0">请求日志（随筛选联动）</h3>
          <button class="btn ghost sm" onclick="exportReqLog()">⬇ 导出 CSV</button></div>
        <table><thead><tr><th class="tl">时间</th><th class="tl">供应商 模型</th><th class="tr">输入</th><th class="tr">命中</th><th class="tr">输出</th><th class="tr">命中率</th><th class="tr">费用</th></tr></thead>
        <tbody id="reqrows"></tbody></table>
        <div class="rtools" style="margin-top:12px;justify-content:flex-end;display:flex">
          每页
          <div class="seg" id="segPage">
            <span data-n="30" class="on" onclick="setPageSize(30,this)">30</span>
            <span data-n="50" onclick="setPageSize(50,this)">50</span>
            <span data-n="100" onclick="setPageSize(100,this)">100</span>
          </div>
          条
          <button class="pager" id="pg-prev" onclick="pgMove(-1)">上一页</button>
          <span id="pg-info">-</span>
          <button class="pager" id="pg-next" onclick="pgMove(1)">下一页</button>
        </div>
      </div>
      <p class="pd" style="margin-top:8px">Token 分布、费用统计、会话明细已整合到「数据分析」页（费用统计在最后），筛选条件两页联动。</p>
    </section>

    <!-- 统计页（CC Switch 式单页：顶部主选项卡 + 筛选联动） -->
    <section class="page" id="page-stats">
      <h2 class="pt">数据分析</h2>
      <p class="pd">请求日志、Provider/模型统计、Token 分布、费用统计与会话明细，全部整合在同一页，顶部选项卡切换。</p>
      <div class="filters">
        <div class="fgroup"><label>时间</label>
          <div class="seg" id="segTime2">
            <span data-d="1" onclick="setTime(1,this)">当天</span>
            <span onclick="setYesterday(this)">昨天</span>
            <span data-d="3" onclick="setTime(3,this)">3天</span>
            <span data-d="7" class="on" onclick="setTime(7,this)">7天</span>
            <span data-d="30" onclick="setTime(30,this)">30天</span>
            <span data-d="3650" onclick="setTime(3650,this)">全部</span>
          </div>
        </div>
        <div class="fgroup"><label>类别</label>
          <div class="seg" id="segCls2">
            <span class="on" onclick="setCls('',this)">全部</span>
            <span onclick="setCls('official',this)">官方·积分</span>
            <span onclick="setCls('third',this)">第三方·Token</span>
          </div>
        </div>
        <div class="fgroup"><label>供应商</label><select id="f-prov2" onchange="provMirror(this)"><option value="">全部</option></select></div>
        <div class="fgroup"><label>模型</label><select id="f-model2" onchange="modelMirror(this)"></select></div>
        <div class="fgroup" style="margin-left:auto"><label>⟳ 刷新</label>
          <select id="f-refresh2" onchange="setRefresh(this.value);document.getElementById('f-refresh').value=this.value">
            <option value="5">5 秒</option>
            <option value="10">10 秒</option>
            <option value="30" selected>30 秒</option>
            <option value="60">60 秒</option>
          </select>
        </div>
      </div>
      <div class="fgroup" style="margin-top:14px;margin-bottom:18px"><label>自定义</label>
        <input type="datetime-local" id="f-from2" style="width:190px" step="60" onchange="dateMirror('f-from2','f-from');customRange()">
        <span style="color:var(--sub);font-size:12px">至</span>
        <input type="datetime-local" id="f-to2" style="width:190px" step="60" onchange="dateMirror('f-to2','f-to');customRange()">
      </div>
      <div id="range-hint2" style="font-size:12px;color:var(--sub);margin:0 0 6px"></div>
      <!-- 主选项卡（CC Switch 式按钮）：Provider 统计 / 模型统计 / 会话明细 / Token 分布 / 费用统计 -->
      <div class="maintabs" id="mainTabs">
        <h3 class="on" onclick="setMain('prov',this)">Provider 统计</h3>
        <h3 onclick="setMain('model',this)">模型统计</h3>
        <h3 onclick="setMain('sess',this)">会话明细</h3>
        <h3 onclick="setMain('dist',this)">Token 分布</h3>
        <h3 onclick="setMain('fee',this)">费用统计</h3>
      </div>

      <!-- Provider 统计（最前） -->
      <div id="main-prov">
      <div class="chartcard">
        <table><thead><tr><th class="tl">供应商</th><th class="tr">请求数</th><th class="tr">输入</th><th class="tr">缓存命中</th><th class="tr">输出</th><th class="tr">命中率</th><th class="tr">费用</th></tr></thead>
        <tbody id="provhit"></tbody></table>
      </div>
      </div>

      <!-- 模型统计 -->
      <div id="main-model" style="display:none">
      <div class="chartcard">
        <table><thead><tr><th class="tl">供应商 模型</th><th class="tr">请求数</th><th class="tr">输入</th><th class="tr">缓存命中</th><th class="tr">输出</th><th class="tr">命中率</th><th class="tr">费用</th></tr></thead>
        <tbody id="reqstats"></tbody></table>
      </div>
      </div>

      <!-- 会话明细 -->
      <div id="main-sess" style="display:none">
      <div class="chartcard"><h3>会话明细</h3>
        <table><thead><tr><th class="tl">会话</th><th class="tl">供应商 模型</th><th class="tr">Token</th><th class="tl">计费</th><th class="tr">上下文</th><th class="tl">时间</th></tr></thead>
        <tbody id="sessrows"></tbody></table></div>
      </div>

      <!-- Token 分布 -->
      <div id="main-dist" style="display:none">
      <div class="chartcard">
        <div class="tabs" id="distTabs">
          <h3 class="on" onclick="setTab('distTabs','model',this)">按模型分布</h3>
          <h3 onclick="setTab('distTabs','prov',this)">按供应商分布</h3>
        </div>
        <div id="dist-model"><div id="modelrows"></div></div>
        <div id="dist-prov" style="display:none"><div id="provrows"></div></div>
      </div>
      </div>

      <!-- 费用统计（最后） -->
      <div id="main-fee" style="display:none">
      <div class="chartcard">
        <div class="tabs" id="feeTabs">
          <h3 class="on" onclick="setTab('feeTabs','feemodel',this)">按模型费用</h3>
          <h3 onclick="setTab('feeTabs','feeprov',this)">按供应商费用</h3>
        </div>
        <div id="fee-feemodel">
          <table><thead><tr><th class="tl">供应商 模型</th><th class="tl">类别</th><th class="tr">费用</th><th class="tr">占比</th></tr></thead>
          <tbody id="feemodelrows2"></tbody></table>
        </div>
        <div id="fee-feeprov" style="display:none">
          <table><thead><tr><th class="tl">供应商</th><th class="tl">类别</th><th class="tr">费用</th><th class="tr">占比</th></tr></thead>
          <tbody id="feeprovrows2"></tbody></table>
        </div>
      </div>
      </div>
    </section>

    <!-- 价格表页（仅官方价，来源 cc-switch 价格库，可在线更新/导入导出/手动添加） -->
    <section class="page" id="page-pricing">
      <h2 class="pt">模型价格表</h2>
      <p class="pd">官方公开价（每百万 token）。币种按模型官方定价显示：中国模型 <b>¥</b>、美元模型 <b>$</b>，不做汇率换算。费用统计、请求日志的费用估算均以此表为准。</p>
      <div class="filters">
        <div class="fgroup"><label>搜索</label><input type="text" id="pr-search" placeholder="模型名…" style="width:180px" oninput="renderPricing()"></div>
        <div class="fgroup"><span style="font-size:12px;color:var(--sub)" id="pr-count">-</span></div>
        <div class="fgroup" style="margin-left:auto">
          <button class="btn ghost sm" onclick="exportPricing()">⬇ 导出</button>
          <button class="btn ghost sm" onclick="importPricing()">⬆ 导入</button>
          <button class="btn ghost sm" onclick="addPricingRow()">＋ 手动添加</button>
          <button class="btn ghost sm" id="pr-refresh-btn" onclick="refreshPricing()">↻ 获取最新价格表</button>
        </div>
      </div>
      <div class="chartcard" style="max-height:640px;overflow:auto">
        <table class="prtable" style="min-width:640px"><thead><tr>
          <th style="text-align:left;white-space:nowrap">模型</th>
          <th>输入 /M</th><th>缓存命中 /M</th><th>输出 /M</th><th>计价币种</th><!-- 币种符号逐行跟随模型（¥/$） -->
        </tr></thead><tbody id="prrows"></tbody></table>
      </div>
      <div id="pr-msg" style="font-size:12px;color:var(--sub);margin-top:10px"></div>
    </section>

    <!-- 模型倍率页（仅第三方模型倍率；官方模型价格见「官方模型 → 模型管理」） -->
    <section class="page" id="page-multi">
      <h2 class="pt">模型倍率</h2>
      <p class="pd">按「供应商 + 模型」组合设倍率：实际价 = 官方价 × 倍率（5 折填 0.5，<b>0 = 免费，结果为 0</b>，正常为 1，限时折扣自填）。同一模型不同供应商各算各的。保存时可选「全部修改」（追溯历史）或「当前修改」（从现在起生效）。<b>官方模型不在此设置</b>——其正价与积分倍率标识见「官方模型 → 模型管理」。</p>
      <div class="filters">
        <div class="fgroup"><button class="btn" onclick="saveMulti()">💾 保存全部倍率</button></div>
        <div class="fgroup"><button class="btn danger sm" onclick="delMulti()">🗑 删除所选倍率（重置为 1）</button></div>
        <div class="fgroup">
          <button class="btn ghost sm" onclick="exportMulti()">⬇ 导出</button>
          <button class="btn ghost sm" onclick="importMulti()">⬆ 导入</button>
          <button class="btn ghost sm" onclick="downloadTemplate('multi')">📄 模板</button>
          <button class="btn ghost sm" onclick="addMultiRow()">＋ 手动添加</button>
        </div>
        <div class="fgroup"><span style="font-size:12px;color:var(--sub)" id="mu-hint">勾选=已有自定义倍率的行可删；未勾选/未填写的倍率一律视为 1。</span></div>
      </div>
      <div class="chartcard" style="max-height:600px;overflow:auto" id="mu-card-third">
        <table style="min-width:880px"><thead><tr>
          <th style="width:36px"><input type="checkbox" id="mu-all" onchange="toggleMuAll(this.checked)"></th>
          <th style="text-align:left;white-space:nowrap">供应商 / 模型</th>
          <th>官方输入价</th><th>官方命中价</th><th>官方输出价</th><!-- 币种符号跟随价格表（¥/$）逐格显示 -->
          <th style="width:110px">倍率（空=1）</th>
          <th>实际输入</th><th>实际命中</th><th>实际输出</th>
        </tr></thead><tbody id="murows"></tbody></table>
      </div>
      <div id="mu-msg" style="font-size:12px;color:var(--sub);margin-top:10px"></div>
    </section>

    <!-- 官方模型页（官方按积分计费模型：左右结构：用量分析 / 模型管理） -->
    <section class="page" id="page-official">
      <h2 class="pt">官方模型</h2>
      <p class="pd">WorkBuddy 官方模型（全部中国模型，按积分扣费）的成本一览：积分按<b>对话轮</b>扣。<b>实际积分价值 = 实扣积分 × 系数</b>；系数按官方正价把<b>输入/输出/缓存命中</b>分项算出总成本 ÷ 实扣总积分，由数据回归得出（随时间窗变化，非硬编码）。积分倍率不参与计算，仅用于判断：0=免费免积分，非0=消耗积分（在「官方模型 → 模型管理」里设置）。</p>
      <div class="maintabs" style="margin-bottom:14px">
        <h3 id="of-tab-usage" class="on" onclick="ofTab('usage')">📊 用量与日志</h3>
        <h3 id="of-tab-manage" onclick="ofTab('manage')">⚙️ 模型管理 <span id="of-mcnt">-</span></h3>
      </div>
      <div>
      <div id="of-pane-usage">
      <div class="filters">
        <div class="fgroup"><label>时间</label>
          <div class="seg" id="segOf">
            <span data-d="1" onclick="ofSet(1,this)">当天</span>
            <span onclick="ofYesterday(this)">昨天</span>
            <span data-d="3" onclick="ofSet(3,this)">3天</span>
            <span data-d="7" class="on" onclick="ofSet(7,this)">7天</span>
            <span data-d="30" onclick="ofSet(30,this)">30天</span>
            <span data-d="3650" onclick="ofSet(3650,this)">全部</span>
          </div>
          <input type="datetime-local" id="of-from" style="width:190px" step="60" onchange="ofCustom()"> <span style="color:var(--sub);font-size:12px">至</span>
          <input type="datetime-local" id="of-to" style="width:190px" step="60" onchange="ofCustom()">
        </div>
      </div>
      <div class="of-sum">
        <div class="of-card"><div class="of-label">对话轮次</div><div class="of-num" id="of-rounds">-</div><div class="of-sub" id="of-models"></div></div>
        <div class="of-card"><div class="of-label">积分消耗（实际扣费）</div><div class="of-credit" id="of-credit">-</div><div class="of-sub" id="of-credit-sub"></div></div>
        <div class="of-card"><div class="of-label">实际积分价值（实扣 × 系数）</div><div class="of-cost" id="of-cost">-</div><div class="of-sub" id="of-cost-sub"></div></div>
      </div>
      <div class="chartcard" style="max-height:640px;overflow-y:auto">
        <table><thead><tr>
          <th style="text-align:left">模型</th>
          <th>轮次</th><th>每轮积分</th><th>积分合计</th>
          <th>输入</th><th>缓存命中</th><th>输出</th>
          <th>实际积分价值</th>
        </tr></thead><tbody id="ofrows"></tbody></table>
      </div>
      <div class="chartcard" style="max-height:560px;overflow-y:auto;margin-top:14px">
        <div class="loghead"><h3 style="margin:0">请求日志（官方模型 · 复用用量统计明细）</h3><span id="of-logcount" style="font-size:12px;color:var(--sub)"></span></div>
        <table><thead><tr><th class="tl">时间</th><th class="tl">模型</th><th class="tr">输入</th><th class="tr">命中</th><th class="tr">输出</th><th class="tr">积分</th></tr></thead>
        <tbody id="ofreqrows"></tbody></table>
      </div>
      <div id="of-msg" style="font-size:12px;color:var(--sub);margin-top:10px"></div>
      </div>
      <div id="of-pane-manage" style="display:none">
        <div class="filters">
          <div class="fgroup"><button class="btn" id="of-pull-btn" onclick="pullOfficialCatalog()">⟳ 重新拉取官方模型</button></div>
          <div class="fgroup"><button class="btn ghost sm" onclick="resetOfficialCatalog()">↺ 恢复内置清单</button></div>
          <div class="fgroup"><span style="font-size:12px;color:var(--sub)" id="of-catalog-info">-</span></div>
        </div>
        <div class="chartcard" style="max-height:560px;overflow-y:auto">
          <p class="pd" style="padding:0 14px;margin-top:12px">官方模型清单已写死进程序（不依赖云端缓存也能显示），「重新拉取」会用 WorkBuddy 云端配置的最新清单覆盖。识别错误的模型可直接删除，只影响清单展示，不影响用量统计。<b>积分倍率仅作判断标识（0=免费免积分，非0=消耗积分），不参与计算</b>；输入/命中/输出为官方正价（¥/百万 token）。</p>
          <table style="min-width:760px"><thead><tr>
            <th style="text-align:left">模型</th><th class="tl">ID</th>
            <th>正价输入</th><th>正价命中</th><th>正价输出</th>
            <th>积分倍率</th><th>上下文</th><th class="tr">操作</th>
          </tr></thead><tbody id="ofcatrows"></tbody></table>
        </div>
        <div id="of-cat-msg" style="font-size:12px;color:var(--sub);margin-top:10px"></div>
      </div>
      </div>
    </section>

    <!-- 科普页 -->
    <section class="page" id="page-science">
      <h2 class="pt">科普栏</h2>
      <p class="pd">聊聊 AI 用量背后的门道——看懂数字，才能把钱花在刀刃上。点击标题展开 / 收起。</p>
      <div class="kb-list" id="kblist">

        <div class="kb-item" id="kb-cache">
          <div class="kb-title" onclick="kbToggle('kb-cache')">
            <span class="kb-tag">第 1 期</span>缓存命中率，才是你账单上最隐形的那支笔
            <span class="arrow">▼</span>
          </div>
          <div class="kb-body">
            <p class="kb-lead">你的用量统计里有一列"缓存命中率"。多数人只当它是个百分比飘过——实际上，<b>这列数字直接决定你为同样的 token 付 1 块还是 3 块</b>。这一期就把它讲透：是什么、什么时候高、和你的账单到底是什么关系。</p>

            <h5>一、什么是缓存命中率</h5>
            <p>大模型 API 的计费有个重要规则：<b>重复的输入可以打折</b>。服务商（火山、OpenAI 这些）会在自己那边为你的上下文保留一份<b>临时缓存</b>——下一轮请求进来，开头和上次一模一样的部分不用重新完整计算，按缓存价计费，通常只有原价的 <b>10% 左右</b>。</p>
            <p><span class="hl">缓存命中率 = 命中缓存的输入 token ÷ 总输入 token</span>。命中率 90%，意味着你 9 成的输入都走了"打折通道"，只有 1 成按原价算。</p>

            <h5>二、什么情况命中率高，什么情况低</h5>
            <table class="kb-duo">
              <thead><tr><th class="good">✅ 命中高（省钱）</th><th class="bad2">❌ 命中低（费钱）</th></tr></thead>
              <tbody>
                <tr>
                  <td><b>做项目、写代码</b>：每轮请求都带着同一份系统提示词、同一批文件，前缀完全一致，缓存稳稳命中，轻松 <b>90%+</b>。</td>
                  <td><b>纯闲聊</b>：东一句西一句，每次内容和上次毫不相干，没有可复用的前缀，全部原价重算。</td>
                </tr>
                <tr>
                  <td><b>长对话连续追问</b>：越聊越长的对话，前面聊过的部分全部命中。</td>
                  <td><b>反复修改上下文</b>：缓存按<b>前缀</b>匹配，开头改一个字，后面全部作废重算。</td>
                </tr>
              </tbody>
            </table>
            <div class="kb-tip">⏳ 缓存还有<b>时效</b>：它活在 API 服务商的机器上，一般只保留几分钟到几小时，本质是给"短时间连续对话"准备的加速器。隔了几天再回来找同一个模型，旧缓存早已过期清掉——头一两轮要按原价重新写入缓存，之后才能继续享受折扣。</div>

            <h5>三、命中率 × 成本对照表</h5>
            <p>前提：<b>同一个模型、消耗完全相同的 token</b>，唯一的变量是缓存命中率。计费规则：未命中部分按原价、命中部分按原价 10%。以命中率 <b>95%</b> 时花费 = <b>1.00 元</b> 为基准：</p>

            <table class="kb-table">
              <thead><tr><th>缓存命中率</th><th>相对花费</th><th>对比 95% 命中</th><th>典型的场景</th></tr></thead>
              <tbody>
                <tr><td><b>95%</b></td><td><b>1.00 元</b></td><td>基准</td><td>做项目该有的水平</td></tr>
                <tr><td>90%</td><td>1.31 元</td><td>贵 31%</td><td>长对话、中途换过模型</td></tr>
                <tr><td>85%</td><td>1.62 元</td><td>贵 62%</td><td>频繁改提示词</td></tr>
                <tr><td>80%</td><td>1.93 元</td><td>几乎翻倍</td><td>—</td></tr>
                <tr class="bad"><td>79%</td><td>1.99 元</td><td><b>≈ 2 倍</b></td><td rowspan="2">半聊半干，缓存时有时无</td></tr>
                <tr><td>75%</td><td>2.24 元</td><td>2.2 倍</td></tr>
                <tr><td>70%</td><td>2.55 元</td><td>2.6 倍</td><td>改一行发一次</td></tr>
                <tr><td>65%</td><td>2.86 元</td><td>2.9 倍</td><td>—</td></tr>
                <tr class="bad"><td>63%</td><td>2.99 元</td><td><b>≈ 3 倍</b></td><td rowspan="2">东一句西一句的纯闲聊</td></tr>
                <tr class="bad"><td>60%</td><td>3.17 元</td><td>3.2 倍</td></tr>
                <tr class="bad"><td>50%</td><td>3.79 元</td><td>≈ 3.8 倍</td><td>缓存基本没起作用</td></tr>
                <tr class="bad"><td>46.7%</td><td><b>4.00 元</b></td><td><b>≈ 4 倍</b></td><td>—</td></tr>
                <tr class="bad"><td>40%</td><td>4.41 元</td><td>≈ 4.4 倍</td><td>—</td></tr>
                <tr class="bad"><td>30.6%</td><td><b>5.00 元</b></td><td><b>≈ 5 倍</b></td><td>—</td></tr>
                <tr class="bad"><td>30%</td><td>5.03 元</td><td>≈ 5.2 倍</td><td>接近全价硬扛</td></tr>
                <tr class="bad"><td>0%</td><td>6.90 元</td><td>≈ 6.9 倍</td><td>完全无缓存（极限对照）</td></tr>
              </tbody>
            </table>
            <div class="kb-formula">总成本系数 = (1 − H) × 1 + H × 0.1　　相对花费 = 当前系数 ÷ 95% 命中的系数<br>H 为缓存命中率。例：H=79% → 0.21 + 0.079 = 0.289；H=95% → 0.145；0.289 ÷ 0.145 ≈ 2 倍</div>

            <p>这张表有三个读法，一个比一个扎心：</p>
            <ul>
              <li><b>79% 是翻倍线</b>：命中率从 95% 掉到 79%，同样的活儿，钱翻倍。</li>
              <li><b>63% 是三倍线</b>：再往下掉到 63%，钱变三倍。</li>
              <li><b>越往下掉得越快</b>：这不是线性关系，是<b>指数级</b>的——95→90 只贵三毛一，90→85 贵三毛一，85 以后每掉 5 个点，分别要多掏 3 毛、3 毛、3 毛、3 毛、4 毛、5 毛、9 毛……一旦掉到 0% 命中，你要付近 7 倍的钱。命中率高时曲线平得像价格保护，一旦跌破 80%，斜率陡然抬头，掉得越低、摔得越重。</li>
            </ul>

            <h5>四、怎么把命中率做上去</h5>
            <ul>
              <li><b>先看数据再挑供应商</b>：同样的模型，有的家缓存折扣给力、保留时间长，有的家干脆不给缓存。打开「用量统计 → 供应商命中率汇总」，几家的命中率横向一比就见分晓——那列绿色的 90%+ 就是真金白银。</li>
              <li><b>长任务别中途换模型</b>：每换一个模型（甚至换成别家同 ID 的），缓存全部作废重来。</li>
              <li><b>一鼓作气，别让会话冷掉</b>：缓存时效就几分钟到几小时，趁热打铁干完一个项目，比断断续续好几天省得多。</li>
              <li><b>系统提示词放最前、少改动</b>：前缀越稳定，能复用的部分越长。</li>
            </ul>

            <div class="kb-tip">💡 一句话总结：<b>命中率是唯一一个不用换模型、不用砍用量，纯靠使用习惯就能把账单砍半的指标</b>。下次做完项目，回头看一眼用量统计里那列绿字——90% 以上，钱花在了刀刃上；跌破 60%，先别骂模型贵，是姿势出了问题。</div>

        </div>

      </div>
    </section>

    <!-- 汇总页（移到用量统计之后） -->
    <section class="page" id="page-summary">
      <h2 class="pt">汇总战报</h2>
      <p class="pd">一段时间的完整账本——token、积分、花费、命中率一页看全。</p>
      <div class="filters">
        <div class="fgroup"><label>范围</label>
          <div class="seg" id="segSum">
            <span data-s="1" onclick="setSum(1,this)">当天</span>
            <span onclick="setSumYesterday(this)">昨天</span>
            <span data-s="3" onclick="setSum(3,this)">3天</span>
            <span data-s="7" onclick="setSum(7,this)">7天</span>
            <span data-s="30" onclick="setSum(30,this)">30天</span>
            <span data-s="3650" class="on" onclick="setSum(3650,this)">全部</span>
          </div>
        </div>
        <div class="fgroup"><button class="btn" onclick="copyReport()">📋 复制战报图片</button></div>
      </div>
      <!-- v2.66 看板按逻辑分组：账本 → 规模 → 效率与趋势，单日最高附完整日期 -->
      <div class="sumgroup"><div class="sumgroup-t">💰 这段时间花了多少</div>
        <div class="biggrid">
          <div class="bigstat"><div class="bv" id="su-token">-</div><div class="bl">累计 Token</div></div>
          <div class="bigstat"><div class="bv" id="su-credit" style="color:#8b5cf6">-</div><div class="bl">官方积分消耗</div></div>
          <div class="bigstat"><div class="bv" id="su-cost" style="color:#e07b28">-</div><div class="bl">第三方花费（官方价）</div></div>
        </div>
      </div>
      <div class="sumgroup"><div class="sumgroup-t">📏 用得有多大规模</div>
        <div class="biggrid">
          <div class="bigstat"><div class="bv" id="su-sess">-</div><div class="bl">总会话数</div></div>
          <div class="bigstat"><div class="bv" id="su-active">-</div><div class="bl">活跃天数</div></div>
          <div class="bigstat"><div class="bv" id="su-models">-</div><div class="bl">用过的模型</div></div>
        </div>
      </div>
      <div class="sumgroup"><div class="sumgroup-t">📈 用得怎么样</div>
        <div class="biggrid">
          <div class="bigstat"><div class="bv" id="su-hit">-</div><div class="bl">缓存命中率</div></div>
          <div class="bigstat"><div class="bv" id="su-peak" style="font-size:17px" title="Token 消耗最多的那一天（日期 · 当日 Token）">-</div><div class="bl">单日最高</div></div>
          <div class="bigstat"><div class="bv" id="su-grow">-</div><div class="bl">近7天环比</div></div>
        </div>
      </div>
      <div class="chartcard"><h3>💰 花费排行 Top 5（积分 + 官方价）</h3><div id="su-feerows"></div></div>
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
    <label>供应商名称</label><input id="f-name" placeholder="例如：火山方舟 / OpenAI">
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
<!-- 手动添加倍率（表单式弹窗，替代连环 prompt） -->
<dialog id="muadddlg" style="width:430px">
  <h3>手动添加倍率</h3>
  <p style="font-size:12px;color:var(--sub);margin:0 0 12px">仅第三方模型（供应商不能填「官方」；官方模型在「官方模型 → 模型管理」）。</p>
  <label>供应商</label><input id="madd-prov" placeholder="如：MyProvider">
  <label>模型 id</label><input id="madd-model" placeholder="如：example-model">
  <label>倍率</label><input id="madd-mult" type="number" step="0.01" min="0" value="1" style="width:130px">
  <div class="dlgacts">
    <button class="btn ghost" onclick="muadddlg.close()">取消</button>
    <button class="btn" onclick="submitMultiAdd()">添加</button>
  </div>
</dialog>
<!-- 手动添加价格（表单式弹窗） -->
<dialog id="pradddlg" style="width:430px">
  <h3>手动添加价格</h3>
  <p style="font-size:12px;color:var(--sub);margin:0 0 12px">每百万 token 官方价，币种按模型官方定价选。</p>
  <label>模型 id</label><input id="padd-model" placeholder="如：glm-5.3-flash">
  <label>输入价 /M</label><input id="padd-in" type="number" step="0.01" min="0" value="0" style="width:130px">
  <label>缓存命中价 /M</label><input id="padd-cache" type="number" step="0.01" min="0" value="0" style="width:130px">
  <label>输出价 /M</label><input id="padd-out" type="number" step="0.01" min="0" value="0" style="width:130px">
  <label>计价币种</label>
  <select id="padd-cur" style="width:130px"><option value="CNY">人民币 ¥</option><option value="USD">美元 $</option></select>
  <div class="dlgacts">
    <button class="btn ghost" onclick="pradddlg.close()">取消</button>
    <button class="btn" onclick="submitPricingAdd()">添加</button>
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
      <li><b>新增供应商</b>：填 API 地址和 Key，「加载全部模型」可直接从供应商拉取模型清单。</li>
      <li><b>启用某个预设</b>：写入 WorkBuddy 配置（models.json / settings.json / 偏好 / 会话记录四层一次写对），点右上角「重启使配置生效」后新会话默认用它。</li>
      <li><b>所有模型开关</b>：开启后全部供应商的模型同时注入 WorkBuddy 模型列表，不用来回切换；关闭恢复单模型。注意：WorkBuddy 按<b>模型 ID</b> 识别模型，不同供应商用了相同 ID（如三家都用 glm-5.3-flash）时同名条目会互相覆盖，只有排最前的（当前供应商）生效——这是 WorkBuddy 的机制限制。想四家都独立可用，给每家选不同的模型 ID 即可。</li>
    </ul>

    <h4>三、用量统计页看什么</h4>
    <ul>
      <li><b>总用量</b>：token 总数（过万显示 x.x万，过亿显示 x.x亿）。</li>
      <li><b>缓存命中率</b>：命中率越高越省钱——同样的上下文，命中缓存的部分按缓存价计费（通常只有正价的 5%~20%）。供应商命中率汇总表可对比各家的命中率，>=60% 绿 / >=30% 橙 / &lt;30% 红。</li>
      <li><b>费用</b>：第三方模型按官方价计（价格表内置 model_pricing.json，55 个官网正价模型），国内模型显示 ¥、国外模型显示 $，与官方计价币种一致；官方模型用<b>积分</b>显示，积分取自 WorkBuddy 会话的实际扣费记录（credit_json），同会话多模型按 token 占比分摊，第三方请求不消耗积分。</li>
      <li>支持 时间 / 类别 / 供应商 / 模型 四维筛选，全部图表联动。</li>
    </ul>

    <h4>四、日常使用</h4>
    <ul>
      <li>Windows 首选双击桌面 <b>WB Switch.exe</b>（桌面软件，独立应用窗口，关窗后服务仍在后台继续统计）；也可用 <b>WB Switch.bat</b>。macOS 双击 <b>WB Switch.command</b>。面板会自动接管端口加载最新版。</li>
      <li>WorkBuddy 更新后模型丢了 → 打开面板 → 点对应预设「启用」→ 重启 WorkBuddy，恢复。</li>
      <li>守护进程每 2 秒巡检，models.json 丢失或损坏会自动从快照恢复。</li>
      <li><b>科普栏</b>：点击侧边栏「💡 科普」，点文章标题展开 / 再点收起。讲清用量数据背后的原理（缓存命中率、成本等），教你看懂数字、省下真金白银。</li>
    </ul>

    <h4>五、免责声明</h4>
    <p class="disclaim">本工具为第三方开源辅助工具，与 WorkBuddy 官方无关。它只读写本机 WorkBuddy 配置文件与本地数据库（只读统计，不上传任何数据）。费用为按公开官方价的估算值，实际以各供应商账单为准。使用本工具产生的一切后果由使用者自行承担。</p>
    <p class="verline">WB Switch v2.55 · 零依赖单文件 · 数据目录 ~/.workbuddy/wb-switch</p>
  </div>
  <div class="dlgacts"><button class="btn" onclick="helpdlg.close()">我知道了</button></div>
</dialog>

<script>
/* 页面版本：每次发版 +1。前端发现服务器版本更新时自动 location.reload()，
   根治「浏览器缓存旧页面脚本导致显示错误数据」（如旧版昨天窗口 +1000 天） */
const PAGE_BUILD = 100;
(async()=>{try{
  if(sessionStorage.getItem('wb_reloaded'))return; /* 防刷新死循环：刷新过一次就不再自检重载 */
  const s=await (await fetch('/api/version')).json();
  if(s.build && s.build!==PAGE_BUILD){sessionStorage.setItem('wb_reloaded','1');location.reload();}
}catch(e){}})();
setInterval(async()=>{try{
  const s=await (await fetch('/api/version')).json();
  if(s.build && s.build>PAGE_BUILD && !sessionStorage.getItem('wb_reloaded')){
    sessionStorage.setItem('wb_reloaded','1');toast('检测到新版本，自动刷新…');setTimeout(()=>location.reload(),800);}
}catch(e){}},60000);
let presets=[], allSessions=[], providerMap={}, officialIds=[], reqAll=[], reqStats=[], reqTotal=0;
let curDays=7, curFrom=null, curTo=null;

const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtNumFull=n=>{n=Math.round(+n||0);return n.toLocaleString('en-US')};
const fmtTok=n=>{n=+n||0;return n>=1e8?(n/1e8).toFixed(2)+'亿':n>=1e4?(n/1e4).toFixed(1)+'万':String(Math.round(n))};
// 供应商简称 + 模型名合并展示："ProviderName model-id" / "官方 model-id"；
// 模型名剥 custom-local: 前缀，超过 28 字符用 … 截断
const trimId=id=>{id=String(id||'');if(id.startsWith('custom-local:'))id=id.slice(13);return id.length>28?id.slice(0,27)+'…':id};
const dispName=(prov,model,third)=>{const p=third?(prov||''):(prov||'官方');
  return p+' '+trimId(model||'')};
function toast(m){const t=$('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2200)}
function go(p,el){document.querySelectorAll('.nav').forEach(n=>n.classList.toggle('on',n===el));
  document.querySelectorAll('.page').forEach(pg=>pg.classList.toggle('on',pg.id==='page-'+p));
  if(p==='usage'||p==='stats')loadUsage(); if(p==='summary')loadSummary(); if(p==='logs')loadEvents(); if(p==='pricing')loadPricing(); if(p==='multi')loadMulti(); if(p==='official')loadOfficial();}

/* ---------- 模型倍率 ---------- */
let multiRows=[], multiOfficialRows=[];  // official_rows 仅供「官方模型→模型管理」取正价
async function loadMulti(){
  try{
    const d=await (await fetch('/api/multipliers')).json();
    multiRows=d.rows||[];
    multiOfficialRows=d.official_rows||[];
    renderMulti();
  }catch(e){$('mu-msg').textContent='加载失败：'+e}
}
function renderMulti(){
  // 价格列带币种符号（¥/$ 跟随价格表计价币种）；mid 用行索引，
  // 旧写法把中文供应商替换成下划线，不同供应商的同名模型 mid 会撞车，
  // 导致改一家的倍率、另一家的实际价跟着变（串行 bug）
  const f=(x,cur)=>x>0?(cur==='USD'?'$':'¥')+x.toLocaleString('zh-CN',{maximumFractionDigits:2}):'<span style="color:#c3cad9">无价格</span>';
  $('murows').innerHTML=multiRows.map((r,i)=>{
    const tagcls=r.cls==='third'?'t3':'builtin';
    const mid='mul-'+i;
    const hasMult=r.mult!==1;
    const cb=`<input type="checkbox" class="mu-del" data-key="${esc(r.key)}">`;
    return `<tr>
    <td class="tr">${cb}</td>
    <td class="tl"><span class="tag ${tagcls}">${esc(r.prov)}</span> <b>${esc(r.model)}</b><span style="color:#9aa3bd;font-size:11px;margin-left:6px">${r.reqs} 次</span></td>
    <td class="tr">${f(r.in,r.currency)}</td><td class="tr">${f(r.cache,r.currency)}</td><td class="tr">${f(r.out,r.currency)}</td>
    <td class="tr"><input type="number" step="0.01" min="0" placeholder="1" value="${hasMult?r.mult:''}"
      data-mid="${esc(r.key)}" data-cell="${mid}" style="width:90px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;font-size:12.5px"
      oninput="previewMulti(this)"></td>
    <td class="tr" id="${mid}-in">${f(r.in*(r.mult||1),r.currency)}</td>
    <td class="tr" id="${mid}-ca">${f(r.cache*(r.mult||1),r.currency)}</td>
    <td class="tr" id="${mid}-out">${f(r.out*(r.mult||1),r.currency)}</td>
  </tr>`}).join('')
    ||'<tr><td colspan="9" style="text-align:center;color:var(--sub)">还没有任何使用记录</td></tr>';
}
function toggleMuAll(on,grp){
  document.querySelectorAll('.mu-del').forEach(cb=>cb.checked=on);
}
async function delMulti(){
  const keys=[...document.querySelectorAll('.mu-del:checked')]
    .map(cb=>cb.dataset.key);
  if(!keys.length){toast('先勾选要删除的倍率行');return}
  if(!confirm('删除选中的 '+keys.length+' 个倍率？删除后按官方价（倍率 1）计算。'))return;
  try{
    const d=await (await fetch('/api/multipliers',{method:'POST',body:JSON.stringify({multipliers:{},remove:keys})})).json();
    $('mu-msg').textContent=d.message||('已删除 '+keys.length+' 个倍率');
    $('mu-msg').style.color=d.ok?'#0f8a55':'#d24a3e';
    if(d.ok){toast('已删除，费用已按官方价重算');loadMulti()}
  }catch(e){$('mu-msg').textContent='删除失败：'+e;$('mu-msg').style.color='#d24a3e'}
}
function exportMulti(){window.open('/api/export-multipliers')}
function importMulti(){
  const inp=document.createElement('input');inp.type='file';inp.accept='.json';
  inp.onchange=async()=>{
    try{
      const data=JSON.parse(await inp.files[0].text());
      const all=confirm('导入倍率\n\n【确定】= 全部修改：历史请求费用也按导入倍率重算\n【取消】= 当前修改：从现在起生效');
      const d=await (await fetch('/api/import-multipliers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data,mode:all?'all':'now'})})).json();
      $('mu-msg').textContent=d.message||('已导入 '+d.imported+' 条');
      $('mu-msg').style.color=d.ok?'#0f8a55':'#d24a3e';
      toast(d.message||'导入完成');loadMulti();
    }catch(e){toast('导入失败：'+e)}
  };
  inp.click();
}
function addMultiRow(){
  $('madd-prov').value=''; $('madd-model').value=''; $('madd-mult').value='1';
  muadddlg.showModal();
}
function submitMultiAdd(){
  const prov=($('madd-prov').value||'').trim(), mid=($('madd-model').value||'').trim();
  const v=parseFloat($('madd-mult').value);
  if(!prov||!mid){toast('请填写供应商和模型 id');return}
  if(isNaN(v)||v<0){toast('倍率需为非负数');return}
  if(prov==='官方'){
    toast('官方模型不在此设置，请到「官方模型 → 模型管理」操作');return;
  }else{
    fetch('/api/import-multipliers',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({data:{rows:[{key:prov+'|'+mid,multiplier:v}]},mode:'all'})})
      .then(r=>r.json()).then(d=>{toast(d.message||'已添加');muadddlg.close();loadMulti()})
      .catch(e=>toast('添加失败：'+e));
  }
}
function previewMulti(inp){
  const key=inp.dataset.mid, cell=inp.dataset.cell, v=parseFloat(inp.value);
  const r=multiRows.find(x=>x.key===key); if(!r)return;
  const sym=r.currency==='USD'?'$':'¥';
  const use=isNaN(v)?(r.mult||1):v;   // 清空 = 视回当前倍率
  const put=(id,val)=>{const el=document.getElementById(id);if(!el)return;
    if(!(val>0)){el.textContent='-';return}
    const res=val*use;
    el.textContent=sym+res.toLocaleString('zh-CN',{maximumFractionDigits:4});};
  put(cell+'-in',r.in); put(cell+'-ca',r.cache); put(cell+'-out',r.out);
}
async function saveMulti(mode){
  const m={};
  document.querySelectorAll('#murows input[data-mid]').forEach(inp=>{
    if(inp.value!==''&&!isNaN(parseFloat(inp.value)))m[inp.dataset.mid]=parseFloat(inp.value);
  });
  if(!Object.keys(m).length){toast('没有填写任何倍率');return}
  if(!mode){
    // 弹出选择框：全部修改（追溯所有历史请求） / 当前修改（从现在起生效）
    if(confirm('保存倍率\n\n【确定】= 全部修改：之前所有的请求费用都按新倍率重算\n【取消】= 当前修改：从现在开始生效，历史请求按原倍率保留')){
      mode='all';
    }else{
      mode='now';
    }
  }
  try{
    const d=await (await fetch('/api/multipliers',{method:'POST',body:JSON.stringify({multipliers:m,mode:mode})})).json();
    $('mu-msg').textContent=d.message||('已保存 '+d.saved+' 个倍率');
    $('mu-msg').style.color=d.ok?'#0f8a55':'#d24a3e';
    if(d.ok)toast('倍率已保存（'+(mode==='now'?'当前修改':'全部修改')+'），费用已重算');
    loadMulti();
  }catch(e){$('mu-msg').textContent='保存失败：'+e;$('mu-msg').style.color='#d24a3e'}
}
function __muMode(mode){saveMulti(mode)}

/* ---------- 价格表 ---------- */
let pricingRows=[];
async function loadPricing(){
  try{
    const d=await (await fetch('/api/pricing')).json();
    pricingRows=d.rows||[];
    $('pr-count').textContent='共 '+pricingRows.length+' 个模型 · 来源：cc-switch 价格库';
    renderPricing();
  }catch(e){$('pr-msg').textContent='加载失败：'+e}
}
function renderPricing(){
  const kw=($('pr-search').value||'').toLowerCase();
  const rows=pricingRows.filter(r=>!kw||(r.name||'').toLowerCase().includes(kw));
  // 每格都带币种符号：人民币 ¥、美元 $（与官方计价一致，不换算）
  const f=(x,sym)=>x>0?'<span class="pr-sym">'+sym+'</span>'+x.toLocaleString('zh-CN',{maximumFractionDigits:2}):'<span style="color:#c3cad9">—</span>';
  $('prrows').innerHTML=rows.map(r=>{
    const sym=r.cur==='USD'?'$':'¥';
    const isCny=r.cur!=='USD';
    return `<tr>
    <td class="tl"><b>${esc(r.name||'')}</b><span class="omid" style="margin-left:6px">${esc(r.id||'')}</span></td>
    <td class="tr">${f(r.in,sym)}</td><td class="tr">${f(r.cache,sym)}</td><td class="tr">${f(r.out,sym)}</td>
    <td class="tr"><span class="pr-cur ${isCny?'cny':'usd'}">${isCny?'人民币 ¥':'美元 $'}</span></td></tr>`}).join('')
    ||'<tr><td colspan="5" style="text-align:center;color:var(--sub)">没有匹配的模型</td></tr>';
  $('pr-count').textContent=kw?('匹配 '+rows.length+' / '+pricingRows.length+' 个模型'):('共 '+pricingRows.length+' 个模型 · 来源：cc-switch 价格库');
}
async function refreshPricing(){
  const btn=$('pr-refresh-btn');
  btn.disabled=true;btn.textContent='⏳ 正在向当前供应商查询…';
  $('pr-msg').textContent='正在调用当前供应商的模型查询最新官方价格，约需 30-120 秒…';
  try{
    const d=await (await fetch('/api/pricing-refresh',{method:'POST',body:'{}'})).json();
    $('pr-msg').textContent=d.message;
    $('pr-msg').style.color=d.ok?'#0f8a55':'#d24a3e';
    if(d.ok){await loadPricing()}
  }catch(e){$('pr-msg').textContent='失败原因：请求面板接口失败 '+e;$('pr-msg').style.color='#d24a3e'}
  btn.disabled=false;btn.textContent='↻ 获取最新价格表';
}
function exportPricing(){window.open('/api/export-pricing')}
function exportReqLog(){  // 请求日志 CSV：带当前筛选窗口
  let u='/api/export-requests?days='+curDays;
  if(curFrom)u+='&from='+curFrom; if(curTo)u+='&to='+curTo;
  window.open(u);
}
function importPricing(){
  const inp=document.createElement('input');inp.type='file';inp.accept='.json';
  inp.onchange=async()=>{
    try{
      const data=JSON.parse(await inp.files[0].text());
      const d=await (await fetch('/api/import-pricing',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data})})).json();
      $('pr-msg').textContent=d.message||('已导入 '+d.imported+' 条');
      $('pr-msg').style.color=d.ok&&d.imported?'#0f8a55':'#d24a3e';
      toast(d.message||'导入完成');await loadPricing();
    }catch(e){toast('导入失败：'+e)}
  };
  inp.oncancel=()=>toast('已取消。首次导入可先点「📄 模板」下载导入模板');
  inp.click();
}
function addPricingRow(){
  $('padd-model').value=''; $('padd-in').value='0'; $('padd-cache').value='0'; $('padd-out').value='0'; $('padd-cur').value='CNY';
  pradddlg.showModal();
}
function submitPricingAdd(){
  const id=($('padd-model').value||'').trim();
  const fin=parseFloat($('padd-in').value), fcache=parseFloat($('padd-cache').value), fout=parseFloat($('padd-out').value);
  const cur=$('padd-cur').value;
  if(!id){toast('请填写模型 id');return}
  if([fin,fcache,fout].some(isNaN)){toast('价格需为数字');return}
  fetch('/api/import-pricing',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({data:{rows:[{id:id,name:id,in:fin,out:fout,cache:fcache,currency:cur}]}})})
    .then(r=>r.json()).then(d=>{toast(d.message||'已添加');pradddlg.close();loadPricing()})
    .catch(e=>toast('添加失败：'+e));
}
function downloadTemplate(kind){
  window.open(kind==='multi'?'/api/template-multipliers':'/api/template-pricing');
}

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
// 费用格式化：彩色币种符号+数字同色 ¥(橙) $(绿) ∫(紫=积分)；¥/$ 保留 4 位小数，积分 2 位
function fmtCost(c,cur,dp){if(!c)return '';const col=cur==='USD'?'#15a35f':'#e07b28';return '<i class="cur" style="color:'+col+'">'+(cur==='USD'?'$':'¥')+'<span>'+c.toFixed(dp==null?4:dp)+'</span></i>';}
function fmtCredit(c){return '<i class="cur" style="color:#8b5cf6">∫<span>'+c.toFixed(2)+'</span></i>';}
let curCls='';
function setCls(v,el){curCls=v;
  // 两组类别 seg（用量页/统计页）同步高亮
  document.querySelectorAll('#segCls span, #segCls2 span').forEach(s=>{
    if(s.textContent===(el?el.textContent:''))s.classList.toggle('on',true);
    else s.classList.remove('on');});
  renderUsage();}
function setTime(d,el){curDays=d;curFrom=null;curTo=null;
  document.querySelectorAll('#segTime span, #segTime2 span').forEach(s=>{
    if(el&&(s.textContent===el.textContent||(el.dataset.d&&s.dataset.d===el.dataset.d)))s.classList.add('on');
    else s.classList.remove('on');});
  ['f-from','f-to','f-from2','f-to2'].forEach(id=>{const e=$(id);if(e)e.value='';});
  updateRangeHint&&updateRangeHint();  // 点档位立即更新范围提示（不等慢请求）
  loadUsage();}
// 「昨天」档：00:00:00 ~ 23:59:59（本地时区），以毫秒 from/to 下发
function setYesterday(el){
  // 昨天 = 前一自然日 00:00:00 ~ 23:59:59.999。旧写法把结束时间算成
  // y + 864e5*1000（多乘了 1000，≈1000 天），导致「昨天」和「全部」一样多——已修
  const y=new Date(Date.now()-864e5); y.setHours(0,0,0,0);
  const y2=new Date(y.getTime()+864e5-1);
  curFrom=y.getTime(); curTo=y2.getTime(); curDays=1;
  document.querySelectorAll('#segTime span, #segTime2 span').forEach(s=>{
    if(s.textContent==='昨天')s.classList.add('on');else s.classList.remove('on');});
  ['f-from','f-to','f-from2','f-to2'].forEach(id=>{const e=$(id);if(e)e.value='';});
  updateRangeHint&&updateRangeHint();
  loadUsage();}
function dateMirror(src,dst){const d=$(dst);if(d)d.value=$(src).value;}
// 统计页的供应商/模型下拉与用量页镜像联动
function provMirror(sel){$('f-prov').value=sel.value;fillFilterOptions();renderUsage();}
function modelMirror(sel){$('f-model').value=sel.value;renderUsage();}
function customRange(){
  // datetime-local 精确到分钟：yyyy-MM-ddTHH:mm
  const a=$('f-from').value||($('f-from2')?$('f-from2').value:'');
  const b=$('f-to').value||($('f-to2')?$('f-to2').value:'');
  if(a||b){curFrom=a?new Date(a).getTime():null;
    curTo=b?new Date(b).getTime():null;
    document.querySelectorAll('#segTime span, #segTime2 span').forEach(s=>s.classList.remove('on'));}
  updateRangeHint&&updateRangeHint();
  loadUsage();
}
let loadUsageSeq=0;  // 请求序号：快速切换时间档时丢弃过期响应（30天算得慢，后返回会覆盖先点的「昨天」）
async function loadUsage(){
  const seq=++loadUsageSeq;
  let url='/api/usage?days='+curDays;
  if(curFrom)url+='&from='+curFrom; if(curTo)url+='&to='+curTo;
  const d=await (await fetch(url)).json();
  if(seq!==loadUsageSeq)return;  // 已有更新的请求，丢弃本次过期结果
  allSessions=d.sessions||[]; providerMap=d.provider_map||{};
  try{
    let rurl='/api/requests?days='+curDays;
    if(curFrom)rurl+='&from='+curFrom; if(curTo)rurl+='&to='+curTo;
    const rq=await (await fetch(rurl)).json();
    if(seq!==loadUsageSeq)return;
    reqAll=rq.requests||[]; reqStats=rq.stats||[]; reqTotal=rq.total||reqAll.length;
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
  // 统计页镜像下拉同步（值跟随用量页）
  const p2=$('f-prov2'),m2=$('f-model2');
  if(p2){p2.innerHTML=$('f-prov').innerHTML;p2.value=$('f-prov').value;
    m2.innerHTML=$('f-model').innerHTML;m2.value=$('f-model').value;}
}
function filtered(){
  const p=$('f-prov').value,m=$('f-model').value;
  return allSessions.filter(s=>(!p||s.provider===p)&&(!m||s.model===m)&&(!curCls||s.cls===curCls));
}
function filtReq(){
  const p=$('f-prov').value,m=$('f-model').value;
  return reqAll.filter(r=>(!p||r.provider===p)&&(!m||r.model===m)&&(!curCls||r.cls===curCls));
}
// 命中率五段配色：达到哪段整段就是该色（>=90 绿 / 80-90 / 70-80 / 60-70 / <60 红）
function hitCls(h){return h>=90?'h5':h>=80?'h4':h>=70?'h3':h>=60?'h2':'h1';}
function hitCol(h){return h>=90?'#15a35f':h>=80?'#7cb342':h>=70?'#c2891b':h>=60?'#e07b28':'#c2504d';}
function setTab(groupId,tab,el){
  document.querySelectorAll('#'+groupId+' h3').forEach(h=>h.classList.toggle('on',h===el));
  ['model','prov'].forEach(t=>{const d=document.getElementById('dist-'+t);if(d)d.style.display=(groupId==='distTabs'&&t===tab)?'':'none';});
  ['feemodel','feeprov'].forEach(t=>{const d=document.getElementById('fee-'+t);if(d)d.style.display=(groupId==='feeTabs'&&t===tab)?'':'none';});
}
// 统计页主选项卡：费用统计 / Provider 统计 / 模型统计 / 会话明细 / Token 分布
function setMain(tab,el){
  document.querySelectorAll('#mainTabs h3').forEach(h=>h.classList.toggle('on',h===el));
  ['fee','prov','model','sess','dist'].forEach(t=>{
    const d=document.getElementById('main-'+t);if(d)d.style.display=(t===tab)?'':'none';});
}
// 请求日志分页（每页 30/50/100，数据上限 1000 条由后端截取）
let pageSize=30, pageIdx=0;function setPageSize(n,el){
  pageSize=n; pageIdx=0;
  document.querySelectorAll('#segPage span').forEach(s=>s.classList.toggle('on',s===el));
  renderUsage();
}
// 用量页自动刷新：5/10/30/60 秒一档，默认 30s；只在用量页可见时刷新（防后台占用）
let refreshSec=30, refreshTimer=null, refreshLeft=0;
function setRefresh(v){
  refreshSec=+v||30;
  if(refreshTimer)clearInterval(refreshTimer);
  refreshLeft=refreshSec;
  refreshTimer=setInterval(()=>{
    // 仅用量/统计页在前台时倒计时并刷新
    if(!$('page-usage').classList.contains('on')&&!$('page-stats').classList.contains('on'))return;
    refreshLeft--;
    if(refreshLeft<=0){refreshLeft=refreshSec;doRefresh(true);}
  },1000);
}
async function doRefresh(withDot){
  const dot=$('refdot');
  if(withDot&&dot){dot.classList.add('tick');setTimeout(()=>dot.classList.remove('tick'),400);}
  await loadUsage();
}
function pgMove(d){
  const rr=filtReq();
  const maxPage=Math.max(0,Math.ceil(rr.length/pageSize)-1);
  pageIdx=Math.min(maxPage,Math.max(0,pageIdx+d));
  renderUsage();
}
// 统计范围描述（用户口径）：当天=今天0点到现在；昨天=昨天0点~24点；
// N天=（N-1）天前0点~现在；全部=所有。用于页面实时提示，避免对边界产生疑惑
function updateRangeHint(){
  const t=rangeDesc();
  const hint=$('range-hint'); if(hint)hint.textContent=t;
  const hint2=$('range-hint2'); if(hint2)hint2.textContent=t;
}
function rangeDesc(){
  const f=t=>{const d=new Date(t);return d.getMonth()+1+'月'+d.getDate()+'日 '+String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0')};
  const fd=t=>{const d=new Date(t);return (d.getMonth()+1)+'月'+d.getDate()+'日'};
  const now=Date.now();
  if(curFrom&&curTo){
    const spanDays=Math.round((curTo-curFrom)/864e5);
    // 昨天档（1天且起点是昨天0点）特殊标注
    const y=new Date(now-864e5); y.setHours(0,0,0,0);
    if(spanDays<=1&&Math.abs(curFrom-y.getTime())<1000)return '📅 统计范围：昨天 00:00 ~ 23:59（'+fd(curFrom)+'全天，不含今天）';
    return '📅 统计范围：'+f(curFrom)+' ~ '+f(curTo);
  }
  if(curDays>=3650)return '📅 统计范围：全部历史 ~ 现在（'+fd(now)+'）';
  if(curDays===1)return '📅 统计范围：今天 00:00 ~ 现在（'+fd(now)+'）';
  return '📅 统计范围：'+fd(now-(curDays-1)*864e5)+' 00:00 ~ 现在（含今天，共 '+curDays+' 个自然日）';
}
function renderUsage(){
  updateRangeHint();
  const ss=filtered();
  // v2.65 数据同源：数据分析页所有统计选项卡统一用后端全量聚合 reqStats
  // （此前 Provider 统计用前端 2000 条请求现算、Token 分布/费用统计用会话级分摊积分，
  //   三套口径数字互对不上；reqStats = 后端按「供应商+模型」全量聚合，无截断）
  const p=$('f-prov').value,m=$('f-model').value;
  const rsAll=reqStats.filter(a=>(!p||a.provider===p)&&(!m||a.model===m)&&(!curCls||a.cls===curCls));
  // v2.65 总览大卡同源：Token/请求数/输入/输出/命中全部从 rsAll（后端全量聚合）算，
  // 与下方统计选项卡数字完全一致（此前总览用会话级、命中用前端 2000 条，对不上）
  const total=rsAll.reduce((a,s)=>a+s.inp+s.out,0);
  const credit=rsAll.reduce((a,s)=>a+(s.credit||0),0);
  // 费用按币种分开汇总（官方价分币种，不混算）
  const costCNY=rsAll.reduce((a,s)=>a+(s.cls==='third'&&s.currency!=='USD'?s.cost||0:0),0);
  const costUSD=rsAll.reduce((a,s)=>a+(s.cls==='third'&&s.currency==='USD'?s.cost||0:0),0);
  const inpS=rsAll.reduce((a,s)=>a+(s.inp||0),0), outS=rsAll.reduce((a,s)=>a+(s.out||0),0);
  // CC Switch 式总览大卡
  $('cc-total').firstChild.textContent=fmtNumFull(total);
  $('cc-total-approx').textContent=total>=1e4?'≈ '+fmtTotal(total):'';
  $('cc-reqs').textContent=rsAll.reduce((a,s)=>a+s.reqs,0);
  let costHtml='';
  if(costCNY>0)costHtml+='<div>'+fmtCost(costCNY,'CNY')+'</div>';
  if(costUSD>0)costHtml+='<div>'+fmtCost(costUSD,'USD')+'</div>';
  if(!costHtml&&credit>0)costHtml='<div>'+fmtCredit(credit)+'</div>';
  $('cc-cost').innerHTML=costHtml||'<div>-</div>';
  $('cc-inp').textContent=fmtTok(inpS);
  $('cc-out').textContent=fmtTok(outS);
  $('cc-sess').textContent=new Set(ss.map(s=>s.id)).size;
  const dayset=[...new Set(allSessions.map(s=>s.ts?new Date(s.ts).toDateString():null))].filter(Boolean);
  const span=Math.max(1,curFrom&&curTo?Math.ceil((curTo-curFrom)/864e5):curDays>365?3650:curDays);
  const cinp=inpS, ccache=rsAll.reduce((a,r)=>a+r.cached,0);
  $('cc-cache').textContent=fmtTok(ccache);
  // 缓存命中率：细进度条（动态五段变色）
  const hit=cinp?ccache*100/cinp:0;
  const hitEl=$('cc-hitpct');
  hitEl.textContent=cinp?hit.toFixed(1)+'%':'-';
  hitEl.className=cinp?hitCls(hit):'';
  const fill=$('hitbar-fill');
  fill.style.width=(cinp?Math.min(hit,100):0)+'%';
  if(cinp)fill.style.backgroundColor=hitCol(hit);
  $('hitbar-legend').innerHTML=[['≥90 优秀','#15a35f'],['80-89 良好','#7cb342'],['70-79 一般','#c2891b'],['60-69 偏低','#e07b28'],['<60 差','#c2504d']]
    .map(x=>`<span><b style="background:${x[1]}"></b>${x[0]}</span>`).join('');
  // 按模型 / 按供应商 Token 分布（v2.65 同源：用后端全量聚合 rsAll，与模型统计同口径）
  // 条长按占总 token 的百分比；行尾附费用与请求次数
  const totAll=rsAll.reduce((a,s)=>a+s.inp+s.out,0)||1;
  const aggRows=(keyFn,cls)=>{
    const g={}; rsAll.forEach(a=>{const k=keyFn(a);g[k]=g[k]||{t:0,n:0,cny:0,usd:0,cr:0,third:false};
      g[k].t+=a.inp+a.out; g[k].n+=a.reqs;
      if(a.cls==='third'){g[k].third=true; if(a.currency==='USD')g[k].usd+=a.cost||0; else g[k].cny+=a.cost||0;}
      else g[k].cr+=a.credit||0;
    });
    const arr=Object.entries(g).sort((x,y)=>y[1].t-x[1].t).slice(0,10);
    const feeHtml=x=>[x.cr>0?fmtCredit(x.cr):'',x.cny>0?fmtCost(x.cny,'CNY'):'',x.usd>0?fmtCost(x.usd,'USD'):''].filter(Boolean).join(' ')||'';
    return arr.map(([name,x])=>{const pc=x.t/totAll*100;
      return `<div class="mrow ${cls}"><span class="mname" title="${esc(name)}">${esc(name)}<i class="clsbadge ${x.third?'t3':''}">${x.third?'第三方':'官方'}</i></span><span class="mbarwrap" title="占总 Token ${pc.toFixed(1)}%"><span class="mbar" style="display:block;width:${pc}%"></span><span class="mpctin">${pc.toFixed(1)}%</span></span><span class="mval"><span class="mtok">${fmtTok(x.t)}</span><span class="msess">${x.n} 请求</span>${feeHtml(x)?'<span class="mfee">'+feeHtml(x)+'</span>':''}</span></div>`}).join('')||'<span style="color:var(--sub);font-size:12px">暂无数据</span>'};
  $('modelrows').innerHTML=aggRows(a=>dispName(a.provider,a.model,a.cls==='third'),'');
  $('provrows').innerHTML=aggRows(a=>a.provider,'m2');
  // ---- 费用统计（v2.65 同源：与 Provider/模型统计同一套 rsAll 聚合）----
  // 官方=逐请求精确积分合计，第三方=官方价 ¥/$ 分币种合计；占比以"费用"为尺
  const feeRender=(rows,elId)=>{
    if(!rows.length){$(elId).innerHTML='<tr><td colspan="4" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';return}
    const sumC=rows.reduce((a,r)=>a+r.cny,0), sumU=rows.reduce((a,r)=>a+r.usd,0), sumCr=rows.reduce((a,r)=>a+r.cr,0);
    const scale=v=>v.cr>0?v.cr*0.01+v.cny+v.usd*7.2:v.cny+v.usd*7.2;  // 排序/占比用近似尺
    const arr=[...rows].sort((a,b)=>scale(b)-scale(a));
    const totScale=arr.reduce((a,r)=>a+scale(r),0);
    const fmtFee=r=>[r.cr>0?fmtCredit(r.cr):'',r.cny>0?fmtCost(r.cny,'CNY'):'',r.usd>0?fmtCost(r.usd,'USD'):''].filter(Boolean).join(' + ')||'-';
    let html=arr.map(r=>{
      const pc=totScale?scale(r)/totScale*100:0;
      const cls=r.cr>0&&r.cny<=0&&r.usd<=0?'builtin':(r.cr>0?'':'t3');
      return `<tr>
      <td class="tl"><span class="tag ${cls}">${esc(r.name)}</span></td>
      <td class="tl">${r.cr>0?(r.cny>0||r.usd>0?'积分+Token':'官方·积分'):'第三方·Token'}</td>
      <td class="tr"><b>${fmtFee(r).split(' + ').map(x=>'<div>'+x+'</div>').join('')}</b></td>
      <td class="tr"><span class="pct"><i style="width:${pc}%"></i></span>${pc.toFixed(1)}%</td></tr>`;
    }).join('');
    html+=`<tr style="background:#f4f7fe;font-weight:700">
      <td class="tl">合计</td><td class="tl"></td>
      <td class="tr">${[sumCr>0?'<div>'+fmtCredit(sumCr)+'</div>':'',sumC>0?'<div>'+fmtCost(sumC,'CNY')+'</div>':'',sumU>0?'<div>'+fmtCost(sumU,'USD')+'</div>':''].filter(Boolean).join('')||'-'}</td>
      <td class="tr">100%</td></tr>`;
    $(elId).innerHTML=html;
  };
  const feeModel={}, feeProv={};
  rsAll.forEach(a=>{
    const name=dispName(a.provider,a.model,a.cls==='third');
    const fm=feeModel[name]=feeModel[name]||{name,cny:0,usd:0,cr:0};
    const fp=feeProv[a.provider]=feeProv[a.provider]||{name:a.provider,cny:0,usd:0,cr:0};
    if(a.cls==='third'){if(a.currency==='USD'){fm.usd+=a.cost||0;fp.usd+=a.cost||0}else{fm.cny+=a.cost||0;fp.cny+=a.cost||0}}
    else {fm.cr+=a.credit||0;fp.cr+=a.credit||0}
  });
  feeRender(Object.values(feeModel),'feemodelrows2');
  feeRender(Object.values(feeProv),'feeprovrows2');
  // 明细
  $('sessrows').innerHTML=ss.slice(0,50).map(s=>{
    const bill=s.cls==='third'
      ?(s.cost>0?`<span class="tag t3">${fmtCost(s.cost,s.currency)}</span>`:'<span class="tag t3">Token 计费</span>')
      :(s.credit>0?`<span class="tag builtin">${fmtCredit(s.credit)}</span>`:'<span class="tag builtin">官方·积分</span>');
    return `<tr>
    <td class="tl" style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(s.title)}">${esc(s.title)}</td>
    <td class="tl"><span class="tag ${s.cls==='third'?'t3':'builtin'}">${esc(dispName(s.provider,s.model,s.cls==='third'))}</span></td>
    <td class="tr" title="输入(含缓存) ${fmtTok(s.inp||0)} + 输出 ${fmtTok(s.out||0)}">${fmtTok(s.used)}<i style="opacity:.55;font-size:10.5px;margin-left:5px">${s.reqs||0}次</i></td>
    <td class="tl">${bill}</td>
    <td class="tr"><span class="pct"><i style="width:${Math.min(s.pct,100)}%"></i></span>${s.pct}%</td>
    <td class="tl">${s.ts?new Date(s.ts).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'-'}</td></tr>`}).join('')
    ||'<tr><td colspan="6" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
  // ---- 请求维度：Provider 统计（v2.65 同源：改用后端全量聚合 rsAll，不再用前端 2000 条现算）----
  // 按供应商聚合：请求数/输入/缓存/输出/命中率/费用全部与模型统计同口径
  const pg={}; rsAll.forEach(a=>{const g=pg[a.provider]=pg[a.provider]||{reqs:0,inp:0,cached:0,out:0,third:false,cny:0,usd:0,credit:0};
    g.reqs+=a.reqs;g.inp+=a.inp;g.cached+=a.cached;g.out+=a.out;if(a.cls==='third')g.third=true;
    if(a.cls==='third'){if(a.currency==='USD')g.usd+=a.cost||0;else g.cny+=a.cost||0;}
    else g.credit+=a.credit||0;});
  const parr=Object.entries(pg).sort((a,b)=>b[1].inp-a[1].inp);
  $('provhit').innerHTML=parr.map(([pr,g])=>{
    const hit2=g.inp?Math.round(g.cached*1000/g.inp)/10:0;
    const cost=(g.cny>0?fmtCost(g.cny,'CNY'):'')+(g.usd>0?(g.cny>0?' + ':'')+fmtCost(g.usd,'USD'):'');
    const fee=g.third?cost:(g.credit>0?fmtCredit(g.credit):'<span style="color:#9aa3bd">∫0</span>');
    return `<tr>
    <td class="tl"><span class="tag ${g.third?'t3':'builtin'}">${esc(pr)}</span></td>
    <td class="tr">${g.reqs}</td><td class="tr">${fmtTok(g.inp)}</td><td class="tr">${fmtTok(g.cached)}</td><td class="tr">${fmtTok(g.out)}</td>
    <td class="tr"><b class="${hitCls(hit2)}">${hit2}%</b></td>
    <td class="tr">${fee}</td></tr>`}).join('')
    ||'<tr><td colspan="7" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
  // 按模型统计（同一 rsAll，仅按模型粒度）
  const rs=rsAll;
  $('reqstats').innerHTML=rs.map(a=>{
    // 官方积分说明：该模型调用 x 次共花费 m 积分，平均每次 m/x 积分
    const crTip=a.cls==='official'&&a.credit>0
      ?` title="此模型调用 ${a.reqs} 次共花费 ${a.credit.toFixed(2)} 积分，平均每次 ${(a.credit/a.reqs).toFixed(2)} 积分（整轮扣费按 token 占比分摊到每条请求）"`
      :(a.cls==='official'?' title="官方模型：当前窗口内积分扣 0（免费或无实扣记录）"':'');
    const fee=a.cls==='third'
      ?(a.cost>0?fmtCost(a.cost,a.currency):'-')
      :(a.credit>0?fmtCredit(a.credit):'<span style="color:#9aa3bd">∫0</span>');
    return `<tr${crTip}>
    <td class="tl"><span class="tag ${a.cls==='third'?'t3':'builtin'}">${esc(dispName(a.provider,a.model,a.cls==='third'))}</span></td>
    <td class="tr">${a.reqs}</td><td class="tr">${fmtTok(a.inp)}</td><td class="tr">${fmtTok(a.cached)}</td><td class="tr">${fmtTok(a.out)}</td>
    <td class="tr"><b class="${hitCls(a.hit)}">${a.hit}%</b></td>
    <td class="tr">${fee}</td></tr>`}).join('')
    ||'<tr><td colspan="7" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
  // 请求日志：分页（最新在前），数据上限 1000 条
  // 官方请求费用 = 该条请求的精确积分（后端按 conversationRequestId 从 credit_json 逐条对应）
  const rr=reqAll.filter(r=>(!p||r.provider===p)&&(!m||r.model===m)&&(!curCls||r.cls===curCls));
  const logAll=rr.slice().reverse();   // 后端已截取最新 2000 条（reqTotal），前端不再重复截
  const maxPage=Math.max(0,Math.ceil(logAll.length/pageSize)-1);
  if(pageIdx>maxPage)pageIdx=maxPage;
  const pageRows=logAll.slice(pageIdx*pageSize,(pageIdx+1)*pageSize);
  $('pg-info').textContent=logAll.length?`第 ${pageIdx+1} / ${maxPage+1} 页 · 共 ${reqTotal} 条`:'-';
  $('pg-prev').disabled=pageIdx<=0;
  $('pg-next').disabled=pageIdx>=maxPage;
  $('reqrows').innerHTML=pageRows.map(r=>{
    let fee, hasFormula=false;
    if(r.cls==='third'){fee=r.cost>0?fmtCost(r.cost,r.currency):'-';hasFormula=r.cost>0&&r.pin>0;}
    else if(r.credit>0){
      // 实扣积分（紫）恒定优先；无实扣记录的轮用估算积分（灰）补显，之后有实扣自动覆盖
      fee=r.credit_est
        ?`<span title="估算积分（该轮无实扣记录，按官方正价折算）；一旦有实扣记录自动改为实扣值">∫<span>${r.credit.toFixed(4)}</span><i style="font-style:normal;font-size:10px;color:#9aa3bd;margin-left:2px">估</i></span>`
        :fmtCredit(r.credit);
      hasFormula=true;
    } else fee='<span style="color:#9aa3bd">∫0</span>';
    const feeHtml=hasFormula
      ?`<span class="feecell" id="fee-${r.rid}" onclick="toggleFee('${r.rid}')">${fee}</span>`
      :fee;
    return `<tr>
    <td class="tl">${new Date(r.ts).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</td>
    <td class="tl"><span class="tag ${r.cls==='third'?'t3':'builtin'}">${esc(dispName(r.provider,r.model,r.cls==='third'))}</span></td>
    <td class="tr">${fmtTok(r.inp)}</td>
    <td class="tr">${fmtTok(r.cached)}</td><td class="tr">${fmtTok(r.out)}</td>
    <td class="tr"><b class="${hitCls(r.hit)}">${r.hit}%</b></td>
    <td class="tr">${feeHtml}</td></tr>`+(hasFormula?`<tr class="feefrow" id="feef-${r.rid}" style="display:none"><td colspan="7" style="padding:0 12px;background:#fbfcff">${feeFormulaHtml(r)}</td></tr>`:'')
  }).join('')
    ||'<tr><td colspan="7" style="text-align:center;color:var(--sub)">当前筛选条件下暂无数据</td></tr>';
}
// 费用计算明细：第三方=官方价公式；官方=整轮积分按 token 占比分摊公式
function feeFormulaHtml(r){
  const sym=r.currency==='USD'?'$':'¥';
  const f6=x=>x.toLocaleString('zh-CN',{maximumFractionDigits:2});
  const f8=x=>x.toFixed(x<0.01?6:4);
  if(r.cls==='third'){
    const parts=[];
    if(r.plain>0)parts.push(`<span class="fk">输入</span>${f6(r.plain)} tok ÷ 1M × ${sym}${f8(r.pin)}/M = ${sym}${f8(r.plain/1e6*r.pin)}`);
    if(r.cached>0)parts.push(`<span class="fk">缓存命中</span>${f6(r.cached)} tok ÷ 1M × ${sym}${f8(r.pcache)}/M = ${sym}${f8(r.cached/1e6*r.pcache)}`);
    if(r.out>0)parts.push(`<span class="fk">输出</span>${f6(r.out)} tok ÷ 1M × ${sym}${f8(r.pout)}/M = ${sym}${f8(r.out/1e6*r.pout)}`);
    // 连加式只列实际存在的分项（修掉「输入费用 + + 缓存费用 + + 输出费用」的重复加号）
    const labels=[];
    if(r.plain>0)labels.push('输入费用');
    if(r.cached>0)labels.push('缓存费用');
    if(r.out>0)labels.push('输出费用');
    const calc=labels.length?labels.join(' + '):'0';
    // 官方价与倍率分行：官方单价永远显示价格表正价，倍率另起一行
    const hasMult=r.omult&&r.omult!==1;
    const priceLines=hasMult
      ?`<div><span class="fk">📦 官方单价</span>${esc(r.model)}（每百万 token，同步价格表）：<span class="fv">输入 ${sym}${f8(r.opin)} / 缓存 ${sym}${f8(r.opcache)} / 输出 ${sym}${f8(r.opout)}</span></div>
      <div><span class="fk">⚖️ 本模型实际倍率</span>${r.omult}（同步倍率）：<span class="fv">输入 ${sym}${f8(r.pin)} / 缓存 ${sym}${f8(r.pcache)} / 输出 ${sym}${f8(r.pout)}</span></div>`
      :`<div><span class="fk">📦 官方单价</span>${esc(r.model)}（每百万 token，同步价格表）：<span class="fv">输入 ${sym}${f8(r.pin)} / 缓存 ${sym}${f8(r.pcache)} / 输出 ${sym}${f8(r.pout)}</span></div>`;
    return `<div class="feeformula show" onclick="event.stopPropagation()">
      ${priceLines}
      ${parts.map(x=>'• '+x).join('<br>')}
      <div style="margin-top:4px"><span class="fk">🧮 计算</span><span class="fml">费用 = ${calc}</span> = <span class="fv">${sym}${f8(r.cost)}</span></div>
      <div style="color:#9aa3bd;font-size:11px">第三方花费按官方公开价 × 倍率估算，实际以供应商账单为准。</div>
    </div>`;
  }
  // 官方：v2.69 钱·分摊——实扣轮按金额占比、估算轮按金额÷系数
  if(r.credit_est){
    const coef=r.est_coef||0;
    const m=r.money||0;
    const mp=[];
    if(r.inp-r.cached>0)mp.push(`输入 ${f6(r.inp-r.cached)} ÷ 1M × ¥${f8(r.opin||r.pin)}`);
    if(Math.min(r.cached,r.inp)>0)mp.push(`缓存命中 ${f6(Math.min(r.cached,r.inp))} ÷ 1M × ¥${f8(r.opcache||r.pcache)}`);
    if(r.out>0)mp.push(`输出 ${f6(r.out)} ÷ 1M × ¥${f8(r.opout||r.pout)}`);
    return `<div class="feeformula show" onclick="event.stopPropagation()">
    <div><span class="fk">📦 计费方式</span>WorkBuddy 官方按整轮对话扣积分，但该轮（${esc((r.creq||'').slice(0,10))}…）在本地扣费记录里<b>查不到实扣数据</b>（会话已删或记录未回写），按官方正价分项算钱 ÷ 数据推导系数<b>估算补显</b>。</div>
    <div style="margin-top:4px"><span class="fk">① 本条算钱</span>${mp.length?mp.join(' + ')+' = ¥'+f8(m):'¥'+f8(m)+'（无官方价，按窗口平均单价折算）'}</div>
    <div style="margin-top:4px"><span class="fk">② 除以系数</span><span class="fml">¥${f8(m)} ÷ ¥${coef.toFixed(4)}/积分</span> = <span class="fv">∫${f8(r.credit)}</span><i style="font-style:normal;font-size:10px;color:#9aa3bd;margin-left:4px">估</i></div>
    <div style="color:#9aa3bd;font-size:11px">¥${coef.toFixed(4)}/积分为数据推导系数（Σ有实扣轮的官方正价金额 ÷ Σ该轮实扣积分，随时间窗实测变化，非硬编码）；估算值仅供参考，该轮一旦出现实扣记录会自动替换。</div>
  </div>`;
  }
  return `<div class="feeformula show" onclick="event.stopPropagation()">
    <div><span class="fk">📦 计费方式</span>WorkBuddy 官方按整轮对话扣积分，<b>两边都算成钱</b>：本轮（会话 ${esc(r.session)}…）实扣 <span class="fv">∫${f8(r.cr_round)}</span>，轮金额合计 <span class="fv">¥${f8(r.cr_money||0)}</span>，本条按官方正价分项算钱 = <span class="fv">¥${f8(r.money||0)}</span></div>
    <div style="margin-top:4px"><span class="fk">🧮 按钱分摊</span><span class="fml">本条积分 = ∫${f8(r.cr_round)} × ¥${f8(r.money||0)} ÷ ¥${f8(r.cr_money||0)}</span> = <span class="fv">∫${f8(r.credit)}</span></div>
    <div style="color:#9aa3bd;font-size:11px">同一轮的多条子请求按各自金额占比分摊整轮积分（比 token 占比更贴近真实计费）；实扣积分恒定不变。</div>
  </div>`;
}
function toggleFee(id){
  const row=document.getElementById('feef-'+id);
  if(!row)return;
  const open=row.style.display!=='none';
  row.style.display=open?'none':'';
  const cell=document.getElementById('fee-'+id);
  if(cell)cell.style.borderBottomStyle=open?'dashed':'solid';
}

/* ---------- 官方模型 ---------- */
let ofDays=7, ofFrom=null, ofTo=null, ofData=null, ofCat=null;
function ofTab(t){
  // v2.66 顶部横排选项卡（maintabs h3 按钮），内容全宽在下方，无侧边留白
  $('of-tab-usage').classList.toggle('on',t==='usage');
  $('of-tab-manage').classList.toggle('on',t==='manage');
  $('of-pane-usage').style.display=t==='usage'?'':'none';
  $('of-pane-manage').style.display=t==='manage'?'':'none';
  if(t==='manage')loadOfficialCatalog();
}
async function loadOfficialCatalog(){
  try{
    const d=await (await fetch('/api/official-catalog')).json();
    ofCat=d;
    // 官方正价三列数据来自倍率接口的 official_rows（key=官方|模型id → in/cache/out）
    try{
      const m=await (await fetch('/api/multipliers')).json();
      ofPrices={};
      (m.official_rows||[]).forEach(r=>{ofPrices[r.model]={in:r.in,cache:r.cache,out:r.out}});
    }catch(e){ofPrices={}}
    renderOfficialCatalog();
  }catch(e){$('of-cat-msg').textContent='加载失败：'+e}
}
let ofPrices={};
function renderOfficialCatalog(){
  const d=ofCat; if(!d)return;
  $('of-mcnt').textContent=d.count||0;
  const when=d.pulled_at?new Date(d.pulled_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'-';
  $('of-catalog-info').textContent='共 '+(d.count||0)+' 个 · 来源：'+(d.source||'-')+' · 拉取时间：'+when;
  const fprice=x=>x>0?'¥'+x.toLocaleString('zh-CN',{maximumFractionDigits:2}):'<span style="color:#c3cad9">无价格</span>';
  $('ofcatrows').innerHTML=(d.models||[]).map(m=>{
    const p=ofPrices[m.id]||{};
    return `<tr>
    <td class="tl"><span class="tag builtin">官方</span> <b>${esc(m.name||m.id)}</b></td>
    <td class="tl"><span class="omid">${esc(m.id)}</span></td>
    <td class="tr">${fprice(p.in)}</td><td class="tr">${fprice(p.cache)}</td><td class="tr">${fprice(p.out)}</td>
    <td class="tr">${m.credits?esc(m.credits):'-'}</td>
    <td class="tr">${m.ctx?fmtCtx(m.ctx):'-'}</td>
    <td class="tr"><button class="btn danger sm" onclick="delOfficialModel('${esc(m.id)}')">删除</button></td>
  </tr>`}).join('')||'<tr><td colspan="8" style="text-align:center;color:var(--sub)">清单为空，点「恢复内置清单」</td></tr>';
}
async function pullOfficialCatalog(){
  const btn=$('of-pull-btn');
  btn.disabled=true;btn.textContent='⏳ 正在拉取…';
  try{
    const d=await (await fetch('/api/official-catalog',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'pull'})})).json();
    $('of-cat-msg').textContent=d.message||d.error||'';
    $('of-cat-msg').style.color=d.ok?'#0f8a55':'#d24a3e';
    toast(d.message||'拉取完成');
    if(d.ok)await loadOfficialCatalog();
  }catch(e){toast('拉取失败：'+e)}
  btn.disabled=false;btn.textContent='⟳ 重新拉取官方模型';
}
async function delOfficialModel(id){
  if(!confirm('删除官方模型「'+id+'」？\n（只从清单移除，用量统计不受影响）'))return;
  try{
    const d=await (await fetch('/api/official-catalog',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'delete',id})})).json();
    $('of-cat-msg').textContent=d.message||d.error||'';
    $('of-cat-msg').style.color=d.ok?'#0f8a55':'#d24a3e';
    if(d.ok)await loadOfficialCatalog();
  }catch(e){toast('删除失败：'+e)}
}
async function resetOfficialCatalog(){
  if(!confirm('恢复内置官方模型清单？（覆盖本地改动）'))return;
  try{
    const d=await (await fetch('/api/official-catalog',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'reset'})})).json();
    toast(d.message||'已恢复');
    if(d.ok)await loadOfficialCatalog();
  }catch(e){toast('恢复失败：'+e)}
}
function ofSet(d,el){ofDays=d;ofFrom=null;ofTo=null;
  document.querySelectorAll('#segOf span').forEach(s=>{
    if(el&&(s.textContent===el.textContent||(el.dataset.d&&s.dataset.d===el.dataset.d)))s.classList.add('on');
    else s.classList.remove('on');});
  $('of-from').value='';$('of-to').value='';
  loadOfficial();}
function ofYesterday(el){
  // 同 setYesterday：旧写法结束时间多乘 1000（≈1000 天），昨天=全部，已修
  const y=new Date(Date.now()-864e5); y.setHours(0,0,0,0);
  ofFrom=y.getTime(); ofTo=y.getTime()+864e5-1; ofDays=1;
  document.querySelectorAll('#segOf span').forEach(s=>{
    if(s.textContent==='昨天')s.classList.add('on');else s.classList.remove('on');});
  $('of-from').value='';$('of-to').value='';
  loadOfficial();}
function ofCustom(){
  const a=$('of-from').value,b=$('of-to').value;
  if(a||b){ofFrom=a?new Date(a).getTime():null;
    ofTo=b?new Date(b).getTime():null;
    document.querySelectorAll('#segOf span').forEach(s=>s.classList.remove('on'));}
  loadOfficial();
}
let loadOfficialSeq=0;  // 同 loadUsage：丢弃快速切换时的过期响应
async function loadOfficial(){
  const seq=++loadOfficialSeq;
  let url='/api/official?days='+ofDays;
  if(ofFrom)url+='&from='+ofFrom; if(ofTo)url+='&to='+ofTo;
  try{
    const d=await (await fetch(url)).json();
    if(seq!==loadOfficialSeq)return;
    ofData=d;
    renderOfficial();
  }catch(e){$('of-msg').textContent='加载失败：'+e}
}
function renderOfficial(){
  const d=ofData; if(!d)return;
  const t=d.totals||{}, rows=d.rows||[];
  $('of-rounds').textContent=fmtNumFull(t.rounds||0);
  $('of-models').textContent='共 '+rows.length+' 个官方模型';
  $('of-credit').innerHTML='∫'+(t.credit||0).toFixed(2);
  $('of-credit-sub').textContent='积分来自实际扣费记录，免费模型计 0';
  // v2.67 实际积分价值 = 实扣积分 × 数据推导系数（官方正价总成本 ÷ 实扣总积分）
  const coef=t.coefficient||0;
  $('of-cost').innerHTML='¥'+(t.worth_cny||0).toFixed(2);
  let sub='系数 ¥'+coef.toFixed(4)+'/积分';
  if(coef>0)sub+='（官方正价成本 ÷ 实扣积分，按本时间窗实测）';
  if(t.cny_all>0)sub+='；官方正价参考价 ¥'+t.cny_all.toFixed(2);
  $('of-cost-sub').textContent=sub;
  $('ofrows').innerHTML=rows.map((r,i)=>{
    const f=r.free?' <span class="tag builtin" title="该时间窗内积分扣 0">免费</span>':'';
    // v2.67 金额列 = 实扣积分 × 系数（实际积分价值）；展开公式里给官方正价成本对照
    const worth=r.worth??(r.credit*(t.coefficient||0));
    const fee=r.credit>0
      ?`<span class="feecell" id="offee-${i}" onclick="ofToggleFee(${i})">¥${worth.toFixed(4)}</span>`
      :'-';
    const credit=r.credit>0?'∫'+r.credit.toFixed(2):'0';
    const per=r.rounds?(r.credit/r.rounds).toFixed(2):'0';
    return `<tr>
    <td class="tl"><span class="tag builtin">官方</span> <b>${esc(r.model)}</b>${f}</td>
    <td class="tr"><span class="feecell" onclick="ofToggleRounds(${i})">${r.rounds}</span></td>
    <td class="tr">${per}</td>
    <td class="tr">${credit}</td>
    <td class="tr">${fmtTok(r.inp)}</td><td class="tr">${fmtTok(r.cached)}</td><td class="tr">${fmtTok(r.out)}</td>
    <td class="tr">${fee}</td></tr>`
    +(r.credit>0?`<tr class="feefrow" id="offeef-${i}" style="display:none"><td colspan="8" style="padding:0 12px;background:#fbfcff">${ofFeeFormula(r,t.coefficient||0)}</td></tr>`:'')
    +`<tr id="ofrounds-${i}" style="display:none"><td colspan="8" style="padding:0 12px;background:#fbfcff">${ofRoundsHtml(r)}</td></tr>`;
  }).join('')||'<tr><td colspan="8" style="text-align:center;color:var(--sub)">当前时间范围内暂无官方模型用量</td></tr>';
  // 请求日志（复用用量统计明细，仅官方模型；积分 0 的免费请求也显示）
  const lg=d.req_log||[], lgT=d.req_log_total||lg.length;
  $('of-logcount').textContent=lg.length?('共 '+lgT+' 条'+(lgT>lg.length?'，显示最新 '+lg.length+' 条':'')):'';
  $('ofreqrows').innerHTML=lg.map((r,li)=>{
    // 积分列：实扣紫色 / 估算灰色带「估」标 / 0 积分 ∫0；全部可点击展开计算方式
    let crCell;
    if(r.credit>0){
      crCell=r.est
        ?`<span title="估算积分（该轮无实扣记录，按官方正价折算）；一旦有实扣记录自动改为实扣值">∫${r.credit.toFixed(2)}<i style="font-style:normal;font-size:10px;color:#9aa3bd;margin-left:2px">估</i></span>`
        :fmtCredit(r.credit);
    } else crCell='<span style="color:#9aa3bd">∫0</span>';
    const clickable=r.credit>0||r.est;
    const feeCell=clickable
      ?`<span class="feecell" id="ofrq-${li}" onclick="ofReqToggle(${li})">${crCell}</span>`
      :crCell;
    return `<tr>
    <td class="tl">${new Date(r.ts).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}</td>
    <td class="tl"><span class="tag builtin">官方</span> <b>${esc(r.model)}</b></td>
    <td class="tr">${fmtTok(r.inp)}</td><td class="tr">${fmtTok(r.cached)}</td><td class="tr">${fmtTok(r.out)}</td>
    <td class="tr">${feeCell}</td></tr>`
    +(clickable?`<tr class="feefrow" id="ofrqf-${li}" style="display:none"><td colspan="6" style="padding:0 12px;background:#fbfcff">${ofReqFormula(r)}</td></tr>`:'');
  }).join('')||'<tr><td colspan="6" style="text-align:center;color:var(--sub)">当前时间范围内暂无官方请求记录</td></tr>';
}
// 官方请求日志单条积分的计算方式（v2.69 钱·分摊：两边都算成钱，金额占比 × 积分）
function ofReqFormula(r){
  const f6=x=>x.toLocaleString('zh-CN',{maximumFractionDigits:2});
  const f8=x=>x.toFixed(x<0.01?6:4);
  if(r.est){
    const coef=r.coef||0;
    if(r.has_price){
      const parts=[];
      if(r.inp-r.cached>0)parts.push(`输入 ${f6(r.inp-r.cached)} tok ÷ 1M × ¥${f8(r.pin)} = ¥${f8((r.inp-Math.min(r.cached,r.inp))/1e6*r.pin)}`);
      if(r.cached>0)parts.push(`缓存命中 ${f6(r.cached)} tok ÷ 1M × ¥${f8(r.pcache)} = ¥${f8(Math.min(r.cached,r.inp)/1e6*r.pcache)}`);
      if(r.out>0)parts.push(`输出 ${f6(r.out)} tok ÷ 1M × ¥${f8(r.pout)} = ¥${f8(r.out/1e6*r.pout)}`);
      const calc=parts.length?parts.join(' + '):'0';
      return `<div class="feeformula show" onclick="event.stopPropagation()">
    <div><span class="fk">📦 计费方式</span>该轮（${esc((r.creq||'').slice(0,10))}…）在本地扣费记录里<b>查不到实扣数据</b>（会话已删或记录未回写），按官方正价分项算钱 ÷ 系数<b>估算补显</b>。</div>
    <div style="margin-top:4px">${parts.map(x=>'• '+x).join('<br>')}</div>
    <div style="margin-top:4px"><span class="fk">🧮 估算</span><span class="fml">估算积分 = (${calc}) ÷ ¥${coef.toFixed(4)}/积分</span> = <span class="fv">∫${f8(r.credit)}</span><i style="font-style:normal;font-size:10px;color:#9aa3bd;margin-left:4px">估</i></div>
    <div style="color:#9aa3bd;font-size:11px">¥${coef.toFixed(4)}/积分为数据推导系数（Σ有实扣轮的官方正价金额 ÷ Σ该轮实扣积分，随时间窗实测变化，非硬编码）；估算值仅供参考，该轮一旦出现实扣记录会自动替换。</div>
  </div>`;
    }
    return `<div class="feeformula show" onclick="event.stopPropagation()">
    <div><span class="fk">📦 计费方式</span>该模型<b>没有官方价格</b>。无官方价的模型按窗口平均单价（¥/token）折算：本条 ${f6(r.inp+r.out)} tok × ¥${Number(r.avg||0).toExponential(3)} ≈ ¥${f8(r.money||0)}，再 ÷ 系数 ¥${coef.toFixed(4)} 得估算积分。</div>
    <div style="margin-top:4px"><span class="fk">🧮 估算</span><span class="fml">${f6(r.inp+r.out)} tok × 平均单价 ÷ ¥${coef.toFixed(4)}/积分</span> = <span class="fv">∫${f8(r.credit)}</span><i style="font-style:normal;font-size:10px;color:#9aa3bd;margin-left:4px">估</i></div>
    <div style="color:#9aa3bd;font-size:11px">平均单价 = 该时间窗内有官方价的官方请求总金额 ÷ 总 token（简单平均，无其他加权）；估算值仅供参考。</div>
  </div>`;
  }
  const moneyParts=[];
  if(r.inp-r.cached>0)moneyParts.push(`输入 ${f6(r.inp-r.cached)} tok ÷ 1M × ¥${f8(r.pin)} = ¥${f8((r.inp-Math.min(r.cached,r.inp))/1e6*r.pin)}`);
  if(r.cached>0)moneyParts.push(`缓存命中 ${f6(r.cached)} tok ÷ 1M × ¥${f8(r.pcache)} = ¥${f8(Math.min(r.cached,r.inp)/1e6*r.pcache)}`);
  if(r.out>0)moneyParts.push(`输出 ${f6(r.out)} tok ÷ 1M × ¥${f8(r.pout)} = ¥${f8(r.out/1e6*r.pout)}`);
  return `<div class="feeformula show" onclick="event.stopPropagation()">
    <div><span class="fk">📦 计费方式</span>WorkBuddy 官方按整轮对话扣积分，<b>两边都算成钱</b>：本轮（${esc((r.creq||'').slice(0,10))}…）实扣 <span class="fv">∫${f8(r.cr_round)}</span>，轮金额合计 <span class="fv">¥${f8(r.cr_money||0)}</span>（轮内各条按官方正价分项算钱求和）。</div>
    <div style="margin-top:4px"><span class="fk">① 本条算钱</span>${moneyParts.map(x=>'• '+x).join('<br>')}　→ <b>¥${f8(r.money||0)}</b></div>
    <div style="margin-top:4px"><span class="fk">② 按钱分摊</span><span class="fml">本条积分 = ∫${f8(r.cr_round)} × ¥${f8(r.money||0)} ÷ ¥${f8(r.cr_money||0)}</span> = <span class="fv">∫${f8(r.credit)}</span></div>
    <div style="color:#9aa3bd;font-size:11px">同一轮的多条子请求按各自金额占比分摊整轮积分（金额占比权重比 token 占比更贴近真实计费）；实扣积分恒定不变。</div>
  </div>`;
}
function ofReqToggle(i){const row=document.getElementById('ofrqf-'+i);if(!row)return;
  row.style.display=row.style.display==='none'?'':'none';}
// 实际积分价值的计算公式（v2.67）：官方正价分项算成本 → 系数 = 总成本 ÷ 实扣总积分 → 积分 × 系数
function ofFeeFormula(r,coef){
  const sym=r.currency==='USD'?'$':'¥';
  const f6=x=>x.toLocaleString('zh-CN',{maximumFractionDigits:2});
  const f8=x=>x.toFixed(x<0.01?6:4);
  const mult=r.omult??1;
  const worth=r.worth??(r.credit*(coef||0));
  const parts=[];
  if(r.plain>0)parts.push(`<span class="fk">输入</span>${f6(r.plain)} tok ÷ 1M × ${sym}${f8(r.pin)}/M = ${sym}${f8(r.plain/1e6*r.pin)}`);
  if(r.cached>0)parts.push(`<span class="fk">缓存命中</span>${f6(r.cached)} tok ÷ 1M × ${sym}${f8(r.pcache)}/M = ${sym}${f8(r.cached/1e6*r.pcache)}`);
  if(r.out>0)parts.push(`<span class="fk">输出</span>${f6(r.out)} tok ÷ 1M × ${sym}${f8(r.pout)}/M = ${sym}${f8(r.out/1e6*r.pout)}`);
  const labels=[];
  if(r.plain>0)labels.push('输入');
  if(r.cached>0)labels.push('缓存');
  if(r.out>0)labels.push('输出');
  const calc=labels.length?labels.join(' + '):'0';
  // 积分倍率仅作判断标识行：0=免费免积分，非0=消耗积分；不进公式
  const multLine=mult===0
    ?`<div><span class="fk">⚖️ 积分倍率</span><span style="color:#15a35f;font-weight:600">0（免费：免积分消耗标识，不参与计算）</span></div>`
    :(mult!==1
      ?`<div><span class="fk">⚖️ 积分倍率</span>${mult}（标识：该模型消耗积分；不参与计算）</div>`
      :`<div><span class="fk">⚖️ 积分倍率</span>1（标识：正常消耗积分；不参与计算）</div>`);
  return `<div class="feeformula show" onclick="event.stopPropagation()">
    <div><span class="fk">📦 官方单价</span>${esc(r.model)}（每百万 token，同步价格表）：<span class="fv">输入 ${sym}${f8(r.pin)} / 缓存 ${sym}${f8(r.pcache)} / 输出 ${sym}${f8(r.pout)}</span></div>
    ${multLine}
    ${parts.map(x=>'• '+x).join('<br>')}
    <div style="margin-top:4px"><span class="fk">🧮 第一步·官方正价成本</span><span class="fml">${calc}</span> = <span class="fv">${sym}${f8(r.cost)}</span>（仅作系数回归的分子，不直接等于实际花费）</div>
    <div style="margin-top:4px"><span class="fk">🧮 第二步·数据推导系数</span><span class="fml">系数 = 全部官方模型正价总成本 ÷ 实扣总积分</span> = <span class="fv">¥${(coef||0).toFixed(4)}/积分</span>（按本时间窗实测，非硬编码）</div>
    <div style="margin-top:4px"><span class="fk">🧮 第三步·实际积分价值</span><span class="fml">∫${f8(r.credit)}（实扣积分） × ¥${(coef||0).toFixed(4)}</span> = <span class="fv">¥${f8(worth)}</span></div>
    <div style="color:#9aa3bd;font-size:11px">官方模型实际按积分扣费。系数由本时间窗数据回归：按官方正价把输入/输出/缓存命中分项算出总成本，除以实扣总积分得出；积分倍率（官方模型 → 模型管理里设置）不参与计算，仅用于判断：0=免费免积分，非0=消耗积分。</div>
  </div>`;
}
// 每轮明细：该模型的每一轮调了哪些模型、各多少次、整轮积分
function ofRoundsHtml(r){
  const det=(ofData.detail||{})[r.model]||[];
  if(!det.length)return '<div class="feeformula show">暂无轮次明细</div>';
  return `<div class="feeformula show" onclick="event.stopPropagation()">
    <div style="margin-bottom:4px">每轮明细（每次对话调用了哪些模型、各多少次；积分为该轮整轮扣费）：</div>
    <table style="width:100%;border-collapse:collapse;font-size:11.5px">
      <thead><tr><th style="text-align:left">轮次</th><th style="text-align:left">调用的模型</th><th style="text-align:right">该轮积分</th></tr></thead>
      <tbody>${det.map(rd=>`<tr>
        <td class="tl" style="font-family:monospace">${esc(String(rd.creq).slice(0,10))}…</td>
        <td class="tl">${rd.comps.map(c=>esc(c.m)+' ×'+c.n).join('，')}</td>
        <td class="tr">∫${rd.credit.toFixed(2)}</td></tr>`).join('')}</tbody>
    </table></div>`;
}
function ofToggleFee(i){const row=document.getElementById('offeef-'+i);if(!row)return;
  row.style.display=row.style.display==='none'?'':'none';}
function ofToggleRounds(i){const row=document.getElementById('ofrounds-'+i);if(!row)return;
  row.style.display=row.style.display==='none'?'':'none';}

/* ---------- 汇总战报 ---------- */
let sumDays=3650, sumData=null, sumFrom=null, sumTo=null;
function setSum(d,el){sumDays=d;sumFrom=null;sumTo=null;
  document.querySelectorAll('#segSum span').forEach(s=>{
    if(el&&(s.textContent===el.textContent||(el.dataset.s&&s.dataset.s===el.dataset.s)))s.classList.add('on');
    else s.classList.remove('on');});
  loadSummary();}
function setSumYesterday(el){
  const y=new Date(Date.now()-864e5); y.setHours(0,0,0,0);
  sumFrom=y.getTime(); sumTo=y.getTime()+864e5-1; sumDays=1;  // 旧写法多乘1000（昨天=全部），已修
  document.querySelectorAll('#segSum span').forEach(s=>{
    if(s.textContent==='昨天')s.classList.add('on');else s.classList.remove('on');});
  loadSummary();}
let loadSummarySeq=0;  // 同 loadUsage：丢弃快速切换时的过期响应
async function loadSummary(){
  const seq=++loadSummarySeq;
  try{
    let u='/api/usage?days='+sumDays;
    if(sumFrom)u+='&from='+sumFrom; if(sumTo)u+='&to='+sumTo;
    const d=await (await fetch(u)).json();
    if(seq!==loadSummarySeq)return;
    const ss=d.sessions||[];
    // v2.68 按日统计全部改用后端 daily（逐请求真实时间聚合），不再按会话最后活跃时间归堆：
    // 跨天大会话的 token 之前被整堆到最后活跃那天，单日最高/活跃天数/环比/趋势全部失真，
    // 且「全部」档的单日最高（4.3亿）会与「当天」档的真实消耗（2.6亿）对不上。
    const darr=(d.daily||[]).map(x=>[x.day,x.used]);
    const dayset=new Set(darr.map(a=>a[0]));
    // 总 token：窗口内逐日相加（= 会话行 used 之和，口径一致）
    const total=darr.reduce((a,x)=>a+x[1],0);
    $('su-token').textContent=fmtTotal(total);
    $('su-sess').textContent=ss.length;
    $('su-active').textContent=dayset.size;
    const g={}; ss.forEach(s=>{g[s.model]=(g[s.model]||0)+s.used});
    const marr=Object.entries(g).sort((a,b)=>b[1]-a[1]);
    $('su-models').textContent=marr.length;
    // 花费/积分汇总（官方=积分，第三方=官方价分币种）
    const sumCr=ss.reduce((a,s)=>a+(s.credit||0),0);
    const sumCNY=ss.reduce((a,s)=>a+(s.cls==='third'&&s.currency!=='USD'?s.cost||0:0),0);
    const sumUSD=ss.reduce((a,s)=>a+(s.cls==='third'&&s.currency==='USD'?s.cost||0:0),0);
    $('su-credit').innerHTML=sumCr>0?fmtCredit(sumCr):'-';
    $('su-cost').innerHTML=(sumCNY>0?fmtCost(sumCNY,'CNY'):'')+(sumUSD>0?(sumCNY>0?'<br>':'')+fmtCost(sumUSD,'USD'):'')||'-';
    // 命中率：后端已在 /api/usage 算好（hit 字段），不再二次拉 3.6MB 请求明细
    let hitPct=d.hit||0, hitTxt=d.hit!=null?d.hit.toFixed(1)+'%':'-';
    const hitEl=$('su-hit');
    hitEl.textContent=hitTxt;
    hitEl.className='bv '+(hitTxt==='-'?'':hitCls(hitPct));
    let peak=null; darr.forEach(x=>{if(!peak||x[1]>peak[1])peak=x});
    $('su-peak').textContent=peak?(peak[0].slice(5)+' · '+fmtTok(peak[1])):'-';
    // 近7天 vs 前7天（自然日口径，按 daily 数组切；与时间筛选档位无关，恒定反映最近两周）
    {
      const dmap={}; darr.forEach(a=>dmap[a[0]]=a[1]);
      const dstr=n=>{const t=new Date(Date.now()-n*864e5);return t.getFullYear()+'-'+String(t.getMonth()+1).padStart(2,'0')+'-'+String(t.getDate()).padStart(2,'0')};
      let l7=0,p7=0;
      for(let i=0;i<7;i++)l7+=dmap[dstr(i)]||0;
      for(let i=7;i<14;i++)p7+=dmap[dstr(i)]||0;
      let gt='-', grow=0;
      if(p7>0){grow=Math.round((l7-p7)/p7*100);gt=(grow>=0?'+':'')+grow+'%'}
      else if(l7>0){gt='新纪录';grow=999}
      $('su-grow').textContent=gt;
      $('su-grow').style.color=grow>0?'#0f8a55':(grow<0?'#c0564f':'var(--pri)');
    }
    // Top5 排行（token）
    const medals=['🥇','🥈','🥉','4️⃣','5️⃣'];
    const top=marr.slice(0,5);
    const mx=Math.max(1,...top.map(a=>a[1]));
    $('su-modelrows').innerHTML=top.map((a,i)=>`<div class="mrow"><span class="mname"><span class="surank">${medals[i]||i+1}</span>${esc(a[0])}</span><span class="mbarwrap"><span class="mbar" style="display:block;width:${a[1]/mx*100}%"></span></span><span class="mval">${fmtTok(a[1])}</span></div>`).join('')
      ||'<span style="color:var(--sub);font-size:12px">暂无数据</span>';
    // 花费排行 Top5（积分与金额统一折算排序，显示原值）
    const fg={}; ss.forEach(s=>{
      const k=dispName(s.provider,s.model,s.cls==='third');
      const f=fg[k]=fg[k]||{name:k,cny:0,usd:0,cr:0};
      if(s.cls==='third'){if(s.currency==='USD')f.usd+=s.cost||0;else f.cny+=s.cost||0}
      else f.cr+=s.credit||0;
    });
    const scale=f=>f.cr*0.01+f.cny+f.usd*7.2;
    const farr=Object.values(fg).sort((a,b)=>scale(b)-scale(a)).slice(0,5);
    // 条长按折算值占比（相对于 Top5 总和），避免第一名恒满格、与显示金额脱节
    const ftot=Math.max(1e-9,...farr.map(scale),farr.reduce((a,f)=>a+scale(f),0));
    const fmx=Math.max(1e-9,...farr.map(scale));
    $('su-feerows').innerHTML=farr.map((f,i)=>`<div class="mrow"><span class="mname"><span class="surank">${medals[i]||i+1}</span>${esc(f.name)}</span><span class="mbarwrap"><span class="mbar" style="display:block;width:${Math.max(2,scale(f)/ftot*100)}%"></span></span><span class="mval">${[f.cr>0?fmtCredit(f.cr):'',f.cny>0?fmtCost(f.cny,'CNY'):'',f.usd>0?fmtCost(f.usd,'USD'):''].filter(Boolean).join(' + ')||'-'}</span></div>`).join('')
      ||'<span style="color:var(--sub);font-size:12px">暂无数据</span>';
    // 趋势
    const tarr=darr.slice(-30);
    const dmax=Math.max(1,...tarr.map(a=>a[1]));
    $('su-trend').innerHTML=tarr.map(a=>`<div class="bar" style="height:${Math.max(3,a[1]/dmax*100)}%" data-tip="${a[0]}：${fmtTok(a[1])}"><span class="d">${a[0].slice(5)}</span></div>`).join('')
      ||'<span style="color:var(--sub)">暂无数据</span>';
    sumData={range:sumFrom?'自定义':(sumDays>=3650?'全部':(sumDays===1?'当天':'近'+sumDays+'天')),total,sess:ss.length,
      active:dayset.size,models:marr.length,peak:peak?peak[0]+'（'+fmtTok(peak[1])+'）':'无',
      grow:gt,top:marr.slice(0,3),
      credit:sumCr,cny:sumCNY,usd:sumUSD,hit:hitTxt,
      feeTop:farr.slice(0,3).map(f=>f.name+'：'+([f.cr>0?fmtCredit(f.cr):'',f.cny>0?fmtCost(f.cny,'CNY'):'',f.usd>0?fmtCost(f.usd,'USD'):''].filter(Boolean).join(' + ')))};
  }catch(e){toast('汇总加载失败')}
}
/* v2.66 复制战报：把看板区（三组卡片 + 排行 + 趋势）渲染成图片复制到剪贴板。
   html2canvas 内嵌精简实现太重，改用原生 SVG foreignObject 截图：
   DOM → SVG foreignObject → canvas → ClipboardItem(png)。Chrome/Edge 支持。 */
async function copyReport(){
  if(!sumData){toast('数据还没加载好');return}
  const btn=event&&event.target;
  if(btn){btn.disabled=true;btn.textContent='⏳ 正在生成图片…'}
  try{
    // 截图范围：汇总页过滤条以下的整个看板（三组卡片+排行+趋势）
    const sec=document.getElementById('page-summary');
    const cards=sec.querySelector('.filters');
    const x=cards.offsetLeft, y=cards.offsetTop+cards.offsetHeight;
    const w=sec.scrollWidth-x-10, h=sec.scrollHeight-y-10;
    const svgStr=`<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}">
      <foreignObject width="100%" height="100%">
        <div xmlns="http://www.w3.org/1999/xhtml" style="width:${w}px;padding:18px;background:linear-gradient(135deg,#eef2ff,#fdf6ec);font-family:'Segoe UI','Microsoft YaHei',sans-serif">
          <div style="font-size:22px;font-weight:800;color:#1b2233;margin-bottom:4px">🔥 我的 WorkBuddy 使用战报（${esc(sumData.range)}）</div>
          <div style="font-size:12px;color:#6b7490;margin-bottom:14px">由 WB Switch 生成 · ${new Date().toLocaleString('zh-CN')}</div>
          ${document.getElementById('page-summary').querySelector('.sumgroup, .chartcard') ? Array.from(document.querySelectorAll('#page-summary .sumgroup, #page-summary .chartcard')).map(n=>n.outerHTML).join('') : ''}
        </div>
      </foreignObject>
    </svg>`;
    const img=new Image();
    const svgBlob=new Blob([svgStr],{type:'image/svg+xml;charset=utf-8'});
    const url=URL.createObjectURL(svgBlob);
    await new Promise((res,rej)=>{img.onload=res;img.onerror=rej;img.src=url});
    const scale=2;  // 2x 清晰度
    const cv=document.createElement('canvas');
    cv.width=w*scale; cv.height=h*scale;
    const ctx=cv.getContext('2d');
    ctx.scale(scale,scale);
    ctx.drawImage(img,0,0);
    URL.revokeObjectURL(url);
    const blob=await new Promise(r=>cv.toBlob(r,'image/png'));
    await navigator.clipboard.write([new ClipboardItem({'image/png':blob})]);
    toast('战报图片已复制，直接粘贴发给朋友');
  }catch(e){
    // 剪贴板写图片失败（权限/浏览器不支持）→ 降级下载 PNG
    try{
      const a=document.createElement('a');
      a.download='wb战报_'+(sumData.range||'')+'.png';
      a.href=cv.toDataURL('image/png');
      a.click();
      toast('已下载战报图片（浏览器不支持剪贴板图片）');
    }catch(e2){toast('生成战报图片失败：'+e.message)}
  }finally{
    if(btn){btn.disabled=false;btn.textContent='📋 复制战报图片'}
  }
}

/* ---------- 科普栏 ---------- */
function kbToggle(id){
  document.getElementById(id).classList.toggle('open');
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
setRefresh(30);  // 用量页自动刷新，默认 30 秒
setInterval(pollStatus,6000);
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

    def _download(self, filename, text):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
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

    def _query(self):
        """解析查询串为 dict。旧写法 self.path.split("?")[1:] 没按 & 拆分，
        days=1&from=..&to=.. 整段塞进 days，int() 失败后 frm/to 全丢，
        导致「昨天」档位返回 30 天数据（与全部一样多）。"""
        q = {}
        parts = self.path.split("?", 1)
        if len(parts) < 2 or not parts[1]:
            return q
        for part in parts[1].split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                q[k] = v
        return q

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
        elif path == "/api/official-catalog":
            # 官方模型管理页数据：写死清单/本地快照 + 手动拉取时间
            models, src = load_official_catalog()
            ts = 0
            try:
                with open(OFFICIAL_CATALOG_TS, "r", encoding="utf-8") as f:
                    ts = float(json.load(f).get("ts") or 0)
            except (OSError, ValueError):
                pass
            self._json({"ok": True, "models": models or [], "source": src,
                        "pulled_at": ts, "count": len(models or [])})
        elif path == "/api/pricing":
            d = _load_pricing()
            rows = []
            for r in sorted(d.values(), key=lambda x: x.get("name", "")):
                r2 = dict(r)
                # 币种：价格表自带 cur 优先，否则按模型族判断（中国模型 ¥ / 美元模型 $，不做换算）
                r2["cur"] = r.get("cur") or _currency_for(r.get("name", ""))
                rows.append(r2)
            self._json({"ok": True, "rows": rows,
                        "file": CCS_PRICING_FILE, "count": len(rows)})
        elif path == "/api/multipliers":
            # 官方 + 第三方两组倍率：第三方按「供应商+模型」计 token 费用；
        # 官方按「官方|模型id」设置积分倍率（仅判断标识：0=免费免积分，非0=消耗积分，不参与计算）。
            mult = _load_multipliers()
            pricing = _load_pricing()
            pname_map = _preset_vendor_names()
            used = {}
            for r in scan_requests():
                b = bare_id(r["model"])
                prov, cls = _classify_request(
                    r.get("pname") or "", b, pname_map, None,
                    billed=r.get("billed", 0), raw_model=r.get("model") or "")
                k = _mkey(prov, b)
                u = used.setdefault(k, {"prov": prov, "model": b, "reqs": 0, "cls": cls})
                u["reqs"] += 1
            third_rows, official_rows = [], []
            for k, u in sorted(used.items(), key=lambda x: -x[1]["reqs"]):
                p = _price_for_raw(u["model"], pricing)
                row = {"key": k, "prov": u["prov"], "model": u["model"],
                       "cls": u["cls"], "reqs": u["reqs"],
                       "in": p.get("in", 0) if p else 0,
                       "out": p.get("out", 0) if p else 0,
                       "cache": p.get("cache", 0) if p else 0,
                       "mult": _mult_value(mult.get(k, 1.0)),
                       "currency": ((p.get("cur") if p else None) or _currency_for(u["model"]))}
                if u["cls"] == "third":
                    third_rows.append(row)
                else:
                    official_rows.append(row)
            # 官方清单里出现但没请求记录的官方模型也列出
            # （「官方模型 → 模型管理」表格要显示每个官方模型的正价三列）
            official_ids_seen = {r["model"] for r in official_rows}
            cat_models, cat_src = load_official_catalog()
            for m in (cat_models or []):
                mid = str(m.get("id") or "")
                if not mid or mid.startswith("custom-local:") or mid in official_ids_seen:
                    continue
                p = _price_for_raw(mid, pricing)
                official_rows.append({"key": _mkey("官方", mid), "prov": "官方", "model": mid,
                                      "cls": "official", "reqs": 0,
                                      "in": p.get("in", 0) if p else 0,
                                      "out": p.get("out", 0) if p else 0,
                                      "cache": p.get("cache", 0) if p else 0,
                                      "mult": _mult_value(mult.get(_mkey("官方", mid), 1.0)),
                                      "currency": "CNY"})  # 官方模型全部中国模型，人民币计费
            # 手动添加/导入、但还没有请求记录的倍率也列出（否则「添加无效」——
            # 保存成功但 UI 永远不显示，用户以为没加上）
            seen_keys = {r["key"] for r in third_rows} | {r["key"] for r in official_rows}
            for k, v in mult.items():
                if k in seen_keys or "|" not in str(k):
                    continue
                prov, mid = str(k).split("|", 1)
                p = _price_for_raw(mid, pricing)
                row = {"key": k, "prov": prov, "model": mid,
                       "cls": "official" if prov == "官方" else "third", "reqs": 0,
                       "in": p.get("in", 0) if p else 0,
                       "out": p.get("out", 0) if p else 0,
                       "cache": p.get("cache", 0) if p else 0,
                       "mult": _mult_value(v),
                       "currency": ((p.get("cur") if p else None) or _currency_for(mid))}
                (official_rows if row["cls"] == "official" else third_rows).append(row)
            self._json({"ok": True, "rows": third_rows, "official_rows": official_rows,
                        "count": len(third_rows), "official_count": len(official_rows)})
        elif path == "/api/official":
            q = self._query()
            try:
                days = min(max(int(q.get("days", 30)), 1), 3650)
            except ValueError:
                days = 30
            frm = int(q["from"]) if q.get("from", "").isdigit() else None
            to = int(q["to"]) if q.get("to", "").isdigit() else None
            self._json(get_official(days=days, frm=frm, to=to))

        elif path == "/api/events":
            with _state_lock:
                evs = list(_state["events"])
            self._json({"events": evs})
        elif path == "/api/export-requests":
            # 请求日志导出 CSV（随当前筛选窗口，最多 2000 条与页面一致）
            q = self._query()
            try:
                days = min(max(int(q.get("days", 7)), 1), 3650)
            except ValueError:
                days = 7
            frm = int(q["from"]) if q.get("from", "").isdigit() else None
            to = int(q["to"]) if q.get("to", "").isdigit() else None
            data = get_requests(days=days, frm=frm, to=to)
            rows = get_requests_slim(data).get("requests") or []
            import csv as _csv
            import io as _io
            buf = _io.StringIO()
            buf.write("﻿")  # UTF-8 BOM：Excel 直接打开不乱码
            w = _csv.writer(buf)
            w.writerow(["时间", "供应商", "模型", "类别", "输入", "缓存命中", "输出", "费用", "币种", "积分"])
            for r in rows:
                ts = r.get("ts")
                tstr = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
                w.writerow([tstr, r.get("provider") or "", r.get("model") or "",
                            "官方" if r.get("cls") == "official" else "第三方",
                            r.get("inp") or 0, r.get("cached") or 0, r.get("out") or 0,
                            r.get("cost") or "", r.get("currency") or "",
                            r.get("credit") or ""])
            self._download("wb-switch-requests.csv", buf.getvalue())
        elif path == "/api/version":
            self._json({"build": 100, "version": "1.0.0"})
        elif path == "/api/wb-status":
            self._json({"running": wb_running()})
        elif path == "/api/usage":
            q = self._query()
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
            q = self._query()
            try:
                days = min(max(int(q.get("days", 30)), 1), 3650)
            except ValueError:
                days = 30
            frm = int(q["from"]) if q.get("from", "").isdigit() else None
            to = int(q["to"]) if q.get("to", "").isdigit() else None
            self._json(get_requests_slim(get_requests(days=days, frm=frm, to=to)))
        elif path == "/api/export-multipliers":
            mult = _load_multipliers()
            rows = [{"key": k, "multiplier": _mult_value(v)} for k, v in mult.items()]
            body = json.dumps({"type": "multipliers", "version": 1, "rows": rows},
                              ensure_ascii=False, indent=1)
            self._download("wb-switch-multipliers.json", body)
        elif path == "/api/export-pricing":
            d = _load_pricing()
            rows = []
            for k, v in d.items():
                rows.append({"id": k, "name": v.get("name") or k,
                             "in": v.get("in", 0), "out": v.get("out", 0),
                             "cache": v.get("cache", 0),
                             "currency": v.get("cur") or _currency_for(k)})
            body = json.dumps({"type": "pricing", "version": 1, "rows": rows},
                              ensure_ascii=False, indent=1)
            self._download("wb-switch-pricing.json", body)
        elif path == "/api/template-multipliers":
            rows = [
                {"key": "MyProvider|example-model", "multiplier": 0.5},
                {"key": "AnotherProvider|example-model-2", "multiplier": 1.0},
                {"key": "FreeProvider|example-free-model", "multiplier": 0},
            ]
            body = json.dumps({"type": "multipliers", "version": 1, "rows": rows,
                               "_说明": "key 格式 = 供应商|模型id，仅第三方模型（官方模型不参与倍率计算，其正价见「官方模型 → 模型管理」）。倍率 0.5=五折。"},
                              ensure_ascii=False, indent=1)
            self._download("multiplier-template.json", body)
        elif path == "/api/template-pricing":
            rows = [
                {"id": "example-model-a", "name": "Example Model A", "in": 2.0, "out": 8.0, "cache": 0.2, "currency": "CNY"},
                {"id": "example-model-b", "name": "Example Model B", "in": 10.0, "out": 40.0, "cache": 2.5, "currency": "USD"},
            ]
            body = json.dumps({"type": "pricing", "version": 1, "rows": rows,
                               "_说明": "每百万 token 价格；currency 只能是 CNY 或 USD，与模型官方计价币种一致，不做汇率换算。"},
                              ensure_ascii=False, indent=1)
            self._download("pricing-template.json", body)
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
        elif path == "/api/pricing-refresh":
            ok, msg = refresh_pricing_online()
            log_event("info" if ok else "error", "价格表在线更新: " + msg)
            self._json({"ok": ok, "message": msg})
        elif path == "/api/multipliers":
            m = payload.get("multipliers")
            remove = payload.get("remove")
            mode = payload.get("mode") or "all"   # all=全部修改(追溯历史) / now=当前修改(从现在起生效)
            if not isinstance(m, dict):
                self._json({"ok": False, "error": "参数错误"}, 400)
                return
            now_ms = int(time.time() * 1000)
            merged = _load_multipliers()
            removed = 0
            if isinstance(remove, list):
                for k in remove:
                    if str(k) in merged:
                        del merged[str(k)]
                        removed += 1
            clean = {}
            for k, v in m.items():
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue  # 非法值按未填写处理 = 倍率 1
                if fv < 0 or "|" not in str(k):
                    continue  # 键必须是 供应商|模型 组合；负数拒绝，0 和正数都走同一套保存逻辑
                k = str(k)
                old = merged.get(k)
                old_segs = _mult_hist(old) if old is not None else []
                if mode == "now":
                    # 当前修改：旧时段原样保留（历史请求按当时的倍率计），
                    # 追加一段「从现在起生效」的新倍率；若与最后一段倍率相同则不重复追加
                    segs = list(old_segs)
                    if segs and abs(segs[-1][1] - fv) < 1e-9 and segs[-1][0] <= now_ms:
                        pass  # 倍率没变，不追加
                    else:
                        segs.append((float(now_ms), fv))
                    clean[k] = {"cur": fv, "hist": segs}
                else:
                    # 全部修改：清掉历史，整条时间线都按新倍率重算
                    clean[k] = {"cur": fv, "hist": [[0.0, fv]]}
            merged.update(clean)
            # 保存时以页面提交为准：页面未提交且未勾选保留的旧键一并清掉（空白=1）
            if not isinstance(remove, list):
                merged = clean
            _save_multipliers(merged)
            _CC_PRICING.update(ts=0)   # 价格缓存失效，下次读取带上新倍率
            _result_ver[0] += 1          # 请求明细/官方页结果缓存失效
            with _usage_lock:
                _usage_cache["data"] = None  # 用量/费用缓存失效，立即重算
            mode_txt = "从现在起生效" if mode == "now" else "全部历史已重算"
            log_event("info", "模型倍率已更新（保存 %d，删除 %d，%s）" % (len(clean), removed, mode_txt))
            self._json({"ok": True, "saved": len(clean), "removed": removed,
                        "message": ("已删除 %d 个倍率，这些模型恢复官方价（倍率 1），费用已全部重算。" % removed)
                        if isinstance(remove, list) and removed
                        else "已保存 %d 个模型的倍率（%s），费用已重算。" % (len(clean), mode_txt)})
        elif path == "/api/official-catalog":
            # 官方模型目录管理：pull=重新拉取云端清单；delete=删除指定 id（错误模型可删）；
            # reset=恢复写死清单
            act = payload.get("action")
            if act == "pull":
                models, msg = load_official_catalog(pull=True)
                log_event("info", "官方模型重新拉取: " + msg)
                self._json({"ok": bool(models), "message": msg})
            elif act == "delete":
                mid = str(payload.get("id") or "")
                models, _src = load_official_catalog()
                models = [m for m in (models or []) if str(m.get("id")) != mid]
                try:
                    with open(OFFICIAL_CATALOG_FILE, "w", encoding="utf-8") as f:
                        json.dump(models, f, ensure_ascii=False, indent=1)
                    with open(OFFICIAL_CATALOG_TS, "w", encoding="utf-8") as f:
                        json.dump({"ts": time.time()}, f)
                    log_event("info", "已删除官方模型: %s（剩余 %d 个）" % (mid, len(models)))
                    self._json({"ok": True, "message": "已删除 %s，剩余 %d 个官方模型。" % (mid, len(models))})
                except OSError as e:
                    self._json({"ok": False, "error": repr(e)}, 500)
            elif act == "reset":
                try:
                    if os.path.isfile(OFFICIAL_CATALOG_FILE):
                        os.remove(OFFICIAL_CATALOG_FILE)
                    self._json({"ok": True, "message": "已恢复内置官方模型清单（%d 个）。" % len(OFFICIAL_CATALOG_BUILTIN)})
                except OSError as e:
                    self._json({"ok": False, "error": repr(e)}, 500)
            elif act == "add":
                # 手动添加官方模型到清单（倍率另存到倍率表）
                mid = str(payload.get("id") or "").strip()
                if not mid:
                    self._json({"ok": False, "error": "模型 id 不能为空"}, 400)
                else:
                    models, _src = load_official_catalog()
                    if any(str(m.get("id")) == mid for m in (models or [])):
                        self._json({"ok": True, "message": "%s 已在官方清单中，倍率已更新。" % mid})
                    else:
                        models = list(models or []) + [{"id": mid, "name": mid, "multiplier": payload.get("multiplier", 1.0)}]
                        try:
                            with open(OFFICIAL_CATALOG_FILE, "w", encoding="utf-8") as f:
                                json.dump(models, f, ensure_ascii=False, indent=1)
                            with open(OFFICIAL_CATALOG_TS, "w", encoding="utf-8") as f:
                                json.dump({"ts": time.time()}, f)
                            log_event("info", "已手动添加官方模型: %s" % mid)
                            self._json({"ok": True, "message": "已添加官方模型 %s。" % mid})
                        except OSError as e:
                            self._json({"ok": False, "error": repr(e)}, 500)
            else:
                self._json({"ok": False, "error": "未知操作"}, 400)
        elif path == "/api/import-multipliers":
            # 导入倍率：兼容模板格式 {"rows":[{"key":..,"multiplier":..}]} 或直接 {"key":value}；
            # mode 同保存逻辑（all=全部修改 / now=当前修改），merge=true 时与现有条目合并不删除
            data = payload.get("data") or {}
            mode = payload.get("mode") or "all"
            now_ms = int(time.time() * 1000)
            merged = _load_multipliers()
            n = 0
            if isinstance(data, dict):
                rows = data.get("rows") if isinstance(data.get("rows"), list) else None
                items = []
                if rows:
                    for r in rows:
                        if isinstance(r, dict) and r.get("key") is not None:
                            items.append((str(r["key"]), r.get("multiplier")))
                else:
                    items = [(k, v) for k, v in data.items() if not str(k).startswith("_")]
                for k, v in items:
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    if fv < 0 or "|" not in k:
                        continue
                    if mode == "now":
                        segs = _mult_hist(merged.get(k)) if k in merged else []
                        segs.append((float(now_ms), fv))
                        merged[k] = {"cur": fv, "hist": segs}
                    else:
                        merged[k] = {"cur": fv, "hist": [[0.0, fv]]}
                    n += 1
            _save_multipliers(merged)
            _CC_PRICING.update(ts=0)
            _result_ver[0] += 1
            with _usage_lock:
                _usage_cache["data"] = None
            log_event("info", "倍率导入完成（%d 条，%s）" % (n, "当前修改" if mode == "now" else "全部修改"))
            self._json({"ok": True, "imported": n,
                        "message": "已导入 %d 条倍率（%s），费用已重算。" % (n, "当前修改" if mode == "now" else "全部修改")})
        elif path == "/api/import-pricing":
            # 导入价格表：{"rows":[{"id","in","out","cache","currency"}]}，逐条合并覆盖
            data = payload.get("data") or {}
            pricing = _load_pricing()
            n = 0
            rows = data.get("rows") if isinstance(data, dict) else None
            if isinstance(rows, list):
                for r in rows:
                    if not isinstance(r, dict) or not r.get("id"):
                        continue
                    try:
                        fin, fout = float(r.get("in") or 0), float(r.get("out") or 0)
                        fcache = float(r.get("cache") or 0)
                    except (TypeError, ValueError):
                        continue
                    k = str(r["id"]).lower()
                    cur = str(r.get("currency") or "CNY").upper()
                    if cur not in ("CNY", "USD"):
                        cur = _currency_for(k)
                    pricing[k] = {"name": r.get("name") or r["id"], "in": fin,
                                  "out": fout, "cache": fcache, "cur": cur}
                    n += 1
            if n:
                tmp = CCS_PRICING_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(pricing, f, ensure_ascii=False, indent=1)
                shutil.move(tmp, CCS_PRICING_FILE)
                _CC_PRICING.update(ts=0)
                _result_ver[0] += 1
                with _usage_lock:
                    _usage_cache["data"] = None
                log_event("info", "价格表导入完成（%d 条）" % n)
            self._json({"ok": True, "imported": n,
                        "message": ("已导入 %d 条价格（合并覆盖），现有 %d 个模型。" % (n, len(pricing)))
                        if n else "没有可导入的价格行（检查模板字段 id/in/out）。"})
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
            return name.startswith("python") or name.startswith("wb switch")
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
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
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
