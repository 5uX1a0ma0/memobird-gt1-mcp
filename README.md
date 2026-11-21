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
| 操作系统 | **Linux**（驱动使用 `socket.AF_BLUETOOTH`，需要内核 RFCOMM 支持） |
| 蓝牙 | 经典蓝牙适配器（BlueZ 5.x），已配对/可发现目标设备 |
| Python | 3.8+ |
| 硬件 | 咕咕机 GT1（其他 Memobird 型号未测试，协议可能兼容） |

> ⚠️ 当前仅支持 Linux。Windows/macOS 的蓝牙 RFCOMM 接口与 Linux 不同，暂未适配。

## 📦 安装

```bash
git clone https://github.com/<your-account>/memobird-gt1-mcp.git
cd memobird-gt1-mcp
pip install -r requirements.txt
```

## 🚀 快速开始（独立使用）

```python
from memobird_driver import MemobirdGT1

# 显式填入你自己的设备地址（见下文「获取蓝牙 MAC」）
printer = MemobirdGT1(mac="AA:BB:CC:DD:EE:FF")

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
        "MEMOBIRD_MAC": "AA:BB:CC:DD:EE:FF"
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
      "env": { "MEMOBIRD_MAC": "00:15:83:41:F8:B2" }
    }
  }
}
```

配置后，即可让 AI 直接调用以下工具。MCP 工具也可通过每次调用的 `mac` 参数指定设备；未提供参数且未设置 `MEMOBIRD_MAC` 时会报错，不会回退到开发者设备地址。

| 工具 | 参数 | 说明 |
|------|------|------|
| `get_device_status` | `mac`(可选) | 文本摘要：连接、序列号、电量百分比与自动关机分钟值 |
| `get_device_details` | `mac`(可选) | 结构化状态、已知 TLV 字段及未知字段原始十六进制 |
| `set_auto_shutdown` | `minutes`, `mac`(可选) | 发送 0..255 分钟单字节值，并报告 ACK 和状态读回；ACK 不等于生效 |
| `print_text` | `content`, `bold`, `underline`, `mac` | 打印 GBK 编码文本小票 |
| `print_image` | `image_path_or_url`, `dither`, `mac` | 打印本地/网络图片，自动二值化 |

## 📡 获取蓝牙 MAC 地址

```bash
bluetoothctl scan on
# 观察输出，找到设备名 MEMOBIRD GT1 对应的 MAC
bluetoothctl scan off
```

也可用 `bluetoothctl devices` 列出已发现设备。

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

- 仅支持 Linux + BlueZ；Windows/macOS 需自行适配 RFCOMM 层。
- 状态流解析器会重组分段/合并帧，校验长度和校验和，并跳过主动 `cmd 0x0a` 通知。已在 GT1 上读回序列号 `636404963750000000`、自动关机 `255` 和电量原始码 `78`。官方 SDK 的 `HandTask.sendToMain` 将握手数组索引 3 作为电量，并对 GT1 使用普通设备表；状态 TLV 的 `Tag 4` 与这条字段顺序对应，`78` 映射为 `70%`。其他/未知电量码不猜测百分比，未知 TLV 在 `raw_fields` 保留十六进制。
- 当前实机查询确认 `Tag 4=0x4e`、电量 `70%`。
- 255 分钟写入后读回 `0xff`；0 分钟写入没有收到设置 ACK，读回仍是 `0xff`。未验证重启持久性、实际关机时间或纸张实际输出。

## 📄 License

[MIT](LICENSE)
