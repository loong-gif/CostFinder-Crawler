# 自部署 Firecrawl on GCP（Docker Compose）

把 Firecrawl 跑在 GCP 上的 Docker Compose 方案，用于替代云端 Firecrawl，彻底绕开 credits 不足问题。

## 目标

- 在 GCP 上跑一个 Firecrawl 自部署实例
- 启用 `/v2/monitor` API（PR #3470 已合并，自部署支持）
- judging 走自己的 OpenAI key，不消耗 Firecrawl 云端 credits
- 让本项目的 `scripts/firecrawl_monitor*.py` 通过 `FIRECRAWL_API_URL` env 指向自部署实例

## 资源需求

Firecrawl 自部署官方推荐：

| 组件 | CPU | RAM |
|---|---|---|
| API + worker | 4 cores | 8 GB |
| Playwright-service | 2 cores | 4 GB |
| Redis / RabbitMQ / Postgres | 1 core | 1-2 GB |
| **合计** | **4+ cores** | **12+ GB** |

GCP 推荐机型（按性价比排序）：

| 机型 | vCPU | RAM | us-east1 月费（按需） | 评价 |
|---|---|---|---|---|
| `e2-standard-4` | 4 | 16 GB | ~$80 | 推荐，留余量给 Playwright |
| `e2-standard-2` | 2 | 8 GB | ~$40 | 最小可跑，并发抓取会卡 |
| `e2-custom-4-8192` | 4 | 8 GB | ~$55 | 中间档 |
| `n2-standard-4` | 4 | 16 GB | ~$140 | 高性能但贵，非必需 |

**建议 `e2-standard-4` + us-east1**（南卡州，美国出口 IP，抓美国 medspa 站命中率高）。

## 部署步骤

### 1. 创建 GCP 项目（若没有）

```bash
gcloud projects create costfinder-firecrawl --name="CostFinder Firecrawl"
gcloud config set project costfinder-firecrawl
gcloud beta billing projects link costfinder-firecrawl --billing-account=<你的BILLING_ACCOUNT_ID>
```

### 2. 启用 API

```bash
gcloud services enable compute.googleapis.com
```

### 3. 创建 VM

```bash
gcloud compute instances create firecrawl \
  --zone=us-east1-b \
  --machine-type=e2-standard-4 \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=50GB \
  --boot-disk-type=pd-ssd \
  --tags=firecrawl,ssh-allowed
```

### 4. 配置防火墙

```bash
# 仅允许你的出口 IP 访问 3002 端口
MY_IP=$(curl -s https://api.ipify.org)
gcloud compute firewall-rules create allow-firecrawl-3002 \
  --network=default \
  --action=allow \
  --rules=tcp:3002 \
  --source-ranges=$MY_IP/32 \
  --target-tags=firecrawl

# SSH 端口
gcloud compute firewall-rules create allow-ssh \
  --network=default \
  --action=allow \
  --rules=tcp:22 \
  --source-ranges=$MY_IP/32 \
  --target-tags=firecrawl
```

### 5. SSH 进 VM

```bash
gcloud compute ssh firecrawl --zone=us-east1-b
```

### 6. 安装 Docker + Docker Compose

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg lsb-release git

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

sudo usermod -aG docker $USER
newgrp docker
```

### 7. 克隆 Firecrawl 并配置 .env

```bash
cd ~
git clone https://github.com/firecrawl/firecrawl.git
cd firecrawl
git checkout v2.0.0  # 选最新稳定 release，确认包含 PR #3470

cp .env.example .env
```

编辑 `.env`，关键项：

```env
# 基础
PORT=3002
HOST=0.0.0.0
USE_DB_AUTHENTICATION=false

# 队列鉴权（改强随机）
BULL_AUTH_KEY=<openssl rand -hex 32>

# LLM provider（judging 用，绕开 Firecrawl 云端 credits）
OPENAI_API_KEY=sk-你的key

# GCS diff 存储（可选；不配时 diff 内联返回）
# GCS_BUCKET_ID=firecrawl-diffs-costfinder
# GOOGLE_APPLICATION_CREDENTIALS=/home/$USER/gcs-key.json

# 自部署模式标志
SELF_HOSTED=true
```

生成 BULL_AUTH_KEY：

```bash
openssl rand -hex 32
```

### 8. 调整 docker-compose 资源限制

编辑 `~/firecrawl/docker-compose.yaml`，确认 Playwright 与 API 的 `mem_limit` 适配 `e2-standard-4`（16GB）：

```yaml
  playwright-service:
    mem_limit: 4G        # 默认即可
  api:
    mem_limit: 8G        # 默认即可
```

### 9. 启动

```bash
cd ~/firecrawl
docker compose up -d
docker compose ps       # 所有服务应为 healthy
docker compose logs -f api   # 看 API 启动日志
```

### 10. Smoke test

```bash
# 在 VM 上
curl -X POST http://localhost:3002/v2/scrape \
  -H "Content-Type: application/json" \
  -d '{"url":"https://example.com"}'
```

应返回 markdown 内容。

### 11. 测试 monitor API

```bash
# 在 VM 上
curl -X POST http://localhost:3002/v2/monitor \
  -H "Content-Type: application/json" \
  -d '{
    "name": "self-hosted smoke test",
    "schedule": {"text": "daily", "timezone": "UTC"},
    "targets": [{"type": "scrape", "urls": ["https://example.com"]}]
  }'
```

记录返回的 `id`，等 1-2 分钟后：

```bash
curl http://localhost:3002/v2/monitor/<id>/checks
```

应看到至少一条 `completed` 状态的 check，含 `summary` 与 `pages`。

### 12. 配置反向代理 + HTTPS（生产建议）

自部署的 3002 端口无鉴权，公网暴露有风险。两种方案：

**方案 A（推荐）：Cloud Endpoints + IAP**

```bash
# 用 Google Cloud IAP 包住 3002 端口，仅授权用户可通过 IAP 访问
gcloud compute instances stop firecrawl --zone=us-east1-b
gcloud compute instances add-metadata firecrawl \
  --zone=us-east1-b \
  --metadata=proxy-mode=serve
# 配置 IAP（详见 https://cloud.google.com/iap/docs）
```

**方案 B（简单）：Nginx + Bearer token**

在 VM 上装 nginx，监听 443，反向代理到 127.0.0.1:3002，加 Bearer token 校验。把 token 当 `FIRECRAWL_API_KEY` 用。

### 13. 配置持久磁盘（可选，但建议）

PostgreSQL 容器数据默认在 Docker volume，VM 重启数据不丢但 VM 销毁会丢。挂 persistent disk：

```bash
# 在 GCP 创建 50GB 数据盘
gcloud compute disks create firecrawl-data --size=50GB --zone=us-east1-b --type=pd-ssd
gcloud compute instances attach-disk firecrawl --disk=firecrawl-data --zone=us-east1-b --device-name=data

# 在 VM 上格式化挂载
sudo mkdir -p /mnt/data
sudo mkfs.ext4 /dev/disk/by-id/google-data
sudo mount /dev/disk/by-id/google-data /mnt/data
echo '/dev/disk/by-id/google-data /mnt/data ext4 defaults 0 0' | sudo tee -a /etc/fstab
sudo chown -R $USER:$USER /mnt/data
```

编辑 `~/firecrawl/docker-compose.yaml`，把 nuq-postgres 的 volume 改为挂 `/mnt/data/pg`。

## 接入本项目

### 改 .env

在 CostFinder-Crawler 项目的 `.env`：

```diff
- FIRECRAWL_API_KEY=fc-381d005b2e8644bda6fe2b7aff7cd670
+ FIRECRAWL_API_URL=http://<VM 外网 IP>:3002
+ FIRECRAWL_API_KEY=<Bearer token 或留空>
```

### 代码改动（3 处）

让 SDK v2 client 支持 `FIRECRAWL_API_URL` env（v2 默认硬编码云端，不读 env）：

1. `scripts/firecrawl_monitor_poll.py:71`：`Firecrawl(api_key=api_key, api_url=os.getenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev"))`
2. `scripts/firecrawl_monitor.py:81`：同上
3. `scripts/analyze_all_monitors.py:104`：同上

详见 [firecrawl 自部署迁移 checklist](../.cursor/plans/firecrawl_自部署迁移_checklist_254fb34b.plan.md) 阶段 4。

### 验证

```bash
# 从本机（CostFinder-Crawler 项目目录）
python scripts/firecrawl_monitor.py list
# 应返回空（自部署新实例无 monitor）

python scripts/firecrawl_monitor.py create --domain acemedspa.com --max-urls 1
python scripts/firecrawl_monitor.py checks --monitor-id <新建的id>
# 应看到 completed check
```

## 运维

### 日常命令（SSH 进 VM 后）

```bash
cd ~/firecrawl
docker compose ps                      # 服务状态
docker compose logs -f --tail=100 api  # 实时日志
docker compose restart api             # 重启 API
docker compose down                    # 停止全部
docker compose pull && docker compose up -d  # 升级镜像
```

### 升级 Firecrawl

```bash
cd ~/firecrawl
git fetch --tags
git checkout <新版本>
docker compose build
docker compose up -d
```

### 监控

- GCP Console → VM 详情 → CPU/内存/磁盘图表
- 建议挂 Stackdriver agent 或简单 cron 跑 `docker compose ps | grep -v healthy | mail -s "Firecrawl unhealthy" you@x.com`

### 备份

PostgreSQL 数据：

```bash
docker compose exec nuq-postgres pg_dump -U firecrawl firecrawl > /mnt/data/backup-$(date +%Y%m%d).sql
```

## 成本估算

- `e2-standard-4` us-east1 按需：~$80/月
- 50GB pd-ssd：~$8/月
- 出口流量：~$0.12/GB（你的 monitor 每天抓 134 域名 × 2 URL × ~500KB = ~130MB/天 ≈ 4GB/月 ≈ $0.5/月）
- OpenAI judging LLM：取决于判定量，134 域名 × 1 check/天 × ~500 tokens × $5/M tokens ≈ $0.3/月
- **合计：~$90/月**

对比 Firecrawl 云端：你之前的 credits 消耗速率决定，但若要支持 134 monitor × daily check，云端至少要 Growth ($50/月) 或 Scale ($500/月)。

## 回滚

不删云端 Firecrawl 账户和 monitor，回滚只需：

1. 注释 `.env` 里的 `FIRECRAWL_API_URL` 行
2. 代码自动 fallback 到 `https://api.firecrawl.dev`
3. 充值云端 credits 后立即恢复

## 常见问题

### `docker compose up` 后 api 容器立即退出

看 `docker compose logs api`。常见原因：
- Redis/RabbitMQ 没起来 → `docker compose up -d redis rabbitmq` 先起，等 10s 再起 api
- `.env` 缺 `PORT`/`HOST` → 补上

### monitor check 一直 `failed`

- 确认 `OPENAI_API_KEY` 已配（judging 没法调 LLM 会失败）
- 确认 RabbitMQ healthy：`docker compose ps rabbitmq`
- 看 `docker compose logs api | grep monitor`

### Playwright OOM

- `e2-standard-2`（8GB）跑 Playwright 容易 OOM，升到 `e2-standard-4`
- 或在 docker-compose 降 `MAX_CONCURRENT_PLAYWRIGHT` 环境变量

### 抓美国站被反爬

- GCP us-east1 出口 IP 一般没问题
- 若被拦，在 Firecrawl `.env` 配 `PROXY=...` 走住宅代理

### 自部署 check 没有 `meaningfulChanges` / `confidence` 字段

- 确认 `OPENAI_API_KEY` 配了且有效
- 确认 Firecrawl 版本包含 PR #3470（v2.0.0+）
- 看日志确认 judging 调用没报错

## 参考

- [Firecrawl 自部署官方文档](https://docs.firecrawl.dev/contributing/self-host)
- [PR #3470 monitor API orchestration](https://github.com/firecrawl/firecrawl/pull/3470)
- [docker-compose.yaml](https://github.com/firecrawl/firecrawl/blob/main/docker-compose.yaml)
- [Self-Host Firecrawl with Docker 指南](https://use-apify.com/blog/self-host-firecrawl-docker)
