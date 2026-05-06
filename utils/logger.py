"""
日志管理器
"""
from loguru import logger
import sys
from config.settings import LOG_DIR, LOG_LEVEL, LOG_ROTATION, LOG_RETENTION

def setup_logger():
    """配置日志系统"""
    # 移除默认handler
    logger.remove()
    
    # 控制台输出(带颜色)
    logger.add(
        sys.stdout,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan> - <level>{message}</level>",
        level=LOG_LEVEL,
        colorize=True,
    )
    
    # 文件输出(详细日志)
    logger.add(
        LOG_DIR / "crawler_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="DEBUG",
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        compression="zip",
        encoding="utf-8",
    )
    
    # 错误日志单独文件
    logger.add(
        LOG_DIR / "error_{time:YYYY-MM-DD}.log",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
        level="ERROR",
        rotation=LOG_ROTATION,
        retention=LOG_RETENTION,
        compression="zip",
        encoding="utf-8",
    )
    
    return logger

# 初始化全局logger
log = setup_logger()
