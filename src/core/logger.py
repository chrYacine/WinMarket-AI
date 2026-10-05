"""
Centralized logging system for WinMarket AI.
Uses Python's logging module with structured logging support.
"""

import logging
import logging.handlers
import json
import sys
from pathlib import Path
from datetime import datetime
from typing import Any

# Setup logging directory
LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Log levels
DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR
CRITICAL = logging.CRITICAL


class JSONFormatter(logging.Formatter):
    """Custom formatter that outputs JSON-structured logs."""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields if provided
        if hasattr(record, "extra_fields"):
            log_data.update(record.extra_fields)
        
        return json.dumps(log_data, ensure_ascii=False)


class ColoredFormatter(logging.Formatter):
    """Colored console formatter for readability."""
    
    COLORS = {
        "DEBUG": "\033[36m",      # Cyan
        "INFO": "\033[32m",       # Green
        "WARNING": "\033[33m",    # Yellow
        "ERROR": "\033[31m",      # Red
        "CRITICAL": "\033[35m",   # Magenta
        "RESET": "\033[0m",       # Reset
    }
    
    FORMAT = (
        "%(asctime)s - %(name)s - %(levelname)s - "
        "[%(filename)s:%(lineno)d] - %(message)s"
    )
    
    def format(self, record: logging.LogRecord) -> str:
        levelname = record.levelname
        if levelname in self.COLORS:
            record.levelname = (
                f"{self.COLORS[levelname]}{levelname}{self.COLORS['RESET']}"
            )
        return super().format(record)


def setup_logger(
    name: str,
    level: int = INFO,
    log_to_file: bool = True,
    log_to_console: bool = True,
    json_format: bool = False,
) -> logging.Logger:
    """
    Setup and return a configured logger.
    
    Args:
        name: Logger name (typically __name__ from calling module).
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_to_file: Whether to write to file (default: True).
        log_to_console: Whether to write to console (default: True).
        json_format: Whether to use JSON format (default: False, use colored).
    
    Returns:
        Configured logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    
    # Avoid adding duplicate handlers
    if logger.handlers:
        return logger
    
    # Formatter
    if json_format:
        formatter = JSONFormatter()
    else:
        formatter = ColoredFormatter(ColoredFormatter.FORMAT)
    
    # Console handler
    if log_to_console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # File handler (rotate every 10MB, keep 5 files)
    if log_to_file:
        log_file = LOG_DIR / f"{name}.log"
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,
        )
        file_handler.setLevel(level)
        
        # File uses JSON format for structured logging
        file_formatter = JSONFormatter() if json_format else formatter
        file_handler.setFormatter(file_formatter)
        logger.addHandler(file_handler)
    
    return logger


# Application root logger
logger = setup_logger("winmarket", level=INFO, json_format=False)


def log_with_context(logger_inst: logging.Logger, level: int, msg: str, **context: Any) -> None:
    """
    Log a message with additional context fields.
    
    Args:
        logger_inst: Logger instance.
        level: Log level.
        msg: Message to log.
        **context: Additional fields to include in log (e.g., user_id=123, action="extraction").
    """
    record = logger_inst.makeRecord(
        logger_inst.name, level, "(context)", 0, msg, (), None
    )
    record.extra_fields = context
    logger_inst.handle(record)


# Convenience functions
def debug(msg: str, **context: Any) -> None:
    """Log debug message with optional context."""
    log_with_context(logger, DEBUG, msg, **context)


def info(msg: str, **context: Any) -> None:
    """Log info message with optional context."""
    log_with_context(logger, INFO, msg, **context)


def warning(msg: str, **context: Any) -> None:
    """Log warning message with optional context."""
    log_with_context(logger, WARNING, msg, **context)


def error(msg: str, **context: Any) -> None:
    """Log error message with optional context."""
    log_with_context(logger, ERROR, msg, **context)


def critical(msg: str, **context: Any) -> None:
    """Log critical message with optional context."""
    log_with_context(logger, CRITICAL, msg, **context)


# Agent-specific loggers
def get_agent_logger(agent_name: str) -> logging.Logger:
    """Get a logger specific to an agent."""
    return setup_logger(f"winmarket.agents.{agent_name}", level=DEBUG)


# B26-T1: minimal structured logging for a job's lifecycle — job_id/step/
# version fields, reusing the EXISTING log_with_context/JSONFormatter
# mechanism above (no new logging framework). `_FORBIDDEN_FIELD_SUBSTRINGS`
# is a structural guard, not just a convention: a caller that tries to pass
# a field whose NAME suggests a secret/prompt/document body gets a loud
# ValueError at log time, rather than that content silently reaching a log
# file (which, unlike an API response, is not something B10/B14's existing
# "never leak raw exception/secret" discipline already covers — a log line
# is a different exposure surface).
_FORBIDDEN_FIELD_SUBSTRINGS = ("token", "password", "secret", "prompt", "content", "api_key", "document", "cookie")


def log_job_event(
    logger_inst: logging.Logger, level: int, event: str, *,
    job_id: str | None = None, step: str | None = None, version: str | int | None = None,
    **extra: Any,
) -> None:
    """Structured job/step/version log line. `event` is a short, fixed,
    safe label (e.g. "job_started", "job_terminal", "step_completed") —
    never a free-text message that might embed request/document content.
    `**extra` is for a HANDFUL of additional safe scalar fields (e.g.
    error_code, status) — never a token, prompt, or document/user-content
    body; any field NAME containing one of _FORBIDDEN_FIELD_SUBSTRINGS
    raises immediately rather than being logged, so this is caught at the
    call site during development, not discovered later by reading a log
    file."""
    fields = {"event": event, "job_id": job_id, "step": step, "version": version, **extra}
    for key in fields:
        lowered = key.lower()
        if any(bad in lowered for bad in _FORBIDDEN_FIELD_SUBSTRINGS):
            raise ValueError(
                f"log_job_event: refusing to log field {key!r} — its name suggests a secret/prompt/"
                f"document body, which must never reach a log line (B26-T1)."
            )
    log_with_context(logger_inst, level, event, **{k: v for k, v in fields.items() if v is not None})


if __name__ == "__main__":
    # Test logging
    test_logger = setup_logger("test", level=DEBUG)
    test_logger.debug("Debug message")
    test_logger.info("Info message")
    test_logger.warning("Warning message")
    test_logger.error("Error message")
    test_logger.critical("Critical message")
    
    # Test context
    log_with_context(test_logger, INFO, "Action completed", 
                    action="extraction", ao_title="Test AO", duration_ms=1234)
