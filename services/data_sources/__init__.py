"""
Data sources package for the Multi-Agent system.

This package contains implementations for various data sources
including Slack, Jira, Google services, DevRev, etc.
"""

from .base import (
    DataSource,
    GlobalSearchResult,
    ChannelSummary,
    FollowUpSuggestion,
)
from .slack_source import SlackDataSource
from .devrev_source import DevRevDataSource

__all__ = [
    "DataSource",
    "GlobalSearchResult",
    "ChannelSummary",
    "FollowUpSuggestion",
    "SlackDataSource",
    "DevRevDataSource",
]

