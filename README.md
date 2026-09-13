# DingDingcheckin

ESP32-C3 舵机定时打卡项目。设备每天在北京时间 08:10–08:40 自动执行一次，也可以从网页手动执行。服务器记录设备在线状态、芯片温度和最近执行结果，数据保存在 SQLite 中。

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
- 如需调整自动时间，修改 `CHECKIN_START` 和 `CHECKIN_END`。

编译和上传：

```bash
pio run
pio run --target upload
pio device monitor
```

设备保持 Wi-Fi 在线，每 5 秒轮询手动指令。断网期间自动打卡仍会执行，执行记录先保存在 ESP32 闪存，联网后补传。为避免断电造成重复动作，动作开始前会先持久化当天锁和指令编号。

## 本地构建服务器镜像

```bash
docker build -t dingdingcheckin server
```
