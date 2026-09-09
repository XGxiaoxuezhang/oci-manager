"""监控指标：OCI Monitoring API（oci_computeagent 命名空间，免费可用）。

一次 summarize 查整个 compartment 全部实例的 24h CPU 曲线（按 resourceId 维度分组），
再拉网络进出速率；用于实例列表行的 sparkline。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import oci

from oci_helpers import build_config, client_kwargs, compartment_of

_CPU_QUERY = "CpuUtilization[1h].mean()"
_MEM_QUERY = "MemoryUtilization[1h].mean()"
_NET_IN_QUERY = "NetworksBytesIn[5m].rate()"  # rate() 把 counter 转成 B/s；mean() 会返回累计字节数
_NET_OUT_QUERY = "NetworksBytesOut[5m].rate()"
_NAMESPACE = "oci_computeagent"


def _monitoring_client(tenant_cfg: dict[str, Any]) -> oci.monitoring.MonitoringClient:
    return oci.monitoring.MonitoringClient(build_config(tenant_cfg), **client_kwargs())


def _summarize(client: oci.monitoring.MonitoringClient, compartment_id: str, query: str, hours: int) -> dict[str, list[float]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    details = oci.monitoring.models.SummarizeMetricsDataDetails(
        namespace=_NAMESPACE,
        query=query,
        start_time=start,
        end_time=end,
        resolution="1h",
    )
    response = client.summarize_metrics_data(compartment_id, details)
    series: dict[str, list[float]] = {}
    for metric in response.data or []:
        resource_id = (metric.dimensions or {}).get("resourceId") or (metric.dimensions or {}).get("instanceId") or ""
        points = series.setdefault(resource_id, [])
        for point in metric.aggregated_datapoints or []:
            value = point.value
            try:
                points.append(round(float(value), 2) if value is not None else 0.0)
            except (TypeError, ValueError):
                points.append(0.0)
    return series


def monitoring_series(tenant_cfg: dict[str, Any], hours: int = 24) -> dict[str, dict[str, Any]]:
    """返回 {instance_id: {"cpu": [...], "net_in": x, "net_out": y}}。

    cpu 是按时序的利用率点列表；net_in/out 是最近一小时平均速率（字节/秒）。
    Monitoring 不可用（如实例从未开机、agent 未装）时返回空 dict，不影响页面。
    """
    try:
        client = _monitoring_client(tenant_cfg)
        compartment = compartment_of(tenant_cfg)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=4) as pool:
            cpu_future = pool.submit(_summarize, client, compartment, _CPU_QUERY, hours)
            mem_future = pool.submit(_summarize, client, compartment, _MEM_QUERY, hours)
            net_in_future = pool.submit(_summarize, client, compartment, _NET_IN_QUERY, 2)
            net_out_future = pool.submit(_summarize, client, compartment, _NET_OUT_QUERY, 2)
        cpu = cpu_future.result()
        mem = mem_future.result()
        net_in = net_in_future.result()
        net_out = net_out_future.result()
    except Exception:
        return {}

    result: dict[str, dict[str, Any]] = {}
    for instance_id, points in cpu.items():
        mem_points = mem.get(instance_id) or []
        result[instance_id] = {
            "cpu": points,
            "cpu_avg": round(sum(points) / len(points), 1) if points else None,
            "cpu_peak": max(points) if points else None,
            "mem_avg": round(sum(mem_points) / len(mem_points), 1) if mem_points else None,
            "net_in": (net_in.get(instance_id) or [None])[-1] if net_in.get(instance_id) else None,
            "net_out": (net_out.get(instance_id) or [None])[-1] if net_out.get(instance_id) else None,
        }
    return result


def attach_metrics_to_rows(rows: list[dict[str, Any]], tenant_cfg: dict[str, Any]) -> None:
    """把监控数据填进 list_instance_rows 的行里（原地修改）。

    cpu_points: [float]；mem_avg: 内存利用率均值；net_in_mb/net_out_mb: 最近一小时平均 MB/s。
    """
    series = monitoring_series(tenant_cfg)
    if not series:
        return
    for row in rows:
        item = series.get(row.get("id"))
        if not item:
            continue
        row["cpu_points"] = item["cpu"]
        row["cpu_avg"] = item["cpu_avg"]
        row["cpu_peak"] = item["cpu_peak"]
        row["mem_avg"] = item["mem_avg"]
        for source, target in (("net_in", "net_in_mb"), ("net_out", "net_out_mb")):
            value = item[source]
            row[target] = round(value / 1024 / 1024, 2) if value is not None else None
