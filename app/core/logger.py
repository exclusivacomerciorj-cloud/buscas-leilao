import sys
from loguru import logger
from app.core.config import get_settings

settings = get_settings()


def setup_logger() -> None:
    logger.remove()

    fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    logger.add(sys.stdout, format=fmt, level="DEBUG" if settings.APP_DEBUG else "INFO")

    if settings.is_production:
        logger.add(
            "logs/app.log",
            rotation="10 MB",
            retention="30 days",
            compression="zip",
            format=fmt,
            level="INFO",
        )


setup_logger()

__all__ = ["logger"]
