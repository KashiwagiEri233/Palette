# Palette 运行时目录（`.runtime/`）

[English](README.md) | **简体中文**

Palette 在运行期间创建、修改或清理的一切受管文件均集中在本目录（可通过 `palette.toml` 中的 `runtime_root` 修改位置）之内，绝不散落到仓库其他路径：

```
.runtime/
├── .operator/                       # 本地运维 IPC 发现端点 (endpoint.json) 与单实例锁 (owner.lock)
├── definitions/<vm_id>/             # Palette Desktop 创建的持久虚拟机定义、受管可写层与 UEFI 变量
├── vms/<vm_id>/<instance_id>/       # 单次运行实例的可写 overlay.qcow2、临时截图暂存 (frames/)、日志与 manifest.json
└── audit/palette.jsonl              # 滚动审计日志（仅含动作类型、长度、时序与结果元数据）
```

## 安全收敛与清理说明

* **`PathGuard` 收敛边界**：本目录已被 Git 忽略，同时也是服务端 `PathGuard` 的强制安全边界。服务解析出的任何受管路径在读取、写入或删除前，均会经过词法边界与符号链接/重解析点（symlink / junction / reparse point）校验，拒绝触碰本目录之外的任何文件。
* **手工清理注意事项**：当没有任何虚拟机或后台进程（`daemon` / `serve`）正在运行时，手工删除临时实例目录是安全的——下次启动会自动从只读基础镜像重建全新 `overlay.qcow2`（注意：`definitions/` 下保存着桌面端创建的持久虚拟机定义与持久磁盘，删除该子目录会移除这些持久虚拟机）。**严禁在虚拟机运行期间手工删除本目录**，因为正在运行的 QEMU 进程正持有对应的磁盘与管道句柄。
