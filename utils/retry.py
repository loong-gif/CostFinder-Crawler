"""
重试装饰器
"""
import asyncio
import time
from functools import wraps
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from config.settings import MAX_RETRIES, RETRY_DELAY, RETRY_BACKOFF
from utils.logger import log

def async_retry(max_attempts=MAX_RETRIES):
    """异步重试装饰器"""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts:
                        log.error(f"函数 {func.__name__} 重试{max_attempts}次后仍然失败: {e}")
                        raise
                    
                    wait_time = RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))
                    log.warning(f"函数 {func.__name__} 第{attempt}次尝试失败,{wait_time}秒后重试: {e}")
                    await asyncio.sleep(wait_time)
            
        return wrapper
    return decorator

def sync_retry(max_attempts=MAX_RETRIES):
    """同步重试装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == max_attempts:
                        log.error(f"函数 {func.__name__} 重试{max_attempts}次后仍然失败: {e}")
                        raise
                    
                    wait_time = RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))
                    log.warning(f"函数 {func.__name__} 第{attempt}次尝试失败,{wait_time}秒后重试: {e}")
                    time.sleep(wait_time)
            
        return wrapper
    return decorator
