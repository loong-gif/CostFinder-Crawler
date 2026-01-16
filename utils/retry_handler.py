"""
Retry handler utility
Provides retry mechanism with timeout, retry limit, and selective retry support
"""

import time
import logging
from typing import Callable, Any, List, Type, Optional
from functools import wraps


class RetryHandler:
    """
    Retry handler class for executing functions with retry logic.
    Supports timeout, retry limit, and selective retry based on exception types.
    """

    def __init__(
        self,
        max_retries: int = 3,
        timeout: Optional[float] = None,
        retry_delay: float = 1.0,
        retryable_exceptions: Optional[List[Type[Exception]]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        """
        Initialize retry handler.

        Args:
            max_retries: Maximum number of retry attempts (default: 3)
            timeout: Timeout in seconds for each attempt (default: None, no timeout)
            retry_delay: Delay in seconds between retries (default: 1.0)
            retryable_exceptions: List of exception types that should trigger retry.
                                 If None, all exceptions will trigger retry.
            logger: Logger instance for logging retry attempts (default: None)
        """
        self.max_retries = max_retries
        self.timeout = timeout
        self.retry_delay = retry_delay
        self.retryable_exceptions = retryable_exceptions or []
        self.logger = logger
        self.stats = {
            "total_attempts": 0,
            "successful_attempts": 0,
            "failed_attempts": 0,
            "retry_count": 0,
        }

    def execute(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with retry logic.

        Args:
            func: Function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function

        Returns:
            Result of the function execution

        Raises:
            Exception: Last exception if all retries are exhausted
        """
        last_exception = None
        self.stats["total_attempts"] = 0
        self.stats["retry_count"] = 0

        for attempt in range(self.max_retries):
            self.stats["total_attempts"] += 1
            attempt_number = attempt + 1

            try:
                # Apply timeout if specified
                if self.timeout is not None:
                    result = self._execute_with_timeout(func, *args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                # Success
                self.stats["successful_attempts"] += 1
                if self.logger and attempt > 0:
                    self.logger.info(
                        f"Function {func.__name__} succeeded after {attempt_number} attempt(s)"
                    )
                return result

            except Exception as e:
                last_exception = e
                self.stats["failed_attempts"] += 1

                # Check if exception is retryable
                if not self._is_retryable(e):
                    if self.logger:
                        self.logger.warning(
                            f"Non-retryable exception occurred: {type(e).__name__}: {str(e)}"
                        )
                    raise

                # Check if we have more retries
                if attempt < self.max_retries - 1:
                    self.stats["retry_count"] += 1
                    if self.logger:
                        self.logger.warning(
                            f"Attempt {attempt_number}/{self.max_retries} failed for {func.__name__}: "
                            f"{type(e).__name__}: {str(e)}. Retrying in {self.retry_delay} seconds..."
                        )
                    time.sleep(self.retry_delay)
                else:
                    if self.logger:
                        self.logger.error(
                            f"All {self.max_retries} attempts failed for {func.__name__}. "
                            f"Last error: {type(e).__name__}: {str(e)}"
                        )

        # All retries exhausted
        raise last_exception

    def _is_retryable(self, exception: Exception) -> bool:
        """
        Check if exception is retryable.

        Args:
            exception: Exception to check

        Returns:
            bool: True if exception is retryable, False otherwise
        """
        if not self.retryable_exceptions:
            # If no retryable exceptions specified, retry on all exceptions
            return True

        # Check if exception is instance of any retryable exception type
        return isinstance(exception, tuple(self.retryable_exceptions))

    def _execute_with_timeout(self, func: Callable, *args, **kwargs) -> Any:
        """
        Execute function with timeout using threading.

        Args:
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result

        Raises:
            TimeoutError: If function execution exceeds timeout
        """
        import threading

        result = [None]
        exception = [None]

        def target():
            try:
                result[0] = func(*args, **kwargs)
            except Exception as e:
                exception[0] = e

        thread = threading.Thread(target=target)
        thread.daemon = True
        thread.start()
        thread.join(timeout=self.timeout)

        if thread.is_alive():
            # Thread is still running, timeout occurred
            raise TimeoutError(
                f"Function {func.__name__} exceeded timeout of {self.timeout} seconds"
            )

        if exception[0]:
            raise exception[0]

        return result[0]

    def get_stats(self) -> dict:
        """
        Get retry statistics.

        Returns:
            dict: Statistics dictionary
        """
        return self.stats.copy()

    def reset_stats(self):
        """Reset statistics."""
        self.stats = {
            "total_attempts": 0,
            "successful_attempts": 0,
            "failed_attempts": 0,
            "retry_count": 0,
        }


def retry(
    max_retries: int = 3,
    timeout: Optional[float] = None,
    retry_delay: float = 1.0,
    retryable_exceptions: Optional[List[Type[Exception]]] = None,
    logger: Optional[logging.Logger] = None,
):
    """
    Decorator for adding retry logic to functions.

    Args:
        max_retries: Maximum number of retry attempts
        timeout: Timeout in seconds for each attempt
        retry_delay: Delay in seconds between retries
        retryable_exceptions: List of exception types that should trigger retry
        logger: Logger instance for logging retry attempts

    Returns:
        Decorated function
    """

    def decorator(func: Callable) -> Callable:
        handler = RetryHandler(
            max_retries=max_retries,
            timeout=timeout,
            retry_delay=retry_delay,
            retryable_exceptions=retryable_exceptions,
            logger=logger,
        )

        @wraps(func)
        def wrapper(*args, **kwargs):
            return handler.execute(func, *args, **kwargs)

        return wrapper

    return decorator
