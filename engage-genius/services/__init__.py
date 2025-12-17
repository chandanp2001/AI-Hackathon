"""
Services package for Engage Genius.

This package contains service implementations for data sources,
authentication, and other integrations.
"""

from services.data_sources import SlackDataSource, DataSource

__all__ = [
    "SlackDataSource",
    "DataSource",
]

