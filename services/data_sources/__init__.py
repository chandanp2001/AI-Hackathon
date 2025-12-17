"""
Data sources package for the Multi-Agent system.

This package contains implementations for various data sources
including Slack, Jira, Google services, etc.
"""

from .base import (
    DataSource,
    GlobalSearchResult,
    ChannelSummary,
    FollowUpSuggestion,
)
from .slack_source import SlackDataSource

__all__ = [
    "DataSource",
    "GlobalSearchResult",
    "ChannelSummary",
    "FollowUpSuggestion",
    "SlackDataSource",
]

