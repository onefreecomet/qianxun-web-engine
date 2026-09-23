"""积分签到 · WorkDaddy 本地服务代理 + 积分/签到领域逻辑。

两条取数通道：
  A) 主通道：转发 WorkDaddy daemon 本地 API（x-workdaddy-token 认证）。
     签到复用 daemon 自带的每日签到任务（幂等、自动刷新 token）。
  B) 降级通道（daemon 不可用时）：直接读 AppData 里 accounts/<uid>.info 的
     auth.accessToken / auth.domain，调用官方接口查积分、签到。

职责边界：
  - token 只在进程内存中拼 header，不落地、不打印、不写日志
  - 不修改 WorkDaddy 的任何配置或账号状态
  - 降级通道不写 WorkDaddy 的签到缓存（那是 daemon 的私有状态），
    因此每次都会真实打接口 —— 若 daemon 稍后恢复，两边判定可能并存但不冲突

降级通道的已知取舍：
  - 不自动刷新 token（刷新要 POST 官方 auth 接口并回写 .info，越界了）。
    token 过期时直接报「登录身份过期」，让用户回 WorkDaddy 续期。
  - 无每日缓存，重复点领取会重复打接口。官方接口对已签到返回
    code 10001 + "今天已签到"，幂等安全，只是多一次请求。
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# 批量积分并发上限：账号数通常个位数，6 路并发已能覆盖，且不会把官方接口打出限流
MAX_CREDIT_WORKERS = 6

# ---------------- WorkDaddy 数据目录定位 ----------------


def _candidate_data_dirs() -> list[Path]:
    out: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "WorkDaddy")
    out.append(Path.home() / "AppData" / "Roaming" / "WorkDaddy")
    return out


def find_data_dir() -> Path | None:
    for d in _candidate_data_dirs():
        if d.is_dir():
            return d
    return None


def read_api_token(data_dir: Path) -> str | None:
    try:
        tok = (data_dir / ".api-token").read_text(encoding="utf-8").strip()
        return tok or None
    except OSError:
        return None


def read_daemon_port(data_dir: Path) -> int | None:
    try:
        payload = json.loads((data_dir / "ui-port.json").read_text(encoding="utf-8"))
        port = int(payload.get("port") or 0)
        return port or None
    except (OSError, ValueError, TypeError):
        return None


# ---------------- daemon 调用 ----------------


class DaemonError(Exception):
    def __init__(self, message: str, status: int = 502, payload: Any = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.payload = payload


def daemon_call(method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> Any:
    """转发一次请求到 WorkDaddy daemon（认证头为 x-workdaddy-token）。"""
    data_dir = find_data_dir()
    if not data_dir:
        raise DaemonError("未找到 WorkDaddy 数据目录，请确认客户端已安装", 503)

    port = read_daemon_port(data_dir)
    if not port:
        raise DaemonError("未找到 WorkDaddy 端口记录（ui-port.json）", 503)

    token = read_api_token(data_dir)
    if not token:
        raise DaemonError("未找到本地 API 令牌（.api-token）", 503)

    url = f"http://127.0.0.1:{port}{path}"
    payload_bytes = None
    headers = {"x-workdaddy-token": token, "accept": "application/json"}
    if body is not None:
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json"

    req = urllib.request.Request(url, data=payload_bytes, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"raw": raw}
        msg = parsed.get("error") if isinstance(parsed, dict) else None
        raise DaemonError(msg or f"daemon 返回 {e.code}", e.code, parsed)
    except urllib.error.URLError as e:
        raise DaemonError(
            f"无法连接 WorkDaddy daemon（{e.reason}）。请确认客户端正在运行。", 503
        )
    except TimeoutError:
        raise DaemonError("WorkDaddy daemon 响应超时", 504)


# ---------------- 官方接口直连（降级通道） ----------------

ISSUER_HOST_MAP = {
    "https://www.workbuddy.ai": "https://www.workbuddy.ai",
    "https://www.workbuddy.cn": "https://www.workbuddy.cn",
    "https://www.codebuddy.cn": "https://www.codebuddy.cn",
    "https://www.codebuddy.ai": "https://www.codebuddy.ai",
}
CHECKIN_PATHS = ["/billing/meter/daily-checkin", "/v2/billing/meter/daily-checkin"]
RESOURCE_PATH = "/v2/billing/meter/get-user-resource"
ENTERPRISE_PATH = "/v2/billing/meter/get-enterprise-user-usage"
# 积分查询固定走国内版主域（对齐 daemon 的 PROFILE.apiHost），不带 x-user-id / x-domain
PROFILE_API_HOST = "https://www.workbuddy.cn"
ENTERPRISE_EDITIONS = ("ultimate", "exclusive")
CHECKIN_TIMEOUT = 12.0
RESOURCE_TIMEOUT = 25.0
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)


class OfficialError(Exception):
    def __init__(self, message: str, status: int = 502, expired: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.expired = expired


def token_issuer_origin(access_token: str) -> str | None:
    """从 JWT payload 的 iss 解析 origin（不解签，只读 payload）。"""
    try:
        part = str(access_token).split(".")[1]
        padded = part.replace("-", "+").replace("_", "/")
        padded += "=" * ((4 - len(padded) % 4) % 4)
        payload = json.loads(base64.b64decode(padded).decode("utf-8", "replace"))
        raw = str(payload.get("iss") or "")
        m = re.match(r"^(https?://[^/]+)", raw, re.I)
        return m.group(1).lower() if m else None
    except (IndexError, ValueError, TypeError):
        return None


def checkin_endpoints(access_token: str, fallback_domain: str = "") -> list[str]:
    """候选签到端点。issuer 不在映射表时退回 auth.domain，再退回两个国内域名。"""
    issuer = token_issuer_origin(access_token)
    host = ISSUER_HOST_MAP.get(issuer or "")
    if not host and fallback_domain:
        host = ISSUER_HOST_MAP.get(f"https://{fallback_domain}")
    hosts = [host] if host else []
    for h in ("https://www.codebuddy.cn", "https://www.workbuddy.cn"):
        if h not in hosts:
            hosts.append(h)
    return [h + p for h in hosts for p in CHECKIN_PATHS]


def _load_account_backups() -> list[dict]:
    """读 accounts/*.info，只取渲染/调用必需的字段。token 不出这个函数之外。"""
    data_dir = find_data_dir()
    if not data_dir:
        return []
    acc_dir = data_dir / "accounts"
    if not acc_dir.is_dir():
        return []
    out: list[dict] = []
    for f in sorted(acc_dir.glob("*.info")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        acc = d.get("account") or {}
        auth = d.get("auth") or {}
        tok = auth.get("accessToken")
        if not tok:
            continue
        out.append({
            "uid": acc.get("uid") or f.stem,
            "nickname": acc.get("nickname") or "未命名",
            "phone": acc.get("phoneNumber") or "",
            "type": acc.get("type") or "personal",
            "enterpriseName": acc.get("enterpriseName") or "",
            "_token": tok,
            "_domain": auth.get("domain") or "",
            "_expiresAt": auth.get("expiresAt") or 0,
            # 凭据健康度用：refreshToken 到期时间与上次刷新时间
            "_refreshExpiresAt": auth.get("refreshExpiresAt") or 0,
            "_lastRefreshTime": auth.get("lastRefreshTime") or 0,
            "_hasRefreshToken": bool(auth.get("refreshToken")),
        })
    return out


def _official_post(url: str, token: str, payload: dict, origin: str,
                   referer_path: str = "/profile/plans-usage",
                   extra_headers: dict | None = None, timeout: float = 25.0) -> tuple[bool, int, dict]:
    """POST 官方接口。返回 (http_ok, status, parsed_json)。"""
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "x-client-platform": "web",
        "origin": origin,
        "referer": origin + referer_path,
        "authorization": "Bearer " + token,
        "user-agent": UA,
    }
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return True, resp.status, (json.loads(raw) if raw else {})
            except ValueError:
                return True, resp.status, {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return False, e.code, parsed
    except urllib.error.URLError as e:
        raise OfficialError(f"官方接口不可达（{e.reason}）", 503)
    except TimeoutError:
        raise OfficialError("官方接口请求超时", 504)


def official_checkin_one(acc: dict) -> dict:
    """对单个账号直连官方接口签到。语义与 daemon 的 classify 保持一致。"""
    token = acc["_token"]
    uid = acc["uid"]
    domain = acc["_domain"]
    endpoints = checkin_endpoints(token, domain)
    last_msg = ""
    first_401: dict | None = None

    for url in endpoints:
        origin = url.split("/billing")[0]
        try:
            http_ok, status, body = _official_post(
                url, token, {},
                origin=origin,
                extra_headers={"x-user-id": str(uid), "x-domain": str(domain)},
                timeout=CHECKIN_TIMEOUT,
            )
        except OfficialError as e:
            last_msg = e.message
            continue

        code = body.get("code")
        message = body.get("msg") or body.get("message") or ("ok" if http_ok else f"HTTP {status}")
        result = {"ok": http_ok, "already": False, "code": code, "message": message}
        try:
            code_num = int(code) if code is not None and code != "" else None
        except (TypeError, ValueError):
            code_num = None

        inactive = bool(INACTIVE_RE.search(str(message)))
        already = code_num == 10001 and not inactive and bool(ALREADY_RE.search(str(message)))
        ok = (not inactive) and ((code_num == 0 and http_ok) or already)

        if ok:
            return {"ok": True, "already": already, "code": code_num, "message": str(message)}
        if status == 401:
            first_401 = {"ok": False, "already": False, "code": code_num,
                         "message": "登录身份过期", "expired": True}
            last_msg = "登录身份过期"
            continue
        if (400 <= status < 500 and status != 404) or (http_ok and status != 404):
            return {"ok": False, "already": False, "code": code_num, "message": str(message)}
        last_msg = str(message)

    if first_401:
        return first_401
    return {"ok": False, "already": False, "code": -1, "message": last_msg or "未知错误"}


def _build_resource_body(now: time.struct_time) -> dict:
    """积分查询 body。对齐 daemon buildCreditResourceBody：结束时间取 +101 年。

    注意别把范围收窄 —— 实测用 +370 天会漏掉长期礼包（少 1 条 500 分记录），
    因为部分礼包的 DeductionEndTime 落在一年之外。
    """
    begin = time.strftime("%Y-%m-%d %H:%M:%S", now)
    end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 101 * 365 * 86400))
    return {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": begin,
        "PackageEndTimeRangeEnd": end,
    }


# 字段兼容表：顺序与 WorkDaddy credit-segments.js 保持一致（Precise 优先，非精确兜底）
REMAIN_FIELDS = (
    "SlicePeriodCapacityRemainPrecise", "SlicePeriodCapacityRemain",
    "CycleCapacityRemainPrecise", "CycleCapacityRemain",
    "CapacityRemainPrecise", "CapacityRemain",
    "RemainPrecise", "Remain", "Remaining", "Balance",
)
TOTAL_FIELDS = (
    "SlicePeriodCapacitySizePrecise", "SlicePeriodCapacitySize",
    "CycleCapacitySizePrecise", "CycleCapacitySize",
    "CycleCapacityPrecise", "CycleCapacity",
    "CapacityPrecise", "Capacity",
    "TotalCapacityPrecise", "TotalCapacity",
    "PackageCapacity", "Quota", "Amount",
)
EXPIRE_FIELDS = (
    "DeductionEndTime", "ExpiredTime", "SlicePeriodEndTime",
    "PackageEndTime", "EndTime", "CycleEndTime",
    "ExpireTime", "ExpirationTime",
    "ValidEndTime", "ValidPeriodEndTime", "EndAt", "ExpireAt",
)
LABEL_FIELDS = ("PackageName", "ProductName", "SubProductName", "Description", "Name")


def _first_text_or_val(item: dict, fields: tuple[str, ...]) -> Any:
    for f in fields:
        v = item.get(f)
        if v not in (None, ""):
            return v
    return None


def _to_num(v: Any) -> float | None:
    """转数字，失败或空返回 None。"""
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


def _to_ms(v: Any) -> int | None:
    """统一成毫秒时间戳。接口可能给秒、毫秒或 ISO/日期字符串。"""
    if v is None or v == "":
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if s.isdigit():
            n = int(s)
            return n * 1000 if n < 100_000_000_000 else n
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return int(time.mktime(time.strptime(s, fmt)) * 1000)
            except ValueError:
                continue
        return None
    n = _to_num(v)
    if n is None:
        return None
    n = int(n)
    return n * 1000 if n < 100_000_000_000 else n


def _first_num(item: dict, fields: tuple[str, ...]) -> float | None:
    for f in fields:
        n = _to_num(item.get(f))
        if n is not None:
            return n
    return None


def _first_text(item: dict, fields: tuple[str, ...]) -> str | None:
    for f in fields:
        v = item.get(f)
        if v not in (None, ""):
            return str(v)
    return None


def official_credits_one(acc: dict) -> dict:
    """直连官方接口取单账号积分，整形成与 daemon /api/credits 相近的结构。

    注意：积分查询与签到不同 —— daemon 查积分时**只用 apiHost + Bearer**，
    不带 x-user-id / x-domain 头，且 apiHost 固定为 profile.apiHost（国内版
    www.workbuddy.cn）。实测带上额外头或换 codebuddy.cn 会返回不同的记录集
    （少 1 段 500 分），故这里严格对齐 daemon 的请求形态。
    """
    token = acc["_token"]
    uid = acc["uid"]
    origin = PROFILE_API_HOST
    now = time.localtime()

    url = origin + RESOURCE_PATH
    try:
        http_ok, status, body = _official_post(
            url, token, _build_resource_body(now),
            origin=origin,
            referer_path="/profile/plans-usage",
            timeout=RESOURCE_TIMEOUT,
        )
    except OfficialError as e:
        raise OfficialError(e.message, e.status)

    if status == 401:
        raise OfficialError("登录身份过期", 401, expired=True)
    if not http_ok:
        raise OfficialError(f"积分查询失败（HTTP {status}）", 502)

    code = body.get("code")
    if code not in (0, "0", None):
        raise OfficialError(str(body.get("msg") or body.get("message") or "积分查询被拒绝"), 502)

    rows = _extract_resource_rows(body)

    segments: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        # 与 daemon 对齐：remaining <= 0 的记录直接丢弃（不是显示为 0）
        remain = _first_num(row, REMAIN_FIELDS)
        if remain is None or remain <= 0:
            continue
        total = _first_num(row, TOTAL_FIELDS)
        if total is None:
            total = remain
        total = max(total, remain)
        segments.append({
            "remaining": round(remain, 2),
            "total": round(total, 2),
            "expiresAt": _to_ms(_first_text_or_val(row, EXPIRE_FIELDS)),
            "source": _first_text(row, LABEL_FIELDS) or "积分",
            "packageCode": str(row.get("PackageCode") or ""),
        })

    merged = merge_segments(segments)
    credits = round(sum(s["remaining"] for s in merged), 2)
    return {
        "credits": credits,
        "totalDosage": credits,
        "segments": merged,
        "unlimited": False,
        "meterError": None,
        "packageError": None,
    }


def _first_text_or_val(item: dict, fields: tuple[str, ...]) -> Any:
    for f in fields:
        v = item.get(f)
        if v not in (None, ""):
            return v
    return None


def _extract_resource_rows(body: dict) -> list:
    """从 get-user-resource 响应里挖出账号记录数组。

    实测路径（2026-09-15）：data.Response.Data.Accounts
    写成逐层兜底，上游调层级时不至于直接崩。
    """
    data = body.get("data")
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for wrapper in ("Response", "response"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            data = inner
            break
    rows = data.get("Accounts")
    if isinstance(rows, list):
        return rows
    for key in ("Data", "List", "Items", "accounts"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and isinstance(v.get("Accounts"), list):
            return v["Accounts"]
    return []


def merge_segments(segments: list[dict]) -> list[dict]:
    """按 packageCode|expiresAt 合并同礼包的多条记录（如 10 条 500 分 = 1 个 5000 分礼包）。

    与 daemon mergeCreditSegments 对齐：key = packageCode|expiresAt，total 累加。
    """
    bucket: dict[str, dict] = {}
    for s in segments:
        if not s:
            continue
        rem = _to_num(s.get("remaining")) or 0.0
        if rem <= 0:
            continue
        exp = s.get("expiresAt")
        key = f"{s.get('packageCode') or s.get('source') or '积分'}|{exp if exp is not None else 'unknown'}"
        cur = bucket.get(key)
        if cur is None:
            bucket[key] = {
                "remaining": rem,
                "total": _to_num(s.get("total")) or rem,
                "expiresAt": exp,
                "source": str(s.get("source") or "积分"),
                "packageCode": str(s.get("packageCode") or ""),
            }
        else:
            cur["remaining"] += rem
            cur["total"] += _to_num(s.get("total")) or rem

    out = []
    for v in bucket.values():
        v["remaining"] = round(v["remaining"], 2)
        v["total"] = round(v["total"], 2)
        out.append(v)
    out.sort(key=lambda x: (x["expiresAt"] is None, x["expiresAt"] or 0))
    return out


# ---------------- 领域逻辑：签到判定 ----------------

ALREADY_RE = re.compile(r"已签到|已领取|已经.*(?:签到|领取)|重复签到|already", re.I)
INACTIVE_RE = re.compile(r"未开启|未开始|未开放|已过期|无.*活动|活动.*(?:结束|关闭|暂停)", re.I)


def classify_checkin(raw: dict | None) -> dict:
    """把 daemon 的 checkin 展示值归一成前端语义。"""
    if not raw:
        return {"state": "none", "label": "未签到", "code": None, "message": ""}
    code = raw.get("code")
    message = str(raw.get("message") or "")
    ok = raw.get("ok") is True
    already = raw.get("already") is True

    if ok and (already or ALREADY_RE.search(message)):
        return {"state": "already", "label": "今日已签到", "code": code, "message": message}
    if ok:
        return {"state": "done", "label": "今日已签到", "code": code, "message": message}
    if INACTIVE_RE.search(message):
        return {"state": "inactive", "label": "活动未开启", "code": code, "message": message}
    if code in (0, 10001):
        return {"state": "done", "label": "今日已签到", "code": code, "message": message}
    return {"state": "fail", "label": "签到失败", "code": code, "message": message or "未知错误"}


# ---------------- 领域逻辑：积分段 ----------------

SOON_WINDOW_MS = 7 * 86400_000

# 凭据健康度阈值（对齐 WorkDaddy token-refresh.js 的 shouldRefreshAccessToken）
ACCESS_RENEW_MS = 1 * 86400_000    # accessToken 剩余 < 1 天 → daemon 会惰性刷新
REFRESH_WARN_MS = 3 * 86400_000    # refreshToken 剩余 < 3 天 → 提醒用户留意


def classify_expiry(ts: Any, now_ms: float) -> str:
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return "unknown"
    if t <= 0:
        return "unknown"
    if t < now_ms:
        return "expired"
    if t - now_ms <= SOON_WINDOW_MS:
        return "soon"
    return "ok"


def fmt_expiry(ts: Any) -> str | None:
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t / 1000.0))


def fmt_days(ts: Any, now_ms: float) -> str:
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return "—"
    if t <= 0:
        return "—"
    delta = t - now_ms
    if delta < 0:
        return "已过期"
    days = delta / 86400_000
    if days < 1:
        return f"{max(1, int(delta / 3600_000))} 小时"
    return f"{days:.1f} 天"


def credential_health(expires_at: Any, refresh_expires_at: Any, now_ms: float) -> dict:
    """凭据健康度：把 accessToken / refreshToken 的剩余寿命翻译成人话。

    背景：accessToken 短命（几天）是正常的，靠 refreshToken 滚动续期；
    真正致命的是 refreshToken 过期 —— 那时必须回 WorkDaddy 重新登录。

    因为 accessToken 会由 daemon 在「签到前惰性刷新」（阈值 1 天）自动续，
    所以 accessToken 临期本身不是问题，只作为「待续期」提示；
    只有 refreshToken 临期/过期才是需要用户动手的事。

    返回 state 四态：
      ok      —— 两层都健康
      soon    —— accessToken 已进入续期窗口（< 1 天），下次签到会自动续
      refresh —— refreshToken 临期（< 3 天），要留意
      expired —— refreshToken 已过期或缺失，必须重新登录
    """
    def _ms(v: Any) -> float:
        try:
            t = float(v)
        except (TypeError, ValueError):
            return 0.0
        return t if t > 0 else 0.0

    at = _ms(expires_at)
    rt = _ms(refresh_expires_at)

    refresh_state = "unknown"
    if rt:
        if rt < now_ms:
            refresh_state = "expired"
        elif rt - now_ms <= REFRESH_WARN_MS:
            refresh_state = "soon"
        else:
            refresh_state = "ok"

    if refresh_state == "expired":
        state = "expired"
    elif refresh_state == "soon":
        state = "refresh"
    elif at and at - now_ms <= ACCESS_RENEW_MS:
        state = "soon"
    else:
        state = "ok"

    return {
        "state": state,
        "accessExpiresAt": int(at) if at else None,
        "refreshExpiresAt": int(rt) if rt else None,
        "accessDays": round((at - now_ms) / 86400_000, 2) if at else None,
        "refreshDays": round((rt - now_ms) / 86400_000, 2) if rt else None,
    }


def build_account_view(payload: dict, now_ms: float) -> dict:
    """把 daemon /api/credits 的响应整理成前端渲染结构。"""
    segs_raw = payload.get("segments") or []
    segments: list[dict] = []
    for s in segs_raw:
        if not isinstance(s, dict):
            continue
        exp = s.get("expiresAt")
        segments.append({
            "remaining": s.get("remaining"),
            "total": s.get("total"),
            "expiresAt": exp,
            "expiresText": fmt_expiry(exp) or "长期有效",
            "daysText": fmt_days(exp, now_ms),
            "state": classify_expiry(exp, now_ms),
            "source": s.get("source") or "其他积分",
            "packageCode": s.get("packageCode") or "",
        })

    def _sort_key(x: dict):
        exp = x["expiresAt"]
        try:
            t = float(exp)
        except (TypeError, ValueError):
            t = 0.0
        return (t <= 0, t)

    segments.sort(key=_sort_key)

    total_remaining = 0.0
    for s in segments:
        try:
            total_remaining += float(s["remaining"] or 0)
        except (TypeError, ValueError):
            pass

    soon = [s for s in segments if s["state"] == "soon"]
    soon_sum = 0.0
    for s in soon:
        try:
            soon_sum += float(s["remaining"] or 0)
        except (TypeError, ValueError):
            pass

    return {
        "segments": segments,
        "segmentsCount": len(segments),
        "totalRemaining": round(total_remaining, 2),
        "soonCount": len(soon),
        "soonSum": round(soon_sum, 2),
        "unlimited": bool(payload.get("unlimited")),
        "meterError": payload.get("meterError") or None,
        "packageError": payload.get("packageError") or None,
    }


CHECKIN_TASK_ID = "daily-account-checkin"


def _fallback_accounts() -> dict:
    """降级：直连官方接口逐个查签到状态。"""
    backups = _load_account_backups()
    if not backups:
        raise DaemonError("WorkDaddy 数据目录里没有可用的账号备份", 503)

    now_ms = time.time() * 1000
    accounts = []
    expired_n = 0
    for a in backups:
        try:
            ck = official_checkin_one(a)
        except OfficialError as e:
            ck = {"ok": False, "already": False, "code": None, "message": e.message}
        if ck.get("expired"):
            expired_n += 1
        token_alive = not (a["_expiresAt"] and float(a["_expiresAt"]) < now_ms)
        health = credential_health(
            a.get("_expiresAt"), a.get("_refreshExpiresAt"), now_ms
        )
        health["lastRefreshTime"] = a.get("_lastRefreshTime") or None
        health["hasRefreshToken"] = bool(a.get("_hasRefreshToken"))
        # 降级通道不会自动刷新（刷新要回写 .info，越界），所以这里要更悲观：
        #   1) refreshToken 缺失 → 救不回来，直接 expired
        #   2) accessToken 已经过期 → 没有 daemon 帮忙续，现在就是不可用状态
        health["noAutoRenew"] = True
        if not a.get("_hasRefreshToken"):
            health["state"] = "expired"
        elif a.get("_expiresAt") and float(a["_expiresAt"]) < now_ms:
            health["state"] = "expired"
        accounts.append({
            "uid": a["uid"],
            "nickname": a["nickname"],
            "phone": a["phone"],
            "uid8": a["uid"][:8],
            "type": a["type"],
            "enterpriseName": a["enterpriseName"],
            "authValid": token_alive and not ck.get("expired"),
            "checkin": classify_checkin(ck),
            "todayUsage": None,
            "isCurrent": False,
            "credential": health,
        })

    return {
        "ok": True,
        "accounts": accounts,
        "currentUid": None,
        "fetchedAt": now_ms,
        "degraded": True,
        "degradedReason": "WorkDaddy 客户端未运行，已改为直连官方接口（今日用量不可用）",
        "expiredCount": expired_n,
    }


def _fallback_credit_view(uid: str) -> dict:
    """降级：直连官方接口取积分明细。"""
    backups = _load_account_backups()
    target = next((a for a in backups if a["uid"] == uid), None)
    if target is None:
        raise DaemonError("账号备份不存在", 404)

    payload = official_credits_one(target)
    now_ms = time.time() * 1000
    view = build_account_view(payload, now_ms)
    view.update({
        "ok": True,
        "uid": uid,
        "credits": payload.get("credits"),
        "totalDosage": payload.get("totalDosage"),
        "todayUsage": None,
        "degraded": True,
    })
    return view


def _fallback_claim() -> dict:
    """降级：直连官方接口对全部账号签到（幂等，官方对已签到返回 10001）。"""
    backups = _load_account_backups()
    if not backups:
        raise DaemonError("WorkDaddy 数据目录里没有可用的账号备份", 503)

    results = []
    ok_n = already_n = fail_n = 0
    for a in backups:
        try:
            r = official_checkin_one(a)
        except OfficialError as e:
            r = {"ok": False, "message": e.message}
        item = {
            "uid": a["uid"],
            "nickname": a["nickname"],
            "ok": bool(r.get("ok")),
            "already": bool(r.get("already")),
            "message": r.get("message") or "",
        }
        results.append(item)
        if item["ok"] and item["already"]:
            already_n += 1
        elif item["ok"]:
            ok_n += 1
        else:
            fail_n += 1

    return {
        "ok": fail_n == 0,
        "triggered": True,
        "degraded": True,
        "mode": "direct",
        "success": ok_n,
        "already": already_n,
        "failed": fail_n,
        "results": results,
    }


def fetch_accounts() -> dict:
    """账号列表。优先 daemon；不可用时降级直连官方接口。"""
    try:
        res = daemon_call("GET", "/api/accounts", timeout=25)
    except DaemonError:
        return _fallback_accounts()

    now_ms = time.time() * 1000
    current_uid = (res.get("current") or {}).get("uid")

    accounts = []
    for a in res.get("accounts") or []:
        health = credential_health(
            a.get("tokenExpiresAt"), a.get("refreshExpiresAt"), now_ms
        )
        health["lastRefreshTime"] = a.get("lastRefreshTime") or None
        accounts.append({
            "uid": a.get("uid"),
            "nickname": a.get("nickname") or "未命名",
            "phone": a.get("phone") or "",
            "uid8": (a.get("uid") or "")[:8],
            "type": a.get("type") or "personal",
            "enterpriseName": a.get("enterpriseName") or "",
            "authValid": a.get("authValid") is not False,
            "checkin": classify_checkin(a.get("checkin")),
            "todayUsage": a.get("todayUsage") or None,
            "isCurrent": a.get("uid") == current_uid,
            "credential": health,
        })

    return {
        "ok": True,
        "accounts": accounts,
        "currentUid": current_uid,
        "fetchedAt": now_ms,
    }


def fetch_credit_view(uid: str) -> dict:
    """单账号积分明细。优先 daemon；不可用时降级直连官方接口。"""
    try:
        res = daemon_call("POST", "/api/credits", {"uid": uid}, timeout=40)
    except DaemonError:
        return _fallback_credit_view(uid)

    now_ms = time.time() * 1000
    view = build_account_view(res, now_ms)
    view.update({
        "ok": True,
        "uid": uid,
        "credits": res.get("credits"),
        "totalDosage": res.get("totalDosage"),
        "todayUsage": res.get("todayUsage") or None,
    })
    return view


def fetch_credit_views(uid_list: list[str]) -> dict:
    """批量积分明细：并发拉取多个账号，一次请求拿完。

    背景：前端逐个账号串行请求时，N 个账号就要 N 次往返（每次 1~3 秒），
    进页面要等很久。这里用线程池并发，总耗时约等于最慢的那一个账号。

    单个账号失败不影响其它账号：失败项以 {ok: False, error} 返回。
    """
    uids = [u for u in (uid_list or []) if u]
    if not uids:
        return {"ok": True, "views": {}, "failed": {}}

    views: dict[str, Any] = {}
    failed: dict[str, str] = {}

    def _one(uid: str) -> None:
        try:
            views[uid] = fetch_credit_view(uid)
        except DaemonError as e:
            failed[uid] = e.message
        except OfficialError as e:
            failed[uid] = e.message
        except Exception as e:  # noqa: BLE001 - 单账号异常不拖垮整批
            failed[uid] = f"{type(e).__name__}: {e}"

    workers = max(1, min(len(uids), MAX_CREDIT_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(_one, uids))

    return {"ok": True, "views": views, "failed": failed, "fetchedAt": time.time() * 1000}


def trigger_claim(wait_seconds: float = 55.0) -> dict:
    """一键领取。优先触发 daemon 的签到任务；不可用时降级直连官方接口。

    daemon 通道：调 panelOpened 事件（不要求手动运行权限），任务内部遍历全部
    账号签到并幂等跳过已签到的，然后轮询运行记录拿最终状态。
    """
    try:
        daemon_call("POST", "/api/automations/events", {"type": "panelOpened"}, timeout=20)
    except DaemonError:
        return _fallback_claim()

    deadline = time.time() + wait_seconds
    run: dict | None = None
    while time.time() < deadline:
        time.sleep(1.2)
        try:
            res = daemon_call("GET", "/api/automations", timeout=15)
        except DaemonError:
            break
        for r in res.get("runs") or []:
            if r.get("taskId") == CHECKIN_TASK_ID:
                run = r
                break
        if run and run.get("status") in ("success", "failed", "cancelled"):
            break

    if not run:
        return {
            "ok": True,
            "triggered": True,
            "note": "已触发领取，但未取到运行状态，请点刷新查看结果",
        }

    return {
        "ok": run.get("status") == "success",
        "triggered": True,
        "status": run.get("status"),
        "error": run.get("error") or None,
        "startedAt": run.get("startedAt"),
        "finishedAt": run.get("finishedAt"),
    }


def health() -> dict:
    """诊断：daemon 可达性 / 端口 / 账号数；不可用时报告降级通道是否可用。"""
    data_dir = find_data_dir()
    info: dict[str, Any] = {
        "ok": False,
        "dataDir": str(data_dir) if data_dir else None,
    }
    if not data_dir:
        info["error"] = "未找到 WorkDaddy 数据目录"
        info["degradedUsable"] = False
        return info

    info["daemonPort"] = read_daemon_port(data_dir)
    info["hasToken"] = bool(read_api_token(data_dir))

    try:
        res = daemon_call("GET", "/api/accounts", timeout=10)
    except DaemonError as e:
        info["error"] = e.message
        backups = _load_account_backups()
        info["degradedUsable"] = bool(backups)
        info["backupCount"] = len(backups)
        return info

    info["ok"] = True
    info["degraded"] = False
    info["accountCount"] = len(res.get("accounts") or [])
    return info
