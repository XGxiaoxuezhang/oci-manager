# OCI Manager

![License](https://img.shields.io/badge/license-MIT-blue.svg)
![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)
![Framework](https://img.shields.io/badge/framework-Flask-lightgrey)

# 注意，抢机，救机之类得功能未测试，谨慎使用，重点是数据库、邮件、对象存储的简单使用，本程序全程由ai开发，自行进行审计。

**OCI Manager** 是一个专为个人和团队运维打造的 **Oracle Cloud Infrastructure (OCI) 多租户 Web 控制台**。
它采用极其轻量级的后端架构（Python/Flask），不需要繁重的数据库，原生支持纯服务端渲染（SSR）。配合精心设计的**暗黑工业指挥中心**风格 UI，让你抛开操作繁琐的官方控制台，聚合所有的核心 OCI 运维能力。

> **亮点**：无前端框架、无数据库依赖、支持自动生成高频操作命令、自动轮询化繁为简。

---

## ✨ 核心特性

| 功能模块 | 特性说明 |
| :--- | :--- |
| **🏢 多租户管理** | 一键添加、切换、删除多个 OCI 账户（租户），各租户之间环境、凭据与计费完全隔离。 |
| **💻 实例与计算** | 可视化实例列表与状态查看；一键启停/重启机器；便捷更换实例绑定的公网 IP。 |
| **🚀 抢机辅助 (Launcher)** | 解决热门区域始终 Free 实例配额不足的问题。自动化创建任务，采用后台守护线程循环重试（支持 ARM 4C24G 等预设）。 |
| **🚑 机器救援** | 一键开通并生成控制台（VNC/串口）连接信息；支持快捷执行引导卷容量扩容。 |
| **📦 对象存储** | 完善的 Bucket 管理，支持拖拽化的对象文件上传、下载与无缝的浏览器在线图片/视频预览。 |
| **🗄️ 自治数据库** | 支持新建、启停和销毁 ADB，并且支持直接从控制台一键下载 Wallet 文件，开箱即用。 |
| **📧 邮件发送控制** | 直连 OCI Email Delivery Data Plane！**不仅能收发测试邮件、管理发件人，还支持一键生成 DKIM 记录并直接在界面签发应用层专属的 SMTP 发信账号密码**。 |
| **🔒 网络与安全** | 便捷查看 VCN 子网详情与管理安全列表（Security Lists）的入站/出站规则放行。 |

---

## 🎨 界面一览 (UI Design)

项目内建自研的 Design System (`static/app.css`)：
- **暗金与黑体**，主色调深色（`#0d0f14`），点缀品牌青绿与操作蓝，带来纯正的 DevTools 体验。
- 完善的表单和卡片网格响应式设计。支持大屏指挥台或小屏移动端查阅。
- 安全优先：内建严格的 CSP（内容安全策略）防护和请求过滤。

---

## 🛠️ 安装与部署

本项目设计之初就考虑了极简的运维环境，部署不需要 Redis，也不需要 MySQL，即装即用：

### 1. 基础环境
确保服务器已安装 `Python 3.10+`

### 2. 克隆项目与安装依赖
```bash
git clone https://github.com/your-username/oci-manager.git](https://github.com/XGxiaoxuezhang/oci-manager.git
cd oci-manager
pip install -r requirements.txt
```

### 3. 配置（生产环境必须）
我们提供了一些基础环境变量用于增强安全性与调试：

| 环境变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `OCI_MANAGER_SECRET_KEY` | *(自动生成)* | 极其关键的 Flask Session 独立秘钥，生产环境**务必手工设定并妥善保管**。 |
| `OCI_MANAGER_USERNAME` | `admin` | 初次启动默认生成的系统唯一管理账号名。 |
| `OCI_MANAGER_PASSWORD` | `admin123456` | 初次启动默认生成的初始管理密码（**部署后务必马上修改**）。 |
| `OCI_MANAGER_DEBUG` | `""` | 设为 `1` 或 `true` 时，开启 Flask debug 热重载模式。 |

### 4. 运行服务

**开发测试环境：**
```bash
python app.py
# 默认将会监听 http://127.0.0.1:5000
```

**生产环境（推荐使用 Gunicorn 后台常驻）：**
```bash
# 生成随机高强度秘钥
export OCI_MANAGER_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
export OCI_MANAGER_PASSWORD=YourStrongPasswordHere!

pip install gunicorn
gunicorn -w 4 -b 127.0.0.1:5080 app:app
```
*(使用 Nginx 或 Caddy 将 `5080` 端口反代到公网域名，或者配置 Let's Encrypt 证书)*

---

## 🏗️ 架构相关

深入了解项目的路由组织、数据 YAML 落地形式及核心 OCI SDK 封装方案，请查阅 [PROJECT_ARCHITECTURE.md](./docs/PROJECT_ARCHITECTURE.md)。其中包括了增加页面的流程、组件规范和防超时阻塞的设计。

---

## ⚠️ 安全与免责声明

- 本系统会在运行目录下的 `tenants/` 文件夹中保存你上传的 OCI 私钥 (`key.pem`)：请**绝对不要**将运行时环境的文件夹直接通过 Git 提交或泄露！
- （已在本开源版中加入限制配置，防止密钥随源码共享）。
- 这是管理重要云资产的系统，生产环境**强烈建议套用 HTTPS 并使用复杂的密码**。

---

## 🤝 参与贡献

欢迎通过 Issues 提交反馈和 Bug。如果有想集成的其他 OCI 操作模块，请随时抛出 Pull Request！

*License: MIT*
