# 客户机基础镜像目录（`images/`）

[English](README.md) | **简体中文**

本目录用于存放 `profiles/*.toml` 所指向的**只读客户机桌面基础镜像**（例如 `debian-13-xfce-x86_64.qcow2` 或 `debian-13-xfce-aarch64.qcow2`）。

## 1. 在 Profile 中引用镜像

将构建或准备好的只读镜像放入本目录后，在对应的 `profiles/*.toml` 中配置相对路径（相对于包含 `palette.toml` 的配置根目录解析）并锁定 SHA-256 摘要：

```toml
[images]
base = "images/debian-13-xfce-x86_64.qcow2"
format = "qcow2"
sha256 = "<64 位十六进制 SHA-256 摘要>"
size_gib = 24
```

* **`x86_64` 与 `aarch64` 镜像不可混用**：`aarch64` Profile（如 `macos-aarch64`、`linux-aarch64`、`tcg-aarch64`）必须使用独立构建的 `images/debian-13-xfce-aarch64.qcow2`（含匹配的 AArch64 UEFI 引导与 VirtIO 驱动）。
* **为什么镜像文件被 Git 忽略**：客户机磁盘镜像体积庞大且带有操作系统发行版许可义务，因此本目录下的镜像文件均被 `.gitignore` 排除。提交到版本库的是 Profile 中的路径、格式与 `sha256` 摘要记录——正是这三项把“某个路径上的文件”锁定为“该 Profile 唯一授权启动的基础镜像”。

## 2. 校验与只读保护机制

1. **构建与固化**：标准镜像构建流程参见[客户机模板构建指南](../docs/guest-template.zh-CN.md)。
2. **环境与哈希自检**：放好镜像后运行以下命令：
   ```bash
   uv run palette doctor --hash-base
   ```
   `palette doctor` 会调用 `qemu-img info` 探测文件的真实容器格式（不盲信文件扩展名），核对声明的格式，并在配置了 `sha256` 时校验全文件哈希。
3. **逐字节只读不变量**：存放在本目录中的基础镜像**永远不会被写入或修改**。无论是 CLI 启动的临时实例还是 Palette Desktop 创建的持久虚拟机，所有磁盘写入均发生在 `.runtime/` 下的独立 `qcow2` overlay 层中；执行重置（`vm reset` 或桌面端“恢复初始状态”）只会替换 `.runtime/` 下的可写层，绝不触碰基础镜像。
