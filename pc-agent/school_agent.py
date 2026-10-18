"""
school-tools PC 에이전트 (Windows)

컴퓨터에 열려 있는 프로그램(컴시간 알리미, 충북소통메신저 등)을 가볍게 감지하고,
화면 내용이 바뀌면 AI(OpenAI API)로 요약해서 teacher-tool.html 에 실시간으로 전달합니다.

  - 감지:   Win32 API(창 목록/전면 창) + UI Automation(창 안의 글자 읽기)
  - 전달:   http://127.0.0.1:8765  (JSON + Server-Sent Events, 이 컴퓨터에서만 접속 가능)
  - 요약:   내용이 바뀌었을 때만, 디바운스/최소 간격/일일 한도를 지켜서 호출

실행:  python school_agent.py               (일반 실행)
       python school_agent.py --list-windows (감지 대상 창 이름 확인)
       python school_agent.py --dump-text 메신저 (창 글자 읽기 테스트)
"""

import argparse
import copy
import datetime as dt
import difflib
import hashlib
import json
import mimetypes
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
DATA_DIR = HERE / "data"
STATE_FILE = DATA_DIR / "state.json"

# ═══════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════
DEFAULT_CONFIG = {
    "port": 8765,
    # teacher-tool.html 을 여는 주소(origin). 여기에 없는 웹사이트는 에이전트 데이터를 읽을 수 없습니다.
    "allowed_origins": [
        "https://springthief12.github.io",
        "http://127.0.0.1:8765",
        "http://localhost:8765",
    ],
    # html 파일을 더블클릭(file://)으로 열어서 쓰는 경우에만 true 로 바꾸세요.
    "allow_file_origin": False,
    # 이 시간대에만 창 내용을 읽습니다. (방과 후·주말에는 자동 정지)
    "active_hours": {"start": "07:30", "end": "18:30", "weekdays_only": True},
    "intervals": {
        "foreground_sec": 2,       # 맨 앞 창 확인 (매우 가벼움)
        "window_list_sec": 10,     # 보이는 창 목록 확인 (새 알림 팝업 감지)
        "process_list_sec": 30,    # 트레이에만 있는 프로그램 실행 여부 확인
        "idle_after_sec": 300,     # 이 시간 동안 입력이 없으면 '자리 비움'
        "idle_multiplier": 4,      # 자리 비움일 때 확인 주기를 몇 배로 늘릴지
    },
    "llm": {
        "enabled": True,
        "provider": "openai",
        "api_key": "",             # 비워두면 환경변수 OPENAI_API_KEY 사용 (권장)
        "model": "gpt-5-mini",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "debounce_sec": 20,        # 마지막 변화 후 이만큼 조용해지면 요약 (연속 메시지 묶기)
        "min_interval_sec": 90,    # 같은 프로그램에 대해 최소 호출 간격
        "daily_limit": 200,        # 하루 최대 호출 수 (비용 안전장치)
        "timeout_sec": 60,
    },
    # 선생님 정보 — 컴시간 화면에서 '내 시간표' 변경만 골라내는 데 쓰입니다.
    "teacher": {
        "description": "",         # 예: "3학년 2반 담임, 국어 담당 홍길동"
    },
    "apps": [
        {
            "id": "comcigan",
            "label": "컴시간 알리미",
            "kind": "timetable",
            "title_keywords": ["컴시간"],
            "process_names": [],   # --list-windows 로 확인한 exe 이름을 넣으면 더 정확합니다.
            "scan_active_sec": 10,      # 그 창을 보고 있을 때
            "scan_background_sec": 300, # 뒤에 있을 때 (5분)
            "max_chars": 8000,
        },
        {
            "id": "messenger",
            "label": "충북소통메신저",
            "kind": "messenger",
            "title_keywords": ["소통메신저", "충북소통"],
            "process_names": [],
            "scan_active_sec": 5,
            "scan_background_sec": 60,
            "max_chars": 12000,
        },
    ],
    "ignore_line_patterns": [
        r"^\d{1,2}:\d{2}(:\d{2})?$",             # 시계
        r"^(오전|오후)\s?\d{1,2}:\d{2}$",
        r"^\d+$",                                # 안 읽은 개수 같은 숫자
    ],
    "uia": {"max_depth": 25, "max_nodes": 4000},
}


def deep_merge(base, extra):
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    cfg = DEFAULT_CONFIG
    if path and Path(path).exists():
        with open(path, encoding="utf-8-sig") as f:
            cfg = deep_merge(DEFAULT_CONFIG, json.load(f))
    return cfg


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ═══════════════════════════════════════════════
#  WINDOW SOURCES
# ═══════════════════════════════════════════════
class WinSource:
    """Win32 API(ctypes)로 창/프로세스 정보를 읽습니다. 외부 패키지 없이 동작합니다."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ct, self.wt = ctypes, wintypes
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, k = self.user32, self.kernel32
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._exe_cache = {}
        self._exe_cache_at = time.monotonic()
        try:
            import uiautomation  # noqa: F401  (선택 설치)
            self.uia = uiautomation
        except ImportError:
            self.uia = None

    # ── 기본 정보 ──
    def _title(self, hwnd):
        n = self.user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = self.ct.create_unicode_buffer(n + 1)
        self.user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def _pid(self, hwnd):
        pid = self.wt.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, self.ct.byref(pid))
        return pid.value

    def _exe(self, pid):
        if time.monotonic() - self._exe_cache_at > 600:  # pid 재사용 대비
            self._exe_cache, self._exe_cache_at = {}, time.monotonic()
        if pid in self._exe_cache:
            return self._exe_cache[pid]
        name = ""
        h = self.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            try:
                size = self.wt.DWORD(1024)
                buf = self.ct.create_unicode_buffer(1024)
                if self.kernel32.QueryFullProcessImageNameW(h, 0, buf, self.ct.byref(size)):
                    name = os.path.basename(buf.value)
            finally:
                self.kernel32.CloseHandle(h)
        self._exe_cache[pid] = name
        return name

    def _info(self, hwnd):
        pid = self._pid(hwnd)
        return {"hwnd": int(hwnd), "title": self._title(hwnd), "pid": pid, "exe": self._exe(pid)}

    def foreground(self):
        hwnd = self.user32.GetForegroundWindow()
        return self._info(hwnd) if hwnd else None

    def windows(self):
        out = []

        def cb(hwnd, _):
            if self.user32.IsWindowVisible(hwnd) and self.user32.GetWindowTextLengthW(hwnd) > 0:
                out.append(hwnd)
            return True

        self.user32.EnumWindows(self._enum_proc(cb), 0)
        return [self._info(h) for h in out]

    def processes(self):
        arr = (self.wt.DWORD * 4096)()
        needed = self.wt.DWORD()
        if not self.kernel32.K32EnumProcesses(arr, self.ct.sizeof(arr), self.ct.byref(needed)):
            return []
        count = needed.value // self.ct.sizeof(self.wt.DWORD)
        return [self._exe(arr[i]) for i in range(count) if arr[i]]

    def idle_seconds(self):
        class LASTINPUTINFO(self.ct.Structure):
            _fields_ = [("cbSize", self.wt.UINT), ("dwTime", self.wt.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = self.ct.sizeof(lii)
        if not self.user32.GetLastInputInfo(self.ct.byref(lii)):
            return 0
        return ((self.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0

    # ── 창 안의 글자 읽기 (UI Automation) ──
    def thread_context(self):
        if self.uia:
            return self.uia.UIAutomationInitializerInThread()
        return _NullContext()

    def read_text(self, hwnd, max_depth=25, max_nodes=4000, max_chars=12000):
        if not self.uia:
            return None  # uiautomation 미설치 → 창 제목만 사용
        auto = self.uia
        lines, total = [], 0
        root = auto.ControlFromHandle(hwnd)
        if not root:
            return ""
        for n, (ctrl, _depth) in enumerate(auto.WalkControl(root, includeTop=True, maxDepth=max_depth)):
            if n >= max_nodes or total >= max_chars:
                break
            texts = []
            try:
                if ctrl.Name:
                    texts.append(ctrl.Name)
                if ctrl.ControlTypeName in ("EditControl", "DocumentControl"):
                    try:
                        v = ctrl.GetValuePattern().Value
                        if v:
                            texts.append(v)
                    except Exception:
                        try:
                            texts.append(ctrl.GetTextPattern().DocumentRange.GetText(max_chars))
                        except Exception:
                            pass
            except Exception:
                continue
            for t in texts:
                for line in str(t).splitlines():
                    line = line.strip()
                    if line:
                        lines.append(line)
                        total += len(line)
        return "\n".join(lines)[:max_chars]


class _NullContext:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class MockSource:
    """Windows 가 아닌 곳에서 테스트할 때 사용합니다. JSON 파일을 매번 다시 읽습니다.
    {"foreground": {"title": "...", "exe": "..."}, "idle_sec": 0,
     "windows": [{"hwnd": 1, "title": "...", "exe": "...", "text": "..."}],
     "processes": ["a.exe"]}"""

    def __init__(self, path):
        self.path = Path(path)

    def _data(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def foreground(self):
        return self._data().get("foreground")

    def windows(self):
        return [{k: w.get(k) for k in ("hwnd", "title", "exe")} | {"pid": 0}
                for w in self._data().get("windows", [])]

    def processes(self):
        return self._data().get("processes", [])

    def idle_seconds(self):
        return self._data().get("idle_sec", 0)

    def thread_context(self):
        return _NullContext()

    def read_text(self, hwnd, **_):
        for w in self._data().get("windows", []):
            if w.get("hwnd") == hwnd:
                return w.get("text", "")
        return ""


# ═══════════════════════════════════════════════
#  DATE HELPERS
# ═══════════════════════════════════════════════
WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]
DATE_RE = re.compile(r"(?:(\d{4})[.\-/년]\s*)?(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?")


def week_context(today):
    """이번 주·다음 주 요일별 날짜 — AI가 '목요일 3교시' 같은 표현을 날짜로 바꿀 때 사용."""
    weekend = today.weekday() >= 5
    mon = today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7 if weekend else 0)
    labels = ("다가오는 주", "그다음 주") if weekend else ("이번 주", "다음 주")
    rows = []
    for w, label in enumerate(labels):
        days = [mon + dt.timedelta(days=7 * w + i) for i in range(5)]
        rows.append(label + ": " + ", ".join(f"{WEEKDAY_KO[d.weekday()]}={d.isoformat()}" for d in days))
    return "\n".join(rows)


def parse_deadline(text, today):
    if re.search(r"오늘", text):
        return today.isoformat()
    if re.search(r"내일", text):
        return (today + dt.timedelta(days=1)).isoformat()
    if re.search(r"모레", text):
        return (today + dt.timedelta(days=2)).isoformat()
    m = DATE_RE.search(text)
    if not m:
        return None
    y = int(m.group(1)) if m.group(1) else today.year
    try:
        d = dt.date(y, int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if not m.group(1) and d < today - dt.timedelta(days=60):
        d = d.replace(year=y + 1)
    return d.isoformat()


def valid_date(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        dt.date.fromisoformat(s)
        return s
    except ValueError:
        return None


# ═══════════════════════════════════════════════
#  ANALYZER (AI 요약 / 규칙 기반 대체)
# ═══════════════════════════════════════════════
TASK_KEYWORDS = re.compile(r"마감|까지|제출|회신|기한|필수|협조|요청|공문|등록|입력|신청")

SYSTEM_PROMPT = """당신은 한국 학교 교사의 업무 비서입니다. 교사 PC의 프로그램 화면에서 새로 바뀐 글자를 받아,
교사가 바로 확인할 수 있도록 짧게 정리합니다. 반드시 아래 JSON 하나만 출력하세요.

{
  "summary": "핵심 1~3문장 (존댓말, 120자 이내)",
  "important": true 또는 false,
  "timetable_changes": [
    {"date": "YYYY-MM-DD", "period": 1~7 정수, "before": "변경 전 과목/학급", "after": "변경 후 과목/학급", "reason": "보강/교체 등"}
  ],
  "tasks": [
    {"title": "40자 이내 업무 제목", "detail": "필요한 행동과 대상", "deadline": "YYYY-MM-DD 또는 null", "priority": "high|normal|low"}
  ]
}

규칙:
- 교사가 직접 해야 하는 일(제출, 회신, 입력, 참석, 신청 등)만 tasks 에 넣습니다. 단순 안내는 넣지 않습니다.
- important 는 마감이 있거나 교사의 행동이 필요한 경우 true 입니다.
- 시간표 변경은 '교사 정보'와 관련된 것만 timetable_changes 에 넣습니다. 확실하지 않으면 넣지 말고 summary 에 언급합니다.
- '목요일', '다음 주 화요일' 같은 표현은 제공된 날짜표로 YYYY-MM-DD 로 바꿉니다. 알 수 없으면 null 입니다.
- 학생 개인정보(이름, 연락처)는 요약에 옮겨 적지 않습니다.
- 변화가 의미 없는 화면 잡음(시계, 읽음 표시 등)이면 summary 를 "의미 있는 변경 없음", important=false, 나머지 빈 배열로 둡니다."""


class Analyzer:
    def __init__(self, cfg):
        self.cfg = cfg
        self.llm = cfg["llm"]

    def api_key(self):
        return self.llm.get("api_key") or os.environ.get("OPENAI_API_KEY", "")

    def llm_ready(self):
        return bool(self.llm.get("enabled") and self.api_key())

    def build_user_prompt(self, app, change, today):
        parts = [
            f"오늘: {today.isoformat()} ({WEEKDAY_KO[today.weekday()]}요일)",
            "날짜표:\n" + week_context(today),
            f"교사 정보: {self.cfg['teacher'].get('description') or '(미입력)'}",
            f"프로그램: {app['label']} ({'시간표 알림' if app['kind'] == 'timetable' else '업무 메신저'})",
        ]
        if app["kind"] == "timetable":
            parts.append("[변경 전 화면]\n" + change["before"][-5000:])
            parts.append("[변경 후 화면]\n" + change["after"][-5000:])
        else:
            parts.append("[새로 나타난 글자]\n" + "\n".join(change["new_lines"])[-8000:])
        return "\n\n".join(parts)

    def call_llm(self, user_prompt):
        body = {
            "model": self.llm["model"],
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            self.llm["endpoint"],
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key()}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.llm.get("timeout_sec", 60)) as r:
            data = json.loads(r.read().decode("utf-8"))
        return json.loads(data["choices"][0]["message"]["content"])

    def rule_based(self, app, change, today):
        lines = change["new_lines"]
        if app["kind"] == "timetable":
            return {
                "summary": f"{app['label']} 화면이 바뀌었습니다 ({len(lines)}줄). 시간표 변경 여부를 확인해 주세요.",
                "important": True, "timetable_changes": [], "tasks": [],
            }
        tasks = []
        for line in lines:
            if TASK_KEYWORDS.search(line):
                tasks.append({"title": line[:40], "detail": line[:300],
                              "deadline": parse_deadline(line, today), "priority": "normal"})
        preview = " / ".join(lines[:3])
        return {
            "summary": f"새 메시지 {len(lines)}줄: {preview[:110]}",
            "important": bool(tasks), "timetable_changes": [], "tasks": tasks[:5],
        }

    def sanitize(self, res):
        out = {
            "summary": str(res.get("summary") or "")[:300],
            "important": bool(res.get("important")),
            "timetable_changes": [],
            "tasks": [],
        }
        for c in res.get("timetable_changes") or []:
            try:
                period = int(c.get("period"))
            except (TypeError, ValueError):
                continue
            date = valid_date(c.get("date"))
            if date and 1 <= period <= 7:
                out["timetable_changes"].append({
                    "date": date, "period": period,
                    "before": str(c.get("before") or "")[:60],
                    "after": str(c.get("after") or "")[:60],
                    "reason": str(c.get("reason") or "")[:60],
                })
        for t in res.get("tasks") or []:
            title = str(t.get("title") or "").strip()
            if not title:
                continue
            pri = t.get("priority") if t.get("priority") in ("high", "normal", "low") else "normal"
            out["tasks"].append({
                "title": title[:80], "detail": str(t.get("detail") or "")[:600],
                "deadline": valid_date(t.get("deadline")), "priority": pri,
            })
        return out

    def analyze(self, app, change):
        """returns (result, engine, error)"""
        today = dt.date.today()
        if self.llm_ready():
            try:
                res = self.call_llm(self.build_user_prompt(app, change, today))
                return self.sanitize(res), "ai", None
            except urllib.error.HTTPError as e:
                err = f"AI 호출 실패 (HTTP {e.code}): {e.read().decode('utf-8', 'replace')[:200]}"
            except Exception as e:  # 네트워크, JSON 형식 오류 등
                err = f"AI 호출 실패: {e}"
            log(err)
            return self.sanitize(self.rule_based(app, change, today)), "rule", err
        return self.sanitize(self.rule_based(app, change, today)), "rule", None


# ═══════════════════════════════════════════════
#  STATE (보고서 + 화면 스냅샷)
# ═══════════════════════════════════════════════
class Store:
    MAX_REPORTS = 200

    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"reports": [], "snapshots": {}, "llm": {"day": "", "calls": 0}}
        self.subscribers = set()
        try:
            self.data.update(json.loads(STATE_FILE.read_text(encoding="utf-8")))
        except FileNotFoundError:
            pass
        except Exception as e:
            log("state.json 을 읽지 못해 새로 시작합니다:", e)

    def save(self):
        with self.lock:
            DATA_DIR.mkdir(exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, STATE_FILE)

    def snapshot(self, app_id):
        with self.lock:
            return self.data["snapshots"].get(app_id)

    def set_snapshot(self, app_id, text):
        with self.lock:
            self.data["snapshots"][app_id] = text
            self.save()

    def llm_quota_left(self, limit):
        with self.lock:
            today = dt.date.today().isoformat()
            if self.data["llm"].get("day") != today:
                self.data["llm"] = {"day": today, "calls": 0}
            return limit - self.data["llm"]["calls"]

    def count_llm_call(self):
        with self.lock:
            self.data["llm"]["calls"] += 1

    def add_report(self, report):
        with self.lock:
            self.data["reports"] = (self.data["reports"] + [report])[-self.MAX_REPORTS:]
            self.save()
        self.publish("report", report)

    def reports(self):
        with self.lock:
            return list(self.data["reports"])

    # ── SSE 구독 ──
    def subscribe(self):
        q = queue.Queue(maxsize=100)
        with self.lock:
            self.subscribers.add(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.subscribers.discard(q)

    def publish(self, event, payload):
        msg = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        with self.lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass


# ═══════════════════════════════════════════════
#  MONITOR
# ═══════════════════════════════════════════════
class AppState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.id = cfg["id"]
        self.open = False
        self.proc_open = False
        self.foreground = False
        self.hwnds = []
        self.known_hwnds = set()
        self.next_scan = 0.0
        self.scan_queued = False
        self.last_scan = None
        self.scan_count = 0
        self.readable = None
        self.error = None
        # 분석 대기 중인 변경 사항
        self.pending = None          # {"before", "after", "new_lines"}
        self.last_change = 0.0
        self.last_llm = 0.0
        self.force = False

    def matches(self, win):
        title = (win.get("title") or "")
        exe = (win.get("exe") or "").lower()
        if exe and exe in [p.lower() for p in self.cfg.get("process_names", [])]:
            return True
        return any(k and k in title for k in self.cfg.get("title_keywords", []))

    def public(self):
        return {
            "id": self.id, "label": self.cfg["label"], "kind": self.cfg["kind"],
            "open": self.open, "foreground": self.foreground,
            "last_scan": self.last_scan, "scan_count": self.scan_count,
            "readable": self.readable, "error": self.error,
            "pending": self.pending is not None,
        }


class Monitor:
    def __init__(self, cfg, source, store, analyzer):
        self.cfg, self.src, self.store, self.analyzer = cfg, source, store, analyzer
        self.apps = [AppState(a) for a in cfg["apps"]]
        self.lock = threading.RLock()
        self.scan_q = queue.Queue()
        self.stop = threading.Event()
        self.idle = False
        self.paused = False
        self.started = dt.datetime.now().isoformat(timespec="seconds")
        self.ignore_re = [re.compile(p) for p in cfg.get("ignore_line_patterns", [])]

    # ── 정책 ──
    def within_active_hours(self):
        ah = self.cfg.get("active_hours") or {}
        now = dt.datetime.now()
        if ah.get("weekdays_only") and now.weekday() >= 5:
            return False
        hm = now.strftime("%H:%M")
        return ah.get("start", "00:00") <= hm < ah.get("end", "24:00")

    def factor(self):
        return self.cfg["intervals"]["idle_multiplier"] if self.idle else 1

    def public_state(self):
        with self.lock:
            return {
                "version": VERSION, "started": self.started,
                "now": dt.datetime.now().isoformat(timespec="seconds"),
                "paused": self.paused, "idle": self.idle,
                "apps": [a.public() for a in self.apps],
                "llm": {
                    "ready": self.analyzer.llm_ready(), "model": self.cfg["llm"]["model"],
                    "calls_left_today": self.store.llm_quota_left(self.cfg["llm"]["daily_limit"]),
                },
                "uia": getattr(self.src, "uia", True) is not None,
            }

    def force_scan(self, app_id=None):
        with self.lock:
            for a in self.apps:
                if app_id in (None, a.id):
                    a.next_scan = 0.0
                    a.force = True

    # ── 스레드 1: 일정 관리 (가벼운 확인만) ──
    def run_scheduler(self):
        iv = self.cfg["intervals"]
        nxt = {"fg": 0.0, "win": 0.0, "proc": 0.0}
        last_public = None
        while not self.stop.is_set():
            now = time.monotonic()
            try:
                if now >= nxt["fg"]:
                    self.idle = self.src.idle_seconds() >= iv["idle_after_sec"]
                    self.paused = not self.within_active_hours()
                    fg = self.src.foreground()
                    with self.lock:
                        for a in self.apps:
                            a.foreground = bool(fg) and a.matches(fg)
                    nxt["fg"] = now + iv["foreground_sec"] * (15 if self.paused else self.factor())
                if now >= nxt["win"]:
                    self.check_windows(now)
                    nxt["win"] = now + iv["window_list_sec"] * (6 if self.paused else self.factor())
                if now >= nxt["proc"]:
                    self.check_processes()
                    nxt["proc"] = now + iv["process_list_sec"] * (4 if self.paused else self.factor())
                if not self.paused:
                    self.queue_due_scans(now)
            except Exception as e:
                log("감지 오류:", e)
            pub = json.dumps(self.public_state()["apps"], sort_keys=True) + str(self.paused) + str(self.idle)
            if pub != last_public:  # 상태가 바뀌었을 때만 브라우저로 전송
                self.store.publish("status", self.public_state())
                last_public = pub
            self.stop.wait(0.5)

    def check_windows(self, now):
        wins = self.src.windows()
        with self.lock:
            for a in self.apps:
                mine = [w for w in wins if a.matches(w)]
                a.hwnds = [w["hwnd"] for w in mine]
                if set(a.hwnds) - a.known_hwnds and a.scan_count:
                    a.next_scan = 0.0  # 새 창(알림 팝업 등) → 바로 읽기
                a.known_hwnds = set(a.hwnds)
                a.open = bool(a.hwnds) or a.proc_open

    def check_processes(self):
        procs = {p.lower() for p in self.src.processes() if p}
        with self.lock:
            for a in self.apps:
                names = {p.lower() for p in a.cfg.get("process_names", [])}
                a.proc_open = bool(names & procs)
                a.open = bool(a.hwnds) or a.proc_open

    def queue_due_scans(self, now):
        with self.lock:
            for a in self.apps:
                if not a.hwnds or a.scan_queued or now < a.next_scan:
                    continue
                a.scan_queued = True
                self.scan_q.put(a)

    # ── 스레드 2: 창 글자 읽기 (상대적으로 무거운 작업을 따로 처리) ──
    def run_scanner(self):
        with self.src.thread_context():
            while not self.stop.is_set():
                try:
                    a = self.scan_q.get(timeout=1)
                except queue.Empty:
                    continue
                try:
                    self.scan(a)
                except Exception as e:
                    a.error = f"읽기 오류: {e}"
                    log(a.cfg["label"], a.error)
                finally:
                    with self.lock:
                        a.scan_queued = False
                        a.scan_count += 1
                        a.last_scan = dt.datetime.now().isoformat(timespec="seconds")
                        sec = a.cfg["scan_active_sec"] if a.foreground else a.cfg["scan_background_sec"]
                        a.next_scan = time.monotonic() + sec * self.factor()

    def clean_lines(self, text):
        out, prev = [], None
        for line in text.splitlines():
            line = line.strip()
            if not line or line == prev or any(r.search(line) for r in self.ignore_re):
                continue
            out.append(line)
            prev = line
        return out

    def scan(self, a):
        uia = self.cfg["uia"]
        chunks, readable = [], []
        for hwnd in list(a.hwnds):
            text = self.src.read_text(hwnd, max_depth=uia["max_depth"], max_nodes=uia["max_nodes"],
                                      max_chars=a.cfg["max_chars"])
            if text is None:  # UI Automation 미설치 → 창 제목만
                text = next((w["title"] for w in self.src.windows() if w["hwnd"] == hwnd), "")
                readable.append(False)
            else:
                readable.append(bool(text.strip()))
            chunks.append(text)
        a.readable = any(readable)
        lines = self.clean_lines("\n".join(chunks))
        text = "\n".join(lines)[: a.cfg["max_chars"]]
        a.error = None if a.readable else "창 글자를 읽지 못했습니다. --dump-text 로 확인해 주세요."
        old = self.store.snapshot(a.id)
        if old is None:  # 첫 실행: 기준 화면만 저장
            self.store.set_snapshot(a.id, text)
            log(f"{a.cfg['label']}: 기준 화면 저장 ({len(lines)}줄)")
            return
        if old == text:
            return
        old_set = set(old.splitlines())
        new_lines = [l for l in lines if l not in old_set]
        if a.cfg["kind"] == "messenger" and not new_lines:
            # 메시지가 사라지기만 한 경우(스크롤 등)는 보고하지 않음
            self.store.set_snapshot(a.id, text)
            return
        with self.lock:
            if a.pending is None:
                a.pending = {"before": old, "after": text, "new_lines": new_lines}
            else:  # 디바운스 중 추가 변화 → 합치기
                seen = set(a.pending["new_lines"])
                a.pending["after"] = text
                a.pending["new_lines"] += [l for l in new_lines if l not in seen]
            a.last_change = time.monotonic()
        self.store.set_snapshot(a.id, text)
        log(f"{a.cfg['label']}: 변경 감지 (+{len(new_lines)}줄)")

    # ── 스레드 3: 요약 (변경이 있을 때만) ──
    def run_analyzer(self):
        llm = self.cfg["llm"]
        while not self.stop.wait(2):
            now = time.monotonic()
            for a in self.apps:
                with self.lock:
                    if a.pending is None:
                        continue
                    quiet = now - a.last_change >= llm["debounce_sec"]
                    spaced = now - a.last_llm >= llm["min_interval_sec"]
                    if not (a.force or (quiet and spaced)):
                        continue
                    change, a.pending, a.force = a.pending, None, False
                    a.last_llm = now
                self.report(a, change)

    def report(self, a, change):
        if self.analyzer.llm_ready() and self.store.llm_quota_left(self.cfg["llm"]["daily_limit"]) <= 0:
            res, engine, err = self.analyzer.sanitize(
                self.analyzer.rule_based(a.cfg, change, dt.date.today())), "rule", "오늘 AI 호출 한도 도달"
        else:
            res, engine, err = self.analyzer.analyze(a.cfg, change)
            if engine == "ai":
                self.store.count_llm_call()
        if res["summary"] == "의미 있는 변경 없음" and not res["tasks"] and not res["timetable_changes"]:
            log(f"{a.cfg['label']}: 의미 있는 변경 없음")
            return
        report = {
            "id": hashlib.sha1(f"{a.id}{time.time()}".encode()).hexdigest()[:16],
            "ts": dt.datetime.now().isoformat(timespec="seconds"),
            "app": a.id, "app_label": a.cfg["label"], "kind": a.cfg["kind"],
            "engine": engine, "error": err,
            "excerpt": "\n".join(change["new_lines"])[:1500],
            **res,
        }
        self.store.add_report(report)
        log(f"{a.cfg['label']}: 보고 생성 — {res['summary']}")

    def start(self):
        for fn in (self.run_scheduler, self.run_scanner, self.run_analyzer):
            threading.Thread(target=fn, daemon=True).start()


# ═══════════════════════════════════════════════
#  HTTP (JSON + SSE + 정적 파일)
# ═══════════════════════════════════════════════
def make_handler(cfg, monitor, store):
    port = cfg["port"]
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    allowed_origins = set(cfg["allowed_origins"]) | ({"null"} if cfg.get("allow_file_origin") else set())

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # 콘솔 소음 줄이기
            pass

        # ── 보안 검사 ──
        def check(self):
            if self.headers.get("Host") not in allowed_hosts:  # DNS rebinding 방지
                self.send_error(403, "bad host")
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in allowed_origins:
                self.send_error(403, "origin not allowed")
                return False
            return True

        def cors(self):
            origin = self.headers.get("Origin")
            if origin and origin in allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def send_json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            if not self.check():
                return
            self.send_response(204)
            self.cors()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self.check():
                return
            path = urlparse(self.path).path
            if path == "/api/state":
                return self.send_json({**monitor.public_state(), "reports": store.reports()})
            if path == "/api/events":
                return self.sse()
            return self.static(path)

        def do_POST(self):
            if not self.check():
                return
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except json.JSONDecodeError:
                return self.send_json({"error": "bad json"}, 400)
            if path == "/api/scan":
                monitor.force_scan(body.get("app"))
                return self.send_json({"ok": True})
            return self.send_json({"error": "not found"}, 404)

        def sse(self):
            self.send_response(200)
            self.cors()
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = store.subscribe()
            try:
                hello = json.dumps(monitor.public_state(), ensure_ascii=False)
                self.wfile.write(f"retry: 5000\nevent: status\ndata: {hello}\n\n".encode())
                self.wfile.flush()
                while not monitor.stop.is_set():
                    try:
                        msg = q.get(timeout=20)
                    except queue.Empty:
                        msg = ": keep-alive\n\n"  # 20초마다 연결 유지 신호
                    self.wfile.write(msg.encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                store.unsubscribe(q)

        def static(self, path):
            rel = path.lstrip("/") or "index.html"
            target = (REPO_ROOT / rel).resolve()
            if REPO_ROOT not in target.parents or HERE in target.parents or target == HERE \
                    or not target.is_file() or target.suffix.lower() not in (
                    ".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".svg", ".ico", ".json", ".webp"):
                return self.send_json({"error": "not found"}, 404)
            body = target.read_bytes()
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return Handler


# ═══════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════
def cmd_list_windows(src, cfg):
    apps = [AppState(a) for a in cfg["apps"]]
    print("보이는 창 목록 (exe | 제목) — 감지 대상은 ★ 표시")
    for w in sorted(src.windows(), key=lambda w: (w["exe"] or "").lower()):
        hit = [a.cfg["label"] for a in apps if a.matches(w)]
        print(f"{'★' if hit else ' '} {w['exe'] or '?':<28} | {w['title']}" + (f"  ← {', '.join(hit)}" if hit else ""))


def cmd_dump_text(src, cfg, keyword):
    if getattr(src, "uia", True) is None:
        print("uiautomation 패키지가 없습니다:  pip install uiautomation")
        return
    with src.thread_context():
        for w in src.windows():
            if keyword in (w["title"] or "") or keyword.lower() == (w["exe"] or "").lower():
                t0 = time.perf_counter()
                text = src.read_text(w["hwnd"], **cfg["uia"], max_chars=20000)
                ms = (time.perf_counter() - t0) * 1000
                print(f"\n===== {w['exe']} | {w['title']}  ({ms:.0f} ms, {len(text or '')}자) =====")
                print(text or "(읽을 수 있는 글자 없음)")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="school-tools PC 에이전트")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--list-windows", action="store_true", help="보이는 창과 exe 이름 출력")
    ap.add_argument("--dump-text", metavar="키워드", help="창 제목에 키워드가 있는 창의 글자 출력")
    ap.add_argument("--mock", metavar="JSON", help="(테스트용) 실제 창 대신 JSON 파일 사용")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.mock:
        src = MockSource(args.mock)
    elif sys.platform == "win32":
        src = WinSource()
    else:
        sys.exit("Windows 에서 실행해 주세요. (테스트는 --mock 사용)")

    if args.list_windows:
        return cmd_list_windows(src, cfg)
    if args.dump_text:
        return cmd_dump_text(src, cfg, args.dump_text)

    store = Store()
    analyzer = Analyzer(cfg)
    monitor = Monitor(cfg, src, store, analyzer)
    monitor.start()

    server = ThreadingHTTPServer(("127.0.0.1", cfg["port"]), make_handler(cfg, monitor, store))
    server.daemon_threads = True
    log(f"school-tools PC 에이전트 {VERSION} 시작 — http://127.0.0.1:{cfg['port']}/teacher-tool.html")
    log("AI 요약:", "사용 (" + cfg["llm"]["model"] + ")" if analyzer.llm_ready()
        else "꺼짐 — 규칙 기반 요약만 사용 (OPENAI_API_KEY 미설정)")
    if getattr(src, "uia", True) is None:
        log("uiautomation 미설치 — 창 제목만 감지합니다. (pip install uiautomation)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("종료합니다.")
    finally:
        monitor.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
