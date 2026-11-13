# secrets 目录

此目录存放运行时密钥，已被 `.gitignore` 排除。每个文件一行纯文本，不要加引号。

| 文件 | 作用 | 必需 |
|---|---|---|
| `nd_botadmin_pass` | Navidrome 管理员账号密码，面板用它调用各线路 API | 是 |
| `panel_secret` | 面板会话签名密钥，随便一串长随机字符串 | 建议 |
| `stats_token` | 统计代理共享令牌，需与 `stats_agent.py` 的 STATS_TOKEN 一致 | 用播放页时必需 |
| `tg_token` | Telegram Bot Token，用于审核结果推送 | 可选 |
| `mongo_url` | Bot 的 MongoDB 连接串，用于账号绑定查询 | 可选 |

生成随机串：

```bash
openssl rand -hex 32 > secrets/panel_secret
```

权限收紧（容器内可读即可）：

```bash
chmod 600 secrets/*
```
