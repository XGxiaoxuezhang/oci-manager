# OCI Manager

基于 Flask + OCI Python SDK 的本地 OCI 管理工具。

## 当前已支持

- 多租户凭据管理
- 本地登录鉴权、CSRF 防护和登录失败限速
- 修改登录密码
- 用户创建、密码生成、MFA 重置
- 实例列表、电源操作、更换公网 IP、终止实例、CSV 导出
- 实例创建任务：支持 AMD 1C1G、ARM 2C12G、ARM 4C24G 预设，并可后台循环重试创建
- 实例创建任务持久化：服务重启后保留历史任务，中断中的任务会标记为 interrupted
- 救机中心：支持串口控制台连接、软关机/强关机/软重启/强重启、引导卷扩容
- Web VNC：内置 noVNC 前端，配合 websockify 在浏览器中打开控制台 VNC
- 离线模式：页面资源本地加载，默认关闭外部通知；OCI 管理操作仍会连接 Oracle API
- 安全列表规则增删
- 操作日志：记录关键 POST 操作，默认写入 SQLite
- 系统状态：检查 Python、OCI SDK、数据目录、租户配置和私钥存在性
- 备份恢复：导出/恢复本地数据目录，SQLite 使用一致性快照
- 实例总览：跨租户查看实例状态、公网 IP 和 SSH 命令
- 巡检：手动检查每个租户的 OCI API 连通性
- 通知：支持通用 Webhook 和 ntfy，用于实例创建成功/失败提醒，设置页可发送测试通知
- 对象存储：Bucket 管理、对象上传/下载/预览/删除、新建目录、CSV 导出和 rclone 配置下载
- 自治数据库：创建、启动、停止、Wallet 下载、手动备份、删除、备份查看和 CSV 导出
- 邮件：域名和发件人列表、测试发信、CSV 导出
- 免费额度提示：列出常见 Always Free 规格和成本风险点

## 安装

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## Windows 启动

```bash
.\start.ps1
```

启动后访问：`http://127.0.0.1:5080`

常用参数：

```powershell
.\start.ps1 -Port 5080 -HostName 127.0.0.1 -DataDir .\data
```

如果需要局域网访问，把 `-HostName` 改成 `0.0.0.0`。公网或 NAS 反代环境不要开启 `-DebugMode`。

## NAS / Docker 启动

```bash
docker compose up -d --build
```

启动后访问：`http://NAS_IP:5080`

首次部署前建议先修改 `docker-compose.yml` 里的：

- `OCI_MANAGER_PASSWORD`
- `OCI_MANAGER_SECRET_KEY`

容器数据默认挂载到外层 `./data`，包括登录配置、租户配置、私钥目录、SQLite 数据库和巡检结果。升级代码或重建容器不会覆盖这部分数据。

## 目录结构

```text
.
├── README.md
├── requirements.txt
└── oci-manager/
    ├── app.py
    ├── templates/
    ├── static/
    └── tenants/
```

默认直接运行源码时，数据会放在 `oci-manager/` 目录下。使用 `start.ps1` 或 Docker 时，数据会放在外层 `data/` 目录。

`auth.yaml`、`tenants.yaml`、`app.db`、`audit.log.jsonl`、`launch_tasks.json` 和 `tenants/` 下的私钥文件都是本地数据，已经在 `.gitignore` 中忽略，不要提交到仓库。

## 首次登录

- 用户名：`admin`
- 密码：`admin123456`

建议首次登录后立刻在首页修改密码。

也可以通过环境变量覆盖默认登录信息：

- `OCI_MANAGER_USERNAME`
- `OCI_MANAGER_PASSWORD`
- `OCI_MANAGER_SECRET_KEY`
- `OCI_MANAGER_DATA_DIR`
- `OCI_MANAGER_HOST`
- `OCI_MANAGER_PORT`
- `OCI_MANAGER_DEBUG`
- `OCI_MANAGER_SESSION_HOURS`
- `OCI_MANAGER_SESSION_COOKIE_SECURE`
- `OCI_MANAGER_LOGIN_MAX_FAILURES`
- `OCI_MANAGER_LOGIN_WINDOW_SECONDS`
- `OCI_MANAGER_LOGIN_LOCK_SECONDS`
- `OCI_MANAGER_WEBHOOK_URL`
- `OCI_MANAGER_NTFY_SERVER`
- `OCI_MANAGER_NTFY_TOPIC`

生产或公网环境建议一定设置 `OCI_MANAGER_SECRET_KEY`，并关闭 Flask debug 模式。

## 常用页面

- `/`：首页
- `/tenants`：租户
- `/instances`：实例总览，可按租户筛选
- `/object-storage`：对象存储入口
- `/databases`：数据库入口
- `/email`：邮件入口
- `/tenant/<tenant>/instances`：实例列表和创建任务
- `/tenant/<tenant>/object-storage`：对象存储
- `/tenant/<tenant>/databases`：自治数据库
- `/tenant/<tenant>/email`：邮件
- `/audit`：操作日志
- `/system`：系统状态
- `/settings`：运行参数和通知测试
- `/backup`：备份恢复
- `/cost`：免费额度与风险提示
- `/checks`：租户 API 巡检

## 通知配置

默认启用离线模式，实例创建任务成功、失败或取消时不会发送外部通知。页面上的 CSS、JS 和 noVNC 文件都在 `oci-manager/static/` 内，不依赖 CDN。

如果需要通知，到 `/settings` 关闭离线模式后再配置 Webhook 或 ntfy。

通用 Webhook：

```bash
OCI_MANAGER_WEBHOOK_URL=https://example.com/webhook
```

ntfy：

```bash
OCI_MANAGER_NTFY_SERVER=https://ntfy.sh
OCI_MANAGER_NTFY_TOPIC=your-private-topic
```

也可以把 `OCI_MANAGER_NTFY_SERVER` 指到自建 ntfy 服务。离线模式打开时，即使这些值存在也不会发送。

## 实例创建任务

创建任务需要：

- 可用子网
- 可用镜像
- 可登录实例的 SSH 公钥

任务使用进程内后台线程，状态写入本地数据目录。

## 救机说明

当前“救机”包含：

- 串口控制台连接创建
- 实例电源操作
- 引导卷扩容
