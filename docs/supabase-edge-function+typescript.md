# CostFinder 内部 Supabase Edge Function 快速运行手册

本手册用于在 CostFinder 中新增由仓库内 **Python 服务端任务** 调用的 Supabase Edge Function。函数运行在 Deno/TypeScript 环境，入口固定为 `POST /functions/v1/<function_name>`，并使用独立共享密钥 `x-internal-function-secret` 鉴权。

> 当前仓库的正式数据处理路径是 Python 脚本、受控 schema 和可审计 SQL。价格补全、促销原价回填、历史修复和其他批量数据修复**不得**改成 Edge Function 或数据库触发器；继续使用现有的预览、人工复核和事务 SQL 流程。

## 1. 何时适用

仅在下列情况使用 Edge Function：

- 需要一个明确的 HTTP/Webhook 入口，并且需要立即返回结果；
- 调用方是本仓库中受控的服务端 Python 任务；
- 请求契约、小范围读写和失败处理可以在一次函数调用中清楚定义。

下列情况不适用：批量回填、长期任务、需要人工审核的数据修复、以数据库触发器串联的异步写入，或需要多个表保持原子一致的复杂写入。

多表原子写入必须先完成 migration/RPC 设计和审查；函数不可自行拼装多次独立写入来模拟事务。

## 2. 创建前检查清单

在创建代码前，在 issue、设计说明或 PR 描述中写清楚：

1. 函数名、唯一调用方和业务目的；
2. `POST` 请求与成功/失败响应的 JSON 契约；
3. 幂等键（如 `request_id`）及重复请求的处理方式；
4. 读取/写入的真实表、列和 RLS/`service_role` 需求；
5. 超时、下游失败、重试和人工处理路径；
6. 是否需要 migration、RPC 或对 `schema/live_schema_snapshot.json` 的更新。

不得为尚未在 live schema 中存在的列编写请求体或写入逻辑。若调用需要用户浏览器、Supabase 用户 JWT 或第三方 webhook 签名，本手册不适用，应单独设计鉴权模型。

## 3. 创建、本地验证与鉴权约定

- 先以 `supabase functions <new|serve|deploy> --help` 确认当前 CLI 参数；不要为新增函数重新 `supabase init`。
- `supabase functions new <name>`（小写连字符、单一职责），入口 `supabase/functions/<name>/index.ts`；Deno 依赖固定版本。
- 函数仅接受 `POST`（否则 405），以 `x-internal-function-secret` 对照 `FUNCTION_INTERNAL_SECRET` 鉴权：缺失 401、错误 403、非法 JSON 400、合法请求 200。完整 TypeScript 模板见 `git show c0a2d35:"docs/supabase-edge-function+typescript.md"`。
- 本地：`.env.local`（已忽略）写开发密钥，`supabase functions serve <name> --env-file .env.local --no-verify-jwt`，用 curl 验证上述四个场景，并为其写 Deno 测试。
- Python 调用方从环境变量读取共享密钥，请求设置超时（模板为 15 秒）。

## 4. 远端密钥与部署

内部共享密钥与 Supabase 服务端密钥是不同的值。生成和设置远端共享密钥时，避免在命令历史、终端输出或 PR 中出现值：

```bash
read -rs FUNCTION_SECRET
supabase secrets set FUNCTION_INTERNAL_SECRET="$FUNCTION_SECRET"
unset FUNCTION_SECRET
```

此调用模型由函数自身的 `x-internal-function-secret` 鉴权，因此部署时必须显式关闭网关 JWT 验证；先通过帮助命令确认当前 CLI 的对应参数：

```bash
supabase functions deploy --help
supabase functions deploy example-internal --no-verify-jwt
```

部署前必须完成函数 Deno 测试和 `python -m pytest`。部署后，用**非生产测试数据**发送一次带共享密钥的请求，并在 Dashboard 的 Edge Functions Logs 或 CLI 日志中确认：请求成功、没有密钥或完整敏感请求体、没有未处理异常。

## 5. 写入、故障与回滚

- 写入只使用已存在的 schema 表和列；schema 变更先创建 migration，再更新 `schema/live_schema_snapshot.json` 和测试。
- 每次写入都必须基于幂等键；在重试前读取既有结果或依赖受审查的数据库约束/RPC。
- 调用数据库失败时返回通用 5xx，不向调用方透露 SQL、服务端错误详情或密钥；日志只保留必要的请求标识和错误类别。
- 不要从数据库触发器调用函数，也不要在函数中实施价格补全、历史回填或其他批量修复。

Supabase CLI 没有可替代 Git 历史的“远端函数版本回滚”流程。发生故障时，恢复最后一个已知可用提交中的函数与其相关 Deno 配置，重新运行测试，然后重新部署：

```bash
git diff <known-good-commit>..HEAD -- supabase/functions/example-internal
git restore --source <known-good-commit> -- \
  supabase/functions/example-internal
deno test --allow-env supabase/functions/example-internal/index_test.ts
python -m pytest
supabase functions deploy example-internal --no-verify-jwt
```

将回滚作为新的 Git 提交保留，并在部署后再次以非生产测试数据验证。若故障涉及 schema、RPC 或共享密钥泄露，停止重试，先完成迁移/密钥轮换和影响评估。
