# PROJECT_ARCHITECTURE.md

> **用途：** 供 AI 助手快速上下文恢复。读完本文件即可掌握项目全貌、规范和核心决策，无需翻阅源码。

---

## 目录

1. [项目概述](#1-项目概述)
2. [技术栈](#2-技术栈)
3. [目录结构](#3-目录结构)
4. [架构总览](#4-架构总览)
5. [核心模块说明](#5-核心模块说明)
6. [路由与页面清单](#6-路由与页面清单)
7. [数据存储机制](#7-数据存储机制)
8. [认证与安全](#8-认证与安全)
9. [开发规范](#9-开发规范)
10. [如何新增页面](#10-如何新增页面)
11. [如何调用 OCI API](#11-如何调用-oci-api)
12. [前端设计系统](#12-前端设计系统)
13. [已知问题与改进方向](#13-已知问题与改进方向)
14. [环境变量与部署](#14-环境变量与部署)

---

## 1. 项目概述

**OCI Manager** 是一个面向个人/团队运维的 **Oracle Cloud Infrastructure（OCI）多租户 Web 控制台**，用 Python/Flask 构建，无前端构建工具，纯服务端渲染（SSR）。

核心功能：

| 功能 | 描述 |
|------|------|
| 多租户管理 | 添加/删除多个 OCI 账户（租户），每个租户独立 API 凭据 |
| 实例管理 | 列出实例、启动/停止/重启、换公网 IP |
| 抢机（Launcher） | 后台线程轮询，自动重试创建 Always-Free 实例 |
| 救机（Rescue） | 串口控制台连接、引导卷扩容 |
| 对象存储 | Bucket 管理、文件上传/下载/预览 |
| 自治数据库 | 创建/启停/删除 ADB、下载 Wallet |
| 邮件 | 发信域名管理、DKIM 自动化、发件人授权、一键生成 SMTP 凭据、发送测试邮件 |
| 安全列表 | VCN 安全列表规则查看/添加/删除 |
| 用户管理 | 创建 IAM 用户、重置 MFA |

---

## 2. 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| **后端框架** | Flask 2.x | Blueprint 模块化路由 |
| **OCI SDK** | `oci` Python SDK | 所有 OCI API 调用均通过此 SDK |
| **模板引擎** | Jinja2（Flask 内置） | SSR，无前端框架 |
| **样式** | 单文件 CSS（`static/app.css`） | 自研 Design System，黑色工业风 |
| **字体** | IBM Plex Sans + IBM Plex Mono | 通过 Google Fonts CDN 引入 |
| **认证** | Flask session + werkzeug 密码哈希 | 单用户 admin，凭据存 `auth.yaml` |
| **数据持久化** | YAML 文件（`tenants.yaml`、`auth.yaml`） | 无数据库，轻量部署 |
| **异步任务** | Python `threading`（daemon 线程） | 仅用于抢机任务后台循环 |
| **超时保护** | `timeout_utils.py`（Queue + Thread） | 防止 OCI API 长时间阻塞页面 |
| **Python 版本** | 3.10+（使用 `X | Y` 类型注解） | |
| **密码哈希** | `werkzeug.security.generate_password_hash` | 默认 PBKDF2 |

---

## 3. 目录结构

```
oci-manager/                       ← 项目根目录
├── app.py                         ← Flask 应用工厂，注册蓝图，安全响应头
├── settings.py                    ← 全局配置：路径、区域列表、抢机预设、超时、密钥
├── rendering.py                   ← render_page() 统一渲染入口，页面→模板映射
├── storage.py                     ← YAML 读写：租户配置、认证配置、工具函数
├── oci_helpers.py                 ← OCI 客户端工厂、config 构建、分页封装
├── timeout_utils.py               ← run_with_timeout() 防 API 阻塞
│
├── auth_routes.py                 ← Blueprint: /login /logout /account /account/password
├── tenant_routes.py               ← Blueprint: / /tenant/add /tenant/<n>/... (核心路由)
├── object_storage_routes.py       ← Blueprint: /tenant/<n>/object-storage/...
├── database_routes.py             ← Blueprint: /tenant/<n>/databases/...
├── email_routes.py                ← Blueprint: /tenant/<n>/email/...
│
├── tenant_services.py             ← 租户相关业务逻辑（实例、用户、安全列表等）
├── object_storage_service.py      ← 对象存储业务逻辑
├── database_service.py            ← 自治数据库业务逻辑
├── email_service.py               ← 邮件业务逻辑
├── launch_manager.py              ← 抢机任务调度、LAUNCH_TASKS 全局字典、后台线程
│
├── check_vnic.py                  ← VNIC 相关工具（辅助）
├── cn.py                          ← 中文输出工具（辅助）
│
├── static/
│   └── app.css                    ← 唯一样式文件，完整 Design System
│
├── templates/
│   ├── base.html                  ← 基础布局：hero、topbar、nav、flash
│   ├── login.html                 ← 登录页（不继承 base body block）
│   ├── account.html               ← 账户/改密页
│   ├── tenants.html               ← 首页：租户卡片列表 + 统计指标
│   ├── add_tenant.html            ← 添加租户表单
│   ├── instances.html             ← 实例列表 + 操作按钮
│   ├── change_ip.html             ← 换公网 IP 确认页
│   ├── rescue.html                ← 救机：串口控制台 + 引导卷扩容
│   ├── launcher.html              ← 抢机：配置表单 + 任务队列
│   ├── users.html                 ← IAM 用户列表 + 创建表单
│   ├── object_storage.html        ← Bucket 列表 + 对象列表 + 上传
│   ├── object_preview.html        ← 对象文件预览
│   ├── databases.html             ← 自治数据库列表 + 创建 + Wallet 下载
│   ├── email.html                 ← 发件人管理 + 发送测试邮件
│   ├── security_lists.html        ← VCN 安全列表列表
│   └── security_rules.html        ← 单个安全列表的规则详情
│
├── tenants/                       ← 运行时生成，每个租户一个子目录
│   └── <tenant_name>/
│       └── key.pem                ← 该租户 OCI API 私钥（600 权限）
│
├── tenants.yaml                   ← 运行时生成，租户配置（OCID、区域、指纹、密钥路径）
├── auth.yaml                      ← 运行时生成，admin 账户（用户名 + 密码哈希）
├── .secret_key                    ← 运行时生成，Flask session 密钥（600 权限）
├── requirements.txt               ← Python 依赖
└── PROJECT_ARCHITECTURE.md        ← 本文件
```

---

## 4. 架构总览

```
浏览器
  │  HTTP Request
  ▼
Flask App (app.py)
  ├── after_request → 注入安全响应头 (CSP / X-Frame-Options / ...)
  │
  ├── auth_bp        ← 登录/登出/账户
  ├── tenant_bp      ← 租户、实例、抢机、救机、安全列表、用户
  ├── object_storage_bp ← 对象存储
  ├── database_bp    ← 自治数据库
  └── email_bp       ← 邮件
         │
         ▼
    *_service.py  (业务逻辑)
         │
         ▼
    oci_helpers.py  (OCI 客户端工厂)
         │
         ▼
    oci Python SDK  →  OCI Cloud API

数据存储
  ├── tenants.yaml  (租户配置，仅元数据)
  ├── auth.yaml     (用户名 + pbkdf2 哈希)
  └── tenants/<name>/key.pem  (API 私钥)
```

**请求→响应完整流程：**

1. 浏览器发起请求
2. `before_app_request` 检查 session，未登录跳 `/login`
3. 对应 Blueprint 路由函数接收请求
4. 调用 `run_with_timeout(N, service_func, tenant_cfg, ...)` 包裹 OCI 调用
5. service 层通过 `oci_helpers.get_*_client(tenant_cfg)` 获取 SDK 客户端
6. 结果返回路由层，调用 `render_page(page_name, **context)` 渲染模板
7. `after_request` 追加安全响应头

---

## 5. 核心模块说明

### `app.py` — 应用工厂

- `create_app()` 创建 Flask 实例，注册所有蓝图
- `after_request` 中统一注入 HTTP 安全头（CSP、X-Frame-Options 等）
- `format_bytes` 注册为 Jinja2 过滤器
- 启动时从环境变量或 `.secret_key` 文件读取 session 密钥
- Debug 模式通过 `OCI_MANAGER_DEBUG=1` 环境变量控制，**不再硬编码 `debug=True`**

### `settings.py` — 全局配置

- `BASE_DIR`、`CONFIG_PATH`（tenants.yaml）、`AUTH_PATH`（auth.yaml）、`TENANT_DIR`（tenants/）
- `DEFAULT_REGIONS`：所有 OCI 商业区域列表（value, label 元组）
- `LAUNCH_PRESETS`：抢机预设（AMD 1C1G、ARM 2C12G、ARM 4C24G）
- `RETRYABLE_KEYWORDS`：抢机可重试错误关键字列表
- `OCI_CONNECT_TIMEOUT = 5`、`OCI_READ_TIMEOUT = 15`（秒）
- `ALLOWED_KEY_EXTENSIONS = {".pem", ".key"}`、`MAX_KEY_SIZE_BYTES = 8192`
- `secret_key()`：优先读 `OCI_MANAGER_SECRET_KEY` 环境变量，否则自动生成并持久化到 `.secret_key`

### `rendering.py` — 渲染入口

```python
def render_page(page: str, **context) -> Response:
    """
    统一渲染函数。自动注入：
      - page: 当前页面名（用于 nav active 状态）
      - current_user: 当前登录用户名（来自 session）
      - launch_presets: 抢机预设（全局可用）
    """
```

`PAGE_TEMPLATES` 字典维护 page name → html 文件名的映射。

### `storage.py` — 数据读写

| 函数 | 说明 |
|------|------|
| `load_tenants()` | 从 tenants.yaml 读取所有租户配置 |
| `save_tenants(tenants)` | 写回 tenants.yaml（按名字排序） |
| `load_auth_settings()` | 读取 auth.yaml |
| `save_auth_settings(data)` | 写回 auth.yaml |
| `ensure_auth_settings()` | 首次启动时初始化 admin 账号 |
| `normalize_tenant_name(name)` | 租户名清洗：仅保留 `[a-zA-Z0-9._-]` |
| `fmt_dt(value)` | 日期格式化为 `YYYY-MM-DD HH:MM` |
| `now_iso()` | 返回当前时间 ISO 字符串 |

### `oci_helpers.py` — OCI 客户端工厂

```python
# 获取各服务客户端
get_identity_client(tenant_cfg)   → oci.identity.IdentityClient
get_compute_client(tenant_cfg)    → oci.core.ComputeClient
get_network_client(tenant_cfg)    → oci.core.VirtualNetworkClient
get_block_client(tenant_cfg)      → oci.core.BlockstorageClient

# 分页封装（自动翻页）
list_all(func, *args, **kwargs)   → List[Any]

# 根据 tenant_name 查找完整配置
find_tenant_config(tenant_name)   → dict | None
```

`build_config(tenant_cfg)` 从租户字典构建 OCI SDK config，并验证（`oci.config.validate_config`）。

### `timeout_utils.py` — 超时保护

```python
result = run_with_timeout(8, some_oci_function, arg1, arg2)
# 超过 8 秒抛出 TimedCallError
```

通过 daemon 线程 + Queue 实现，防止 OCI API 网络慢导致 Flask 工作线程长时间占用。

### `launch_manager.py` — 抢机任务

- `LAUNCH_TASKS: dict[str, dict]`：全局内存任务字典（进程重启后清空）
- `TASK_LOCK: threading.Lock`：所有读写操作必须持锁
- `launch_worker(task_id, tenant_cfg, form)`：在 daemon 线程中执行，循环重试 CreateInstance
- `filtered_tasks(tenant_name)`：返回当前租户最近 12 条任务（线程安全快照）
- `task_snapshot(task_id)`：线程安全读取单条任务
- `update_task(task_id, **kwargs)`：线程安全更新任务字段
- `append_task_log(task_id, message)`：向任务追加带时间戳的日志行

---

## 6. 路由与页面清单

### auth_bp（`auth_routes.py`）

| Method | URL | 说明 |
|--------|-----|------|
| GET/POST | `/login` | 登录页 |
| POST | `/logout` | 退出（清除 session） |
| GET | `/account` | 账户设置页 |
| POST | `/account/password` | 修改密码 |

### tenant_bp（`tenant_routes.py`）

| Method | URL | 说明 |
|--------|-----|------|
| GET | `/` | 首页：租户总览 |
| GET/POST | `/tenant/add` | 添加租户 |
| POST | `/tenant/<n>/delete` | 删除租户 |
| GET | `/tenant/<n>/users` | 用户列表 |
| POST | `/tenant/<n>/user/create` | 创建 IAM 用户 |
| POST | `/tenant/<n>/user/reset_mfa/<user_id>` | 重置 MFA |
| GET | `/tenant/<n>/instances` | 实例列表 |
| POST | `/tenant/<n>/instance/action/<id>/<action>` | 实例操作（start/stop/reset等） |
| GET/POST | `/tenant/<n>/instance/<id>/change_ip` | 换公网 IP |
| GET | `/tenant/<n>/launcher` | 抢机页面 |
| POST | `/tenant/<n>/launcher/start` | 启动抢机任务 |
| POST | `/tenant/<n>/launcher/task/<task_id>/cancel` | 取消抢机任务 |
| GET | `/tenant/<n>/instance/<id>/rescue` | 救机页面 |
| POST | `/tenant/<n>/instance/<id>/rescue/console` | 创建串口控制台连接 |
| POST | `/tenant/<n>/instance/<id>/rescue/boot-volume` | 引导卷扩容 |
| GET | `/tenant/<n>/security-lists` | 安全列表 |
| GET | `/tenant/<n>/security-list/<sl_id>/rules` | 安全规则详情 |
| POST | `/tenant/<n>/security-list/<sl_id>/add-rule` | 添加规则 |
| POST | `/tenant/<n>/security-list/<sl_id>/delete-rule/<type>/<idx>` | 删除规则 |

### object_storage_bp（`object_storage_routes.py`）

| Method | URL | 说明 |
|--------|-----|------|
| GET | `/tenant/<n>/object-storage` | Bucket 列表/对象列表 |
| POST | `/tenant/<n>/object-storage/bucket` | 创建 Bucket |
| POST | `/tenant/<n>/object-storage/object` | 上传对象 |
| POST | `/tenant/<n>/object-storage/object/delete` | 删除对象 |
| GET | `/tenant/<n>/object-storage/object/download` | 下载对象 |
| GET | `/tenant/<n>/object-storage/object/preview` | 预览对象 |

### database_bp（`database_routes.py`）

| Method | URL | 说明 |
|--------|-----|------|
| GET | `/tenant/<n>/databases` | ADB 列表 |
| POST | `/tenant/<n>/databases/create` | 创建 ADB |
| POST | `/tenant/<n>/databases/<db_id>/wallet` | 下载 Wallet |
| POST | `/tenant/<n>/databases/<db_id>/start` | 启动 ADB |
| POST | `/tenant/<n>/databases/<db_id>/stop` | 停止 ADB |
| POST | `/tenant/<n>/databases/<db_id>/delete` | 删除 ADB |

### email_bp（`email_routes.py`）

| Method | URL | 说明 |
|--------|-----|------|
| GET | `/tenant/<n>/email` | 邮件首页 |
| POST | `/tenant/<n>/email/domains` | 创建发信域名并且自动提交获取 DKIM |
| POST | `/tenant/<n>/email/domains/<id>/delete` | 删除发信域名 |
| POST | `/tenant/<n>/email/domains/<id>/dkim` | 手动触发生成 DKIM |
| POST | `/tenant/<n>/email/smtp-credential` | 为当前 API 账号一键生成 SMTP 密码 |
| POST | `/tenant/<n>/email/senders` | 创建发件人 |
| POST | `/tenant/<n>/email/senders/<id>/delete` | 删除发件人 |
| POST | `/tenant/<n>/email/send-test` | 发送测试邮件 |

---

## 7. 数据存储机制

项目**无数据库**，使用 YAML 文件持久化：

### `tenants.yaml` 结构

```yaml
my-tenant:
  tenant_id: ocid1.tenancy.oc1..aaaa...
  user_id: ocid1.user.oc1..aaaa...
  region: ap-tokyo-1
  fingerprint: "xx:xx:xx:xx:..."
  key_path: /path/to/tenants/my-tenant/key.pem
  created: "2025-01-01T12:00:00"
```

### `auth.yaml` 结构

```yaml
username: admin
password_hash: pbkdf2:sha256:...
created: "2025-01-01T12:00:00"
updated: "2025-06-01T10:00:00"   # 改密后追加
```

### 私钥文件

`tenants/<tenant_name>/key.pem` — 每个租户独立目录，删除租户时整个目录被 `shutil.rmtree` 清除。

---

## 8. 认证与安全

### 认证机制

- 单用户 admin，凭据存于 `auth.yaml`（密码 PBKDF2 哈希）
- `before_app_request` 钩子：每次请求检查 `session["authenticated"]`，未登录重定向 `/login`
- 白名单：`auth.login`、`static` 端点免登录检查

### 已修复的安全漏洞

| 漏洞 | 修复方式 |
|------|---------|
| 开放重定向（Open Redirect） | `_safe_next()` 函数校验 next 参数只允许相对路径 |
| `debug=True` 硬编码 | 改为 `OCI_MANAGER_DEBUG` 环境变量控制 |
| 随机 secret_key | 自动生成并持久化到 `.secret_key`（chmod 600） |
| 文件上传无校验 | `_validate_key_file()`：校验扩展名、大小（≤8KB）、PEM 格式 |
| 密码修改无二次确认 | 新增 `confirm_password` 字段校验 |
| 引导卷扩容无范围检查 | 限制 50GB ≤ size ≤ 32768GB |
| 无 HTTP 安全头 | `after_request` 注入 CSP、X-Frame-Options、X-Content-Type-Options、Referrer-Policy |

### 生产部署安全建议

```bash
# 必须设置强随机密钥
export OCI_MANAGER_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

# 使用强密码覆盖默认账号
export OCI_MANAGER_USERNAME=your_username
export OCI_MANAGER_PASSWORD=your_strong_password

# 不要暴露在公网，建议反代 + TLS + IP 白名单
```

---

## 9. 开发规范

### Python 代码风格

```python
from __future__ import annotations   # 每个文件顶部必须有
# 类型注解使用 X | Y 语法（需 Python 3.10+）
def example(value: str | None) -> dict[str, Any]:
    ...
```

- 所有 OCI 调用必须用 `run_with_timeout(秒, func, *args)` 包裹
- 业务逻辑放在 `*_service.py`，路由层只做参数解析、调用 service、flash 消息、redirect
- 错误处理：`except Exception as exc: flash(f"操作失败：{exc}", "error")`
- 不使用全局数据库连接，每次请求重新调用 `load_tenants()` / `get_*_client()`

### 模板规范

- 所有页面继承 `base.html`（`{% extends "base.html" %}`）
- 登录页覆盖 `{% block body %}`（不使用 shell/topbar）
- 其他页面使用 `{% block content %}`
- 传递给模板的变量通过 `render_page("page_name", **context)` 注入
- 不要在模板中写业务逻辑，只做简单的条件/循环渲染

### 表单提交规范

- 写操作（POST）必须使用 `<form method="post">`，不用 GET 修改状态
- 危险操作（删除）使用 `onsubmit="return confirm('...')"` 前端二次确认
- 表单 action 使用 `url_for()`，不要硬编码 URL 字符串

---

## 10. 如何新增页面

以新增"快照管理"功能为例，完整步骤：

### Step 1：创建 Service 函数

在 `tenant_services.py`（或新建 `snapshot_service.py`）中写业务逻辑：

```python
# snapshot_service.py
from oci_helpers import get_compute_client, list_all

def list_snapshots(tenant_cfg: dict) -> list[dict]:
    client = get_compute_client(tenant_cfg)
    raw = list_all(client.list_boot_volume_backups,
                   compartment_id=tenant_cfg["tenant_id"])
    return [{"id": s.id, "name": s.display_name, ...} for s in raw]
```

### Step 2：创建路由（新蓝图或追加到现有蓝图）

```python
# snapshot_routes.py
from flask import Blueprint, flash, redirect, url_for
from rendering import render_page
from tenant_routes import require_tenant
from timeout_utils import run_with_timeout
from snapshot_service import list_snapshots

snapshot_bp = Blueprint("snapshot", __name__)

@snapshot_bp.route("/tenant/<tenant_name>/snapshots")
def snapshots_home(tenant_name: str):
    tenant_cfg = require_tenant(tenant_name)
    if tenant_cfg is None:
        return redirect(url_for("tenant.index"))
    try:
        items = run_with_timeout(8, list_snapshots, tenant_cfg)
        return render_page("snapshots", tenant_name=tenant_name, items=items)
    except Exception as exc:
        flash(f"读取快照失败：{exc}", "error")
        return redirect(url_for("tenant.index"))
```

### Step 3：注册蓝图（`app.py`）

```python
from snapshot_routes import snapshot_bp
# 在 create_app() 中：
app.register_blueprint(snapshot_bp)
```

### Step 4：注册模板映射（`rendering.py`）

```python
PAGE_TEMPLATES = {
    ...
    "snapshots": "snapshots.html",   # 新增这一行
}
```

### Step 5：创建模板（`templates/snapshots.html`）

```html
{% extends "base.html" %}
{% block title %}快照 | OCI Manager{% endblock %}
{% block hero_title %}快照管理{% endblock %}

{% block content %}
<section class="section-head">
  <div>
    <div class="eyebrow">Boot Volume Backups</div>
    <h2>快照列表</h2>
    <div class="section-sub">租户：{{ tenant_name }}</div>
  </div>
</section>

<section class="table-card">
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>名称</th><th>ID</th><th>状态</th></tr>
      </thead>
      <tbody>
        {% for item in items %}
        <tr>
          <td>{{ item.name }}</td>
          <td><span class="code small">{{ item.id[:20] }}…</span></td>
          <td><span class="badge badge-good">{{ item.state }}</span></td>
        </tr>
        {% else %}
        <tr><td colspan="3"><div class="empty">暂无快照。</div></td></tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</section>
{% endblock %}
```

### Step 6：在导航栏添加链接（`base.html`）

```html
{% if tenant_name %}
...
<a class="nav-pill {{ 'active' if page == 'snapshots' else '' }}"
   href="/tenant/{{ tenant_name }}/snapshots">快照</a>
{% endif %}
```

---

## 11. 如何调用 OCI API

### 基本模式

```python
from oci_helpers import get_compute_client, get_network_client, list_all
from timeout_utils import run_with_timeout

def my_oci_operation(tenant_cfg: dict) -> list[dict]:
    # 1. 获取客户端（每次调用都新建，无连接池）
    compute = get_compute_client(tenant_cfg)
    network = get_network_client(tenant_cfg)

    # 2. 列举资源（自动分页）
    instances = list_all(
        compute.list_instances,
        compartment_id=tenant_cfg["tenant_id"]
    )

    # 3. 单条操作（直接调用，不用 list_all）
    compute.instance_action(instance_id, "STOP")

    return [{"id": i.id, ...} for i in instances]

# 在路由中调用（带超时保护）
result = run_with_timeout(8, my_oci_operation, tenant_cfg)
```

### 可用客户端工厂

```python
get_identity_client(tenant_cfg)   # IAM: 用户、策略、可用域
get_compute_client(tenant_cfg)    # 计算: 实例、镜像、控制台
get_network_client(tenant_cfg)    # 网络: VCN、子网、安全列表、IP
get_block_client(tenant_cfg)      # 块存储: 引导卷、备份
```

### 租户配置字典结构

```python
tenant_cfg = {
    "tenant_id":   "ocid1.tenancy...",   # 同 compartment_id（根区）
    "user_id":     "ocid1.user...",
    "region":      "ap-tokyo-1",
    "fingerprint": "xx:xx:...",
    "key_path":    "/abs/path/to/key.pem",
    "_tenant_name": "my-tenant",         # 由 find_tenant_config() 注入，内部使用
}
```

---

## 12. 前端设计系统

### 设计语言

**黑色工业指挥中心风格**，主色调深色（`#0d0f14`），品牌色蓝（`#4f8aff`），点缀色青绿（`#00d4a8`）。

### CSS 变量速查

```css
/* 背景层级 */
--bg           #0d0f14    /* 最深背景 */
--bg-2         #111520    /* 次级背景（表单输入框） */
--surface      #1a2030    /* 卡片/面板 */
--surface-2    #1f2740    /* ghost 按钮、代码块 */
--border       rgba(99,140,255,0.12)   /* 边框 */
--border-hover rgba(99,140,255,0.28)   /* hover 边框 */

/* 文字 */
--text         #e8edf8    /* 主文字 */
--text-2       #8a95b0    /* 次级/描述文字 */
--text-3       #5a637a    /* 表头/label */

/* 品牌色 */
--brand        #4f8aff    /* 主品牌蓝 */
--brand-dim    rgba(79,138,255,0.18)   /* 蓝色浅底 */
--accent       #00d4a8    /* 点缀青绿 */

/* 状态色 */
--good         #2ecc8a    /* 成功/运行中 */
--warn         #f5a623    /* 警告 */
--danger       #ff4d6a    /* 错误/危险 */
```

### 常用组件类名

```html
<!-- 布局 -->
<div class="shell">          <!-- 最大宽度容器 -->
<div class="grid grid-2">   <!-- 两列网格 -->
<div class="stack">         <!-- 垂直堆叠（gap:16px） -->

<!-- 内容容器 -->
<section class="panel">      <!-- 通用面板（带padding） -->
<section class="table-card"> <!-- 表格容器 -->
<article class="tenant-card"><!-- 租户卡片 -->

<!-- 数据展示 -->
<div class="metrics"> + <div class="metric"> <!-- 指标卡 -->
<span class="badge badge-good">RUNNING</span>  <!-- 状态徽章 -->
<!-- badge 变体：badge-good / badge-mid / badge-bad / badge-warn / badge-accent -->

<!-- 按钮 -->
<button class="btn btn-primary">主要操作</button>
<button class="btn btn-secondary">次要操作</button>
<button class="btn btn-ghost">普通操作</button>
<button class="btn btn-danger">危险操作</button>
<button class="btn btn-sm">小按钮</button>

<!-- 表单 -->
<div class="field">
  <label>字段名</label>
  <input type="text" name="...">
</div>

<!-- 空状态 -->
<div class="empty">暂无数据。</div>

<!-- 代码 -->
<code>内联代码</code>
<pre>多行代码块</pre>
```

### 响应式断点

| 断点 | 变化 |
|------|------|
| `≤ 1024px` | `grid-3 / grid-4` → 2列 |
| `≤ 768px` | 所有多列 → 1列；topbar 非sticky |
| `≤ 480px` | metrics → 1列；section-head 竖向 |

---

## 13. 已知问题与改进方向

### 已修复（本次更新）

- ✅ 开放重定向漏洞（login next 参数）
- ✅ `debug=True` 硬编码
- ✅ Flask secret_key 不持久化问题
- ✅ 文件上传无类型/大小/内容校验
- ✅ 修改密码无二次确认
- ✅ 引导卷扩容无范围校验
- ✅ 缺少 HTTP 安全响应头
- ✅ UI 视觉重设计（深色工业风）

### 待改进

- ⚠️ **抢机任务内存存储**：进程重启后任务丢失，可改为 SQLite 持久化
- ⚠️ **无 CSRF 保护**：POST 表单缺少 CSRF Token（建议接入 `flask-wtf`）
- ⚠️ **单用户系统**：无多用户/角色权限支持
- ⚠️ **无速率限制**：登录接口无限制，可能被暴力破解（建议 `flask-limiter`）
- ⚠️ **日志**：无结构化日志，生产环境建议接入 `logging` 模块
- ⚠️ **文件锁**：`tenants.yaml` 并发写入无锁保护（低并发场景可接受）

---

## 14. 环境变量与部署

### 支持的环境变量

| 变量名 | 默认值 | 说明 |
|--------|--------|------|
| `OCI_MANAGER_SECRET_KEY` | 自动生成 | Flask session 密钥，生产环境必须显式设置 |
| `OCI_MANAGER_USERNAME` | `admin` | 初次启动时创建的管理员用户名 |
| `OCI_MANAGER_PASSWORD` | `admin123456` | 初次启动时创建的管理员密码（**必须修改**） |
| `OCI_MANAGER_DEBUG` | `""` | 设为 `1` / `true` 开启 Flask debug 模式 |

### 启动方式

```bash
# 开发环境
cd oci-manager/
pip install -r requirements.txt
python app.py

# 生产环境（推荐 gunicorn）
pip install gunicorn
export OCI_MANAGER_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export OCI_MANAGER_USERNAME=youruser
export OCI_MANAGER_PASSWORD=your_strong_password
gunicorn -w 2 -b 0.0.0.0:5080 app:app

# Nginx 反代配置片段
location / {
    proxy_pass http://127.0.0.1:5080;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### 首次运行流程

1. 启动时 `ensure_auth_settings()` 检测 `auth.yaml` 是否存在
2. 不存在则读环境变量创建默认 admin 账号
3. `secret_key()` 检测 `.secret_key` 文件，不存在则生成并写入（chmod 600）
4. `TENANT_DIR`（`tenants/`）目录自动创建

---

*文档版本：2025年更新 | 对应代码分支：main*
