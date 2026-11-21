# Memobird GT1 MCP Server 🖨️

无需官方 App、无需云服务的**咕咕机 GT1 热敏打印机**开源驱动 + MCP Server。

通过经典蓝牙 SPP 协议直连设备，让你的 Claude / Cursor / 任何 MCP 客户端直接指挥打印机输出小票、待办清单、表情包图片等。

> 本项目完全基于对官方 Android SDK 的逆向分析，协议逆向细节见下文 [协议说明](#协议说明)。

---

## ✨ 特性

- 🚫 **零官方服务依赖**：不装官方 App、不注册账号、不走云打印；打印机链路为本机蓝牙直连。图片 URL 会由 MCP 主机下载。
- 🔌 **标准 MCP 工具**：`get_device_status` / `get_device_details` / `set_auto_shutdown` / `print_text` / `print_image`，任何 MCP 客户端可用。
- 🖼️ **任意图片打印**：本地文件或网络 URL 均可，自动等比缩放 + Floyd–Steinberg 误差扩散抖动，输出 384 点阵单色图。
- 🧩 **独立可用**：不接 MCP 也能当作普通 Python 库直接调用。
- 🔧 **单文件驱动**：`memobird_driver.py` 零第三方蓝牙依赖，仅需 Pillow。

## 🖥️ 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows、macOS、Linux；使用 RFCOMM 原生后端时为支持 AF_BLUETOOTH 的系统，其他平台使用系统 SPP 串口 |
| 蓝牙 | 经典蓝牙 SPP 设备；先在系统蓝牙设置中配对，或配置已创建的 RFCOMM/虚拟串口端点 |
| Python | 3.8+ |
| 硬件 | 咕咕机 GT1（其他 Memobird 型号未测试，协议可能兼容） |

> 传输支持系统 RFCOMM socket 与操作系统已配对的 SPP 虚拟串口。系统负责配对和创建端口；本项目不扫描设备、不触发配对，也不支持 BLE GATT。Windows/macOS 的串口端点需按各自系统设置选择。

## 📦 安装

```bash
git clone https://github.com/<your-account>/memobird-gt1-mcp.git
cd memobird-gt1-mcp
pip install -r requirements.txt
# Optional system Bluetooth SPP serial-port backend (pySerial):
pip install -r requirements-serial.txt
```

## 🚀 快速开始（独立使用）

```python
from memobird_driver import MemobirdGT1

# RFCOMM socket (Linux/BlueZ)
printer = MemobirdGT1(mac="AA:BB:CC:DD:EE:FF")

# Or, use an OS-paired SPP virtual serial port (Windows/macOS/Linux)
# printer = MemobirdGT1(serial_port="COM7")
# printer = MemobirdGT1(serial_port="/dev/cu.MEMOBIRD-SPP")

# 打印文本
with printer:
    printer.print_text("你好，咕咕机！\n这是本地直连打印。")

# 打印图片（本地路径 / PIL 对象 / 二进制均可）
with printer:
    printer.print_image("hello.png")
    # 网络 URL 使用 MCP 的 print_image 工具；库接口接收本地路径、bytes 或 PIL.Image。
```

## 🔗 MCP 集成

在 MCP 客户端配置中添加（以 Claude Desktop 为例）：

```json
{
  "mcpServers": {
    "memobird-gt1": {
      "command": "python",
      "args": ["/absolute/path/to/memobird_mcp.py"],
      "env": {
        "MEMOBIRD_MAC": "AA:BB:CC:DD:EE:FF",
        "MEMOBIRD_TRANSPORT": "auto"
      }
    }
  }
}
```

Cursor 则写入项目根目录 `.cursor/mcp.json`：

```json
{
  "mcpServers": {
    "memobird-gt1": {
      "command": "python",
      "args": ["/absolute/path/to/memobird_mcp.py"],
      "env": {
        "MEMOBIRD_TRANSPORT": "serial",
        "MEMOBIRD_SERIAL_PORT": "COM7"
      }
    }
  }
}
```

配置后，即可让 AI 直接调用以下工具。MCP 工具也可通过每次调用的 `mac` 参数指定设备；未提供参数且未设置 `MEMOBIRD_MAC` 时会报错，不会回退到开发者设备地址。

| 工具 | 参数 | 说明 |
|------|------|------|
| `list_system_serial_ports` | 无 | 列出系统串口端点，不打开、不探测设备 |
| `get_device_status` | `mac`, `serial_port`, `transport` (可选) | 文本摘要：连接、序列号、电量百分比与自动关机分钟值 |
| `get_device_details` | 同上 | 结构化状态、已知 TLV 字段及未知字段原始十六进制 |
| `set_auto_shutdown` | `minutes` 及连接参数 | 发送 0..255 分钟单字节值，并报告 ACK 和状态读回；ACK 不等于生效 |
| `print_text` | `content`, `bold`, `underline` 及连接参数 | 打印 GBK 编码文本小票 |
| `print_image` | `image_path_or_url`, `dither` 及连接参数 | 打印本地/网络图片，自动二值化 |

连接参数 `transport` 可为 `auto`、`rfcomm` 或 `serial`。省略时 `auto` 根据 `serial_port` 选择；`serial_port` 可为 Windows 的 `COM7`、macOS 的 `/dev/cu.*` 或 Linux 的 `/dev/rfcomm0` 等系统端点。

## 📡 系统蓝牙与 SPP 串口

先在操作系统蓝牙设置中配对打印机。连接 SPP 虚拟串口时需安装可选串口后端：

```bash
pip install -r requirements-serial.txt
```

再配置系统创建的端口；MCP 可用 `list_system_serial_ports` 只读枚举端点：

```json
{
  "env": {
    "MEMOBIRD_TRANSPORT": "serial",
    "MEMOBIRD_SERIAL_PORT": "COM7"
  }
}
```

Windows 常见端口为设备管理器显示的出站 `COM` 口；macOS 常见为 `/dev/cu.*`；Linux 可用系统创建的 `/dev/rfcomm0`。也可在单次工具调用传 `serial_port`，通常不需要提供 MAC。端口号必须来自本机系统蓝牙/串口设置，项目不会自动扫描、配对或绑定端口。可用 `MEMOBIRD_BAUDRATE` 配置系统串口速率（默认 115200；RFCOMM socket 不使用此项）。pySerial 官方项目：[pyserial/pyserial](https://github.com/pyserial/pyserial)。

Linux 原生 RFCOMM socket 仍可直接用 MAC 和通道连接：

```bash
bluetoothctl devices
```

`MEMOBIRD_TRANSPORT=rfcomm` 可强制该后端；`auto` 在未设置串口时默认选择 RFCOMM。经典蓝牙 SPP 与 BLE GATT 不同，本项目不支持 BLE-only 设备。

## 📐 协议说明

本项目逆向自咕咕机 GT1 官方 Android SDK（`cn.memobird.gtx`），核心结论：

- **物理层**：经典蓝牙 SPP（RFCOMM），默认 Channel 1。
- **帧格式**：
  ```
  | 0xAA | len(2B, LE) | cmd(1B) | payload(TLV 序列) | checksum(1B) |
  ```
  其中 `checksum = (256 - sum(body) & 0xFF) & 0xFF`。
- **主要指令**：
  - `cmd 0x01`：握手 / 状态查询；本项目已确认返回序列号、自动关机字段和 GT1 电量码（Tag 4）。电量码按官方普通设备表换算，未知码保留原值且百分比为 `null`。
  - `cmd 0x03`：设备设置通道；自动关机使用 `Tag 19 (0x13)`，值为一个字节的分钟数。
  - `cmd 0x04`：打印通道，payload 为 TLV 块。
- **关键 TLV Tag**：
  - `Tag 11`(2B)：本次任务总包数
  - `Tag 12`(2B)：当前包序号
  - `Tag 7`：GBK 编码文本
  - `Tag 8`：1-bit 单色 BMP 图像分片（每包 1024 字节）
  - `Tag 13`/`Tag 17`/`Tag 16`：加粗 / 下划线 / 字号
- **固件保护**：每次打印前需发送 2 次 1024 字节全零预热包。
- **自动关机**：SDK 使用 `cmd 0x03` / `Tag 19 (0x13)` / 单字节分钟值。官方 App 列出 0、10、30、60、120、180、240；本驱动允许 0..255 并在同一连接读回。实测 255 得到匹配读回；0 的设置命令未收到对应 ACK，后续字段仍为 255，故不能声称 0(永不关机)可用。
- **图像方向坑**：官方 `BMPFile.createBMPArray` 生成的是 **biHeight 为正、像素按 top-down 行序**写入的非标准 BMP，而 PIL 默认输出 bottom-up。驱动在保存前对图像做 `FLIP_TOP_BOTTOM` 以对齐官方格式，否则打印结果会上下颠倒。
- **分包策略**：采用**连发模式**（发完所有包不等逐包 ACK）。实测「逐包等 ACK」会因每包 3 秒超时把多包任务拖到数十秒，触发固件超时导致不出纸。
- **发送结果**：`print_text` / `print_image` 返回是否收到设备 ACK；ACK 超时返回 `False`，发送异常会抛出。ACK 只能说明设备有响应，不能证明纸张已实际输出。图片传输临时使用 3 秒 socket timeout，并在成功或异常后恢复原 timeout；传输中断不会自动重连重放部分数据。
- **MCP 并发与结果**：状态查询、设置和打印操作串行执行。状态提供结构化工具和文本摘要；设置结果区分 ACK 与读回核验，打印 ACK 不证明纸张已实际输出。图片输入最多 10 MiB。

## ⚠️ 已知限制

- 已实现 Linux RFCOMM socket 与通过 pySerial 使用系统 SPP 串口端点的传输路径。串口后端为可选依赖，需安装 `requirements-serial.txt`；Linux/Windows/macOS 实际可用端点由系统蓝牙栈、配对状态和设备 SPP 服务决定。当前开发机只验证了 Linux RFCOMM 实机；其他平台仍需各自实机验证。
- 状态流解析器会重组分段/合并帧，校验长度和校验和，并跳过主动 `cmd 0x0a` 通知。已在 GT1 上读回序列号、自动关机值和电量原始码。官方 SDK 的 `HandTask.sendToMain` 将握手数组索引 3 作为电量，并对 GT1 使用普通设备表；状态 TLV 的 `Tag 4` 与这条字段顺序对应，`78` 映射为 `70%`。其他/未知电量码不猜测百分比，未知 TLV 在 `raw_fields` 保留十六进制。
- 当前实机查询确认 `Tag 4=0x4e`、电量 `70%`。
- 255 分钟写入后读回 `0xff`；0 分钟写入没有收到设置 ACK，读回仍是 `0xff`。未验证重启持久性、实际关机时间或纸张实际输出。

## 📄 License

[MIT](LICENSE)
