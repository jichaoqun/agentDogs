# 命令沙箱

状态：`planned`

目标里程碑：`M5`

## 当前范围

为测试、代码执行和数据分析提供 Windows 原生 Sandbox Broker，不依赖 Docker。默认无网络，只挂载明确的只读路径、staging 写目录和必要运行时。

## 实现重点

- AppContainer/LPAC 或可用的原生 sandbox adapter；
- Job Object 管理进程树、CPU、内存、进程数和 timeout；
- 命令以参数数组执行，不拼 shell 字符串；
- 环境变量白名单和 secret handle；
- stdout/stderr 限制与 blob 引用；
- 取消、租约、Operation Ledger 和 unknown 对账；
- 特性检测与 fail-closed fallback。

## 验收标准

- 子进程无法读取未授权用户文件；
- 默认无法联网；
- timeout/cancel 能终止进程树；
- 资源上限生效；
- 沙箱无法建立时拒绝执行，而不是静默降级到完整权限。

