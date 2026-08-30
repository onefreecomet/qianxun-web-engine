"""WQ BRAIN API 客户端核心实现。

关键修复（对照原 machine_lib.py 的 P0/P1 问题）：

1. **凭据安全**：`AuthError` 启动时检测，无环境变量直接抛错，绝不在源码明文写密码。
2. **重试有上限**：`max_retries` 控制总次数，不再 while True 死循环。
3. **指数退避**：429/5xx 时按 backoff_factor 退避，遵守 Retry-After 头。
4. **失败显式抛错**：`create_simulation` 失败抛 SimulationError，让上层调度器决定重试/丢弃/记录。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import httpx
from loguru import logger

from .config import BrainConfig


class BrainClientError(Exception):
    """API 客户端基础异常。"""


class AuthError(BrainClientError):
    """认证失败（缺凭据、登录被拒绝、生物识别未通过）。"""


class RateLimitError(BrainClientError):
    """429 限流 / 重试耗尽 / 模拟仍在运行。

    retry_after: 平台建议的等待秒数（轮询模拟时携带）。
    """

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class SimulationError(BrainClientError):
    """模拟提交或轮询失败。"""


class APIClient:
    """同步版 WQ BRAIN API 客户端（线程安全，单实例复用）。"""

    def __init__(self, config: BrainConfig):
        self.config = config
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()
        self._authenticated = False
        self._interrupt_event: threading.Event | None = None
        # v28：最近一次 /simulations 提交响应头的每日配额（limit/remaining/reset_sec/updated_at）
        self.last_ratelimit: dict | None = None

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        """解析 Retry-After：支持秒数或 HTTP-date 格式；解析失败返回 None（不崩溃）。"""
        if not value:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            try:
                from datetime import datetime, timezone
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(value)
                return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                return None

    def set_interrupt_event(self, event: threading.Event | None) -> None:
        """绑定中断事件（调度器取消时 set），让退避 sleep 立即中断、不再重试。"""
        self._interrupt_event = event

    def _sleep_interruptible(self, seconds: float) -> None:
        """可中断的退避 sleep：中断事件被 set 时立即抛错，不等完整退避。"""
        ev = self._interrupt_event
        if ev is not None:
            if ev.wait(seconds):  # True = 事件已设置（任务被取消）
                raise BrainClientError("任务已取消")
        else:
            time.sleep(seconds)

    def __enter__(self) -> "APIClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None
            self._authenticated = False

    def _get_client(self) -> httpx.Client:
        # 加锁：多线程首次并发调用时若不加锁会创建多个 httpx.Client
        # （被覆盖的实例连接泄漏），且可能并发重复登录
        with self._lock:
            if self._client is None:
                self._client = httpx.Client(
                    base_url=self.config.base_url,
                    timeout=self.config.timeout,
                    follow_redirects=True,
                    headers={"User-Agent": "AlphaMachine/0.1"},
                )
            return self._client

    def authenticate(self) -> None:
        """登录 BRAIN。失败抛 AuthError。"""
        if not self.config.is_authenticated():
            raise AuthError(
                "未配置凭据：请设置环境变量 WQ_USERNAME 和 WQ_PASSWORD"
            )
        client = self._get_client()
        try:
            resp = client.post(
                "/authentication",
                auth=(self.config.username, self.config.password),
            )
        except httpx.HTTPError as e:
            raise AuthError(f"认证网络异常：{e}") from e

        # 真实平台 /authentication 成功返回 201（Created），不是 200
        if not resp.is_success:
            # 检查是否需要生物识别
            if resp.headers.get("WWW-Authenticate") == "persona":
                raise AuthError(
                    "需要生物识别登录，请先在浏览器完成后再调用本客户端"
                )
            raise AuthError(
                f"认证失败：HTTP {resp.status_code}，响应：{resp.text[:200]}"
            )

        with self._lock:
            self._authenticated = True
        logger.info("WQ BRAIN 登录成功：user={}", self.config.username)

    def _ensure_authed(self) -> httpx.Client:
        client = self._get_client()
        with self._lock:
            authed = self._authenticated
        if not authed:
            self.authenticate()
        return client

    def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json: Any = None,
        op_name: str = "request",
    ) -> httpx.Response:
        """带指数退避的重试请求。

        触发重试：429 / 5xx / 网络超时 / Retry-After 头存在。
        重试耗尽：抛 RateLimitError 或原异常。
        """
        client = self._ensure_authed()
        delay = 1.0
        last_exc: Exception | None = None
        last_status: int | None = None
        last_body: str = ""
        # 并发超限（CONCURRENT_SIMULATION_LIMIT_EXCEEDED）：等平台释放并发，
        # 不能按普通 max_retries 短退避就放弃（真实回测发现），最多重试 12 次、每次至少 30s
        concurrent_limit_retries = 12
        # 修复：撞并发上限时只放宽（max 取较大值），不把用户配置的
        # 更大重试上限（如 20）压到 12
        max_attempts = max(self.config.max_retries, concurrent_limit_retries)

        # 修复：for range 只求值一次，循环内改 max_attempts 不生效 → 并发超限
        # 重试上限（12 次）从未真正放宽。改用 while + 显式计数。
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                resp = client.request(
                    method, url, params=params, json=json
                )
                last_status = resp.status_code
                last_body = resp.text[:200]

                # 401 重新登录（带退避，避免凭据失效时快速连打认证接口）
                if resp.status_code == 401:
                    logger.warning("{} 收到 401，重新登录", op_name)
                    self._sleep_interruptible(min(delay, self.config.backoff_max))
                    delay *= self.config.backoff_factor
                    self.authenticate()
                    continue

                # 成功
                if resp.status_code < 400:
                    return resp

                # 429 / 5xx 重试
                if resp.status_code in (429, *range(500, 600)):
                    retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
                    if retry_after is not None:
                        sleep_s = min(retry_after, self.config.backoff_max)
                    else:
                        sleep_s = min(delay, self.config.backoff_max)
                    # 并发超限：等前面的模拟完成释放名额，退避至少 30s，重试上限放宽
                    if "CONCURRENT_SIMULATION" in last_body:
                        sleep_s = max(sleep_s, 30.0)
                        max_attempts = concurrent_limit_retries
                    logger.warning(
                        "{} HTTP {}，{}秒后重试 (第{}/{})",
                        op_name, resp.status_code, sleep_s, attempt, max_attempts,
                    )
                    self._sleep_interruptible(sleep_s)
                    delay *= self.config.backoff_factor
                    continue

                # 其他 4xx 不重试（真实验证发现：HTTPStatusError 曾被当网络错误重试 5 次）
                resp.raise_for_status()

            except httpx.HTTPStatusError:
                # 4xx/5xx 已由上面分支处理；这里只处理 raise_for_status 抛出的
                # （400 等不可重试错误），直接抛给上层
                raise

            except httpx.HTTPError as e:
                # 纯网络错误（连接/超时）才重试
                last_exc = e
                sleep_s = min(delay, self.config.backoff_max)
                logger.warning(
                    "{} 网络异常 {}，{}秒后重试 (第{}/{}次)",
                    op_name, e, sleep_s, attempt, self.config.max_retries,
                )
                self._sleep_interruptible(sleep_s)
                delay *= self.config.backoff_factor
                continue

        detail = ""
        if last_status is not None:
            detail = f"（最后一次 HTTP {last_status}：{last_body[:100]}）"
        raise RateLimitError(
            f"{op_name} 重试 {self.config.max_retries} 次后仍失败{detail}"
        ) from last_exc

    # -------- datasets / datafields --------

    def get_datasets(
        self,
        instrument_type: str = "EQUITY",
        region: str = "USA",
        delay: int = 1,
        universe: str = "TOP3000",
    ) -> list[dict]:
        """拉数据集列表。"""
        resp = self._request_with_retry(
            "GET", "/data-sets",
            params={
                "instrumentType": instrument_type,
                "region": region,
                "delay": delay,
                "universe": universe,
            },
            op_name="get_datasets",
        )
        return resp.json().get("results", [])

    def get_datafields(
        self,
        instrument_type: str = "EQUITY",
        region: str = "USA",
        delay: int = 1,
        universe: str = "TOP3000",
        dataset_id: str = "",
        search: str = "",
    ) -> list[dict]:
        """拉数据字段列表（带翻页）。

        修复原 search 模式 count=100 硬编码截断：现在按 search_max_results 封顶。
        """
        if search:
            url_template = (
                "/data-fields?instrumentType={itype}&region={region}"
                "&delay={delay}&universe={universe}&limit={lim}&search={search}&offset={off}"
            )
            # 真实验证发现：search 模式 offset>=100 报 400；平台 search 结果最多 100 条
            max_count = min(self.config.search_max_results, 100)
        else:
            url_template = (
                "/data-fields?instrumentType={itype}&region={region}"
                "&delay={delay}&universe={universe}&dataset.id={dsid}&limit={lim}&offset={off}"
            )
            head = self._request_with_retry(
                "GET", url_template.format(
                    itype=instrument_type, region=region, delay=delay,
                    universe=universe, dsid=dataset_id, lim=self.config.page_size, off=0,
                ),
                op_name="get_datafields.count",
            )
            max_count = head.json().get("count", 0)

        lim = self.config.page_size
        all_results: list[dict] = []
        for offset in range(0, max_count, lim):
            url = url_template.format(
                itype=instrument_type, region=region, delay=delay,
                universe=universe, dsid=dataset_id, lim=lim, off=offset,
                search=search,
            )
            resp = self._request_with_retry(
                "GET", url, op_name=f"get_datafields[{offset}]",
            )
            all_results.extend(resp.json().get("results", []))

        logger.info("get_datafields: region={} search={} dataset={} 共 {} 条",
                    region, search, dataset_id, len(all_results))
        return all_results

    def get_operators(self, scope: str = "REGULAR") -> list[dict]:
        """拉 BRAIN 算子列表（参照 ace_lib.get_operators）。

        平台 /operators 返回 list，每条算子 scope 可能是列表；explode 后按 scope 过滤。
        默认只留 REGULAR（可直接用于 alpha 表达式）。
        返回 [{"name", "category", "description"}, ...]
        """
        resp = self._request_with_retry(
            "GET", "/operators", op_name="get_operators",
        )
        data = resp.json()
        if isinstance(data, dict):
            data = data.get("results") or data.get("operators") or list(data.values())
        out: list[dict] = []
        for op in data:
            if not isinstance(op, dict):
                continue
            name = op.get("name")
            if not name:
                continue
            scopes = op.get("scope")
            if isinstance(scopes, list):
                hit = scope in scopes
            else:
                hit = (scopes == scope)
            if not hit:
                continue
            out.append({
                "name": name,
                "category": op.get("category") or "Unknown",
                "description": op.get("description") or "",
            })
        logger.info("get_operators: scope={} 共 {} 个", scope, len(out))
        return out

    # -------- simulations --------

    def create_simulation(self, simulation_data: dict) -> str:
        """提交单个模拟，返回 progress URL。

        修复原 multi_simulate 失败静默丢任务：失败抛 SimulationError。
        """
        resp = self._request_with_retry(
            "POST", "/simulations", json=simulation_data,
            op_name="create_simulation",
        )
        if "Location" not in resp.headers:
            raise SimulationError(
                f"提交成功但响应头无 Location：{resp.text[:200]}"
            )
        return resp.headers["Location"]

    def create_multi_simulations(self, simulations: list[dict]) -> str:
        """批量提交多个模拟（POST body 为数组，multi-sim）。

        与原 machine_lib.multi_simulate 一致：一次 POST 一个 multi-sim，
        只占 1 个并发名额（避免单模拟逐个提交撞 CONCURRENT_SIMULATION_LIMIT_EXCEEDED）。
        返回 multi-sim 的 progress URL。

        v28：解析响应头的 x-ratelimit-limit/remaining/reset（每日回测配额），
        存入 self.last_ratelimit 供 UI 显示"回测槽剩余 + 重置时间"。
        """
        resp = self._request_with_retry(
            "POST", "/simulations", json=simulations,
            op_name="create_multi_sim",
        )
        # v28：每日模拟配额（插件 WebDataScope 同款机制：响应头）
        try:
            h = {k.lower(): v for k, v in resp.headers.items()}
            limit = int(float(h.get("x-ratelimit-limit", "0")))
            remaining = int(float(h.get("x-ratelimit-remaining", "0")))
            reset_sec = float(h.get("x-ratelimit-reset", "0"))
            if limit > 0:
                self.last_ratelimit = {
                    "limit": limit,
                    "remaining": remaining,
                    "reset_sec": reset_sec,      # 距重置的剩余秒数
                    "updated_at": time.time(),
                }
        except Exception:
            pass
        if "Location" not in resp.headers:
            raise SimulationError(
                f"批量提交成功但响应头无 Location：{resp.text[:200]}"
            )
        return resp.headers["Location"]

    def get_multi_sim_progress(self, progress_url: str) -> dict:
        """轮询 multi-sim 进度。返回 {status, children, type}；仍在运行抛 RateLimitError。

        children 是子模拟 ID 列表，与提交顺序对应。
        """
        resp = self._request_with_retry(
            "GET", progress_url, op_name="multi_sim_progress",
        )
        retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
        if retry_after is not None:
            raise RateLimitError(
                f"multi-sim still running, retry after {retry_after}s",
                retry_after=retry_after,
            )
        # 修复：模拟刚完成时平台可能返回空 body（200 空响应）→ json() 抛
        # JSONDecodeError 逃逸会把整批 sim 留在 submitted；空 body 当 RUNNING 重试
        try:
            return resp.json()
        except ValueError:
            return {}

    def get_single_sim_alpha(self, sim_id: str) -> dict:
        """查单个子模拟结果：成功含 alpha，失败含 message/status。"""
        resp = self._request_with_retry(
            "GET", f"/simulations/{sim_id}", op_name=f"sim_child[{sim_id}]",
        )
        return resp.json()

    def get_simulation_progress(self, progress_url: str) -> dict:
        """轮询单次模拟进度。仍在运行则抛 RateLimitError 让上层 sleep。

        修复原 while True 死循环：上层调度器控制总轮询时长。
        """
        resp = self._request_with_retry(
            "GET", progress_url, op_name="simulation_progress",
        )
        retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
        if retry_after is not None:
            raise RateLimitError(
                f"simulation still running, retry after {retry_after}s",
                retry_after=retry_after,
            )
        return resp.json()

    # -------- alphas --------

    def get_alphas(
        self,
        start_date: str,
        end_date: str,
        sharpe_th: float,
        fitness_th: float,
        region: str,
        alpha_num: int,
        usage: str = "track",
        *,
        year: int | None = None,
    ) -> list[dict]:
        """拉取用户 alpha 列表。

        修复原 get_alphas：
        - 日期硬编码 2026-（改 year 参数，默认当前年）
        - 裸 except 丢页（翻页失败抛 RateLimitError 让上层重试当前页）
        """
        if year is None:
            # 修复：默认取美东年（平台 dateCreated 是美东时间，
            # 北京 1 月 1 日 0-13 点美东还是去年，用本地年会差一天）
            from datetime import datetime
            try:
                from zoneinfo import ZoneInfo
                year = datetime.now(ZoneInfo("America/New_York")).year
            except Exception:
                year = datetime.now().year

        def _ts_param(date_part: str) -> str:
            """① MM-dd → {year}-MM-ddT00:00:00-04:00
            ② 完整日期 yyyy-MM-dd（end 加一天跨年）→ 直接拼 T00:00:00-04:00
            ③ 完整时间戳（含 T）原样用。
            """
            if "T" in date_part:
                return date_part
            if re.match(r"^\d{4}-\d{2}-\d{2}$", date_part):
                return f"{date_part}T00:00:00-04:00"
            return f"{year}-{date_part}T00:00:00-04:00"

        urls = [
            "/users/self/alphas?limit=100&offset={off}"
            f"&status=UNSUBMITTED%1FIS_FAIL&dateCreated%3E={_ts_param(start_date)}"
            f"&dateCreated%3C{_ts_param(end_date)}"
            f"&is.fitness%3E{fitness_th}&is.sharpe%3E{sharpe_th}"
            f"&settings.region={region}&order=-is.sharpe&hidden=false&type!=SUPER",
        ]
        if usage != "submit":
            urls.append(
                "/users/self/alphas?limit=100&offset={off}"
                f"&status=UNSUBMITTED%1FIS_FAIL&dateCreated%3E={_ts_param(start_date)}"
                f"&dateCreated%3C{_ts_param(end_date)}"
                f"&is.fitness%3C-{fitness_th}&is.sharpe%3C-{sharpe_th}"
                f"&settings.region={region}&order=is.sharpe&hidden=false&type!=SUPER",
            )

        all_results: list[dict] = []
        for i in range(0, alpha_num, 100):
            for url_template in urls:
                url = url_template.format(off=i)
                resp = self._request_with_retry(
                    "GET", url, op_name=f"get_alphas[{i}]",
                )
                results = resp.json().get("results", [])
                all_results.extend(results)
        # 正负 sharpe 两页可能返回同一 alpha，按 id 去重
        seen: set[str] = set()
        deduped: list[dict] = []
        for a in all_results:
            aid = a.get("id")
            if aid in seen:
                continue
            seen.add(aid)
            deduped.append(a)
        return deduped

    def list_all_submitted_alphas(
        self,
        page_size: int = 100,
        progress_cb=None,
        max_pages: int | None = None,
    ) -> list[dict]:
        """全量已提交 alpha 列表（无 search 过滤），供 PnL 同步用。

        端点与 ProdMemo 一致：
          GET /users/self/alphas?limit=100&offset=N&order=-dateSubmitted
              &hidden=false&status%21=UNSUBMITTED%1FIS-FAIL

        返回的每条至少含：id / dateSubmitted / status / stage / classifications /
        settings（region 等）。如果某 alpha dateSubmitted 为空或 status 是
        UNSUBMITTED 会过滤掉。

        progress_cb(done, total, alpha_id) 用于推 UI 进度。
        """
        all_results: list[dict] = []
        seen: set[str] = set()
        offset = 0
        page = 0
        # 第一页拿 count
        first_url = (
            f"/users/self/alphas?limit={page_size}&offset=0"
            f"&order=-dateSubmitted&hidden=false"
            f"&status%21=UNSUBMITTED%1FIS-FAIL"
        )
        resp = self._request_with_retry("GET", first_url, op_name="list_all_submitted[0]")
        data = resp.json()
        total = data.get("count") or 0
        for a in data.get("results", []):
            aid = a.get("id")
            if not aid or aid in seen:
                continue
            if not a.get("dateSubmitted"):
                continue
            if a.get("status") == "UNSUBMITTED":
                continue
            seen.add(aid)
            all_results.append(a)
        if progress_cb:
            progress_cb(len(all_results), total, all_results[-1]["id"] if all_results else None)
        page = 1
        while offset < total:
            if max_pages is not None and page >= max_pages:
                break
            offset = page * page_size
            url = (
                f"/users/self/alphas?limit={page_size}&offset={offset}"
                f"&order=-dateSubmitted&hidden=false"
                f"&status%21=UNSUBMITTED%1FIS-FAIL"
            )
            resp = self._request_with_retry("GET", url, op_name=f"list_all_submitted[{offset}]")
            page_data = resp.json()
            results = page_data.get("results", [])
            if not results:
                break
            for a in results:
                aid = a.get("id")
                if not aid or aid in seen:
                    continue
                if not a.get("dateSubmitted"):
                    continue
                if a.get("status") == "UNSUBMITTED":
                    continue
                seen.add(aid)
                all_results.append(a)
            if progress_cb:
                progress_cb(len(all_results), total, aid)
            page += 1
        return all_results

    def list_scope_alphas(
        self,
        region: str,
        delay: int,
        *,
        max_scan: int = 1000,
        page_size: int = 100,
        hidden: bool | None = False,
    ) -> list[dict]:
        """拉取指定 region/delay 下已提交 alpha 列表（Osmosis 用）。

        端点：
          GET /users/self/alphas?limit=100&offset=N&order=-dateSubmitted
              &settings.region=USA&settings.delay=1&hidden=false
              &status%21=UNSUBMITTED%1FIS_FAIL

        返回的每条含 settings/self_corr/prod_corr 等。
        """
        all_results: list[dict] = []
        seen: set[str] = set()
        offset = 0
        limit = page_size
        target_region = str(region).strip().upper()
        target_delay = int(str(delay).strip())

        while offset < max_scan:
            hidden_part = f"&hidden={'true' if hidden else 'false'}" if hidden is not None else ""
            url = (
                f"/users/self/alphas?limit={limit}&offset={offset}"
                f"&order=-dateSubmitted&settings.region={target_region}"
                f"&settings.delay={target_delay}{hidden_part}"
                f"&status%21=UNSUBMITTED%1FIS_FAIL"
            )
            resp = self._request_with_retry("GET", url, op_name=f"list_scope_alphas[{offset}]")
            data = resp.json()
            results = data.get("results", [])
            if not results:
                break
            for a in results:
                aid = a.get("id")
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                all_results.append(a)
            if len(results) < limit:
                break
            offset += len(results)
            if offset >= max_scan:
                break
        return all_results

    def get_alpha_details(self, alpha_id: str) -> dict:
        """单 alpha 详情。"""
        resp = self._request_with_retry(
            "GET", f"/alphas/{alpha_id}", op_name=f"get_alpha_details[{alpha_id}]",
        )
        return resp.json()

    @staticmethod
    def extract_alpha_metrics(detail: dict) -> dict:
        """从 /alphas/{id} 详情提取 alphas 表字段（对齐原 machine_lib.get_alphas 的取值）。"""
        is_ = detail.get("is") or {}
        settings = detail.get("settings") or {}
        regular = detail.get("regular") or {}
        return {
            "alpha_id": detail.get("id") or "",
            "expression": regular.get("code") or "",
            "sharpe": is_.get("sharpe"),
            "returns": is_.get("returns"),
            "fitness": is_.get("fitness"),
            "turnover": is_.get("turnover"),
            "margin": is_.get("margin"),
            "long_count": is_.get("longCount"),
            "short_count": is_.get("shortCount"),
            "decay": settings.get("decay"),
            "region": settings.get("region"),
            "neutralization": settings.get("neutralization"),
            "date_created": detail.get("dateCreated"),
        }

    def get_alpha_pnl(self, alpha_id: str) -> list[dict]:
        """拉取单个 alpha 的日度 PnL（recordsets 端点）。

        真实验证发现（两轮实测）：
        1. `/alphas/{id}/pnl` 是 404；正确端点是 `/alphas/{id}/recordsets/daily-pnl`
        2. records 是**二维数组**（每行按 schema.properties 顺序对应值），不是对象数组
        3. 记录集生成需要时间：模拟刚完成时可能返回空 body / Retry-After，需重试

        返回：列表，元素形如 {"date": "...", "pnl": float}（按 schema 属性名组装）。
        """
        for _ in range(self.config.max_retries):
            resp = self._request_with_retry(
                "GET", f"/alphas/{alpha_id}/recordsets/daily-pnl",
                op_name=f"get_alpha_pnl[{alpha_id}]",
            )
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            if retry_after is not None:
                time.sleep(min(retry_after, self.config.backoff_max))
                continue
            text = resp.text.strip()
            if not text:
                time.sleep(2)
                continue
            try:
                data = resp.json()
            except Exception:
                time.sleep(2)
                continue
            if isinstance(data, dict) and "records" in data:
                props = [
                    p.get("name")
                    for p in (data.get("schema") or {}).get("properties") or []
                ]
                out: list[dict] = []
                for rec in data["records"]:
                    if isinstance(rec, dict):
                        out.append(dict(rec))
                    elif isinstance(rec, (list, tuple)):
                        # 数组格式：按 schema 属性顺序 zip 成 dict
                        out.append(dict(zip(props, rec)))
                return out
            raise BrainClientError(
                f"pnl 响应结构异常：{str(data)[:200]}"
            )
        raise RateLimitError(
            f"get_alpha_pnl[{alpha_id}] 重试 {self.config.max_retries} 次后记录集仍未就绪"
        )

    @staticmethod
    def parse_alpha_metrics(details: dict) -> dict:
        """从 alpha 详情提取入库所需的指标字段（storage.upsert_alpha 的输入）。"""
        is_data = details.get("is") or {}
        settings = details.get("settings") or {}
        regular = details.get("regular") or {}
        return {
            "alpha_id": details["id"],
            "expression": regular.get("code", ""),
            "sharpe": is_data.get("sharpe"),
            "returns": is_data.get("returns"),
            "fitness": is_data.get("fitness"),
            "turnover": is_data.get("turnover"),
            "margin": is_data.get("margin"),
            "long_count": is_data.get("longCount"),
            "short_count": is_data.get("shortCount"),
            "decay": settings.get("decay"),
            "region": settings.get("region"),
            "neutralization": settings.get("neutralization"),
            "date_created": details.get("dateCreated"),
        }

    def check_submission(self, alpha_id: str) -> dict:
        """提交检查（含 Retry-After 轮询）。"""
        # Retry-After 头存在表示还在处理，需要轮询
        for attempt in range(1, self.config.max_retries + 1):
            resp = self._request_with_retry(
                "GET", f"/alphas/{alpha_id}/check",
                op_name=f"check_submission[{alpha_id}]",
            )
            if "Retry-After" not in resp.headers:
                return resp.json()
            retry_after = self._parse_retry_after(resp.headers.get("Retry-After"))
            if retry_after is None:
                retry_after = self.config.backoff_max
            sleep_s = retry_after
            logger.info("check_submission[{}] 仍在处理，{}秒后再试", alpha_id, sleep_s)
            time.sleep(min(sleep_s, self.config.backoff_max))
        raise RateLimitError(
            f"check_submission[{alpha_id}] 重试 {self.config.max_retries} 次仍超时"
        )

    def set_alpha_properties(
        self,
        alpha_id: str,
        name: str | None = None,
        color: str | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        """PATCH alpha 属性。"""
        params = {
            "color": color,
            "name": name,
            "tags": tags or ["ace_tag"],
            "category": None,
        }
        resp = self._request_with_retry(
            "PATCH", f"/alphas/{alpha_id}", json=params,
            op_name=f"set_alpha_properties[{alpha_id}]",
        )
        return resp.status_code < 400

    def submit_alpha(self, alpha_id: str) -> dict:
        """提交 alpha（POST /alphas/{id}/submit）。

        注意：这是**真实提交**操作，调用方必须已确认。返回提交响应 JSON。
        """
        resp = self._request_with_retry(
            "POST", f"/alphas/{alpha_id}/submit",
            op_name=f"submit_alpha[{alpha_id}]",
        )
        logger.info("submit_alpha[{}] HTTP {}：{}", alpha_id, resp.status_code, resp.text[:200])
        return {
            "alpha_id": alpha_id,
            "status_code": resp.status_code,
            "message": resp.text[:300],
        }


# 兼容别名：BrainClient 是当前 MVP 推荐入口
BrainClient = APIClient