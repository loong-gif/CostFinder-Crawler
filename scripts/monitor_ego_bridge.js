// monitor 渲染桥接：在 ego-lite 中打开一个页面，把本机 HTTP 请求转发为该页面的 CDP 命令。
// 由 Python 用 `ego-browser nodejs -e` 启动；__SPACE__、__PORT__ 由调用方替换。
// 每个渲染槽位一个任务空间，空间里只保留一个页面并反复使用，桥接被强制结束也不会留下多余的页面。
// ego-browser 的 console 输出要到脚本结束才送出，所以端口由调用方事先选定，调用方请求 {op: "ping"} 直到得到应答。
// 请求体：{op: "cdp", method, params, timeout} 执行 CDP 命令；{op: "events"} 取出并清空缓冲的协议事件；{op: "close"} 关闭页面并退出。
const http = await import("node:http");
const task = await taskSpace(__SPACE__);
const page = (await task.pages())[0] ?? await task.newPage();
await page.events(); // 清掉上一次渲染留下的协议事件
__DENY_PERMISSIONS__
// 窗口最小化时 Chromium 拒绝截图（服务器上断开远程桌面后窗口常处于最小化），恢复为普通窗口
const { windowId, bounds } = await task.cdp("Browser.getWindowForTarget", { targetId: page.targetId });
if (bounds.windowState === "minimized") await task.cdp("Browser.setWindowBounds", { windowId, bounds: { windowState: "normal" } });
let closing = null;
const server = http.createServer(async (request, response) => {
  let body = "";
  for await (const chunk of request) body += chunk;
  const message = JSON.parse(body || "{}");
  let reply;
  try {
    // 整页截图等慢命令需要比 ego 默认更长的超时，由调用方按剩余时间传入 timeout（毫秒）
    if (message.op === "cdp") reply = { ok: true, value: await page.cdp(message.method, message.params || {}, { timeout: message.timeout }) };
    else if (message.op === "events") reply = { ok: true, value: await page.events() };
    else if (message.op === "ping") reply = { ok: true, value: {} };
    else if (message.op === "close") reply = { ok: true, value: {} };
    else reply = { ok: false, error: "unknown_op" };
  } catch (error) {
    reply = { ok: false, error: String(error && error.message || error).slice(0, 500) };
  }
  response.setHeader("Content-Type", "application/json");
  response.end(JSON.stringify(reply));
  if (message.op === "close") closing = new Promise((resolve) => server.close(resolve));
});
await new Promise((resolve) => server.listen(__PORT__, "127.0.0.1", resolve));
await new Promise((resolve) => server.on("close", resolve));
await closing;
await page.goto("about:blank"); // 页面留给下一次渲染复用，先停下当前网站
