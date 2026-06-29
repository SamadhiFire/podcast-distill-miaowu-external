# 历史回填

> 播客 / YouTube 一年历史内容回填系统。
> 当前阶段: C — 41 源完整盘点。

## 运行环境

- Python 3.10+
- 依赖: `pip install pyyaml`
- 工作目录: `D:\桌面\A自媒体账号\podcast-distill`

## 命令

```cmd
# 初始化 (可重复运行)
python -m scripts.backfill.cli init

# 查看状态
python -m scripts.backfill.cli status
```

## 目录结构

```
backfill/
├── config/          # 回填配置 (backfill.yaml, sources.yaml)
├── state/           # SQLite 状态库 + 进度快照 + 飞书映射
├── catalog/         # 节目总清单 + 来源审计
├── items/           # 单集字幕与摘要 (youtube/, xiaoyuzhou/)
├── daily/           # 日报视图 (YYYY/YYYY-MM/YYYY-MM-DD/)
├── temp/            # ASR 临时音频
├── failures/        # 失败队列
└── logs/            # 批处理日志
```

## 阶段

| 阶段 | 内容 | 状态 |
|------|------|------|
| A | 搭架子 | ✅ 已完成 |
| B | 两源试跑 | ✅ 已完成 (2026-06-29) |
| C | 41 源完整盘点 | ✅ 已完成 (2026-06-29) |
| D | 字幕批处理 | ⬜ 待开始 |
| E | 单集摘要与日报 | ⬜ 待开始 |
| F | 飞书灰度发布 | ⬜ 待开始 |

## 原则

1. 可中断、可恢复、不重复
2. 历史数据与现有日报完全隔离
3. 单集摘要只生成一次；日报只读摘要不重复喂全文
4. 每一步必须通过验收才能进入下一阶段
