from __future__ import annotations

import queue
import threading
from typing import Any, Callable


class TimedCallError(TimeoutError):
    pass


def run_with_timeout(timeout_seconds: int, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
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
