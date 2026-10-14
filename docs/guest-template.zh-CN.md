# 客户机模板构建指南

[English](guest-template.md) | **简体中文**

本文说明如何构建并固化 `profiles/*.toml` 所指向的**只读桌面基础镜像**。Palette 仓库本身不分发客户机系统镜像：基础镜像体积庞大、附带操作系统发行版许可义务，且强依赖于目标指令集与启动固件，因此应由运维人员按本规范在本地构建并锁定 SHA-256 摘要。

## 1. 标准客户机模板基线要求

| 属性维度 | 推荐标准取值 | 为什么要固定这一项 |
| --- | --- | --- |
| **操作系统发行版** | Debian 13 (`trixie`)，匹配 `qemu.arch` 的 `x86_64` 或 `aarch64` | 保持跨宿主实验环境一致；`x86_64` 与 `aarch64` 需要各自独立安装的镜像 |
| **桌面会话环境** | X11 上的 **XFCE** | 键盘焦点与指针坐标行为确定且轻量；不同 Wayland 合成器在虚拟设备下的行为差异较大 |
| **内部分辨率与缩放** | `1280 × 800`，`100%` 缩放 | 在客户机桌面环境**内部**固定配置，确保与 Profile 的 `[display]` 声明一致，避免中途跳变 |
| **键盘布局** | 标准 **US** 布局 | 视觉基线的 `type_text` 发送 US 硬件扫描码；若客户机设为其他布局会静默打错符号 |
| **登录用户** | 普通非 `root` 用户（仅受控开发模板可开启自动登录） | 严禁在可复用的基础模板中存放宿主机真实账号、生产凭据、SSH 私钥或浏览器 Cookie |
| **网络依赖** | 离线自足（支持 `offline` 与 `restricted-lab`） | 桌面及验收夹具不得依赖公网连接才能启动或渲染 |
| **离线验收夹具** | 预置仓库中的 `guest/fixtures/offline-workbench.html` | 让单击、双击、右键、拖拽、滚轮和键盘输入都能产生确定、可见的像素状态变化 |

## 2. 镜像构建步骤

在开始安装前，请先根据[平台与客户机架构指南](platforms.zh-CN.md)确定客户机 ISA 与引导模式：

* **`aarch64`（`virt` 机器模型）**：使用匹配的 AArch64 UEFI（EDK2/AAVMF）固件与 `virtio-gpu-pci` / `virtio-blk-pci` 驱动，输出至 `images/debian-13-xfce-aarch64.qcow2`。
* **`x86_64`（`q35` 机器模型）**：匹配所选 BIOS 或 x86 OVMF UEFI 及 `VGA` / `virtio-blk-pci` 驱动，输出至 `images/debian-13-xfce-x86_64.qcow2`。

> **注意**：切换到 `tcg-x86_64` 或 `tcg-aarch64` 不会转换已安装系统的架构，重命名或 `qemu-img convert` 也只改变磁盘容器格式。

按以下步骤构建并固化基础镜像：

1. **从可信安装介质安装系统**：记录所用安装 ISO 的确切版本号与 SHA-256 摘要。
2. **安装桌面与基础工具**：安装 XFCE（X11）、轻量级浏览器、文本编辑器（如 `mousepad`）、文件管理器（如 `thunar`），并将仓库内的 `guest/fixtures/offline-workbench.html` 复制到客户机本地（例如 `~/palette-fixture/index.html`）。
3. **固定显示分辨率与键盘布局**：在客户机内部将显示分辨率固定为 `1280 × 800`（100% 缩放）、键盘布局设为 `us`，并通过客户机内的 `xrandr` 验证实际输出尺寸。
4. **关闭一切自发画面干扰源**：禁用屏幕保护程序、自动熄屏、自动锁屏、桌面通知弹窗、浏览器首次运行向导以及后台自动升级（构建期间完成系统更新后即关闭自动更新，防止 Agent 执行任务中途弹出更新窗口改变画面）。
5. **清理敏感数据**：确认使用普通非特权用户，清除命令历史、临时缓存，确保镜像内不含任何宿主机凭据、私钥或真实个人文档。
6. **正常关机并转换为只读 `qcow2` 模板**：在客户机内执行正常关机以确保文件系统完全同步，随后在宿主机上转换格式并计算 SHA-256 摘要：

```bash
qemu-img convert -O qcow2 template.raw images/debian-13-xfce-x86_64.qcow2
qemu-img info images/debian-13-xfce-x86_64.qcow2
sha256sum images/debian-13-xfce-x86_64.qcow2
```

7. **将路径与摘要锁定到 Profile**：打开对应的 `profiles/*.toml`，填入相对路径（从配置根目录解析）与 64 位十六进制 SHA-256 摘要：

```toml
[images]
base = "images/debian-13-xfce-x86_64.qcow2"
format = "qcow2"
sha256 = "<上面计算出的 64 位 SHA-256 摘要>"
size_gib = 24
```

锁定 `sha256` 的作用，是把“恰好放在那个路径上的某个磁盘文件”变成“经此 Profile 明确授权运行的那一份不可变镜像”。未配置 `sha256` 时，`palette doctor` 会提示基础镜像未经哈希锁定；`doctor` 同时会通过 `qemu-img info` 探测真实容器格式，而非仅看文件扩展名。

## 3. 验证客户机模板

### 步骤 1：运行 `doctor` 与基础启动验证

```bash
uv run palette doctor --hash-base
uv run palette daemon start
uv run palette vm start --vm-id desktop
uv run palette vm status --vm-id desktop
```

观察 `vm_status` 输出：当 `screen_ready` 变为 `true` 时，说明已抓取到有效显示缓冲区；`desktop_ready` 在未运行客户机内增强服务时保持为 `null`（服务不会从帧缓冲猜测是否已进入桌面）。请打开 **Palette Desktop 控制台**或调用 `vm_observe` 亲眼确认客户机已进入 XFCE 桌面。

### 步骤 2：运行实机 E2E 验收套件

配置好真实 QEMU 与模板镜像后，可通过环境变量开启实机端到端测试：

```bash
PALETTE_E2E=1 PALETTE_E2E_PROFILE=default uv run pytest tests/e2e -v
```

`tests/e2e` 会验证连续启停循环、基础镜像哈希逐字节不变、真实点击到达客户机桌面，以及过期 `frame_id` 在真实实例上被正确拒绝。

## 4. 可选：集成客户机内增强服务

若需在客户机内启用 `vm_guest_input_text`（原生 Unicode/中文输入）与 `vm_guest_launch_app`（启动注册白名单应用），需在客户机的交互式图形会话中部署实现 `org.palette.tools.0`（`virtio-serial`）协议的守护进程。完整协议与安全要求见[客户机内增强服务契约](../guest/agent/README.zh-CN.md)。
