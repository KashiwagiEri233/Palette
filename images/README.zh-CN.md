# 客户机镜像

[English](README.md) | **简体中文**

把只读桌面模板放在这里，例如 `debian-13-xfce-x86_64.qcow2`，然后让 profile 指向它：

```toml
[images]
base = "images/debian-13-xfce-x86_64.qcow2"
format = "qcow2"
sha256 = "<摘要>"
```

相对路径从配置根目录解析。AArch64 profile 使用独立构建的
`images/debian-13-xfce-aarch64.qcow2` 模板，包含匹配的 UEFI 与 virtio 驱动。

本目录中的文件被 Git 忽略：镜像体积大、与宿主强相关，通常还附带发行版的许可义务。被提交的是
profile 对这份镜像的记录——路径、格式和 SHA-256——正是它把“那个路径上恰好放着的文件”变成
“这个 profile 被允许运行的镜像”。

构建方法见[客户机模板指南](../docs/guest-template.zh-CN.md)。放好镜像后运行
`uv run palette doctor`：它通过 `qemu-img info` 探测实际格式，与声明进行比较，并在固定了
摘要时核对摘要。

这里的文件永远不会被修改。每次运行都会在 `.runtime/` 下拿到自己的一份可写 qcow2 overlay；
重置替换这份 overlay，不触碰基础镜像。停止的 overlay 是否留在磁盘上由保留策略决定。
