from __future__ import annotations

import structlog
import logging
from pathlib import Path
from typing import Any


_initialized = False


def init_logging(
    level: str = "INFO",
    log_dir: str | None = None,
    # 2026-11-04：轮转文件名可指定。Windows 上多个进程同时持有同一个
    # RotatingFileHandler 的名字时，轮转改名会失败（WinError 32），失败后
    # 这一进程的日志记录被丢弃 —— 引擎从 2026-11-03 22:19 起就一条没落盘。
    # 引擎改用独立文件，避免被调度器的轮转拖死。
    filename: str = "yinyue.log",
    json_format: bool = False,
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 10,
) -> None:
    global _initialized
    if _initialized:
        return
    _initialized = True

    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_level)
    if json_format:
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )
    else:
        console_handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )

    logging.basicConfig(
        level=log_level,
        handlers=[console_handler],
        format="%(message)s",
    )

    if log_dir:
        from logging.handlers import RotatingFileHandler

        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_path / filename,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setLevel(log_level)
        fh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=shared_processors,
            )
        )
        logging.root.addHandler(fh)
