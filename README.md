# DingDingcheckin

ESP32-C3 舵机定时打卡项目。默认每天北京时间 08:10–08:40 自动执行一次，04:00 自动重启一次，Wi-Fi 一直在线。网页可控制舵机位置、完整打卡、动作测试、执行时间、动作序列和重启。服务器保存 SQLite 记录；设备缓存计划，服务器断网时仍可自动执行。

> “动作完成”只表示舵机动作序列执行完毕。ESP32 没有读取钉钉考勤结果，请在钉钉中核实最终记录。

## 服务端

服务默认监听所有网络接口的 `8888` 端口，网页不需要密码。设备 API 仍使用 Token 鉴权。

```bash
cd server
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(24))"
```

把输出填入 `.env` 的 `DEVICE_TOKEN`，然后启动：

```bash
docker compose up -d
```

浏览器打开 `http://服务器IP:8888`。查看运行状态：

```bash
docker compose ps
docker compose logs -f
```

Compose 会拉取公开镜像 `ghcr.io/miubu/dingdingcheckin:latest`，SQLite 数据保存在 Docker 卷 `checkin-data` 中。

由于网页没有登录保护，任何能访问 8888 端口的人都能查看状态并点击手动打卡。是否允许公网访问应由服务器防火墙或反向代理决定。

## ESP32

本项目使用 PlatformIO、ESP32-C3、GPIO 4 和 `ESP32Servo`。复制配置模板：

```bash
cp include/config.example.h include/config.h
```

修改 `include/config.h`：

- 填写 Wi-Fi 名称和密码。
- `SERVER_URL` 改成服务器局域网地址，例如 `http://192.168.1.100:8888`。
- `DEVICE_TOKEN` 必须与服务端 `.env` 完全相同。
- `CHECKIN_START`、`CHECKIN_END` 和舵机常量只作首次离线启动的默认配置；连接新版服务器后以网页设置为准。
- 不必改 GPIO、原来的脉宽和等待参数。本版保留了原来的两轮动作，调试时再按实际机械位置调整。

编译和上传：

```bash
pio run
pio run --target upload
pio device monitor
```

设备保持 Wi-Fi 在线，每 5 秒轮询；Wi-Fi 断开时每 30 秒重连。通过 NTP 校时；NTP 不通时从有 Token 鉴权的服务器获取时间（服务器系统时间应准确）。断网期间沿用最后持久化配置；断电后必须重新校时，不会凭错误时间执行。

为避免复位造成重复动作，动作开始前先保存当天锁和指令编号。动作中断不自动重放，核实后从网页手动补打。最多保存 16 条离线结果，满了暂停新动作并在网页提示，联网后逐条补传；持久化从旧版字符串迁移为 NVS blob，保留每日锁和历史队列。存储写入失败会暂停并每 30 秒重试，不清空闪存。

## 网页控制

- **立即打卡**：执行完整动作；占用当天自动次数，但仍允许人工再次补打。
- **测试动作**：执行同样的动作，不计作当天打卡，也不改变每日锁。仍会真实按到手机，测试前注意手机位置。
- **舵机位置**：拖动或输入脉宽（400–2500 μs），点击移动才执行；保持 100–5000 ms 后关闭 PWM，不持续施力。脉宽不是精确角度，请按机械装配校准。
- **计划和动作配置**：开始/结束时间、自动开关、每日重启时间、归位脉宽、重复次数、每步脉宽和保持时间。每行格式 `1500,200`；每轮后归位 600 ms，最后再归位 500 ms，总长不超过 20 秒。保存后约 5–10 秒同步，网页显示版本是否应用。离线时也能保存，等待设备重连后生效。
- **每日重启**：默认 04:00，不清除配置或当天锁。开机不足 1 小时不定时重启；错过当日重启时间会在运行满 1 小时后补一次，避开打卡窗口。也能立即远程重启。
- **诊断**：温度、信号、可用内存、运行时长、重启原因、HTTP 状态、存储状态和离线队列。重启原因 9 为掉压，4 为程序异常，5–7 为看门狗；这些信息帮助排查，不代替供电和串口实测。

定位/测试/重启记录只出现在“最近执行”，不会使“今天”显示已打卡。编辑表单时暂停页面自动刷新，防止参数丢失。当前指令未完成时不接受另一条；定位间隔至少 5 秒，其余指令至少 30 秒；未领取指令 2 分钟过期，不会在很久以后意外动作。

## 升级旧设备和服务器

必须同时升级固件和 Docker 镜像，才能使用新控制功能。普通 PlatformIO Upload 即可，不要擦除全部 flash，以保留每日锁。原 `include/config.h` 的 Wi-Fi、地址和 Token 不需要重填。

已有 Compose 项目，在原来的 `server` 目录执行：

```bash
docker compose pull
docker compose up -d
```

如原来使用 `-p dignding`，继续使用同一项目名，例如 `docker compose -p dignding up -d`，否则会得到新的数据卷。不要使用 `down -v`。旧 SQLite 表会自动升级，旧执行记录保留；新增配置默认 08:10–08:40、04:00 重启。

用 Docker Desktop 单独建的 `DD` 容器，先备份数据库，再记录并复用原来的 Token、设备 ID、8888 端口和数据卷名称，用最新镜像重新创建容器。示例备份（Windows PowerShell）：

```powershell
docker exec DD python -c "import sqlite3; a=sqlite3.connect('/data/checkin.db'); b=sqlite3.connect('/data/checkin-before-v2.db'); a.backup(b); b.close(); a.close()"
docker pull ghcr.io/miubu/dingdingcheckin:latest
```

不能只点击启动原来的旧容器；要重新创建，仍挂载同一个 `/data` 卷。不要把示例 Token 与真实设备 Token 混用。

## 验证

```bash
cd server
python3 -m venv .venv-wsl
.venv-wsl/bin/pip install -r requirements.txt
.venv-wsl/bin/python -m unittest -v test_app.py
```

WSL 如提示缺少虚拟环境组件，执行 `sudo apt-get install python3-venv`。Windows 可使用 `py -m venv .venv` 和 `.venv/Scripts/python -m unittest -v test_app.py`。宿主机有 g++ 时可从项目根目录检查定时锁与每日重启策略：

```bash
g++ -Iinclude tests/firmware_policy.cpp -o .pio/firmware-policy-test
.pio/firmware-policy-test
```

## 本地构建服务器镜像

```bash
docker build -t dingdingcheckin server
```
