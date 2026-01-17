"""
Rate limiter utility
Implements token bucket algorithm for rate limiting requests
"""

import time
import threading
from typing import Optional
from collections import deque


class RateLimiter:
    """
    Rate limiter using token bucket algorithm.
    Supports multiple time windows (per second, per minute, per hour).
    """

    def __init__(
        self,
        requests_per_second: int = 2,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
    ):
        """
        Initialize rate limiter.

        Args:
            requests_per_second: Maximum requests per second
            requests_per_minute: Maximum requests per minute
            requests_per_hour: Maximum requests per hour
        """
        self.requests_per_second = requests_per_second
        self.requests_per_minute = requests_per_minute
        self.requests_per_hour = requests_per_hour

        # Token buckets for different time windows
        self.second_tokens = requests_per_second
        self.minute_tokens = requests_per_minute
        self.hour_tokens = requests_per_hour

        # Track request timestamps for sliding window
        self.second_requests: deque = deque()
        self.minute_requests: deque = deque()
        self.hour_requests: deque = deque()

        # Lock for thread safety
        self.lock = threading.Lock()

        # Last token refill time
        self.last_refill = time.time()

    def acquire(self, wait: bool = True) -> bool:
        """
        Acquire permission to make a request.
        Blocks until permission is granted if wait=True.

        Args:
            wait: If True, wait until permission is granted. If False, return immediately.

        Returns:
            bool: True if permission granted, False if not (only when wait=False)
        """
        with self.lock:
            current_time = time.time()

            # Clean old requests from sliding windows
            self._clean_old_requests(current_time)

            # Check if we can make a request
            if self._can_make_request():
                # Record the request
                self._record_request(current_time)
                return True

            if not wait:
                return False

            # Calculate wait time
            wait_time = self._calculate_wait_time(current_time)
            if wait_time > 0:
                time.sleep(wait_time)
                # Try again after waiting
                return self.acquire(wait=False)

            return True

    def _can_make_request(self) -> bool:
        """Check if we can make a request based on all rate limits."""
        return (
            len(self.second_requests) < self.requests_per_second
            and len(self.minute_requests) < self.requests_per_minute
            and len(self.hour_requests) < self.requests_per_hour
        )

    def _record_request(self, timestamp: float):
        """Record a request timestamp."""
        self.second_requests.append(timestamp)
        self.minute_requests.append(timestamp)
        self.hour_requests.append(timestamp)

    def _clean_old_requests(self, current_time: float):
        """Remove old requests from sliding windows."""
        # Clean second window (last 1 second)
        while self.second_requests and current_time - self.second_requests[0] >= 1.0:
            self.second_requests.popleft()

        # Clean minute window (last 60 seconds)
        while self.minute_requests and current_time - self.minute_requests[0] >= 60.0:
            self.minute_requests.popleft()

        # Clean hour window (last 3600 seconds)
        while self.hour_requests and current_time - self.hour_requests[0] >= 3600.0:
            self.hour_requests.popleft()

    def _calculate_wait_time(self, current_time: float) -> float:
        """Calculate how long to wait before next request is allowed."""
        wait_times = []

        # Wait time for second limit
        if len(self.second_requests) >= self.requests_per_second:
            if self.second_requests:
                oldest = self.second_requests[0]
                wait_times.append(1.0 - (current_time - oldest))

        # Wait time for minute limit
        if len(self.minute_requests) >= self.requests_per_minute:
            if self.minute_requests:
                oldest = self.minute_requests[0]
                wait_times.append(60.0 - (current_time - oldest))

        # Wait time for hour limit
        if len(self.hour_requests) >= self.requests_per_hour:
            if self.hour_requests:
                oldest = self.hour_requests[0]
                wait_times.append(3600.0 - (current_time - oldest))

        return max(wait_times) if wait_times else 0.0

    def get_stats(self) -> dict:
        """
        Get current rate limiter statistics.

        Returns:
            dict: Statistics including current request counts
        """
        with self.lock:
            current_time = time.time()
            self._clean_old_requests(current_time)

            return {
                "requests_last_second": len(self.second_requests),
                "requests_last_minute": len(self.minute_requests),
                "requests_last_hour": len(self.hour_requests),
                "limit_per_second": self.requests_per_second,
                "limit_per_minute": self.requests_per_minute,
                "limit_per_hour": self.requests_per_hour,
            }

    def reset(self):
        """Reset all rate limit counters."""
        with self.lock:
            self.second_requests.clear()
            self.minute_requests.clear()
            self.hour_requests.clear()
            self.last_refill = time.time()
