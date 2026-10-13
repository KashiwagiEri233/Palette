# MCP、Agent 与桌面应用端到端测试

[English](acceptance-test.md) | **简体中文**

本流程验证同一台虚拟机上的 MCP 观察、Agent 点击、GUI 连续画面及状态同步。
主线使用常驻 daemon；MCP 和 GUI 必须连接相同的配置目录与运行目录。

## 1. 准备环境与证据目录

在仓库根目录运行：

```bash
uv sync --locked
npm --prefix desktop ci
npm --prefix desktop run build
```

需要 Python 3.12/3.13、uv、Node.js 22.12+、匹配客户机架构的 QEMU 和固件。
原生 Apple Silicon 使用 aarch64/HVF；跨架构测试必须显式选择 TCG。
先准备一台可启动的桌面镜像，或 ISO 来源的虚拟机。真实 QEMU 测试与 fake QEMU
测试分别记录，fake 结果不能证明 Windows/WinPE 已进入桌面。

约定下文 `CONFIG_ROOT` 是包含 `palette.toml` 和 `profiles/` 的目录；
`VM_ID` 从 GUI 的“状态”页复制，不能用显示名称代替。
证据放在运行目录或 `.runtime/acceptance/`，不要提交磁盘、截图或连接凭据。

## 2. 启动共享后台

```bash
uv run python -m palette.cli --root CONFIG_ROOT config show
uv run python -m palette.cli --root CONFIG_ROOT doctor --json
uv run python -m palette.cli --root CONFIG_ROOT daemon start
uv run python -m palette.cli --root CONFIG_ROOT daemon status
```

预期：环境检查 `ok=true`；owner 类型为 `daemon`，记录其 PID、generation、
`config_root` 和 `runtime_root`。环境检查只证明启动条件，不能代替客户机桌面验收。
若该运行目录已有 stdio owner，先正常结束原 MCP 会话再启动 daemon；不要开启第二个
`--standalone` owner。此步骤不需要把 `serve` 手动留在终端里运行。

## 3. GUI 创建并启动测试虚拟机

```bash
PALETTE_ROOT="CONFIG_ROOT" npm --prefix desktop run preview
```

PowerShell 使用：

```powershell
$env:PALETTE_ROOT = "CONFIG_ROOT"
npm --prefix desktop run preview
```

1. 在“环境设置”确认配置目录和运行目录与第 2 步一致。
2. 创建虚拟机：选择 qcow2/raw 镜像或 ISO，选择匹配架构的 profile，配置 CPU、内存、
   磁盘和网络，完成确认。记录 VM 标识。
3. 启动后打开“控制台”，保持“仅查看”。等待客户机完成启动，不能把 UEFI、登录画面
   或 `RUNNING` 状态当作桌面可用。
4. 在“状态”页记录当前实例、操作序号、控制权、代次、画面尺寸、保持的键鼠输入。
   状态在窗口可见时每 2 秒轮询更新；画面通过独立 RFB 连接连续更新。
5. 切换回“控制台”继续看画面。切离控制台会释放人工输入；检查持续按键时不要同时
   切到“状态”页。

状态页中的“客户机桌面：尚未确认”表示未取得桌面就绪证据，不代表无法观察或点击。
“控制权：Agent”表示允许 Agent 操作，不证明某个 MCP 客户端当前在线。
后台断线以错误提示为准，旧状态快照不能作为最新状态。

## 4. 连接 MCP 客户端

在支持 stdio MCP 和图片转发的客户端添加连接。替换三个绝对路径：uv 可执行文件、
Palette 仓库和 `CONFIG_ROOT`。

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

预期：初始化成功；工具列表至少包含 `vm_status`、`vm_observe`、`vm_act`、
`vm_wait`、`vm_release_inputs`。`serve --connect-only` 不创建第二个 owner；如果后台
不存在，应报错而不是启动独立服务。每个 MCP 客户端单独启动自己的 stdio 代理。

调用 `vm_status({"vm_id":"VM_ID"})`，确认 `ok=true`、`state=RUNNING`，并与 GUI 比较
相同的 `instance_id`。调用 `vm_observe`，确认 Agent 实际收到了图片，而不只是 JSON。
只列出工具或只看到 `ImageContent` 元数据，不能算视觉 Agent 连接通过。

## 5. Agent 点击验收

使用客户机中可逆且结果明显的目标，例如双击 WinPE 的“此电脑”打开资源管理器，或点击
可正常工作的开始菜单；也可在客户机浏览器打开
仓库的 `guest/fixtures/offline-workbench.html`，点击一个网格单元，观察变绿和计数变化。
夹具必须先复制进客户机，宿主的 `localhost` 和宿主文件路径不等于客户机内的地址。

可以把下面的测试任务交给 Agent，替换 VM 标识：

> 对虚拟机 VM_ID 执行一次视觉点击测试。先调用 vm_status，确认正在运行且控制权可用；
> 再调用 vm_observe，根据实际收到的最新截图选择“此电脑”或测试网格等可逆目标。
> 使用本次 frame 的 instance_id、frame_id 和新的 request_id，只调用一次 vm_act。
> 目标需要双击时在 click 动作中使用 count=2。
> 检查返回的截图确认目标确实发生变化，必要时重新观察。
> 不猜坐标，不重复结果未知的动作，不关机、不重置、不执行安装或删除。
> 报告前后实例、操作序号、回执、画面变化和键鼠释放状态。

调用顺序与关键参数：

```json
{"name":"vm_observe","arguments":{"vm_id":"VM_ID"}}
```

从结果的 `frame` 取实例和帧标识，坐标使用 `image_size` 定义的交付图片像素，
不是 GUI 缩放后的 CSS 像素。随后调用：

```json
{
  "name":"vm_act",
  "arguments":{
    "vm_id":"VM_ID",
    "instance_id":"FROM_LATEST_FRAME",
    "frame_id":"FROM_LATEST_FRAME",
    "request_id":"acceptance-click-unique-001",
    "action":{"type":"click","x":100,"y":200,"button":"left"}
  }
}
```

`100,200` 只是参数格式示例，实际坐标必须从截图选择。预期：

- 回执 `ok=true`、`effect_state=submitted`，有新的截图与帧标识。
- 客户机目标发生可见变化；GUI 控制台显示同样的变化。
- GUI 状态页与 `vm_status` 的实例一致，操作序号增加，键和按钮最终均为空。
- 此时关闭 MCP 代理连接，GUI 的 VM 继续运行，daemon PID/generation 不改变。

成功回执只证明输入被提交，不证明点击命中了正确应用。画面未改变时记录“不通过／
待确认”，不能仅凭 `ok=true` 判定应用点击成功。

## 6. 可执行的 MCP 观察／点击脚本

脚本连接既有 owner，不启动、重置或停止 VM。先仅观察并保存截图：

```bash
uv run python scripts/mcp_click_test.py \
  --root CONFIG_ROOT --vm-id VM_ID --out .runtime/acceptance/observe
```

查看 `02-before.png`，选定可逆目标，并确认客户机未改变。再基于同样布局执行一次点击；
脚本会重新观察，使用本次新 frame。若布局已改变，应重新选坐标：

```bash
uv run python scripts/mcp_click_test.py \
  --root CONFIG_ROOT --vm-id VM_ID --x X --y Y \
  --out .runtime/acceptance/click
```

输出 `report.json`、点击前图片和成功点击后的 `03-click.png`。默认再通过 `vm_wait`
等待 1 秒，保存 `04-verification.png`；可用 `--wait-ms 2000` 调整，上限 10 秒。
如果客户机仍未响应，继续只读观察，不重放该次输入。报告记录工具名、结构化
结果和每次 MCP 调用的耗时，不保存凭据。返回码 0 表示所选检查通过，4 表示失败。
目标需要双击时加 `--count 2`；它仍是一次有界 `vm_act` 调用。
要打开右键菜单可加 `--button right`。
脚本没有视觉推理能力，仍需人工或视觉 Agent 核对目标变化。
`elapsed_ms` 包含传输和截图等处理，不能称为纯输入延迟或 GUI 帧延迟。

## 7. 人工接管与冲突测试

1. GUI 在控制台点击“接管”，保持窗口焦点和该页，不切到另一个应用。
2. 独立 MCP 客户端仍可 `vm_observe`；`vm_act` 应返回 `BUSY`，客户机不能产生该次输入。
   可由另一个客户端运行以下命令，或预先安排延迟执行，避免切终端触发失焦释放：

```bash
uv run python scripts/mcp_click_test.py \
  --root CONFIG_ROOT --vm-id VM_ID --x X --y Y --expect-error BUSY \
  --out .runtime/acceptance/takeover
```

3. 在控制台画面上按住一个键、拖动和滚动，核对客户机响应；使用另一个客户端
   `vm_status` 确认相应键／按钮在保持期间存在。
4. 点击“交还 Agent”，或使用 Ctrl+Alt+G。重新观察后，Agent 点击应恢复；操作旧 frame
   应返回 `STALE_FRAME`。失焦、切换 VM、离开控制台和显示断线均应释放输入。
5. 要验证 3 秒租约超时，需要暂停心跳或受控断开客户端；单纯网络空闲不等于心跳丢失。
   观察保持键鼠清空、控制权恢复。释放未确认时应继续阻止写入。

## 8. 其他回归与退出

| 用例 | 动作 | 通过条件 |
| --- | --- | --- |
| 旧帧 | 一次成功动作后再用动作前的 frame | `STALE_FRAME`，无新增客户机效果 |
| 重复请求 | 相同 request_id、完全相同参数重发 | 返回相同回执，无重复点击；换参数应拒绝 |
| 坐标／缩放 | 适应窗口、原始尺寸、高 DPI 下点击同一目标 | 目标一致；MCP 使用图片坐标，人工使用映射后的坐标 |
| 全屏 | 进入和退出控制台全屏 | 画面正常，退出释放人工控制 |
| 重启 | 正常关机再启动 | 新 instance_id，持久盘与 UEFI 变量保留；旧输入失效 |
| 退出取消 | 有 VM 运行时关闭窗口并取消 | 窗口和 VM 保留 |
| 后台运行 | 关闭窗口选择后台继续运行 | MCP 仍能观察；再次开 GUI 看见同一实例 |
| 全部关机 | 选择全部关机并退出 | 先正常关机；超时保留窗口并说明原因，不自动强停 |

WinPE 的 X: RAM 盘会随重启重建；测试持久化时使用受管理磁盘上的测试文件，
且先确认该 WinPE 能识别磁盘控制器。ISO 来源“恢复初始状态”回到空白盘，会丢弃持久盘修改。
测试结束先正常关机，之后再 `daemon stop`。`daemon stop` 的清理不能当作客户机正常关机证据。

## 9. 本机 ARM64 WinPE 专项

提供的 ISO 是 `CNBYDJ_PE_V4.0.0.ISO`。当前准备的独立配置在
`.runtime/winpe-acceptance/`，不混入普通 VM 的运行目录；ISO 保持在原路径且以只读介质挂载。
配置使用 QEMU aarch64、HVF、`virt,gic-version=3,its=off`、UEFI 和 `ramfb`，
2 核／4 GiB／8 GiB 空白盘／离线网络。Windows ARM 的显示兼容性参考
[QEMU 官方说明](https://www.qemu.org/docs/master/system/whpx.html)。

重新打开本机测试 GUI：

```bash
PALETTE_ROOT="$PWD/.runtime/winpe-acceptance" npm --prefix desktop run preview
```

此 ISO 有“Press any key to boot from CD or DVD”提示，出现时及时按键；错过后可能进入
UEFI shell。先依据屏幕的 Mapping table 找到安装介质的文件系统。本机见到的是 `FS1:`，
可输入 `fs1:`，再执行 `EFI\BOOT\BOOTAA64.EFI`，并及时再次按 Enter 确认光盘启动。
其他启动环境的 FS 编号可能不同，不要固定照抄。只有出现实际 WinPE 桌面后，才继续
第 5 步的可见目标点击测试。

### 2026-10-13 本机实测记录

宿主 macOS ARM64，QEMU 11.1.2，HVF；ISO SHA-256：
`11bc4c89e6529febc07beb60768c4bab2f3167c739ada9182547095363f2f61d`。
本机 VM 为 `vm_489cdb67aca0`，实例为 `inst_90f74625c80b`，画面 800 × 600。

| 检查 | 结果与证据 |
| --- | --- |
| WinPE 启动 | 通过；确认光盘启动提示后进入实际桌面 |
| MCP 初始化、状态、截图和键盘 | 通过；标准 stdio 代理连接到共享 daemon |
| 开始按钮 | 点击回执成功，即时截图没有确认菜单展开；不将其单独计为应用点击通过 |
| 双击“此电脑” | 通过；即时截图先显示图标选中，后续只读截图确认资源管理器已打开 |
| 右键点击 | 通过；后续截图确认资源管理器空白区右键菜单可见 |
| GUI 同步 | 通过；同一实例的操作序号从 11 更新到 12，实际画面正常显示 |
| 人工接管 | 通过；MCP 观察正常，MCP 点击返回 BUSY；交还后键鼠均为空、释放已确认 |
| MCP 代理退出 | 通过；多次测试会话结束后 VM 和 daemon 继续运行 |
| 实机重启、持久化、全部关机 | 本次未执行；测试 VM 保持运行供人工继续检查 |

MCP 完整点击调用的单次耗时：开始按钮 693.15 ms，双击 889.70 ms，右键 273.42 ms。
包含传输、输入和即时截图；只有各 1 个样本，不是分位数，也不是 GUI 帧延迟。
客户机反应可晚于动作回执，应用效果根据后续截图确认，未重复相同的结果未知动作。
证据在本机 `.runtime/winpe-acceptance/evidence/`，其中 `gui-verification.json`、
`gui-status.png` 和各测试目录下的 `report.json` / PNG 不纳入 Git。

准备其他宿主时，不复制 HVF 配置到 Windows/Linux；依据平台指南选择对应加速器，并验证
WinPE 的启动路径、显示和磁盘驱动。出现 UEFI shell 或无法找到启动设备时，先检查 ISO
是否包含可用的 ARM64 EFI 启动文件；这是引导未完成，不是 Agent 点击成功。

## 10. 记录验收结果

```text
日期／宿主系统／架构：
Git SHA／Python／QEMU／加速器：
ISO 或镜像版本／摘要：
CONFIG_ROOT／runtime_root／daemon PID／generation：
VM_ID／instance_id／客户机画面尺寸：
MCP 客户端／模型／是否实际收到图片：
点击目标／图片坐标／request_id／前后 action_seq：
输入回执／客户机可见结果／GUI 同步结果：
人工接管／BUSY／旧 frame／释放结果：
正常关机／代理断开／GUI 退出结果：
耗时口径／样本数量／截图和 report.json 路径：
通过、失败或未执行的项目／原因：
```

自动回归命令：

```bash
uv run pytest tests/integration/test_desktop.py tests/unit/test_display.py
uv run python scripts/check_docs.py
npm --prefix desktop test
npm --prefix desktop run test:e2e
```

Electron 测试使用 fake QEMU、真实 stdio MCP 和本机 RFB 连接，包含“GUI 创建 → MCP
观察与点击 → GUI 状态更新”。其成功不能替代以上 WinPE 或其他真实客户机的人工视觉验收。
