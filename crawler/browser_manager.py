"""
浏览器管理器 - 管理Playwright浏览器实例
"""
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from config.settings import (
    BROWSER_ARGS,
    BROWSER_TYPE,
    HEADLESS,
    PAGE_LOAD_TIMEOUT,
    REQUEST_TIMEOUT,
    VIEWPORT,
)
from config.user_agents import get_headers, get_random_user_agent
from utils.logger import log


class BrowserManager:
    """浏览器管理器"""

    def __init__(self, headless: Optional[bool] = None):
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None
        self.contexts: list[BrowserContext] = []
        self.shared_context: Optional[BrowserContext] = None
        self.headless = HEADLESS if headless is None else headless

    async def start(self):
        """启动浏览器"""
        try:
            self.playwright = await async_playwright().start()

            if BROWSER_TYPE == "chromium":
                browser_launcher = self.playwright.chromium
            elif BROWSER_TYPE == "firefox":
                browser_launcher = self.playwright.firefox
            else:
                browser_launcher = self.playwright.webkit

            try:
                self.browser = await browser_launcher.launch(
                    headless=self.headless,
                    args=BROWSER_ARGS,
                )
            except Exception as launch_error:
                if BROWSER_TYPE != "chromium":
                    raise
                log.warning(f"内置 Chromium 启动失败，尝试系统 Chrome: {launch_error}")
                self.browser = await browser_launcher.launch(
                    channel="chrome",
                    headless=self.headless,
                    args=BROWSER_ARGS,
                )

            log.info(f"浏览器启动成功: {BROWSER_TYPE}, headless={self.headless}")
        except Exception as e:
            log.error(f"浏览器启动失败: {e}")
            raise

    async def create_context(self, track: bool = True) -> BrowserContext:
        """
        创建新的浏览器上下文(带反爬虫配置)

        Args:
            track: 是否跟踪此上下文(如果为False,调用者需要手动关闭)
        """
        if not self.browser:
            raise RuntimeError("浏览器未启动,请先调用start()")

        user_agent = get_random_user_agent()
        context = await self.browser.new_context(
            viewport=VIEWPORT,
            user_agent=user_agent,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers=get_headers(user_agent),
            ignore_https_errors=True,
            java_script_enabled=True,
        )

        context.set_default_timeout(REQUEST_TIMEOUT)
        context.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT)

        await context.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });

            window.chrome = {
                runtime: {}
            };

            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            """
        )

        if track:
            self.contexts.append(context)
            log.debug(f"创建新的浏览器上下文,当前共{len(self.contexts)}个")
        else:
            log.debug("创建非跟踪浏览器上下文")

        return context

    async def get_shared_context(self) -> BrowserContext:
        """获取共享上下文（单例模式）"""
        if self.shared_context is None:
            self.shared_context = await self.create_context()
            log.info("创建共享浏览器上下文")
        return self.shared_context

    async def create_page(self, use_shared_context: bool = True) -> Page:
        """
        创建新页面

        Args:
            use_shared_context: 是否使用共享上下文
        """
        if use_shared_context:
            context = await self.get_shared_context()
        else:
            context = await self.create_context()

        page = await context.new_page()

        async def handle_route(route):
            if route.request.resource_type in ["image", "font", "media"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", handle_route)
        return page

    async def close_context(self, context: BrowserContext):
        """关闭上下文"""
        if context in self.contexts:
            await context.close()
            self.contexts.remove(context)
            log.debug(f"关闭浏览器上下文,剩余{len(self.contexts)}个")

    async def close(self):
        """关闭所有资源"""
        try:
            if self.shared_context:
                try:
                    await self.shared_context.close()
                    log.debug("共享上下文已关闭")
                except Exception as e:
                    log.debug(f"关闭共享上下文时出错(可忽略): {e}")
                self.shared_context = None

            for context in self.contexts[:]:
                try:
                    await context.close()
                except Exception as e:
                    log.debug(f"关闭上下文时出错(可忽略): {e}")
            self.contexts.clear()

            if self.browser:
                try:
                    await self.browser.close()
                    log.info("浏览器已关闭")
                except Exception as e:
                    log.debug(f"关闭浏览器时出错(可忽略): {e}")
                self.browser = None

            if self.playwright:
                try:
                    await self.playwright.stop()
                except Exception as e:
                    log.debug(f"停止Playwright时出错(可忽略): {e}")
                self.playwright = None
        except Exception as e:
            log.error(f"关闭浏览器时出错: {e}")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
