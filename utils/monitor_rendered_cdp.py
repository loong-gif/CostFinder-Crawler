from __future__ import annotations

import json
import queue
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import requests

from utils.ego_browser import DENY_PERMISSIONS_JS, ego_command

ROOT = Path(__file__).resolve().parents[1]
EGO_RENDER_SPACE = "costfinder monitor render"
BRIDGE_START_TIMEOUT_S = 90
# 同时渲染的页面数；每个槽位独占一个任务空间（名称后缀为槽位号），空间里只有一个反复使用的页面
RENDER_SLOT_COUNT = 2
_RENDER_SLOTS: queue.Queue = queue.Queue()
for _slot in range(RENDER_SLOT_COUNT):
    _RENDER_SLOTS.put(_slot)


FRAME_VISIBILITY = """function() {
  const r=this.getBoundingClientRect(), doc=this.ownerDocument.documentElement;
  let left=Math.max(0,r.left),right=Math.min(doc.clientWidth,r.right);
  let top=Math.max(-scrollY,r.top),bottom=Math.min(doc.scrollHeight-scrollY,r.bottom);
  for(let p=this;p;p=p.parentElement || p.getRootNode().host) {
    const s=getComputedStyle(p);
    if(s.display==='none' || s.visibility==='hidden' || Number(s.opacity)===0 || p.hidden) return {visible:false};
    if(p!==this && p!==doc && p!==this.ownerDocument.body) {
      const c=p.getBoundingClientRect();
      if(/hidden|clip|auto|scroll/.test(s.overflowX)) {left=Math.max(left,c.left+p.clientLeft);right=Math.min(right,c.left+p.clientLeft+p.clientWidth);}
      if(/hidden|clip|auto|scroll/.test(s.overflowY)) {top=Math.max(top,c.top+p.clientTop);bottom=Math.min(bottom,c.top+p.clientTop+p.clientHeight);}
    }
  }
  return {visible:right>left && bottom>top,bounds:{x:left+scrollX,y:top+scrollY,width:Math.max(0,right-left),height:Math.max(0,bottom-top)}};
}"""


def frame_regions(page, tree):
    regions = []
    for child in tree.get("childFrames", []):
        frame = child["frame"]
        try:
            owner = page.call("DOM.getFrameOwner", frameId=frame["id"])
            node = page.call("DOM.resolveNode", backendNodeId=owner["backendNodeId"])["object"]
            result = page.call("Runtime.callFunctionOn", objectId=node["objectId"],
                               functionDeclaration=FRAME_VISIBILITY, returnByValue=True)
            visible = result.get("result", {}).get("value", {})
            if visible.get("visible") is False:
                continue
        except (RuntimeError, KeyError):
            visible = {}
        # Child document text can lie outside its visible inner scroll window.
        regions.append({"kind":"embedded", "frame_id":frame["id"], "url":frame["url"],
                        "visibility_known":visible.get("visible") is True, **visible.get("bounds", {})})
    return regions


class EgoCdpPage:
    """经桥接脚本操作 ego-lite 中的一个页面：call() 发送 CDP 命令，events 累积该页面的协议事件。"""

    def __init__(self, port: int, deadline: float):
        self.base = f"http://127.0.0.1:{port}"
        self.session = requests.Session()
        self.session.trust_env = False
        self.events: list[dict] = []
        self.deadline = deadline

    def _send(self, message: dict, timeout: float):
        try:
            reply = self.session.post(self.base, json=message, timeout=timeout).json()
        except requests.Timeout:
            raise RuntimeError("rendered_page_timeout") from None
        if not reply["ok"]:
            raise RuntimeError(f"rendered_browser_command_failed: {message.get('method') or message['op']}: {reply['error']}")
        return reply["value"]

    def ready(self) -> bool:
        """桥接已在监听并应答；启动期间端口尚未打开时连接被拒绝。"""
        try:
            self._send({"op":"ping"}, 2)
        except requests.ConnectionError:
            return False
        return True

    def _drain_events(self):
        self.events.extend(self._send({"op":"events"}, 15))

    def call(self, method, **params):
        remaining = min(65, self.deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError("rendered_page_timeout")
        # ego 内部超时比本地 HTTP 超时早 2 秒，让浏览器端先返回错误原文
        value = self._send({"op":"cdp","method":method,"params":params,
                            "timeout":int(max(1, remaining - 2) * 1000)}, remaining)
        self._drain_events()
        return value or {}

    def evaluate(self, expression, context_id=None):
        params = {"expression":expression,"awaitPromise":True,"returnByValue":True,"timeout":60000}
        if context_id is not None:
            params["contextId"] = context_id
        result = self.call("Runtime.evaluate",**params)
        if result.get("exceptionDetails"):
            raise RuntimeError("rendered_frame_failed")
        return result.get("result",{}).get("value")

    def wait_loaded(self):
        deadline = min(time.monotonic() + 60, self.deadline)
        while not any(event.get("method") == "Page.loadEventFired" for event in self.events):
            if time.monotonic() >= deadline:
                raise RuntimeError("rendered_page_timeout")
            time.sleep(.3)
            self._drain_events()

    def close(self):
        self._send({"op":"close"}, 30)


def _bridge_output(process: subprocess.Popen, lines: queue.Queue) -> None:
    # ego-browser 的 console 输出写到 stderr（已并入 stdout）；一直读到进程结束，避免管道写满
    for line in process.stdout:
        lines.put(line)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def ego_cdp_page(deadline: float):
    """占用一个渲染槽位，在该槽位的 ego-lite 任务空间里启动桥接；退出时停下页面并归还槽位。

    ego-browser 的 console 输出要到脚本结束才送出，所以端口由这里选定后传给桥接，再轮询 ping 直到桥接应答。
    """
    slot = _RENDER_SLOTS.get()
    try:
        port = _free_port()
        script = ((ROOT / "scripts/monitor_ego_bridge.js").read_text(encoding="utf-8")
                  .replace("__DENY_PERMISSIONS__", DENY_PERMISSIONS_JS)
                  .replace("__SPACE__", json.dumps(f"{EGO_RENDER_SPACE} {slot}"))
                  .replace("__PORT__", str(port)))
        process = subprocess.Popen([ego_command(), "nodejs", "-e", script], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        lines: queue.Queue = queue.Queue()
        threading.Thread(target=_bridge_output, args=(process, lines), daemon=True).start()
        page = EgoCdpPage(port, deadline)
        started = time.monotonic()
        while not page.ready():
            if process.poll() is not None:
                output = "".join(lines.get() for _ in range(lines.qsize()))
                raise RuntimeError("rendered_browser_unavailable: " + output[-600:])
            if time.monotonic() - started > BRIDGE_START_TIMEOUT_S:
                process.kill()
                raise RuntimeError("rendered_browser_unavailable")
            time.sleep(.3)
        try:
            yield page
        finally:
            page.close()
            process.wait(timeout=60)
    finally:
        _RENDER_SLOTS.put(slot)
