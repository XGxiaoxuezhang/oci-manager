from __future__ import annotations

import queue
import threading
from typing import Any, Callable


class TimedCallError(TimeoutError):
    pass


def run_with_timeout(timeout_seconds: int, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """给同步调用加一个"放弃等待"的上限。

    **重要语义**：超时只是不再等待结果，底层线程**不会被终止**，OCI 调用仍会在
    后台继续跑，最终由 SDK 自身的 connect/read timeout 兜底退出。因此：

    - 只用于**只读 / 幂等**操作（拉列表、构建 context、下载、预览等）。
    - **严禁**用于非幂等的写操作（发邮件、创建/删除资源等）——否则会出现
      "用户看到超时失败、实际操作却已生效，重试造成副作用"的情况。

    Python 线程无法强制终止；要做到真·取消只能换 multiprocessing 进程，
    但 OCI client 不能跨进程复用，代价远大于收益，故这里维持线程方案。
    """
    result_queue: "queue.Queue[tuple[str, Any]]" = queue.Queue()

    def runner() -> None:
        try:
            result_queue.put(("ok", func(*args, **kwargs)))
        except Exception as exc:  # pragma: no cover
            result_queue.put(("err", exc))

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        raise TimedCallError(f"请求 OCI 超时（>{timeout_seconds} 秒）。")
    status, payload = result_queue.get()
    if status == "err":
        raise payload
    return payload
