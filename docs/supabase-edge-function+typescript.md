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

## 3. 初始化与目录

先检查 CLI 的当前参数，CLI 版本变化时以帮助输出为准：

```bash
supabase --version
supabase functions new --help
supabase functions serve --help
supabase functions deploy --help
```

本仓库已有 `supabase/` 目录。只有在负责人确认需要建立项目级 Supabase 配置时，才运行 `supabase init` 并将生成的配置作为独立、可审查的改动提交；不要因为新增函数而盲目重新初始化。

登录并关联目标项目（项目引用不得写入代码或文档中的真实值）：

```bash
supabase login
supabase link --project-ref <project-ref>
```

创建函数。下例使用占位名称 `example-internal`；实际名称应为小写连字符格式并表达单一职责：

```bash
supabase functions new example-internal
```

函数入口位于：

```text
supabase/functions/example-internal/index.ts
```

新增 Deno 依赖时，在项目级 Deno 配置中固定版本并随函数一同提交。不要使用未固定版本的 URL 导入，也不要把 Node `package.json` 当作函数依赖清单。

## 4. 可复制的内部函数模板

将生成的 `index.ts` 替换为下列最小模板，再把 `request_id` 和 `data` 换成已审查的真实请求契约。模板不读写数据库，避免把未经审查的示例变成生产数据逻辑。

```ts
type ErrorCode = "bad_request" | "forbidden" | "method_not_allowed" | "unauthorized" | "internal_error";

type ResponseBody =
  | { ok: true; data: { request_id: string } }
  | { ok: false; error: { code: ErrorCode; message: string } };

const JSON_HEADERS = { "content-type": "application/json; charset=utf-8" };

function json(body: ResponseBody, status: number, headers: HeadersInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...JSON_HEADERS, ...headers },
  });
}

function error(code: ErrorCode, message: string, status: number): Response {
  return json({ ok: false, error: { code, message } }, status);
}

function isRequestBody(value: unknown): value is { request_id: string } {
  return Boolean(
    value &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      typeof (value as { request_id?: unknown }).request_id === "string" &&
      (value as { request_id: string }).request_id.trim().length > 0,
  );
}

export async function handleRequest(request: Request): Promise<Response> {
  if (request.method !== "POST") {
    return json(
      { ok: false, error: { code: "method_not_allowed", message: "POST is required" } },
      405,
      { allow: "POST" },
    );
  }

  const expectedSecret = Deno.env.get("FUNCTION_INTERNAL_SECRET");
  if (!expectedSecret) {
    console.error("FUNCTION_INTERNAL_SECRET is not configured");
    return error("internal_error", "Function is not configured", 500);
  }

  const suppliedSecret = request.headers.get("x-internal-function-secret");
  if (!suppliedSecret) {
    return error("unauthorized", "Missing internal function credential", 401);
  }
  if (suppliedSecret !== expectedSecret) {
    return error("forbidden", "Invalid internal function credential", 403);
  }

  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return error("bad_request", "Request body must be valid JSON", 400);
  }
  if (!isRequestBody(body)) {
    return error("bad_request", "request_id must be a non-empty string", 400);
  }

  try {
    // Add reviewed, idempotent business work here. Do not log secrets or the full body.
    return json({ ok: true, data: { request_id: body.request_id } }, 200);
  } catch (cause) {
    console.error("example-internal failed", {
      request_id: body.request_id,
      error: cause instanceof Error ? cause.name : "unknown",
    });
    return error("internal_error", "Internal function failure", 500);
  }
}

if (import.meta.main) {
  Deno.serve(handleRequest);
}
```

约定如下：

- 成功响应为 `{ "ok": true, "data": ... }`，失败响应为 `{ "ok": false, "error": { "code", "message" } }`；
- 缺少共享密钥返回 401，错误共享密钥返回 403，非法 JSON/契约返回 400；
- 不记录共享密钥、`Authorization`、完整请求体或包含个人/业务敏感信息的字段；
- 不能只检查 `Bearer` 前缀，不能信任 `x-forwarded-for`/`host` 作为访问控制，也不能用 IP 白名单代替鉴权；
- 需要 Supabase 数据访问时，函数内从环境变量读取服务端密钥；绝不把 `SUPABASE_SERVICE_ROLE_KEY` 交给调用方、浏览器、日志、迁移 SQL 或仓库文件。

## 5. 本地配置、测试与 Python 调用

`.env.local` 已被仓库忽略。创建本地开发值，绝不提交真实密钥：

```dotenv
FUNCTION_INTERNAL_SECRET=replace-with-a-local-development-secret
```

启动本地 Supabase 和函数。共享密钥模型不使用网关 JWT，因此本地运行同样要显式关闭 JWT 验证，确保 401/403 来自函数模板而不是网关；先以 `supabase functions serve --help` 确认可用参数：

```bash
supabase start
supabase functions serve example-internal --env-file .env.local --no-verify-jwt
```

在另一个终端设置临时变量并验证四个必须场景：

```bash
FUNCTION_SECRET='replace-with-a-local-development-secret'

# 401：缺少共享密钥
curl -i -X POST http://localhost:54321/functions/v1/example-internal \
  -H 'content-type: application/json' \
  --data '{"request_id":"local-001"}'

# 403：错误共享密钥
curl -i -X POST http://localhost:54321/functions/v1/example-internal \
  -H 'content-type: application/json' \
  -H 'x-internal-function-secret: wrong-secret' \
  --data '{"request_id":"local-002"}'

# 400：非法 JSON
curl -i -X POST http://localhost:54321/functions/v1/example-internal \
  -H 'content-type: application/json' \
  -H "x-internal-function-secret: $FUNCTION_SECRET" \
  --data '{not-json}'

# 200：合法请求
curl -i -X POST http://localhost:54321/functions/v1/example-internal \
  -H 'content-type: application/json' \
  -H "x-internal-function-secret: $FUNCTION_SECRET" \
  --data '{"request_id":"local-004"}'
```

Python 调用方只从其服务端环境读取共享密钥：

```python
import os
import requests

response = requests.post(
    "https://<project-ref>.supabase.co/functions/v1/example-internal",
    headers={
        "content-type": "application/json",
        "x-internal-function-secret": os.environ["FUNCTION_INTERNAL_SECRET"],
    },
    json={"request_id": "job-20260730-001"},
    timeout=15,
)
response.raise_for_status()
payload = response.json()
if not payload["ok"]:
    raise RuntimeError(payload["error"]["code"])
```

每个函数至少添加 Deno 单元测试，直接导入 `handleRequest`，并覆盖：缺少密钥、错误密钥、非法 JSON、合法请求、下游 Supabase 失败；有写入时还要覆盖相同 `request_id` 的幂等行为。

下列测试可作为上面最小模板的 `index_test.ts` 起点。测试密钥仅为固定测试值，不能复用任何本地或远端密钥：

```ts
import { handleRequest } from "./index.ts";

const TEST_SECRET = "test-internal-function-secret";

function request(body: string, secret?: string): Request {
  const headers = new Headers({ "content-type": "application/json" });
  if (secret) headers.set("x-internal-function-secret", secret);
  return new Request("http://localhost/functions/v1/example-internal", {
    method: "POST",
    headers,
    body,
  });
}

async function withTestSecret(run: () => Promise<void>): Promise<void> {
  const previous = Deno.env.get("FUNCTION_INTERNAL_SECRET");
  Deno.env.set("FUNCTION_INTERNAL_SECRET", TEST_SECRET);
  try {
    await run();
  } finally {
    if (previous === undefined) Deno.env.delete("FUNCTION_INTERNAL_SECRET");
    else Deno.env.set("FUNCTION_INTERNAL_SECRET", previous);
  }
}

Deno.test("rejects a request without a shared secret", async () => {
  await withTestSecret(async () => {
    const response = await handleRequest(request('{"request_id":"test-001"}'));
    if (response.status !== 401) throw new Error(`expected 401, got ${response.status}`);
  });
});

Deno.test("rejects an incorrect shared secret", async () => {
  await withTestSecret(async () => {
    const response = await handleRequest(request('{"request_id":"test-002"}', "wrong-secret"));
    if (response.status !== 403) throw new Error(`expected 403, got ${response.status}`);
  });
});

Deno.test("rejects invalid JSON", async () => {
  await withTestSecret(async () => {
    const response = await handleRequest(request("{not-json}", TEST_SECRET));
    if (response.status !== 400) throw new Error(`expected 400, got ${response.status}`);
  });
});

Deno.test("accepts a valid internal request", async () => {
  await withTestSecret(async () => {
    const response = await handleRequest(request('{"request_id":"test-004"}', TEST_SECRET));
    const body = await response.json();
    if (response.status !== 200 || body.ok !== true || body.data.request_id !== "test-004") {
      throw new Error("valid request was not accepted");
    }
  });
});
```

在函数引入 Supabase、HTTP 或其他下游依赖时，不得调用真实远端服务来替代单元测试；将下游调用封装为可注入依赖，并增加返回 5xx 且不泄露内部错误详情的失败测试。

```bash
deno test --allow-env supabase/functions/example-internal/index_test.ts
python -m pytest
```

## 6. 远端密钥与部署

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

## 7. 写入、故障与回滚

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
