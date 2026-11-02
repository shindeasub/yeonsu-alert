# -*- coding: utf-8 -*-
"""
서울시 공무원 연수원 빈 객실 알림 프로그램 (GitHub Actions 서버용)
- 컴퓨터가 꺼져 있어도 GitHub 서버에서 5분마다 실행됨
- 한 번 실행될 때마다 RUN_MINUTES(기본 4분) 동안 1분 간격으로 조회
- 빈 방이 새로 생기면 폰(ntfy 앱)으로 알림 (예약은 하지 않음)
- 아이디/비밀번호/폰알림 이름은 GitHub Secrets 에서 읽음 (설정.ini 에 적지 않음)
"""
import configparser
import datetime as dt
import http.cookiejar
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "설정.ini")
STATE_PATH = os.path.join(BASE_DIR, "알림기록.json")
STOP_PATH = os.path.join(BASE_DIR, "로그인중지.txt")
KST = dt.timezone(dt.timedelta(hours=9))


def now():
    return dt.datetime.now(KST)

SITE = "https://yeonsu.eseoul.go.kr"
RSV_PAGE = SITE + "/onlineRsv/list"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# 연수원 코드와 연수원별 객실형 (사이트 화면 기준)
YEONSU = {
    "속초": ("00003002", "속초수련원", ["A", "B"]),
    "서천": ("00003003", "서천연수원", ["A", "B", "C", "O"]),
    "수안보": ("00003004", "수안보연수원", ["A", "B", "O"]),
}
TYPE_FIELD = {"A": "atye_room_num", "B": "btye_room_num", "C": "ctye_room_num",
              "D": "dtye_room_num", "O": "otye_room_num"}
WEEKDAYS = "월화수목금토일"


def log(msg):
    print(f"[{now():%m-%d %H:%M:%S}] {msg}", flush=True)


# ───────────────────────── 설정 읽기 ─────────────────────────
class Settings:
    def __init__(self):
        if not os.path.exists(CONFIG_PATH):
            raise SystemExit("설정.ini 파일이 없습니다. 프로그램과 같은 폴더에 두세요.")
        cp = configparser.ConfigParser(interpolation=None)
        with open(CONFIG_PATH, encoding="utf-8-sig") as f:
            cp.read_file(f)
        g = lambda sec, key, default="": cp.get(sec, key, fallback=default).strip()

        self.user_id = os.environ.get("YEONSU_ID", "").strip()
        self.password = os.environ.get("YEONSU_PW", "").strip()
        if not self.user_id or not self.password:
            raise SystemExit("GitHub Secrets 에 YEONSU_ID / YEONSU_PW 를 등록하세요.")

        # 연수원
        self.yeonsu = []
        for name in split_list(g("조건", "연수원", "수안보")):
            key = name.replace("연수원", "").replace("수련원", "")
            if key not in YEONSU:
                raise SystemExit(f"연수원 이름을 확인하세요: '{name}' (속초/서천/수안보 중 선택)")
            self.yeonsu.append(key)

        # 날짜
        self.dates = parse_dates(g("조건", "날짜"))
        if not self.dates:
            raise SystemExit("설정.ini 의 [조건] 날짜를 입력하세요. 예) 2026-11-21 또는 2026-11-20~2026-11-30")

        # 요일 필터
        days = g("조건", "요일")
        self.weekdays = None
        if days and days not in ("전체", "모두"):
            self.weekdays = set()
            for d in split_list(days):
                d = d.replace("요일", "")
                if d not in WEEKDAYS:
                    raise SystemExit(f"요일을 확인하세요: '{d}' (월,화,수,목,금,토,일)")
                self.weekdays.add(WEEKDAYS.index(d))

        try:
            self.nights = max(1, int(g("조건", "박수", "1")))
        except ValueError:
            raise SystemExit("박수는 숫자로 입력하세요. 예) 1")

        types = g("조건", "객실형", "전체").upper()
        self.types = None if types in ("", "전체", "모두", "ALL") else \
            [t.replace("형", "") for t in split_list(types)]
        if self.types:
            for t in self.types:
                if t not in TYPE_FIELD:
                    raise SystemExit(f"객실형을 확인하세요: '{t}' (A,B,C,O 또는 전체)")

        try:
            self.min_rooms = max(1, int(g("조건", "최소객실수", "1")))
        except ValueError:
            self.min_rooms = 1

        try:
            self.interval = max(1, int(g("실행", "조회간격_분", "1")))
        except ValueError:
            self.interval = 1
        self.quiet = parse_quiet(g("실행", "알림쉬는시간"))

        self.topic = os.environ.get("NTFY_TOPIC", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.topic):
            raise SystemExit("GitHub Secrets 에 NTFY_TOPIC(폰알림 이름)을 등록하세요. (영문/숫자/-/_)")
        self.pc_popup = False


def split_list(s):
    return [x.strip() for x in re.split(r"[,\s/]+", s) if x.strip()]


def parse_date(s):
    s = s.strip().replace(".", "-").replace("/", "-")
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def parse_dates(text):
    out = set()
    for part in re.split(r"[,\n]+", text):
        part = part.strip()
        if not part:
            continue
        try:
            if "~" in part:
                a, b = part.split("~", 1)
                a, b = parse_date(a), parse_date(b)
                if b < a:
                    a, b = b, a
                d = a
                while d <= b:
                    out.add(d)
                    d += dt.timedelta(days=1)
            else:
                out.add(parse_date(part))
        except ValueError:
            raise SystemExit(f"날짜 형식을 확인하세요: '{part}' (예: 2026-11-21)")
    return sorted(out)


def parse_quiet(s):
    """'23~7' → (23, 7). 빈 값이면 None"""
    m = re.fullmatch(r"\s*(\d{1,2})\s*~\s*(\d{1,2})\s*", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def in_quiet(quiet):
    if not quiet:
        return False
    h = now().hour
    a, b = quiet
    return a <= h < b if a < b else (h >= a or h < b)


# ───────────────────────── 연수원 사이트 ─────────────────────────
class LoginError(Exception):
    pass


class Yeonsu:
    def __init__(self, user_id, password):
        self.user_id, self.password = user_id, password
        self._new_session()

    def _new_session(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.logged_in = False

    def _req(self, path, data=None, ajax=False):
        headers = {"User-Agent": UA, "Referer": SITE + "/main"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        req = urllib.request.Request(SITE + path, data=body, headers=headers)
        with self.opener.open(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    def login(self):
        self._new_session()
        html = self._req("/main")
        m = re.search(r'name="csrf_token"[^>]*value="([^"]*)"', html) or \
            re.search(r'value="([^"]*)"[^>]*name="csrf_token"', html)
        csrf = m.group(1) if m else ""
        text = self._req("/loginProcAjax", {
            "csrf_token": csrf, "mbmr_id": self.user_id, "mbmr_pwd": self.password,
        }, ajax=True)
        try:
            result = json.loads(text).get("result")
        except ValueError:
            raise LoginError("로그인 응답을 읽지 못했습니다. 사이트 접속 상태를 확인하세요.")
        if result in ("success", "pwdNextChange"):
            self.logged_in = True
            return
        msgs = {
            "notExist": "아이디 또는 비밀번호가 틀렸습니다.",
            "notMatch": "아이디 또는 비밀번호가 틀렸습니다.",
            "fail": "아이디 또는 비밀번호가 틀렸습니다.",
            "mbmrLock": "로그인이 차단된 아이디입니다. 사이트에서 비밀번호를 변경하세요.",
            "exceedLogin": "비밀번호 5회 오류로 로그인이 차단되었습니다.",
            "changePwd": "비밀번호 변경이 필요합니다. 사이트에서 먼저 변경하세요.",
            "notUseMbmr": "사용할 수 없는 계정입니다.",
            "leaveMbmr": "휴직 사유로 사용할 수 없는 계정입니다.",
            "csrfAt": "사이트에서 비정상 접근으로 판단했습니다(보안토큰).",
        }
        raise LoginError(msgs.get(result, f"로그인 실패 (응답: {result})"))

    def month_rooms(self, code, year, month):
        """해당 월의 {날짜: {객실형: 남은수}} 반환"""
        for attempt in range(2):
            if not self.logged_in:
                self.login()
            text = self._req("/onlineRsv/rsvRoomList", {
                "parameter": code, "year_month": f"{year}.{month:02d}"}, ajax=True)
            try:
                data = json.loads(text)
                rows = data["rsvPsblList"]
                break
            except (ValueError, KeyError, TypeError):
                self.logged_in = False   # 세션 만료 → 다시 로그인
        else:
            raise LoginError("객실 정보를 가져오지 못했습니다(로그인 세션 문제).")
        out = {}
        for row in rows:
            try:
                d = dt.datetime.strptime(row["rming_dt"][:10], "%Y-%m-%d").date()
            except (KeyError, ValueError, TypeError):
                continue
            out[d] = {t: to_int(row.get(f)) for t, f in TYPE_FIELD.items()}
        return out


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


# ───────────────────────── 알림 (폰 + PC) ─────────────────────────
class Notifier:
    def __init__(self, topic, pc_popup):
        self.topic, self.pc_popup = topic, pc_popup

    def send(self, title, text, urgent=False):
        ok = False
        if self.topic:
            ok = self._phone(title, text, urgent) or ok
        if self.pc_popup:
            ok = self._pc(title, text, urgent) or ok
        return ok

    def _phone(self, title, text, urgent=False):
        body = json.dumps({
            "topic": self.topic, "title": title, "message": text,
            "priority": 5 if urgent else 3,
            "tags": ["rotating_light"] if urgent else ["hotel"],
            "click": RSV_PAGE,
            "actions": [{"action": "view", "label": "예약하러 가기", "url": RSV_PAGE}],
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request("https://ntfy.sh", data=body,
                                     headers={"Content-Type": "application/json"})
        err = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    if r.status == 200:
                        return True
            except Exception as e:
                err = e
                time.sleep(3)
        log(f"폰 알림 전송 실패: {err}")
        return False

    def _pc(self, title, text, urgent=False):
        if os.name != "nt":
            return False
        try:
            import ctypes
            import winsound
            def show():
                try:
                    if urgent:   # 알람처럼 약 6초간 울림
                        for _ in range(6):
                            winsound.Beep(1500, 400)
                            winsound.Beep(1000, 400)
                    else:
                        winsound.MessageBeep(0x40)
                except Exception:
                    pass
                # 0x40000 = 항상 위, 0x40 = 정보 아이콘
                ctypes.windll.user32.MessageBoxW(0, text, title, 0x40 | 0x40000)
            threading.Thread(target=show, daemon=False).start()
            return True
        except Exception as e:
            log(f"PC 팝업 실패: {e}")
            return False


# ───────────────────────── 조회 로직 ─────────────────────────
def find_available(site, cfg):
    """조건에 맞는 빈 방 목록: [(연수원키, 체크인날짜, 객실형, 남은수)]"""
    today = now().date()
    cache = {}
    found = []
    for ykey in cfg.yeonsu:
        code, _, ytypes = YEONSU[ykey]
        types = [t for t in (cfg.types or ytypes) if t in ytypes]
        for d in cfg.dates:
            if d < today:
                continue
            if cfg.weekdays is not None and d.weekday() not in cfg.weekdays:
                continue
            nights = [d + dt.timedelta(days=i) for i in range(cfg.nights)]
            per_night = []
            for n in nights:
                k = (code, n.year, n.month)
                if k not in cache:
                    cache[k] = site.month_rooms(code, n.year, n.month)
                    time.sleep(1)
                per_night.append(cache[k].get(n))
            if any(p is None for p in per_night):
                continue          # 아직 예약이 열리지 않은 날짜
            for t in types:
                cnt = min(p.get(t, 0) for p in per_night)
                if cnt >= cfg.min_rooms:
                    found.append((ykey, d, t, cnt))
    return found


def fmt_date(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def load_state():
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_state(s):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(s), f, ensure_ascii=False)


def describe(cfg):
    ys = ", ".join(YEONSU[y][1] for y in cfg.yeonsu)
    if len(cfg.dates) > 4:
        ds = f"{cfg.dates[0]} ~ {cfg.dates[-1]} 중 {len(cfg.dates)}일"
    else:
        ds = ", ".join(str(d) for d in cfg.dates)
    wd = "" if cfg.weekdays is None else " / 요일: " + ",".join(WEEKDAYS[i] for i in sorted(cfg.weekdays))
    tp = "전체" if not cfg.types else ",".join(cfg.types)
    return f"{ys} / 체크인 {ds}{wd} / {cfg.nights}박 / 객실형 {tp}"


def main():
    if os.path.exists(STOP_PATH):
        log("로그인중지.txt 가 있어 실행하지 않습니다. "
            "(비밀번호를 고친 뒤 이 파일을 지우면 다시 작동합니다)")
        return
    cfg = Settings()
    notifier = Notifier(cfg.topic, False)
    try:
        run_minutes = max(0, int(os.environ.get("RUN_MINUTES", "4")))
    except ValueError:
        run_minutes = 4
    end_at = time.time() + run_minutes * 60

    site = Yeonsu(cfg.user_id, cfg.password)
    log("연수원 사이트 로그인 중...")
    try:
        site.login()
    except LoginError as e:
        stop_login(e)
        return
    except Exception as e:
        log(f"사이트 접속 실패: {e}")
        log("※ 계속 이 오류가 나면 연수원 사이트가 해외(GitHub 서버) 접속을 막는 것일 수 있습니다.")
        raise SystemExit(1)
    log("로그인 성공")
    log("조회 조건: " + describe(cfg))

    notified = load_state()
    while True:
        try:
            found = find_available(site, cfg)
            current = {f"{y}|{d}|{t}" for y, d, t, _ in found}
            new = [x for x in found if f"{x[0]}|{x[1]}|{x[2]}" not in notified]
            if found:
                log("빈 방: " + ", ".join(f"{YEONSU[y][1]} {fmt_date(d)} {t}형 {c}개"
                                         for y, d, t, c in found))
            else:
                log("조건에 맞는 빈 방 없음")
            if new and not in_quiet(cfg.quiet):
                lines = [f"• {YEONSU[y][1]} {fmt_date(d)} 체크인 {cfg.nights}박 {t}형 {c}개"
                         for y, d, t, c in new]
                if notifier.send("연수원 빈 방 발견!", "\n".join(lines) +
                                 "\n\n빨리 예약하세요!", urgent=True):
                    log(f"알림 전송 ({len(new)}건)")
                    notified |= {f"{y}|{d}|{t}" for y, d, t, _ in new}
            elif new:
                log("알림 쉬는 시간이라 전송을 미룹니다.")
            # 다시 마감된 방은 기록에서 지워서, 또 나오면 다시 알림
            notified &= current
            save_state(notified)
        except LoginError as e:
            if "틀렸" in str(e) or "차단" in str(e) or "변경" in str(e):
                stop_login(e)
                return
            log(f"오류: {e}")
            site.logged_in = False
        except Exception as e:
            log(f"조회 중 오류 (잠시 후 재시도): {e}")
            site.logged_in = False
        wait = cfg.interval * 60 + random.randint(-5, 5)
        if time.time() + wait > end_at:
            log("이번 실행을 마칩니다. (다음 실행은 GitHub가 자동으로 시작)")
            return
        time.sleep(max(55, wait))


def stop_login(e):
    """비밀번호 오류가 반복되면 사이트에서 계정이 잠기므로, 표시 파일을 남겨 다음 실행을 멈춤"""
    log(f"로그인 실패: {e}")
    with open(STOP_PATH, "w", encoding="utf-8") as f:
        f.write(f"{now():%Y-%m-%d %H:%M} 로그인 실패: {e}\n"
                "GitHub Secrets 의 YEONSU_ID / YEONSU_PW 를 고친 뒤 이 파일을 삭제하세요.\n")
    log("계정 잠김을 막기 위해 자동 실행을 멈춥니다. (로그인중지.txt 생성)")
    raise SystemExit(1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code and not isinstance(e.code, int):
            print("\n[설정 확인 필요] " + str(e.code))
            sys.exit(1)
        raise
