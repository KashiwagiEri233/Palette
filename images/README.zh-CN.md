# 客户机镜像

[English](README.md) | **简体中文**

把只读桌面模板放在这里，例如 `debian-13-xfce-desktop.qcow2`，然后让 profile 指向它：

```toml
[images]
base = "G:/Workspace/Palette/images/debian-13-xfce-desktop.qcow2"
format = "qcow2"
sha256 = "<摘要>"
```

本目录中的文件被 Git 忽略：镜像体积大、与宿主强相关，通常还附带发行版的许可义务。被提交的是
profile 对这份镜像的记录——路径、格式和 SHA-256——正是它把“那个路径上恰好放着的文件”变成
“这个 profile 被允许运行的镜像”。

构建方法见 `../docs/guest-template.zh-CN.md`。放好镜像后运行 `uv run palette doctor`：它会校验
扩展名与声明的格式是否一致，并在固定了摘要时核对摘要。

这里的文件永远不会被修改。每次运行都会在 `.runtime/` 下拿到自己的一份可写 qcow2 overlay；重置
是丢弃那份 overlay，而不是触碰基础镜像。
