# MCP、Agent 与桌面应用端到端验收指南

[English](acceptance-test.md) | **简体中文**

本流程用于端到端验证同一台虚拟机上的 **MCP 视觉观察（`vm_observe`）**、**Agent 有界点击（`vm_act`）**、**Palette Desktop 实时画面流（RFB）**、**人机独占接管（`BUSY` / `STALE_FRAME`）** 以及**离线硬件设置修改**。

主线流程使用常驻共享后台（`palette daemon`）；MCP 客户端、CLI 与 Palette Desktop 必须指向相同的配置根目录（`CONFIG_ROOT`）与运行目录（`runtime_root`）。

## 1. 环境准备与证据目录约定

在仓库根目录执行依赖同步与桌面端构建：

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run build
```

* **前置组件**：Python `3.12`/`3.13`、`uv`、Node.js `22.19+`、匹配客户机架构的 QEMU 可执行文件与 UEFI 固件，以及可启动的桌面基础镜像（`qcow2`/`raw`）或系统安装 ISO。
* **目录与标识约定**：下文中 `CONFIG_ROOT` 表示包含 `palette.toml` 和 `profiles/` 的配置根目录；`VM_ID` 必须从 GUI 的“状态”页复制真实虚拟机 ID（如 `vm_489cdb67aca0`），不能用显示名称代替。
* **证据存放**：所有测试报告与截图均保存在 `.runtime/acceptance/` 下，切勿向 Git 提交磁盘镜像、截图或连接凭据。

## 2. 启动并核验共享后台

```bash
uv run python -m palette.cli --root CONFIG_ROOT config show
uv run python -m palette.cli --root CONFIG_ROOT doctor --json
uv run python -m palette.cli --root CONFIG_ROOT daemon start
uv run python -m palette.cli --root CONFIG_ROOT daemon status
```

**通过标准**：

* `doctor` 报告 `ok = true`（环境检查仅证明宿主启动条件具备，不能替代客户机进入桌面的实机验收）。
* `daemon status` 显示 `kind = "daemon"`，记录其 `pid`、`generation`、`config_root` 与 `runtime_root`。
* 若该运行目录已有旧的 `stdio` 托管服务，先正常结束原 MCP 会话再启动 `daemon`，不要额外开启第二个 `--standalone` 服务。

## 3. 在 Palette Desktop 中创建并启动测试虚拟机

启动桌面端预览并指向同一 `CONFIG_ROOT`：

```bash
# macOS / Linux
PALETTE_ROOT="CONFIG_ROOT" npm --prefix desktop run preview
```

```powershell
# Windows PowerShell
$env:PALETTE_ROOT = "CONFIG_ROOT"
npm --prefix desktop run preview
```

1. 打开**“环境设置”**，确认配置目录与运行目录同第 2 步完全一致。
2. 点击**“创建虚拟机”**：选择 `qcow2`/`raw` 基础镜像或安装 ISO，选择匹配架构的 Profile，配置 CPU、内存、磁盘容量与网络模式后完成创建，记录生成的 `VM_ID`。
3. 启动虚拟机并打开**“控制台”**，保持默认的**“仅查看”**模式。等待客户机真正进入可操作的图形桌面（切勿将 UEFI 启动画面、登录界面或单纯的 `RUNNING` 状态视为桌面就绪）。
4. 切换到**“状态”**页，记录当前 `instance_id`、`action_seq` 操作序号、控制权归属、代次（Epochs）、画面尺寸及保持的键鼠输入（状态页每 2 秒自动轮询；控制台画面通过独立 RFB 通道连续更新）。

> **说明**：状态页显示“客户机桌面：尚未确认”（`desktop_ready: null`）属于正常现象，代表未运行客户机内增强服务握手，不影响视觉观察或点击。切换离开“控制台”页会自动释放人工输入，因此测试人工按键保持时请留在控制台页。

## 4. 连接 MCP 视觉客户端

在支持 `stdio` MCP 且会将图像块（`ImageContent`）转发给模型的客户端中添加配置（请将以下三个路径替换为本机绝对路径）：

```json
{
  "mcpServers": {
    "palette": {
      "command": "/absolute/path/to/uv",
      "args": [
        "run", "--project", "/absolute/path/to/Palette",
        "python", "-m", "palette.cli",
        "--root", "/absolute/path/to/CONFIG_ROOT",
        "serve", "--connect-only"
      ]
    }
  }
}
```

**通过标准**：

* MCP 会话初始化成功，工具列表包含 `vm_list`、`vm_status`、`vm_observe`、`vm_act`、`vm_wait`、`vm_release_inputs`。
* `vm_list({})` 可发现持久化及运行时已知 VM ID（包括停机定义），不启动 VM、不领取控制权，也不返回宿主配置。
* 调用 `vm_status({"vm_id": "VM_ID"})` 返回 `ok = true`、`state = "RUNNING"`，且 `instance_id` 与 GUI 状态页完全一致。
* 调用 `vm_observe({"vm_id": "VM_ID"})`，确认模型实际收到了 PNG 图像（仅列出工具或仅看到 JSON 文本不算通过）。

## 5. 执行 Agent 视觉点击验收

选择客户机桌面中**可逆且视觉变化明显**的目标（如双击 WinPE 桌面的“此电脑”打开资源管理器、点击开始菜单，或在客户机浏览器中打开预置的 `guest/fixtures/offline-workbench.html` 点击测试网格单元）。

可直接向视觉 Agent 下达如下验收提示词（替换 `VM_ID`）：

> 对虚拟机 `VM_ID` 执行一次视觉点击测试：
> 1. 先调用 `vm_status` 确认正在运行且控制权可用；
> 2. 调用 `vm_observe` 获取最新截图，根据实际收到的画面选择“此电脑”或测试网格等可逆目标；
> 3. 使用本次 `frame` 的 `instance_id`、`frame_id` 和全新的 `request_id`，只调用一次 `vm_act`（若目标需双击则设置 `count = 2`）；
> 4. 检查返回的动作后截图确认目标已发生可见变化，必要时调用 `vm_observe` 复核；
> 5. 严禁盲猜坐标、严禁重放结果未知的动作、不关机、不重置、不执行删除；
> 6. 汇报前后 `instance_id`、`action_seq`、动作回执、画面变化与键鼠释放状态。

标准 MCP 工具调用参数格式：

```json
{"name": "vm_observe", "arguments": {"vm_id": "VM_ID"}}
```

```json
{
  "name": "vm_act",
  "arguments": {
    "vm_id": "VM_ID",
    "instance_id": "FROM_LATEST_FRAME",
    "frame_id": "FROM_LATEST_FRAME",
    "request_id": "acceptance-click-unique-001",
    "action": {"type": "click", "x": 100, "y": 200, "button": "left", "count": 1}
  }
}
```

**通过标准**：

* `vm_act` 回执 `ok = true`、`effect_state = "submitted"`，并返回新截图与新 `frame_id`。
* 动作后截图与 GUI 控制台画面同步呈现目标变化（如窗口打开、菜单展开或网格变绿）。
* GUI 状态页与 `vm_status` 的 `action_seq` 同步递增，按住的键与鼠标按钮最终均为空。
* 关闭 MCP 客户端连接后，GUI 中的虚拟机继续运行，`daemon` 的 PID 与 `generation` 不变。

## 6. 使用脚本执行确定性 MCP 观察与点击

仓库提供 `scripts/mcp_click_test.py`，可在不依赖外部大模型的情况下通过真实 MCP 协议验证观察与点击：

```bash
# 1. 仅执行观察并保存 02-before.png，用于查看并选定目标像素坐标 (X, Y)
uv run python scripts/mcp_click_test.py   --root CONFIG_ROOT --vm-id VM_ID --out .runtime/acceptance/observe

# 2. 对选定坐标执行单次点击（双击可加 --count 2，右键可加 --button right）
uv run python scripts/mcp_click_test.py   --root CONFIG_ROOT --vm-id VM_ID --x X --y Y   --out .runtime/acceptance/click
```

脚本会输出 `report.json`、点击前截图 `02-before.png`、点击后即时截图 `03-click.png`，并默认通过 `vm_wait` 等待 1 秒后保存 `04-verification.png`（可通过 `--wait-ms 2000` 调整，上限 10 秒）。退出码 `0` 表示检查通过，`4` 表示失败。

## 7. 人工接管与控制权冲突验收

1. 在 Palette Desktop 控制台点击**“接管”**，保持窗口焦点停留在该控制台页面。
2. 从另一客户端或预置延迟命令运行点击测试，验证人工接管期间 Agent 仍可 `vm_observe`，但 `vm_act` 必须被拒绝并返回 `BUSY`：
   ```bash
   uv run python scripts/mcp_click_test.py      --root CONFIG_ROOT --vm-id VM_ID --x X --y Y --expect-error BUSY      --out .runtime/acceptance/takeover
   ```
3. 在控制台画面上按住按键、拖拽鼠标与滚动滚轮，核对客户机响应，并通过另一客户端调用 `vm_status` 确认按键保持期间账本非空。
4. 点击**“交还 Agent”**或按 **`Ctrl+Alt+G`** 释放控制权：验证重新 `vm_observe` 后 Agent 点击恢复正常，而继续使用接管前的旧 `frame_id` 会返回 `STALE_FRAME`。
5. 验证失焦、切换页面、隐藏窗口、断开连接或停止心跳满 **3 秒**后，控制权租约自动过期且按住的键鼠状态被清空。

## 8. 边界回归与安全退出检查清单

| 测试用例 | 执行动作 | 通过条件 |
| --- | --- | --- |
| **旧帧拒绝** | 一次成功动作后再次使用动作前的旧 `frame_id` 调用 `vm_act` | 返回 `STALE_FRAME`，客户机不产生任何额外输入 |
| **幂等去重** | 使用相同 `request_id` 与完全相同的参数重发 `vm_act` | 返回缓存回执（`deduplicated = true`），无重复点击；若参数不同则返回 `REQUEST_ID_CONFLICT` |
| **缩放坐标映射** | 在适应窗口、原始尺寸与高 DPI 下分别通过 MCP 和人工点击同一目标 | 均准确命中目标（MCP 使用图像像素坐标，GUI 人工使用映射坐标） |
| **全屏切换** | 进入并退出控制台全屏模式 | 画面正常显示，退出全屏时自动释放人工控制权 |
| **持久重启** | 正常关机后再次启动同一持久虚拟机 | 生成新 `instance_id`，持久磁盘文件与 UEFI 变量保留，旧 frame 失效 |
| **电源键** | 在控制台、虚拟机列表和菜单点击关机，先取消再确认；使用忽略 ACPI 的一次性客户机验证 | 确认框显示虚拟机名称及未保存数据丢失、文件系统损坏风险；取消保留原运行实例；确认后不发送 ACPI 请求，强制停止并保留持久磁盘 |
| **仅关闭窗口** | 关闭桌面端窗口并选择“仅关闭窗口” | `daemon` 与 VM 继续运行，MCP 仍可观察，重开 GUI 可见同一运行实例 |
| **完全退出** | 关闭桌面端窗口并选择“完全退出” | 先正常关闭所有运行中 VM，再停止当前 `daemon`，确认进程退出后关闭窗口，持久数据保留 |
| **退出异常保护** | 正常关机超时或后台断线时点击“完全退出” | 窗口保留并显示错误原因，支持重试、取消或改为“仅关闭窗口”，绝不静默强杀 |
| **确认后恢复退出** | 关机超时后选择“强制停止此虚拟机并退出…”，先取消一次，再明确确认 | 取消保留虚拟机运行；确认仅停止原实例并保留磁盘，其他虚拟机继续正常退出；实例已被替换时拒绝执行 |
| **客户机内部关机** | 在客户机内部操作正常关机 | 客户机 `SHUTDOWN` 事件和 QEMU 退出码 0 对应 `STOPPED`，释放资源并保留持久磁盘；崩溃或非零退出仍为 `FAILED` |

记录 QMP 事件、进程退出码、耗时及是否使用强制停止。`POWERDOWN` 仅证明发出了电源请求，不证明客户机已完成关机。CLI 停止命令若超时后强杀 QEMU，不能算正常关机通过。客户机代理关机与 ACPI 关机应分别验证。

## 9. 离线硬件设置验收清单

在关机状态下使用真实客户机逐项验证硬件设置修改（运行中设置面板必须只读，且后端拒绝写入）：

1. **处理器与内存**：关机修改 vCPU 与内存大小，启动后通过 QMP 与客户机系统内核对资源配置。
2. **受管磁盘扩容**：在客户机写入测试文件后关机扩容，核对实盘虚拟容量增大、原有测试文件字节完好且 UEFI 变量未变，随后在客户机内扩展分区。
3. **显卡与默认分辨率**：为支持分辨率提示的显卡（如 `virtio-gpu-pci`）修改默认分辨率，记录请求值与实际画面尺寸；验证 `ramfb` 明确提示分辨率由固件/客户机决定。
4. **ISO 挂载与启动顺序**：验证挂载、更换、移除 ISO，以及自动、硬盘优先、光盘优先三种启动顺序。
5. **网络与端口转发**：分别验证 `offline`、`restricted-lab` 与 `nat` 模式；配置 TCP 端口转发后验证宿主机仅在 `127.0.0.1` 监听且可连通客户机服务。
6. **Intel HDA 音频**：启用声音并验证 QEMU 音频后端（CoreAudio / DirectSound / PipeWire 等）初始化成功、客户机播放声音正常输出且不采集麦克风。

## 10. 本机实机验收历史记录（macOS ARM64 + HVF）

### 10.1 2026-10-13 ARM64 WinPE 端到端视觉与接管实测

* **测试环境**：macOS ARM64，QEMU `11.1.2`（`HVF`），机器模型 `virt,gic-version=3,its=off` + UEFI + `ramfb`，2 vCPU / 4 GiB RAM / 8 GiB 磁盘 / `offline` 网络。
* **测试介质**：`CNBYDJ_PE_V4.0.0.ISO`（SHA-256: `11bc4c89e6529febc07beb60768c4bab2f3167c739ada9182547095363f2f61d`），独立运行目录 `.runtime/winpe-acceptance/`，VM ID `vm_489cdb67aca0`，实例 `inst_90f74625c80b`，画面 `800 × 600`。

| 验收项目 | 实测结果与证据说明 |
| --- | --- |
| **WinPE 引导进入桌面** | **通过**：响应光盘启动按键提示（或从 UEFI Shell 执行 `fs1:\EFI\BOOT\BOOTAA64.EFI`）后成功进入 WinPE 桌面 |
| **MCP 初始化与截图** | **通过**：标准 `stdio` 代理连接共享 `daemon`，成功获取 `vm_status` 与 `vm_observe` PNG 截图 |
| **双击“此电脑”** | **通过**：单次耗时 `889.70 ms`（含传输、输入与即时截图），后续截图确认资源管理器窗口已打开 |
| **右键点击空白区** | **通过**：单次耗时 `273.42 ms`，后续截图确认资源管理器右键上下文菜单可见 |
| **GUI 实时同步** | **通过**：同一实例的 `action_seq` 从 `11` 同步更新至 `12`，桌面端 RFB 画面实时反映窗口变化 |
| **人工接管与冲突拦截** | **通过**：人工接管期间 MCP `vm_observe` 正常，MCP `vm_act` 返回 `BUSY`；交还控制权后键鼠账本为空且 `release_confirmed = true` |
| **MCP 代理断开保持运行** | **通过**：多次测试会话断开后，虚拟机与共享 `daemon` 均保持稳定运行 |

### 10.2 2026-10-14 硬件设置与磁盘扩容实测

* **测试环境**：macOS `27.0.1` / ARM64，QEMU `11.1.2` / `HVF`，独立测试虚拟机。
* **实测结果**：
  * 真实 `qemu-img` 将受管理可写磁盘从 `8 GiB` 安全扩容至 `12 GiB`；`qemu-io` 校验偏移 `1048576` 处的 `4096` 字节测试数据完好保留，UEFI 变量文件字节未变。
  * QMP 与进程参数确认 `2` vCPU、`1024 MiB` 内存、VirtIO 网卡、仅 `127.0.0.1` 端口转发监听、Intel HDA 声卡及 macOS `CoreAudio` 后端初始化成功。

## 11. 验收记录模板与自动化回归命令

每次执行实机验收时，请按以下模板归档记录：

```text
日期 / 宿主系统 / 架构：
Git SHA / Python / QEMU / 加速器：
ISO 或基础镜像版本 / SHA-256 摘要：
CONFIG_ROOT / runtime_root / daemon PID / generation：
VM_ID / instance_id / 客户机画面尺寸：
MCP 客户端 / 模型 / 是否实际收到 ImageContent 图片：
点击目标 / 图片像素坐标 / request_id / 前后 action_seq：
输入回执 / 客户机可见变化 / GUI 同步结果：
人工接管 / BUSY 拦截 / STALE_FRAME 拒绝 / 释放确认结果：
正常关机 / 代理断开 / GUI 退出结果：
耗时口径 / 样本数量 / 截图与 report.json 路径：
通过、失败或未执行的项目及原因：
```

自动化回归命令：

```bash
uv run pytest tests/integration/test_desktop.py tests/integration/test_vm_settings.py tests/unit/test_display.py
uv run python scripts/check_docs.py
npm --prefix desktop test
npm --prefix desktop run test:e2e
```
