# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目简介

一个用途单一的 Python 工具，用于绕过 Oracle Cloud Infrastructure（OCI）的「Out of Capacity」（容量不足）错误：它反复调用 OCI 的 `launch_instance` API，并采用自适应退避策略，直到成功开出一台虚拟机为止（通常是 ARM 免费套餐实例 `VM.Standard.A1.Flex`）。成功后会解析出公网 IP 并退出。本项目由 AI 驱动开发，由人工做最终审核。

## 常用命令

```bash
pip install -r requirements.txt      # 安装依赖（Python 3.8+）
python3 setup_wizard.py              # 交互式配置（推荐先执行这一步）
python3 setup_wizard.py --gui        # 同一向导，使用 tkinter 图形对话框
python3 bot.py                       # 运行重试循环
python3 bot.py --config <ini> --oci-config <file>   # 覆盖配置文件路径
tail -f oci_occ.log                  # 实时查看日志
docker compose up -d --build         # 容器化运行；用 `docker logs -f` 查看日志
```

本仓库**没有测试套件、Lint 配置或构建步骤**。`bot.py` 会一直循环，直到成功（`sys.exit(0)`）、发生致命错误或按下 Ctrl-C 才停止。

## 架构

只有两个源文件，没有包结构。

**`bot.py`** —— `OciOccFix` 类。构造函数按固定阶段顺序执行（见 `__init__`）：加载应用配置 → 配置带轮转的文件日志与流式日志 → 读取初始重试间隔 → 创建 OCI 客户端 → 初始化 Telegram → 重置运行时计数器。`run()` 是主循环：先执行一次资源校验，然后无限循环地轮询可用域（availability domain）列表，对每个可用域调用 `create_instance(ad)`。`create_instance` 会捕获 `oci.exceptions.ServiceError`；当错误码属于 `RETRYABLE_ERROR_CODES`（`TooManyRequests`、`OutOfHostCapacity`、`OutOfCapacity`）时触发 `adaptive_retry_wait`。自适应逻辑的要点是：遇到 `TooManyRequests` 时**增大**等待时间（乘以 `backoff_factor`），其他情况下**缩短**等待时间，并限制在 `[min_interval, max_interval]` 区间内 —— 即被限流时退让，仅是容量不足时加快重试。

**`setup_wizard.py`** —— 用于生成两个配置文件的交互式向导（CLI 或 tkinter）。其 INI 写入逻辑（`_update_ini_lines`）按行编辑文件，从而保留原有注释和结构，而不是用 configparser 整体重写。写入前会先把每个文件备份为 `.bak`。

### 两个独立的配置文件 —— 注意区分

1. **`configuration.ini`** —— 应用自身的设置，分隔符为 `" = "`。`bot.py` 读取的小节有 `[OCI] [Instance] [Telegram] [Machine] [Retry] [Logging]`。`load_config()` 硬性要求存在 `OCI、Instance、Telegram、Machine、Retry` 这些小节以及 `[Retry]` 下的四个键 —— 增删任何必需小节时都要同步更新这段校验逻辑。
2. **`config`** —— OCI SDK 凭据文件（由 `oci.config.from_file` 读取），分隔符为 `"="`，`[DEFAULT]` 小节包含 `user/fingerprint/tenancy/region/key_file`。`key_file` 指向下载下来的 PEM 私钥。

**仓库只跟踪模板文件 `configuration.ini.example` 和 `config.example`（含 `xxxx`、`exampleuniqueID` 占位值）。** 真实的 `configuration.ini` 和 `config` 已被 `.gitignore` 忽略（连同 `*.bak`、`*.pem`、`*.key`、`oci_private_key.pem`），不会进入版本库。使用前需先复制模板：`cp configuration.ini.example configuration.ini` 和 `cp config.example config`，再编辑真实文件或运行 `setup_wizard.py`（向导编辑的是真实文件名）。改动模板时记得让两者保持同步。

### 值得了解的约定

- **`xxxx` 是「禁用」哨兵值。** `boot_volume_id`、Telegram 的 `bot_token`/`uid` 默认都是 `xxxx`；代码将其视为「未设置」（Telegram 静默禁用，实例创建则从「引导卷来源」回退到「镜像来源」）。请沿用这一约定，不要改用空字符串。
- **Telegram 是可选的，且尽力而为。** 所有 Telegram 调用都被包裹起来，失败只会产生警告；机器人会原地编辑同一条状态消息（`tg_message_id`），并每 10 次尝试发送一次进度更新。
- **免费套餐配额防护位于 `validate_resources()`。** 它在进入循环前强制检查 200 GB 块存储总量上限、ARM 的 4 OCPU / 24 GB 上限，并拒绝重复的实例显示名。若 OCI 免费套餐规则变化，在这里更新这些限制。

### 已知的不一致

`configuration.ini` 中带有 `[Weixin]`（企业微信 webhook）和 `[Notify]`（`status_interval_minutes`）两个小节，但 **`bot.py` 目前并不读取它们** —— 这是尚未实现的通知功能的占位。`setup_wizard.py` 同样不会填充它们。如果要实现企业微信/通知支持，需要把它接入 `bot.py`，并在 `setup_wizard.py` 中补上对应小节；不要假设配置中已有的键就一定是生效的。
