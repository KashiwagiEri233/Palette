# Palette 运行目录

[English](README.md) | **简体中文**

Palette 运行时创建的一切都在这里，不在别处：

```
vms/<vm_id>/<instance_id>/    实例 overlay、截图暂存、日志、manifest
audit/palette.jsonl           有界的审计元数据
```

本目录被 Git 忽略。它同时是收敛边界：服务解析出的每一条路径，在读取、写入或删除之前都会被检查
是否位于本目录之内；服务也拒绝跟随任何会指向外部的 junction 或 reparse point。

在没有 VM 运行时手工删掉本目录是安全的——下次启动会从只读基础镜像重建一份新的 overlay。**不要**
在 VM 运行时删除：正在运行的进程持有它的 overlay。

用 `palette.toml` 里的 `runtime_root` 可以改位置。
