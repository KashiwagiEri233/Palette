# 客户机模板

[English](guest-template.md) | **简体中文**

如何构建 `profiles/*.toml` 所指向的只读桌面镜像。本项目不分发镜像：镜像是体积庞大、带许可
义务、且与宿主强相关的产物，计划要求运维记录它从哪来、里面有什么。

## 模板必须满足什么

| 属性 | 取值 | 为什么固定 |
| --- | --- | --- |
| 发行版 | Debian 13 (trixie)，x86_64 | 计划的 P0 基线 |
| 桌面 | X11 上的 XFCE | 键盘与指针行为可预测；Wayland compositor 之间差异较大 |
| 分辨率 | 1280×800，100% 缩放 | 在客户机**内部**配置，而不是靠内核参数 |
| 键盘 | US 布局 | 严格文本模式假定 US；换一种布局会静默打错字 |
| 用户 | 普通非 root 用户，仅受控开发模板允许自动登录 | 计划禁止在可分享的模板里放宿主账号与生产凭据 |
| 网络 | 不需要 | profile 不提供网卡；模板不得要求联网 |
| 夹具 | `guest/fixtures/offline-workbench.html` | 让点击、拖拽、滚轮和输入都有可见的结果 |

## 构建步骤概览

1. 从可信介质安装，记录确切版本与介质的 SHA-256。
2. 安装桌面环境、浏览器、文本编辑器，以及上面那个夹具页面（例如放到
   `~/palette-fixture/index.html`）。
3. 固定显示为 1280×800、100% 缩放、US 布局。用客户机内的 `xrandr` 验证，不要从宿主机验证。
4. 关掉一切会自行改变画面的东西：屏幕熄灭、锁屏、通知弹窗、首次运行引导、自动更新。
5. 用普通用户身份操作。**不要**把宿主凭据、cookies、SSH 私钥或真实文档放进模板。
6. 构建期间更新系统，然后关闭自动升级：任务进行到一半时升级软件包，会让桌面在 Agent 眼皮底下
   发生变化。
7. 正常关机，使镜像文件系统一致，然后转换并计算摘要：

   ```sh
   qemu-img convert -O qcow2 template.raw debian-13-xfce-desktop.qcow2
   qemu-img info debian-13-xfce-desktop.qcow2
   sha256sum debian-13-xfce-desktop.qcow2
   ```

8. 把摘要写进 profile：

   ```toml
   [images]
   base = "G:/Workspace/Palette/images/debian-13-xfce-desktop.qcow2"
   format = "qcow2"
   sha256 = "<上面打印出的摘要>"
   ```

固定摘要的作用，是把“那个路径上恰好放着的文件”变成“这个 profile 被允许运行的镜像”。没有它，
`palette doctor` 会把镜像报告为未经校验。

## 验证模板

```bash
uv run palette doctor --profile default --json     # 每一项检查都应当通过
uv run palette vm start --vm-id desktop
uv run palette vm status --vm-id desktop
```

`desktop_ready` 会被刻意报成 `null`：一个有效的显示缓冲区可能是固件界面，也可能是登录提示，
而服务不会去猜。请在截图里自己确认桌面已经起来。

## 自动化验收

设置 `PALETTE_E2E=1`、`PALETTE_E2E_QEMU` 和 `PALETTE_E2E_IMAGE` 之后：

```bash
uv run pytest tests/e2e -v
```

这套测试就是计划的实机门槛：20 次启停循环、基础镜像摘要不变、一次真正到达客户机的点击，以及
在真实硬件上被拒绝的过期 frame。它默认跳过——而被跳过的测试不构成任何证据。

## 客户机增强服务

`guest/agent/` 是客户机内工具服务将来的所在位置。那里目前刻意什么都没有实现：计划把增强路线
保持为一份已定义契约加一个 disabled 后端，这样以后启用它就是实现一个传输层，而不是改动架构。
该服务必须满足的契约见 `guest/agent/README.md`。
