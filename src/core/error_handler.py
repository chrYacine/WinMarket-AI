"""
Error handling and retry logic for API calls and external service integration.
Provides decorators and utilities for robust fault tolerance.
"""

import time
import functools
from typing import Callable, TypeVar, Any, Optional, Type
from enum import Enum
import random

from src.core.logger import get_agent_logger

logger = get_agent_logger("error_handler")

T = TypeVar("T")


class ErrorType(Enum):
    """Classification of errors for recovery strategies."""
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    AUTH_ERROR = "auth_error"
    CLIENT_ERROR = "client_error"
    UNKNOWN = "unknown"


class RetryConfig:
    """Configuration for retry behavior."""
    
    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 60.0,
        exponential_base: float = 2.0,
        jitter: bool = True,
    ):
        """
        Args:
            max_retries: Maximum number of retry attempts.
            initial_delay: Initial delay between retries (seconds).
            max_delay: Maximum delay between retries (seconds).
            exponential_base: Base for exponential backoff calculation.
            jitter: Whether to add random jitter to delays.
        """
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.exponential_base = exponential_base
        self.jitter = jitter
    
    def get_delay(self, attempt: int) -> float:
        """Calculate delay for given attempt number."""
        delay = min(
            self.initial_delay * (self.exponential_base ** attempt),
            self.max_delay
        )
        
        if self.jitter:
            # Add ±20% jitter
            jitter_factor = random.uniform(0.8, 1.2)
            delay *= jitter_factor
        
        return delay


# Default configurations
DEFAULT_RETRY_CONFIG = RetryConfig(
    max_retries=3,
    initial_delay=1.0,
    max_delay=60.0,
    exponential_base=2.0,
    jitter=True,
)

AGGRESSIVE_RETRY_CONFIG = RetryConfig(
    max_retries=5,
    initial_delay=0.5,
    max_delay=120.0,
    exponential_base=2.0,
    jitter=True,
)


def classify_exception(exc: Exception) -> ErrorType:
    """Classify exception type for recovery decision."""
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__
    
    # Rate limit errors
    if (
        "rate limit" in exc_str 
        or "429" in exc_str 
        or "quota" in exc_str
    ):
        return ErrorType.RATE_LIMIT
    
    # Timeout errors
    if (
        "timeout" in exc_str 
        or "timed out" in exc_str
        or "deadline" in exc_str
        or exc_type == "TimeoutError"
    ):
        return ErrorType.TIMEOUT
    
    # Server errors (5xx)
    if (
        "500" in exc_str 
        or "502" in exc_str 
        or "503" in exc_str 
        or "server error" in exc_str
    ):
        return ErrorType.SERVER_ERROR
    
    # Auth errors
    if (
        "401" in exc_str 
        or "403" in exc_str 
        or "unauthorized" in exc_str
        or "forbidden" in exc_str
        or "invalid api key" in exc_str
    ):
        return ErrorType.AUTH_ERROR
    
    # Client errors (4xx)
    if (
        "400" in exc_str 
        or "404" in exc_str 
        or "bad request" in exc_str
    ):
        return ErrorType.CLIENT_ERROR
    
    return ErrorType.UNKNOWN


def is_retryable(exc: Exception) -> bool:
    """Determine if exception should trigger a retry."""
    error_type = classify_exception(exc)

    # Timeout errors are NOT retried — each retry adds another full wait
    # (the caller should handle gracefully instead)
    retryable_types = {
        ErrorType.RATE_LIMIT,
        ErrorType.SERVER_ERROR,
    }

    return error_type in retryable_types


def retry_with_backoff(
    func: Optional[Callable[..., T]] = None,
    config: Optional[RetryConfig] = None,
    on_retry: Optional[Callable[[Exception, int], None]] = None,
) -> Callable:
    """
    Decorator for automatic retry with exponential backoff.
    
    Args:
        func: Function to decorate (set automatically by decorator).
        config: RetryConfig instance (default: DEFAULT_RETRY_CONFIG).
        on_retry: Optional callback called on retry (exc, attempt_number).
    
    Returns:
        Decorated function with retry logic.
    
    Example:
        @retry_with_backoff(config=AGGRESSIVE_RETRY_CONFIG)
        def call_external_api():
            ...
    """
    if config is None:
        config = DEFAULT_RETRY_CONFIG
    
    def decorator(f: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            last_exception: Optional[Exception] = None
            
            for attempt in range(config.max_retries + 1):
                try:
                    return f(*args, **kwargs)
                
                except Exception as e:
                    last_exception = e
                    error_type = classify_exception(e)
                    
                    if not is_retryable(e):
                        logger.error(
                            f"Non-retryable error in {f.__name__} [{error_type.value}]: {e}"
                        )
                        raise

                    if attempt >= config.max_retries:
                        logger.error(
                            f"Max retries ({config.max_retries}) exceeded for {f.__name__} "
                            f"[{error_type.value}] after {attempt + 1} attempts"
                        )
                        raise

                    # Calculate delay
                    delay = config.get_delay(attempt)

                    # Callback
                    if on_retry:
                        on_retry(e, attempt + 1)

                    logger.warning(
                        f"Retrying {f.__name__} after {delay:.2f}s "
                        f"(attempt {attempt + 1}/{config.max_retries + 1}) "
                        f"[{error_type.value}]: {e}"
                    )
                    
                    time.sleep(delay)
            
            # Should not reach here, but just in case
            raise last_exception or Exception(f"Unknown error in {f.__name__}")
        
        return wrapper
    
    # Allow usage as @retry_with_backoff or @retry_with_backoff(config=...)
    if func is None:
        return decorator
    else:
        return decorator(func)


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_timeout: int = 60,
) -> Callable:
    """
    Decorator implementing circuit breaker pattern.
    Opens circuit after N failures, rejects calls for timeout period.
    
    Args:
        failure_threshold: Number of failures before opening circuit.
        recovery_timeout: Seconds to wait before trying half-open.
    
    Returns:
        Decorated function with circuit breaker logic.
    """
    def decorator(f: Callable[..., T]) -> Callable[..., T]:
        # State tracking
        state = {"failures": 0, "last_failure_time": 0, "open": False}
        
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            current_time = time.time()
            
            # Check if circuit should close (recovery timeout passed)
            if state["open"]:
                if current_time - state["last_failure_time"] > recovery_timeout:
                    logger.info(f"Circuit breaker for {f.__name__} attempting recovery")
                    state["open"] = False
                    state["failures"] = 0
                else:
                    raise RuntimeError(
                        f"Circuit breaker OPEN for {f.__name__}. "
                        f"Retry after {recovery_timeout}s"
                    )
            
            try:
                result = f(*args, **kwargs)
                # Success: reset failures
                state["failures"] = 0
                return result
            
            except Exception as e:
                state["failures"] += 1
                state["last_failure_time"] = current_time
                
                if state["failures"] >= failure_threshold:
                    state["open"] = True
                    logger.critical(
                        f"Circuit breaker OPENED for {f.__name__} "
                        f"after {state['failures']} failures"
                    )
                
                raise
        
        return wrapper
    
    return decorator


def timeout_handler(timeout_seconds: float) -> Callable:
    """
    Decorator to enforce maximum execution time (Unix-like systems only).
    
    Args:
        timeout_seconds: Maximum execution time in seconds.
    
    Returns:
        Decorated function with timeout enforcement.
    
    Note:
        This uses signals (Unix only). For cross-platform, use concurrent.futures.
    """
    import signal
    
    def timeout_handler_signal(signum, frame):
        raise TimeoutError(f"Function exceeded {timeout_seconds}s timeout")
    
    def decorator(f: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(f)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            # Set alarm
            old_handler = signal.signal(signal.SIGALRM, timeout_handler_signal)
            signal.alarm(int(timeout_seconds))
            
            try:
                result = f(*args, **kwargs)
            finally:
                # Cancel alarm
                signal.alarm(0)
                # Restore old handler
                signal.signal(signal.SIGALRM, old_handler)
            
            return result
        
        return wrapper
    
    return decorator


class SafeExecutor:
    """Context manager for safe execution with error handling."""
    
    def __init__(
        self,
        operation_name: str,
        fallback_value: Any = None,
        retry_config: Optional[RetryConfig] = None,
        suppress_exceptions: bool = False,
    ):
        """
        Args:
            operation_name: Name of operation for logging.
            fallback_value: Value to return if operation fails.
            retry_config: RetryConfig for automatic retries.
            suppress_exceptions: If True, return fallback_value instead of raising.
        """
        self.operation_name = operation_name
        self.fallback_value = fallback_value
        self.retry_config = retry_config or DEFAULT_RETRY_CONFIG
        self.suppress_exceptions = suppress_exceptions
        self.result: Any = None
        self.exception: Optional[Exception] = None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            return False
        
        self.exception = exc_val
        
        if not is_retryable(exc_val):
            logger.error(f"Non-retryable error in {self.operation_name}: {exc_val}")
            return self.suppress_exceptions

        logger.warning(
            f"Error in {self.operation_name}: {exc_val}. "
            f"Would retry {self.retry_config.max_retries} times."
        )
        
        return self.suppress_exceptions
    
    def set_result(self, value: Any) -> None:
        self.result = value


if __name__ == "__main__":
    # Test retry decorator
    @retry_with_backoff(config=DEFAULT_RETRY_CONFIG)
    def test_api_call():
        import random
        if random.random() < 0.7:
            raise TimeoutError("Simulated timeout")
        return "Success!"
    
    try:
        result = test_api_call()
        print(f"Result: {result}")
    except Exception as e:
        print(f"Failed after retries: {e}")
