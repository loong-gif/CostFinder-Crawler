"""
Logging utility
Provides unified logging functionality
"""

import logging
import sys
from pathlib import Path
from config import LOG_LEVEL, LOG_FORMAT, LOG_FILE


class Logger:
    """Logging management class"""

    _loggers = {}

    @classmethod
    def get_logger(cls, name: str) -> logging.Logger:
        """
        Get or create Logger instance.
        
        Args:
            name: Logger name
            
        Returns:
            logging.Logger: Logger instance
        """
        if name in cls._loggers:
            return cls._loggers[name]

        logger = logging.getLogger(name)
        logger.setLevel(getattr(logging, LOG_LEVEL))

        # Avoid duplicate handler addition
        if not logger.handlers:
            # Console Handler
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(getattr(logging, LOG_LEVEL))
            console_formatter = logging.Formatter(LOG_FORMAT)
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)

            # File Handler
            file_handler = logging.FileHandler(
                LOG_FILE, 
                encoding="utf-8"
            )
            file_handler.setLevel(getattr(logging, LOG_LEVEL))
            file_formatter = logging.Formatter(LOG_FORMAT)
            file_handler.setFormatter(file_formatter)
            logger.addHandler(file_handler)

        cls._loggers[name] = logger
        return logger










