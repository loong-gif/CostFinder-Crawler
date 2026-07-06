# 自部署 Firecrawl on Windows VPS（WSL2 + Docker Compose）

在 Liquid Web Windows Server 2025 VPS 的 WSL2 Ubuntu 里部署 Firecrawl，替代云端 API。

## 前置

- WSL2 Ubuntu 已安装且能进 shell
- 管理员 PowerShell 可用（bootstrap 会调 `powershell.exe` 做 portproxy + 防火墙）
- `OPENAI_API_KEY`（judging 必填）
- 开发机出口 IPv4（本机 `curl -4 https://api.ipify.org`）

## 一键部署（在 VPS 的 WSL 里）

若已 clone 本项目：

```bash
cd /path/to/CostFinder-Crawler
OPENAI_API_KEY='sk-你的key' ALLOWED_CLIENT_IP='58.44.21.62' bash scripts/bootstrap_firecrawl_wsl.sh
```

脚本会自动：

1. 安装 Docker CE + compose plugin
2. `git clone` Firecrawl `v2.11.9` 并写 `.env`
3. `docker compose up -d --build`
4. 本地 smoke test `/v2/scrape` + `/v2/monitor`
5. Windows `netsh portproxy` 把 `0.0.0.0:3002` 转到 WSL IP
6. Windows 防火墙仅放行 `ALLOWED_CLIENT_IP` 访问 3002

## 接入 CostFinder-Crawler

`.env`：

```env
FIRECRAWL_API_URL=http://72.52.161.65:3002
FIRECRAWL_API_KEY=self-hosted
```

验证：

```bash
python scripts/firecrawl_monitor.py list
python scripts/firecrawl_monitor.py create --domain example.com --max-urls 1
```

## Monitor API 说明（v2.11.9 自部署）

官方 `USE_DB_AUTHENTICATION=false` 时 monitor 无法连库；设为 `true` 又需要完整 Supabase schema。

当前方案（`scripts/fix_firecrawl_selfhosted_monitor.sh`）：

- 保持 `USE_DB_AUTHENTICATION=false`（scrape 免鉴权）
- 通过 `DATABASE_URL` + 挂载补丁 JS，让 monitor 使用 `nuq-postgres` 里的表
- 在 Postgres 里建 monitor 相关表 + 最小 `monitoring_claim_due_monitors` RPC

若 API 升级或 `docker compose build api` 后 monitor 失效，在 WSL 重跑：

```bash
bash scripts/fix_firecrawl_selfhosted_monitor.sh
```

**还需**：在 `~/firecrawl/.env` 写入 `OPENAI_API_KEY`（judging 用），然后 `docker compose restart api`。

## 运维

```bash
cd ~/firecrawl
docker compose ps
docker compose logs -f --tail=100 api
```

WSL 重启后 WSL IP 会变，重新跑：

```bash
ALLOWED_CLIENT_IP='58.44.21.62' bash scripts/bootstrap_firecrawl_wsl.sh
```

（已装 Docker/Firecrawl 时会跳过安装，只更新 portproxy。）

## 相关脚本

- [scripts/bootstrap_firecrawl_wsl.sh](../scripts/bootstrap_firecrawl_wsl.sh) — WSL 主部署
- [scripts/setup_firecrawl_windows.ps1](../scripts/setup_firecrawl_windows.ps1) — 仅 Windows 网络
- [scripts/add_thinkbook_ssh_key.ps1](../scripts/add_thinkbook_ssh_key.ps1) — 添加 thinkbook SSH 公钥
- [scripts/fix_firecrawl_selfhosted_monitor.sh](../scripts/fix_firecrawl_selfhosted_monitor.sh) — monitor 补丁 + DB schema
