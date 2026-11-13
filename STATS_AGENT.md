# 统计代理 stats_agent.py

每台 Navidrome 服务器跑一份。只读打开本机 SQLite，返回聚合播放数据。
面板通过 HTTP 拉取，不直接碰数据库文件。

## 为什么需要它

Navidrome 的 HTTP API 不返回 `playCount`、`playlist`、每日趋势这类统计。
业务需要热门歌曲、活跃听众、播放趋势，只能从数据库算。但跨机器挂载数据库既麻烦又不安全，
于是每台本地跑一个只读小代理，面板聚合。

## 部署

```bash
mkdir -p /opt/nd-stats /etc/nd-stats
cp stats_agent.py /opt/nd-stats/
chmod 644 /opt/nd-stats/stats_agent.py
```

`/etc/nd-stats/env`：

```bash
STATS_TOKEN=换成一串长随机
ND_DB=/opt/music-stack/navidrome/data/navidrome.db
PORT=3999
BIND=0.0.0.0
CACHE_TTL=45
```

```bash
chmod 600 /etc/nd-stats/env
```

`/etc/systemd/system/nd-stats.service`：

```ini
[Unit]
Description=Navidrome stats agent
After=network.target

[Service]
Type=simple
DynamicUser=yes
EnvironmentFile=/etc/nd-stats/env
ExecStart=/usr/bin/python3 /opt/nd-stats/stats_agent.py
Restart=always
ProtectSystem=strict
ProtectHome=yes
NoNewPrivileges=yes
ReadOnlyPaths=/

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now nd-stats
```

## 环境变量

| 变量 | 说明 |
|---|---|
| `ND_DB` | SQLite 路径 |
| `PORT` | 默认 3999 |
| `BIND` | 监听地址。默认 127.0.0.1；跨机需 0.0.0.0 并配防火墙 |
| `STATS_TOKEN` | 必填，Bearer 鉴权 |
| `CACHE_TTL` | 缓存秒数，默认 45 |

## 接口

- `GET /health` 免鉴权，供探活
- `GET /stats` 需 `Authorization: Bearer <token>`

返回：totals（歌曲/专辑/艺人/用户/播放）、top_songs、top_albums、top_artists、
top_listeners、trend（30 天补零）、hourly（24 小时）、recent。

## 安全设计

- 只执行写死的聚合 SQL，**不接受任何外部参数拼接**
- `mode=ro` 只读打开，不写库
- 单台失败返回 `ok:false` 并附错误类型，不抛 500

## 部署陷阱（实测踩过）

- `DynamicUser=yes` 读不了 600 权限的程序文件 → 程序 `chmod 644`、目录 755
- `ExecStartPre` 里 `export` 无效 → 用 `EnvironmentFile=`
- 容器访问宿主 `127.0.0.1` 会指向容器自己 → 用 docker 网关 IP
- 跨机访问要同时放行**云厂商安全组**和本机防火墙，只开一边照样不通
