"""
Data sources package for Engage Genius.

This package contains implementations for various data sources
including Slack, Jira, Google services, etc.
"""

from services.data_sources.base import (
    DataSource,
    GlobalSearchResult,
    ChannelSummary,
    FollowUpSuggestion,
)
from services.data_sources.slack_source import SlackDataSource

__all__ = [
    "DataSource",
    "GlobalSearchResult",
    "ChannelSummary",
    "FollowUpSuggestion",
    "SlackDataSource",
]

