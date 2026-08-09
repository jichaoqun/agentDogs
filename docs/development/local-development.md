# 本地开发说明

当前仓库尚未建立 V2 实现。本文件先定义开发环境应提供的稳定入口，具体命令在 M0 实现时补齐，不能长期保留占位。

## 目标命令

```text
install     安装锁定依赖
migrate     创建或升级本地 SQLite
api         启动后端 API
gui         启动桌面前端开发环境
test        运行快速测试
test-all    运行 SQLite、并发和故障测试
lint        执行格式与静态检查
```

## 本地数据

开发数据库、日志、blob 和 artifact 放在 `runtime/`，不得提交 Git。测试使用独立临时目录，不复用开发数据库。

需要真实模型的 smoke test 通过显式环境开关启用；默认测试不得消耗外部 API。

## M0 完成时必须补充

- Python 和 Node 版本；
- 包管理器与 lockfile；
- 实际启动命令；
- migration 命令；
- 环境变量说明；
- Windows 开发注意事项；
- 常见错误排查。

