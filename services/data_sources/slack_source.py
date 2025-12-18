from typing import Dict, Any, List, Set, Optional, Union
from .base import (
    DataSource,
    GlobalSearchResult,
    ChannelSummary,
    FollowUpSuggestion,
)
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import logging
import os
import time
from functools import lru_cache
import asyncio
import re
from collections import defaultdict
from dateparser.search import search_dates
import dateparser
from datetime import datetime, timezone

# Try to import from config, with fallback to environment variables
try:
    from config import settings
    SLACK_BOT_TOKEN = settings.slack_bot_token
    SLACK_USER_TOKEN = settings.slack_user_token
except ImportError:
    SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
    SLACK_USER_TOKEN = os.environ.get("SLACK_USER_TOKEN", "")

class RateLimiter:
    def __init__(self):
        self.tiers = {
            'tier1': {'limit': 50, 'window': 60},  # Standard workspace
            'tier2': {'limit': 20, 'window': 60}   # Large workspace
        }
        self.usage = defaultdict(list)

    async def check_limit(self, method: str) -> None:
        """Enforce rate limits based on API method"""
        tier = 'tier1' if method in ['conversations.history'] else 'tier2'
        now = time.time()
        
        # Remove old entries
        self.usage[tier] = [t for t in self.usage[tier] if now - t < 60]
        
        if len(self.usage[tier]) >= self.tiers[tier]['limit']:
            delay = 60 - (now - self.usage[tier][0])
            await asyncio.sleep(delay)
        
        self.usage[tier].append(now)

class SlackDataSource(DataSource):
    def __init__(self):
        # Initialize with bot token by default
        self.client = WebClient(token=SLACK_BOT_TOKEN)
        
        # Storage for channel data
        self.channel_cache = {}  # Cache to store channel_name -> channel_id mappings
        self.last_cache_update = 0
        self.cache_ttl = 3600  # Cache TTL in seconds (1 hour)
        
        # Add message cache for faster responses
        self.message_cache = {}  # Cache to store channel_id -> messages mappings
        self.message_cache_ttl = 300  # Message cache TTL in seconds (5 minutes)
        self.message_cache_time = {}  # Last update time for messages
        
        # Storage for user tokens
        self.user_tokens = {}  # Map of user_id -> user token
        self.user_clients = {}  # Map of user_id -> WebClient instances
        self.user_scopes = {}   # Map of user_id -> list of available scopes
        
        # Initialize rate limiter
        self.rate_limiter = RateLimiter()
        
        # Last sync times for incremental syncs
        self.last_sync_times = {}
        
        # Workspace info for permalink generation
        self.workspace_domain = None
        
        # User name cache to resolve user IDs to display names
        self.user_name_cache = {}  # Map of user_id -> display_name
        self.user_name_cache_ttl = 3600  # Cache TTL in seconds (1 hour)
        self.user_name_cache_time = {}  # Last update time for user names
        
        # Try to load channel cache from disk at startup
        self._load_channel_cache_from_disk()
        
        # Initialize with default user token from config if available
        if SLACK_USER_TOKEN:
            # Strip any quotes that might have been included in the token
            clean_token = SLACK_USER_TOKEN.strip().strip('"\'')
            try:
                self.default_user_client = WebClient(token=clean_token)
                # Test the token
                test_response = self.default_user_client.auth_test()
                if test_response.get('ok'):
                    logging.info(f"Initialized default user client from config SLACK_USER_TOKEN for user {test_response.get('user')}")
                else:
                    error_msg = test_response.get('error', 'unknown error')
                    logging.warning(f"Default user token is invalid: {error_msg}")
                    self.default_user_client = None
            except Exception as e:
                logging.warning(f"Failed to initialize default user client: {e}")
                self.default_user_client = None
        else:
            self.default_user_client = None
            logging.warning("No SLACK_USER_TOKEN found in config, search functionality will be limited")
        
        # Fetch workspace info for permalink generation
        self._fetch_workspace_info()
    
    def _fetch_workspace_info(self):
        """Fetch workspace domain for permalink generation."""
        try:
            auth_response = self.client.auth_test()
            if auth_response.get('ok'):
                # Get team/workspace URL
                team_id = auth_response.get('team_id')
                team_info = self.client.team_info(team=team_id)
                if team_info.get('ok'):
                    self.workspace_domain = team_info['team'].get('domain')
                    logging.info(f"Workspace domain set to: {self.workspace_domain}")
                else:
                    # Fallback: extract from URL if available
                    url = auth_response.get('url', '')
                    if url:
                        # URL format: https://workspace.slack.com/
                        self.workspace_domain = url.split('//')[1].split('.')[0] if '//' in url else None
                    logging.warning(f"Could not fetch team info, using fallback domain: {self.workspace_domain}")
        except Exception as e:
            logging.warning(f"Could not fetch workspace info for permalinks: {e}")
            self.workspace_domain = None
    
    def _generate_permalink(self, channel_id: str, timestamp: str) -> str:
        """Generate a Slack permalink from channel ID and timestamp.
        
        Args:
            channel_id: Slack channel ID (e.g., C01234567)
            timestamp: Message timestamp (e.g., 1234567890.123456)
            
        Returns:
            Permalink URL string
        """
        if not self.workspace_domain:
            return ""
        
        # Convert timestamp to permalink format (remove decimal point)
        # Slack permalink format: p{timestamp without decimal}
        permalink_ts = timestamp.replace('.', '')
        
        return f"https://{self.workspace_domain}.slack.com/archives/{channel_id}/p{permalink_ts}"
    
    def _get_user_name(self, user_id: str) -> str:
        """Get user display name from user ID, with caching.
        
        Args:
            user_id: Slack user ID (e.g., U01234567)
            
        Returns:
            User's display name or real name, falls back to user_id if not found
        """
        if not user_id:
            return "Unknown"
        
        # Check cache first
        now = time.time()
        if user_id in self.user_name_cache:
            cache_time = self.user_name_cache_time.get(user_id, 0)
            if now - cache_time < self.user_name_cache_ttl:
                return self.user_name_cache[user_id]
        
        # Fetch from Slack API
        try:
            response = self.client.users_info(user=user_id)
            if response.get('ok'):
                user = response.get('user', {})
                # Prefer display_name, then real_name, then name
                display_name = (
                    user.get('profile', {}).get('display_name') or 
                    user.get('real_name') or 
                    user.get('name') or 
                    user_id
                )
                # Cache the result
                self.user_name_cache[user_id] = display_name
                self.user_name_cache_time[user_id] = now
                return display_name
        except SlackApiError as e:
            logging.warning(f"Failed to fetch user info for {user_id}: {e}")
        except Exception as e:
            logging.warning(f"Unexpected error fetching user info for {user_id}: {e}")
        
        # Cache the fallback too to avoid repeated API calls
        self.user_name_cache[user_id] = user_id
        self.user_name_cache_time[user_id] = now
        return user_id
    
    async def _resolve_user_ids_in_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Resolve user IDs to display names in a list of messages.
        
        Args:
            messages: List of message dicts containing 'user' field with user IDs
            
        Returns:
            Same messages with 'user' field updated to display names
        """
        if not messages:
            return messages
        
        # Collect all unique user IDs
        user_ids = set()
        for msg in messages:
            if isinstance(msg, dict):
                user_id = msg.get('user')
                if user_id and isinstance(user_id, str) and user_id.startswith('U'):
                    user_ids.add(user_id)
                # Also check replies
                for reply in msg.get('replies', []):
                    if isinstance(reply, dict):
                        reply_user_id = reply.get('user')
                        if reply_user_id and isinstance(reply_user_id, str) and reply_user_id.startswith('U'):
                            user_ids.add(reply_user_id)
        
        # Batch fetch user names (with caching)
        user_names = {}
        for user_id in user_ids:
            user_names[user_id] = self._get_user_name(user_id)
        
        # Update messages with user names
        for msg in messages:
            if isinstance(msg, dict):
                user_id = msg.get('user')
                if user_id and user_id in user_names:
                    msg['user'] = user_names[user_id]
                    msg['user_id'] = user_id  # Keep the original ID for reference
                # Also update replies
                for reply in msg.get('replies', []):
                    if isinstance(reply, dict):
                        reply_user_id = reply.get('user')
                        if reply_user_id and reply_user_id in user_names:
                            reply['user'] = user_names[reply_user_id]
                            reply['user_id'] = reply_user_id
        
        return messages
        
    def register_user_token(self, user_id: str, token: str) -> bool:
        """Register a user token for better performance on channel operations"""
        try:
            if not user_id or not token:
                logging.warning(f"Cannot register empty token for user {user_id}")
                return False
                
            # Strip any quotes that might have been included in the token
            token = token.strip().strip('"\'')
            
            logging.info(f"Attempting to register token for user {user_id}: {token[:10]}...")
                
            # Create a test client with the token
            test_client = WebClient(token=token)
            
            # Test the token with a simple API call
            try:
                test_response = test_client.auth_test()
                if not test_response.get('ok', False):
                    error_msg = test_response.get('error', 'unknown error')
                    logging.warning(f"Invalid user token provided for user {user_id}: {error_msg}")
                    return False
                    
                # Check available scopes to log what we have access to
                available_scopes = []
                try:
                    # Get token info to check scopes
                    identity_info = test_client.identity_basic()
                    if identity_info.get('ok'):
                        # Log the user we're connecting as
                        user_name = identity_info.get('user', {}).get('name', 'unknown')
                        logging.info(f"User token identifies as: {user_name}")
                        
                    # Try to inspect token directly
                    token_info = test_client.auth_test()
                    if token_info.get('ok'):
                        # Extract scopes if available
                        if 'scope' in token_info:
                            scope_str = token_info.get('scope', '')
                            available_scopes = scope_str.split(',')
                            logging.info(f"User token has {len(available_scopes)} scopes: {scope_str}")
                        
                        # Add useful token metadata for debugging
                        user_id_from_token = token_info.get('user_id')
                        team_id = token_info.get('team_id')
                        logging.info(f"Token belongs to user_id={user_id_from_token}, team_id={team_id}")
                except Exception as scope_error:
                    logging.warning(f"Unable to determine token scopes: {scope_error}")
                
                # Check specifically for important scopes
                has_history_access = any(scope in ['groups:history', 'channels:history', 'chat:write'] for scope in available_scopes)
                if has_history_access:
                    logging.info(f"Token has necessary history access scopes!")
                else:
                    logging.warning(f"Token may have LIMITED access: missing history scopes")
                
                # Token is valid, store it
                self.user_tokens[user_id] = token
                self.user_clients[user_id] = test_client
                
                # Store scope information for future reference
                if hasattr(self, 'user_scopes'):
                    self.user_scopes[user_id] = available_scopes
                else:
                    self.user_scopes = {user_id: available_scopes}
                
                logging.info(f"Successfully registered user token for user {user_id} ({test_response.get('user', 'unknown')})")
                return True
                
            except SlackApiError as e:
                logging.warning(f"Error validating user token: {e}")
                return False
                
        except Exception as e:
            logging.error(f"Error registering user token: {e}")
            return False
            
    def get_client_for_user(self, user_id: str = None) -> WebClient:
        """Get the appropriate Slack client for a user (user client if available, bot client as fallback)"""
        if user_id and user_id in self.user_clients:
            return self.user_clients[user_id]
        return self.client
        
    def _get_channel_id(self, channel_name: str, user_id: str = None) -> str:
        """Get channel ID from name with caching and user token support"""
        # Force lowercase for comparison
        channel_name = channel_name.lower()
        
        # Check if we need to refresh the cache
        current_time = time.time()
        if current_time - self.last_cache_update > self.cache_ttl:
            # Mark cache as expired but don't block this request
            # Prefetch will happen separately
            self.last_cache_update = 0
            
        # Check if the channel is in our cache
        if channel_name in self.channel_cache:
            channel_id = self.channel_cache[channel_name]
            logging.info(f"Channel ID for '{channel_name}' found in cache: {channel_id}")
            return channel_id
            
        # If not in cache and cache is empty or old, trigger a background fetch
        if not self.channel_cache or current_time - self.last_cache_update > self.cache_ttl:
            logging.info(f"Channel cache empty or expired, scheduling background fetch")
            # Don't block the current request, just schedule for next time
            try:
                # Create a task to fetch channels in the background
                loop = asyncio.get_event_loop()
                loop.create_task(self._fetch_all_channels())
            except Exception as e:
                logging.warning(f"Could not schedule background channel fetch: {e}")
            
        # If not in cache, we'll need to search for it
        # Get the appropriate client based on user_id
        client = self.get_client_for_user(user_id)
        
        # For private team- channels, attempt with special handling
        is_team_channel = channel_name.startswith("team-")
        if is_team_channel:
            logging.info(f"Detected private team- channel: {channel_name}, using specialized lookup")
            
        # Check if this matches the current channel ID pattern (C followed by letters/numbers)
        if channel_name.startswith('c') and len(channel_name) >= 9 and channel_name[1:].isalnum():
            # This might be a channel ID already, try to verify it
            try:
                potential_id = channel_name.upper()
                channel_info = self.client.conversations_info(channel=potential_id)
                if channel_info and channel_info.get('ok'):
                    actual_name = channel_info.get('channel', {}).get('name', '').lower()
                    logging.info(f"Verified channel ID: {potential_id} (name: {actual_name})")
                    # Cache for future lookups
                    self.channel_cache[actual_name] = potential_id
                    self.channel_cache[channel_name] = potential_id  # Also cache the ID itself
                    return potential_id
            except Exception as e:
                logging.debug(f"Not a valid channel ID: {channel_name} - {e}")
                # Continue with normal lookup
            
        try:
            # Try to find the channel by name first
            result = client.conversations_list(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200  # Increased from 50 to 200 for better coverage
            )
            channels_list = result.get('channels', [])
            
            # Look for exact matches first
            for ch in channels_list:
                ch_name = ch['name'].lower()
                if ch_name == channel_name:
                    # Found exact match
                    channel_id = ch['id']
                    # Save to cache
                    self.channel_cache[channel_name] = channel_id
                    logging.info(f"Found exact match for channel '{channel_name}': {channel_id}")
                    return channel_id
            
            # If no exact match, try partial match      
            for ch in channels_list:
                ch_name = ch['name'].lower()
                if ch_name.startswith(channel_name) or channel_name.startswith(ch_name):
                    # Found partial match
                    channel_id = ch['id']
                    # Save to cache
                    self.channel_cache[channel_name] = channel_id
                    logging.info(f"Using partial match for channel '{channel_name}': {ch['name']} ({channel_id})")
                    return channel_id
                    
            # If still no match and we have cursor pagination, try next page
            cursor = result.get('response_metadata', {}).get('next_cursor')
            if cursor:
                try:
                    # Try one more page to find the channel
                    result = client.conversations_list(
                        types="public_channel,private_channel",
                        exclude_archived=True,
                        limit=200,
                        cursor=cursor
                    )
                    channels_list = result.get('channels', [])
                    
                    # Look for matches again
                    for ch in channels_list:
                        ch_name = ch['name'].lower()
                        if ch_name == channel_name or ch_name.startswith(channel_name) or channel_name.startswith(ch_name):
                            channel_id = ch['id']
                            self.channel_cache[channel_name] = channel_id
                            logging.info(f"Found match for '{channel_name}' on second page: {channel_id}")
                            return channel_id
                except Exception as e:
                    logging.warning(f"Error checking second page for channels: {e}")
            
            # For private team- channels, try special methods
            if is_team_channel:
                # Try special approaches for team- channels
                try:
                    # Try to list channels the user/bot is a member of
                    member_result = client.users_conversations(
                        types="private_channel",
                        exclude_archived=True,
                        limit=100
                    )
                    
                    # Check each channel the client is a member of
                    member_channels = member_result.get('channels', [])
                    logging.info(f"Checking {len(member_channels)} member channels for team-* match")
                    
                    # Look for exact matches in member channels
                    for ch in member_channels:
                        ch_name = ch.get('name', '').lower()
                        if ch_name == channel_name:
                            channel_id = ch.get('id')
                            self.channel_cache[channel_name] = channel_id
                            logging.info(f"Found exact match for team channel in members list: {channel_id}")
                            return channel_id
                    
                    # Try partial match for team- channels (more flexible matching)
                    for ch in member_channels:
                        ch_name = ch.get('name', '').lower()
                        # Check if both are team- channels with similarity
                        if ch_name.startswith('team-') and (
                               ch_name.startswith(channel_name) or 
                               channel_name.startswith(ch_name) or
                               (len(ch_name) > 6 and len(channel_name) > 6 and
                                ch_name[5:] in channel_name or channel_name[5:] in ch_name)
                           ):
                            channel_id = ch.get('id')
                            self.channel_cache[channel_name] = channel_id
                            logging.info(f"Found similar team channel: {ch_name} ({channel_id})")
                            return channel_id
                            
                    # If we have more pages, check them too
                    cursor = member_result.get('response_metadata', {}).get('next_cursor')
                    if cursor:
                        member_result = client.users_conversations(
                            types="private_channel",
                            exclude_archived=True,
                            limit=100,
                            cursor=cursor
                        )
                        for ch in member_result.get('channels', []):
                            ch_name = ch.get('name', '').lower()
                            if ch_name.startswith('team-') and (
                                   ch_name.startswith(channel_name) or 
                                   channel_name.startswith(ch_name) or
                                   (len(ch_name) > 6 and len(channel_name) > 6 and
                                    ch_name[5:] in channel_name or channel_name[5:] in ch_name)
                               ):
                                channel_id = ch.get('id')
                                self.channel_cache[channel_name] = channel_id
                                logging.info(f"Found similar team channel on second page: {ch_name} ({channel_id})")
                                return channel_id
                                
                    # Try last resort for known naming patterns
                    if "-" in channel_name[5:]:
                        # Try variations of team channel names
                        base_name = channel_name[5:]  # Remove "team-" prefix
                        parts = base_name.split("-")
                        # Try common variations like team-name-project vs team-project-name
                        variations = []
                        if len(parts) >= 2:
                            # Try different orderings
                            variations.append(f"team-{parts[1]}-{parts[0]}")
                            # Try with just the first or second part
                            variations.append(f"team-{parts[0]}")
                            variations.append(f"team-{parts[1]}")
                        
                        for variant in variations:
                            logging.info(f"Trying team channel variation: {variant}")
                            for ch in member_channels:
                                ch_name = ch.get('name', '').lower()
                                if ch_name == variant:
                                    channel_id = ch.get('id')
                                    self.channel_cache[channel_name] = channel_id
                                    logging.info(f"Found team channel through variation: {variant} ({channel_id})")
                                    return channel_id
                
                except SlackApiError as se:
                    logging.warning(f"Error in team channel specialized lookup: {se}")
                    
                # Final fallback - if we're working with the current channel ID in a conversation
                # Try to extract the channel ID from the context
                if user_id and hasattr(self, 'get_current_channel_id'):
                    try:
                        context_channel_id = self.get_current_channel_id(user_id)
                        if context_channel_id:
                            logging.info(f"Using current context channel ID: {context_channel_id}")
                            self.channel_cache[channel_name] = context_channel_id
                            return context_channel_id
                    except Exception as ce:
                        logging.warning(f"Error getting current context: {ce}")
                
        except SlackApiError as e:
            logging.error(f"Error looking up channel by name: {str(e)}")
            
            # If rate limited, try to use the global cache
            if "ratelimited" in str(e).lower() and self.channel_cache:
                # Try fuzzy matching in the existing cache
                for cached_name, cached_id in self.channel_cache.items():
                    if channel_name in cached_name or cached_name in channel_name:
                        logging.info(f"Using fuzzy cache match for '{channel_name}': {cached_name} ({cached_id})")
                        return cached_id
        
        # If team- channel and we still haven't found it, it's likely a private channel
        # that needs a special approach - rather than returning None, try a more lenient approach
        if is_team_channel:
            logging.warning(f"Team channel '{channel_name}' not found in regular lookups, using lenient matching")
            
            # Return the channel name directly to allow the operation to continue
            # The operations might still succeed if the bot has been invited 
            # and the API doesn't need the ID for this specific operation
            # NOTE: This approach has limitations but helps in some cases
            return None
        
        return None
        
    async def _fetch_all_channels(self):
        """Fetch and cache all channel information with unlimited channel support"""
        # Check if cache is still valid
        current_time = time.time()
        if current_time - self.last_cache_update <= self.cache_ttl and self.channel_cache and len(self.channel_cache) > 100:
            logging.info(f"Using existing channel cache with {len(self.channel_cache)} channels (cache is still valid)")
            return
            
        try:
            logging.info("Starting comprehensive channel fetch of ALL channels including DMs...")
            cursor = None
            has_more = True
            all_channels = []
            api_calls = 0
            
            # Unlimited API calls with appropriate rate limiting to ensure we get ALL channels
            while has_more:
                api_calls += 1
                api_args = {
                    "types": "public_channel,private_channel,im",  # Include DMs (im) but not group DMs (mpim) due to permission limitations
                    "exclude_archived": True,
                    "limit": 1000  # Maximum allowed by Slack API
                }
                
                if cursor:
                    api_args["cursor"] = cursor
                
                logging.info(f"Making Slack API call #{api_calls} to fetch channels batch...")
                response = self.client.conversations_list(**api_args)
                channels = response.get('channels', [])
                channels_count = len(channels)
                all_channels.extend(channels)
                
                logging.info(f"Fetched {channels_count} channels in batch {api_calls} (total: {len(all_channels)})")
                
                # Check if we need to fetch more channels
                cursor = response.get('response_metadata', {}).get('next_cursor')
                has_more = bool(cursor and cursor.strip())
                
                # Add a small delay between API calls to avoid rate limiting
                if has_more:
                    await asyncio.sleep(0.5)
                
                # Log progress for larger workspaces
                if api_calls % 5 == 0:
                    logging.info(f"Processing large workspace: {len(all_channels)} channels fetched so far...")
            
            # Update the cache with all channels
            self.channel_cache = {}  # Clear existing cache
            dm_count = 0
            regular_count = 0

            for ch in all_channels:
                try:
                    ch_id = ch['id']
                    
                    # Handle DM channels differently - they don't have a 'name' field
                    if ch_id.startswith('D'):
                        # For DMs, use the user ID as the key or a special format
                        user_id = ch.get('user')
                        if user_id:
                            # Store with a special dm_ prefix to avoid conflicts
                            ch_key = f"dm_{user_id}".lower()
                            self.channel_cache[ch_key] = ch_id
                            dm_count += 1
                        else:
                            # Fallback if no user ID is available
                            ch_key = f"dm_{ch_id}".lower()
                            self.channel_cache[ch_key] = ch_id
                            dm_count += 1
                    else:
                        # Regular channels have a name field
                        if 'name' in ch:
                            ch_name = ch['name'].lower()
                            self.channel_cache[ch_name] = ch_id
                            regular_count += 1
                except Exception as ch_error:
                    logging.warning(f"Error processing channel: {ch_error} - Channel data: {ch.get('id', 'unknown')}")
                    continue
                
            self.last_cache_update = current_time
            logging.info(f"COMPLETE: Updated channel cache with {len(self.channel_cache)} total channels ({regular_count} regular, {dm_count} DMs) after {api_calls} API calls")
            
            # Try to persist the cache to disk for faster future startups
            self._save_channel_cache_to_disk()
            
        except SlackApiError as e:
            if "ratelimited" in str(e).lower():
                logging.error(f"Rate limited when fetching channels: {str(e)}")
                # If we were rate limited but got some channels, still use them
                if all_channels:
                    logging.warning(f"Using partial channel list due to rate limiting ({len(all_channels)} channels)")
                    # Update cache with what we have
                    for ch in all_channels:
                        self.channel_cache[ch['name'].lower()] = ch['id']
                    self.last_cache_update = current_time
                    
                    # Try to persist even the partial cache
                    self._save_channel_cache_to_disk()
            else:
                logging.error(f"Slack API error fetching channels: {str(e)}")
                
                # Try to load channels from disk cache if API fails
                if self._load_channel_cache_from_disk():
                    logging.info(f"Loaded {len(self.channel_cache)} channels from disk cache after API error")
                    return
        except Exception as e:
            logging.error(f"Unexpected error fetching channels: {str(e)}")
            
            # Try to load channels from disk cache if fetch fails
            if self._load_channel_cache_from_disk():
                logging.info(f"Loaded {len(self.channel_cache)} channels from disk cache after error")
                
    def _save_channel_cache_to_disk(self):
        """Save channel cache to disk for persistence between restarts"""
        try:
            import os
            import json
            
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'cache')
            os.makedirs(cache_dir, exist_ok=True)
            
            cache_file = os.path.join(cache_dir, 'slack_channels_cache.json')
            
            # Save channel data
            cache_data = {
                'timestamp': self.last_cache_update,
                'channels': self.channel_cache
            }
            
            with open(cache_file, 'w') as f:
                json.dump(cache_data, f)
                
            logging.info(f"Successfully saved {len(self.channel_cache)} channels to disk cache")
            return True
        except Exception as e:
            logging.error(f"Error saving channel cache to disk: {e}")
            return False
            
    def _load_channel_cache_from_disk(self):
        """Load channel cache from disk if available"""
        try:
            import os
            import json
            
            cache_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), 'cache')
            cache_file = os.path.join(cache_dir, 'slack_channels_cache.json')
            
            if not os.path.exists(cache_file):
                logging.info("No channel cache file found on disk")
                return False
                
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
                
            # Check if cache is still valid (less than 1 day old)
            cache_timestamp = cache_data.get('timestamp', 0)
            current_time = time.time()
            
            if current_time - cache_timestamp > 86400:  # 24 hours
                logging.info("Disk cache is older than 24 hours, not using it")
                return False
                
            self.channel_cache = cache_data.get('channels', {})
            self.last_cache_update = cache_timestamp
            
            logging.info(f"Successfully loaded {len(self.channel_cache)} channels from disk cache")
            return True
        except Exception as e:
            logging.error(f"Error loading channel cache from disk: {e}")
            return False
        
    async def validate_inputs(self, inputs: Dict[str, Any]) -> bool:
        """Validate Slack-specific inputs.
        
        Inputs are valid if ANY of the following are provided:
        1. Thread URLs
        2. Channel(s) 
        3. A query string (enables global search across all channels)
        
        Args:
            inputs: Dictionary of input parameters
            
        Returns:
            bool: True if inputs are valid, False otherwise
        """
        try:
            # Check for thread URLs first
            thread_urls = inputs.get('thread_urls', [])
            if thread_urls:
                # If we have thread URLs, validate them
                if isinstance(thread_urls, list) and len(thread_urls) > 0:
                    logging.info(f"Validating {len(thread_urls)} thread URLs")
                    return True
                elif isinstance(thread_urls, str) and thread_urls.strip():
                    logging.info(f"Validating single thread URL: {thread_urls}")
                    return True
            
            # Check for channel in different formats (existing logic)
            channels = []
            
            # Handle single channel parameter (string, dict, or list)
            if 'channel' in inputs:
                channel_param = inputs.get('channel')
                # Handle different format types
                if isinstance(channel_param, dict):
                    channels.append(channel_param)
                elif isinstance(channel_param, list):
                    channels.extend(channel_param)
                else:
                    channels.append(channel_param)
            
            # Handle channels list parameter
            if 'channels' in inputs:
                channels_param = inputs.get('channels')
                if isinstance(channels_param, list):
                    channels.extend(channels_param)
                else:
                    channels.append(channels_param)
            
            # NEW: If no channels but a query is provided, enable global search mode
            query = inputs.get('query', '').strip()
            if not channels and query:
                logging.info(f"No channel specified but query provided: '{query}' - enabling global search mode")
                inputs['_global_search_mode'] = True
                return True
            
            if not channels:
                logging.error("No channel specified, no thread URLs, and no query provided")
                return False
            
            # For validation, we'll accept any channel format - actual validation will happen during fetch
            # This makes the UI more flexible and less prone to errors
            return True
                
        except Exception as e:
            logging.error(f"Error in validate_inputs: {str(e)}")
            return False
    
    def extract_time_range(self, query: str) -> tuple[Optional[datetime], Optional[datetime]]:
        """
        Extract time ranges from natural language queries.
        
        Args:
            query: The natural language query string
            
        Returns:
            Tuple of (start_time, end_time) as datetime objects, or (None, None) if no time found
            
        Examples:
            "messages from last week" -> (start_of_last_week, end_of_last_week)
            "between Monday and Wednesday" -> (monday_datetime, wednesday_datetime)
            "since yesterday" -> (yesterday_datetime, None)
            "recent messages" -> (2_hours_ago, None)
        """
        try:
            # Check if query contains explicit time indicators first
            time_indicators = [
                'since', 'from', 'after', 'until', 'before', 'to', 'between',
                'last week', 'this week', 'last month', 'this month', 
                'yesterday', 'today', 'this morning', 'ago', 'hours ago', 'days ago',
                'last night', 'last weekend', 'this weekend', 'over the weekend',
                'recent', 'lately', 'latest', 'new', 'just', 'missed', 'happened'
            ]
            
            has_time_indicator = any(indicator in query.lower() for indicator in time_indicators)
            
            # If no explicit time indicators, check for date patterns
            if not has_time_indicator:
                # Look for date patterns like YYYY-MM-DD, MM/DD/YYYY, etc.
                date_patterns = [
                    r'\d{4}-\d{1,2}-\d{1,2}',  # YYYY-MM-DD
                    r'\d{1,2}/\d{1,2}/\d{4}',  # MM/DD/YYYY
                    r'\d{1,2}-\d{1,2}-\d{4}',  # MM-DD-YYYY
                    r'(january|february|march|april|may|june|july|august|september|october|november|december)',  # Month names
                    r'(monday|tuesday|wednesday|thursday|friday|saturday|sunday)',  # Day names
                ]
                
                has_date_pattern = any(re.search(pattern, query.lower()) for pattern in date_patterns)
                
                if not has_date_pattern:
                    return None, None
            
            # First, try to use search_dates to find all dates in the query
            results = search_dates(query)
            
            # Filter out invalid/spurious date matches from search_dates
            if results:
                # Filter out results that are likely false positives
                filtered_results = []
                for date_string, parsed_date in results:
                    # Skip single words that are incorrectly parsed as dates
                    if len(date_string.split()) == 1 and date_string.lower() in ['did', 'any', 'i', 'what', 'the', 'a', 'an']:
                        continue
                    # Skip relative time expressions that should be handled by our patterns
                    if 'minutes' in date_string.lower() or 'hours' in date_string.lower():
                        continue
                    # Keep this result
                    filtered_results.append((date_string, parsed_date))
                
                # If all results were filtered out, set to None to allow recency pattern checking
                results = filtered_results if filtered_results else None
            
            # Also try dateparser.parse for more complex expressions
            if not results:
                # Try parsing the entire query as a date expression
                parsed_date = dateparser.parse(query)
                if parsed_date:
                    results = [(query, parsed_date)]
            
            # Handle specific patterns that search_dates might miss
            if not results:
                # Handle relative time expressions
                relative_patterns = {
                    r'\blast week\b': lambda: self._get_last_week(),
                    r'\bthis week\b': lambda: self._get_this_week(),
                    r'\blast month\b': lambda: self._get_last_month(),
                    r'\bthis month\b': lambda: self._get_this_month(),
                    r'\bthis morning\b': lambda: self._get_this_morning(),
                    r'\btoday\b': lambda: self._get_today(),
                    r'\byesterday\b': lambda: self._get_yesterday(),
                    r'\blast night\b': lambda: self._get_last_night(),
                    r'\blast weekend\b': lambda: self._get_last_weekend(),
                    r'\bthis weekend\b': lambda: self._get_this_weekend(),
                    r'\bover the weekend\b': lambda: self._get_last_weekend(),
                }
                
                for pattern, func in relative_patterns.items():
                    if re.search(pattern, query.lower()):
                        start_time, end_time = func()
                        return start_time, end_time
                
                # Handle implicit recency patterns with default time windows
                recency_patterns = [
                    # Process numbered patterns first (more specific)
                    (r'\bin the last (\d+) minutes?\b', lambda match: self._get_last_minutes(int(match.group(1)))),
                    (r'\blast (\d+) minutes?\b', lambda match: self._get_last_minutes(int(match.group(1)))),
                    (r'\bfew minutes\b', lambda: self._get_last_minutes(5)),  # "last few minutes"
                    
                    # Very recent (last 10-30 minutes)
                    (r'\bjust happened\b', lambda: self._get_last_minutes(15)),
                    (r'\bwhat just\b', lambda: self._get_last_minutes(10)),
                    
                    # Catch-up patterns (since user was away) - check before general patterns  
                    (r'\bmiss\b', lambda: self._get_last_hours(8)),  # miss, missed, missing
                    (r'\bmissed\b', lambda: self._get_last_hours(8)),
                    (r'\bmissing\b', lambda: self._get_last_hours(8)),
                    (r'\bwhile.*away\b', lambda: self._get_last_hours(12)),  # while I was away, while away
                    (r'\bsince I last checked\b', lambda: self._get_last_hours(4)),
                    (r'\bcatch me up\b', lambda: self._get_last_hours(6)),
                    (r'\bfill me in\b', lambda: self._get_last_hours(4)),
                    
                    # Short-term recent (last 1-2 hours)  
                    (r'\brecent\b', lambda: self._get_last_hours(2)),
                    (r'\blately\b', lambda: self._get_last_hours(4)),
                    (r'\blatest\b', lambda: self._get_last_hours(1)),
                    (r'\bnew\b', lambda: self._get_last_hours(2)),
                    (r'\bupdates?\b', lambda: self._get_last_hours(2)),
                    (r'\bactivity\b', lambda: self._get_last_hours(3)),
                    
                    # Digest/summary patterns (longer window)
                    (r'\bdigest\b', lambda: self._get_last_hours(8)),
                    (r'\brundown\b', lambda: self._get_last_hours(6)),
                    (r'\brecap\b', lambda: self._get_last_hours(4)),
                    (r'\bsummary\b', lambda: self._get_last_hours(6)),
                ]
                
                # Check recency patterns with match groups
                for pattern, func in recency_patterns:
                    match = re.search(pattern, query.lower())
                    if match:
                        if func.__code__.co_argcount > 0:  # Function expects match argument
                            start_time, end_time = func(match)
                        else:
                            start_time, end_time = func()
                        return start_time, end_time
                        
                # Handle "between X and Y" patterns with improved parsing
                between_match = re.search(r'between\s+([^and]+)\s+and\s+(.+)', query.lower())
                if between_match:
                    start_phrase, end_phrase = between_match.groups()
                    start_phrase = start_phrase.strip()
                    end_phrase = end_phrase.strip()
                    
                    # Try to parse both parts as dates
                    start_parsed = dateparser.parse(start_phrase, settings={'PREFER_DATES_FROM': 'past'})
                    end_parsed = dateparser.parse(end_phrase, settings={'PREFER_DATES_FROM': 'past'})
                    
                    if start_parsed and end_parsed:
                        # Ensure proper order
                        if start_parsed > end_parsed:
                            start_parsed, end_parsed = end_parsed, start_parsed
                        return start_parsed, end_parsed
            
            if not results:
                return None, None
                
            # Extract datetime objects from results
            parsed_times = [dt for _, dt in results]
            
            if len(parsed_times) == 1:
                # Single time found - could be "since X" or "until X"
                time_point = parsed_times[0]
                
                # Check if it's a "since" query
                if any(word in query.lower() for word in ['since', 'from', 'after']):
                    return time_point, None
                # Check if it's an "until" query  
                elif any(word in query.lower() for word in ['until', 'before', 'to']):
                    return None, time_point
                else:
                    # Default to "since" behavior for single dates
                    return time_point, None
                    
            elif len(parsed_times) >= 2:
                # Multiple times found - use first and last as range
                sorted_times = sorted(parsed_times)
                return sorted_times[0], sorted_times[-1]
                
            return None, None
            
        except Exception as e:
            logging.warning(f"Error extracting time range from query '{query}': {e}")
            return None, None
    
    def _get_last_week(self) -> tuple[datetime, datetime]:
        """Get start and end of last week"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        days_since_monday = now.weekday()
        last_monday = now - timedelta(days=days_since_monday + 7)
        last_sunday = last_monday + timedelta(days=6)
        return last_monday.replace(hour=0, minute=0, second=0, microsecond=0), \
               last_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    def _get_this_week(self) -> tuple[datetime, datetime]:
        """Get start and end of this week"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        days_since_monday = now.weekday()
        this_monday = now - timedelta(days=days_since_monday)
        this_sunday = this_monday + timedelta(days=6)
        return this_monday.replace(hour=0, minute=0, second=0, microsecond=0), \
               this_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    def _get_last_month(self) -> tuple[datetime, datetime]:
        """Get start and end of last month"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        if now.month == 1:
            last_month = now.replace(year=now.year - 1, month=12, day=1)
        else:
            last_month = now.replace(month=now.month - 1, day=1)
        
        # Get last day of last month
        if last_month.month == 12:
            next_month = last_month.replace(year=last_month.year + 1, month=1, day=1)
        else:
            next_month = last_month.replace(month=last_month.month + 1, day=1)
        
        last_day = next_month - timedelta(days=1)
        
        return last_month.replace(hour=0, minute=0, second=0, microsecond=0), \
               last_day.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    def _get_this_month(self) -> tuple[datetime, datetime]:
        """Get start and end of this month"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        first_day = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        # Get last day of this month
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month = now.replace(month=now.month + 1, day=1)
        
        last_day = next_month - timedelta(days=1)
        
        return first_day, last_day.replace(hour=23, minute=59, second=59, microsecond=999999)
    
    def _get_this_morning(self) -> tuple[datetime, datetime]:
        """Get this morning (6 AM to 12 PM today)"""
        now = datetime.now(timezone.utc)
        morning_start = now.replace(hour=6, minute=0, second=0, microsecond=0)
        morning_end = now.replace(hour=12, minute=0, second=0, microsecond=0)
        return morning_start, morning_end
    
    def _get_today(self) -> tuple[datetime, datetime]:
        """Get start and end of today"""
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start_of_day, end_of_day
    
    def _get_yesterday(self) -> tuple[datetime, datetime]:
        """Get start and end of yesterday"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        start_of_day = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
        return start_of_day, end_of_day

    def _get_last_night(self) -> tuple[datetime, datetime]:
        """Get last night (6 PM yesterday to 6 AM today)"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        yesterday = now - timedelta(days=1)
        night_start = yesterday.replace(hour=18, minute=0, second=0, microsecond=0)  # 6 PM yesterday
        night_end = now.replace(hour=6, minute=0, second=0, microsecond=0)  # 6 AM today
        return night_start, night_end

    def _get_last_weekend(self) -> tuple[datetime, datetime]:
        """Get last weekend (Saturday and Sunday)"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        
        # Find last Saturday
        days_since_saturday = (now.weekday() + 2) % 7  # Saturday is 5, Sunday is 6, Monday is 0, etc.
        if days_since_saturday == 0:  # If today is Saturday
            days_since_saturday = 7  # Go to previous Saturday
        elif days_since_saturday == 1:  # If today is Sunday
            days_since_saturday = 8  # Go to previous Saturday
        
        last_saturday = now - timedelta(days=days_since_saturday)
        last_sunday = last_saturday + timedelta(days=1)
        
        weekend_start = last_saturday.replace(hour=0, minute=0, second=0, microsecond=0)
        weekend_end = last_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
        return weekend_start, weekend_end

    def _get_this_weekend(self) -> tuple[datetime, datetime]:
        """Get this weekend (Saturday and Sunday)"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        
        # Find this Saturday
        days_to_saturday = (5 - now.weekday()) % 7  # Saturday is 5
        if days_to_saturday == 0 and now.weekday() == 5:  # If today is Saturday
            this_saturday = now
        elif now.weekday() == 6:  # If today is Sunday
            this_saturday = now - timedelta(days=1)  # Yesterday was Saturday
        else:
            this_saturday = now + timedelta(days=days_to_saturday)
        
        this_sunday = this_saturday + timedelta(days=1)
        
        weekend_start = this_saturday.replace(hour=0, minute=0, second=0, microsecond=0)
        weekend_end = this_sunday.replace(hour=23, minute=59, second=59, microsecond=999999)
        return weekend_start, weekend_end

    def _get_last_minutes(self, minutes: int) -> tuple[datetime, datetime]:
        """Get time range for the last N minutes"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(minutes=minutes)
        return start_time, now

    def _get_last_hours(self, hours: int) -> tuple[datetime, datetime]:
        """Get time range for the last N hours"""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        start_time = now - timedelta(hours=hours)
        return start_time, now

    def to_unix_timestamp(self, dt: datetime) -> float:
        """
        Convert datetime to Unix timestamp for Slack API.
        
        Args:
            dt: datetime object to convert
            
        Returns:
            Unix timestamp as float
        """
        if dt is None:
            return None
            
        # Ensure timezone awareness - assume UTC if naive
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
            
        return dt.timestamp()
    
    def _build_history_args(self, channel_id: str, limit: int = 100, cursor: str = None, 
                           oldest_timestamp: float = None, latest_timestamp: float = None) -> Dict[str, Any]:
        """
        Build arguments for conversations_history API calls with time constraints.
        
        Args:
            channel_id: Slack channel ID
            limit: Maximum number of messages to retrieve
            cursor: Pagination cursor
            oldest_timestamp: Oldest timestamp to include (Unix timestamp)
            latest_timestamp: Latest timestamp to include (Unix timestamp)
            
        Returns:
            Dictionary of arguments for conversations_history API
        """
        history_args = {
            "channel": channel_id,
            "limit": limit
        }
        
        if cursor:
            history_args["cursor"] = cursor
            
        if oldest_timestamp is not None:
            history_args["oldest"] = str(oldest_timestamp)
            
        if latest_timestamp is not None:
            history_args["latest"] = str(latest_timestamp)
            
        return history_args

    async def fetch_data(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Fetch data from Slack channels combining both search API and history API for best results.
        
        This method supports three modes:
        1. Thread URLs - Process specific Slack thread URLs
        2. Channel-specific - Search within specified channel(s)
        3. Global search - Search across all accessible channels (when no channel specified)
        
        Args:
            params: Dictionary of parameters:
                - thread_urls: Optional list of thread URLs to process
                - channel/channels: Optional channel(s) to search
                - query: Search query string
                - user_id: User ID for token lookup
                - deep_search: Enable thorough search
                - limit: Maximum results to return
                
        Returns:
            Dict containing search results and metadata
        """
        try:
            # Check if we have thread URLs to process
            thread_urls = params.get('thread_urls', [])
            if thread_urls:
                return await self._process_thread_urls(thread_urls, params)
            
            # Check for global search mode (no channels specified but query provided)
            query = params.get('query', '').strip()
            is_global_search = params.get('_global_search_mode', False)
            
            # Check if channels are specified
            channels_specified = bool(
                params.get('channel') or params.get('channels')
            )
            
            # Route to global search if no channels but query exists
            if not channels_specified and query:
                is_global_search = True
            
            if is_global_search:
                logging.info(f"Routing to global search for query: '{query}'")
                global_result = await self.global_search(query, params)
                
                # Convert GlobalSearchResult to the expected Dict format
                return self._convert_global_result_to_dict(global_result)
            
            results = {}
            channel_relevance = {}  # Track per-channel relevance
            start_time = time.time()
            failed_channels = []  # Track channels that failed processing
            
            # Extract parameters
            channels = []
            user_token = params.get('user_token')
            user_id = params.get('user_id')
            current_channel_id = params.get('current_channel_id')
            
            # Always enable deep_search for any query to ensure comprehensive results
            deep_search = params.get('deep_search', False)
            if query:
                # Enable deep search for all queries
                deep_search = True
                logging.info(f"Automatically enabled deep search for query: '{query}'")
                
                # Set high page limit for thorough search
                params['max_pages'] = params.get('max_pages', 15)  # Default to 15 pages for all queries
                logging.info(f"Using higher page limit ({params['max_pages']}) to ensure comprehensive results")
            
            # Detect if user is looking for oldest/first messages
            search_mode = params.get('search_mode', 'relevance')
            oldest_message_patterns = [
                'first message', 'oldest message', 'earliest message', 
                'beginning of', 'start of', 'when.*channel.*start',
                'when.*channel.*create', 'creation', 'initial message'
            ]
            
            # Check if query is asking for oldest messages
            if query and any(re.search(pattern, query.lower()) for pattern in oldest_message_patterns):
                search_mode = 'oldest'
                # Increase max pages for better results when looking for oldest messages
                params['max_pages'] = params.get('max_pages', 20)  
                logging.info(f"Detected request for oldest messages: '{query}'. Enabling oldest message search mode.")
            
            # Get the best client for operations
            user_client = None
            if user_token:
                try:
                    user_client = WebClient(token=user_token)
                    logging.info(f"Created user client with provided token")
                except Exception as e:
                    logging.error(f"Error creating user client with provided token: {e}")
            elif user_id and user_id in self.user_tokens:
                user_client = self.user_clients.get(user_id)
                logging.info(f"Using stored user client for user {user_id}")
            
            if not user_client and self.default_user_client:
                user_client = self.default_user_client
                logging.info("Using default user client from config for search operations")
            
            # Process channel parameters
            if 'channel' in params:
                channel_param = params.get('channel')
                if isinstance(channel_param, dict):
                    channels.append(channel_param)
                elif isinstance(channel_param, list):
                    channels.extend(channel_param)
                else:
                    channels.append(channel_param)
            
            if 'channels' in params:
                channels_param = params.get('channels')
                if isinstance(channels_param, list):
                    channels.extend(channels_param)
                else:
                    channels.append(channels_param)
            
            logging.info(f"Processing data from {len(channels)} channels")
            
            # Set search parameters
            # If deep_search is enabled, increase limits substantially
            if deep_search:
                fetch_limit = min(params.get('fetch_limit', 500), 1000)  # Allow up to 1000 messages
                max_return_limit = min(params.get('limit', 100), 200)  # Return more messages
                # Override max_pages for history API
                params['max_pages'] = params.get('max_pages', 10)  # Default to 10 pages for deep search
                logging.info(f"Deep search enabled with fetch_limit={fetch_limit}, max_return_limit={max_return_limit}, max_pages={params.get('max_pages')}")
            else:
                fetch_limit = min(params.get('fetch_limit', 75), 100)
                max_return_limit = min(params.get('limit', 20), 40)
                
            use_search_api = bool(user_client and query and len(query) > 2)
            use_history_api = True  # Always use history API as a fallback or complement
            
            # Extract keywords for relevance calculations
            query_keywords = self._extract_keywords(query) if query else []
            
            # STEP 5: Extract time ranges from query and route time-based queries automatically
            oldest_timestamp = None
            latest_timestamp = None
            has_time_constraint = False
            
            if query:
                start_time_dt, end_time_dt = self.extract_time_range(query)
                if start_time_dt or end_time_dt:
                    has_time_constraint = True
                    oldest_timestamp = self.to_unix_timestamp(start_time_dt) if start_time_dt else None
                    latest_timestamp = self.to_unix_timestamp(end_time_dt) if end_time_dt else None
                    
                    time_desc = []
                    if start_time_dt:
                        time_desc.append(f"from {start_time_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                    if end_time_dt:
                        time_desc.append(f"to {end_time_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                    
                    logging.info(f"Detected time-based query: {' '.join(time_desc)}")
                    logging.info(f"Unix timestamps - oldest: {oldest_timestamp}, latest: {latest_timestamp}")
                    
                    # For time-based queries, prioritize history API for accuracy
                    use_history_api = True
                    
                    # Increase limits for time-based queries since we're looking at specific timeframes
                    if has_time_constraint:
                        fetch_limit = min(params.get('fetch_limit', 1000), 2000)  # Higher limit for time ranges
                        max_return_limit = min(params.get('limit', 200), 500)  # More results for time ranges
                        params['max_pages'] = params.get('max_pages', 20)  # More pages for time-based search
                        logging.info(f"Time-based query detected - increased limits: fetch_limit={fetch_limit}, max_return_limit={max_return_limit}")

            # For search operations, map channels to their IDs
            channel_map = {}  # Maps channel_id to channel_name
            channel_ids = []  # List of channel IDs
            
            # Create mapping of channel IDs to names for search API filtering
            for channel_item in channels:
                try:
                    channel_id = None
                    channel_name = None
                    
                    if isinstance(channel_item, dict):
                        if 'id' in channel_item:
                            channel_id = channel_item['id']
                            channel_name = channel_item.get('name', channel_id)
                        elif 'name' in channel_item:
                            channel_name = channel_item['name']
                            channel_id = self._get_channel_id(channel_name)
                    elif isinstance(channel_item, str):
                        if channel_item.startswith('C'):
                            channel_id = channel_item
                            try:
                                channel_info = self.client.conversations_info(channel=channel_id)
                                channel_name = channel_info['channel']['name']
                            except SlackApiError:
                                channel_name = channel_id
                        else:
                            channel_name = channel_item
                            channel_id = self._get_channel_id(channel_name)
                        
                    if channel_id:
                        channel_ids.append(channel_id)
                        channel_map[channel_id] = channel_name
                except Exception as e:
                    logging.error(f"Error processing channel {channel_item}: {e}")
                    # Continue with other channels
            
            # Data structures to store results
            search_results = {}  # Results from search API
            history_results = {}  # Results from history API
            combined_results = {}  # Final combined results
            
            # Modified search API to support deeper search when enabled
            if use_search_api and channel_ids:
                try:
                    logging.info(f"Using search API for query: '{query}'")
                    
                    # Check if user token has the search:read scope for better search results
                    has_search_scope = False
                    if user_id in self.user_scopes:
                        user_scopes = self.user_scopes.get(user_id, [])
                        has_search_scope = "search:read" in user_scopes
                        if has_search_scope:
                            logging.info(f"User token has search:read scope, will use for search API")
                        else:
                            logging.warning(f"User token missing search:read scope, search may be limited")
                    
                    max_retries = 1
                    for retry in range(max_retries + 1):
                        try:
                            # If deep search, use pagination to get more results
                            search_results_list = []
                            search_pages = 3 if deep_search else 1
                            cursor = None
                            
                            for page in range(search_pages):
                                search_args = {
                                    "query": query,
                                    "sort": "score",  # Sort by relevance
                                    "sort_dir": "desc",
                                    "count": min(fetch_limit, 100)  # API limit is 100 per page
                                }
                                
                                if cursor:
                                    search_args["cursor"] = cursor
                                
                                # Use user token if it has search scope or fall back to default
                                if has_search_scope and user_id in self.user_clients:
                                    search_client = self.user_clients[user_id]
                                    logging.info(f"Using user token for search API call")
                                elif self.default_user_client:
                                    search_client = self.default_user_client
                                    logging.info(f"Using default user token for search API call")
                                else:
                                    search_client = user_client  # Use whatever client we have
                                
                                search_response = search_client.search_messages(**search_args)
                                
                                if search_response.get('ok'):
                                    matches = search_response.get('messages', {}).get('matches', [])
                                    logging.info(f"Search API page {page+1} found {len(matches)} matches")
                                    search_results_list.extend(matches)
                                    
                                    # Get next cursor if more pages requested
                                    if page < search_pages - 1:
                                        cursor = search_response.get('messages', {}).get('pagination', {}).get('next_cursor')
                                        if not cursor:
                                            break  # No more results
                                        await asyncio.sleep(0.5)  # Brief pause to avoid rate limiting
                                else:
                                    # Try to provide detailed error for debugging
                                    error = search_response.get('error', 'unknown error')
                                    needed = search_response.get('needed', 'unknown scope')
                                    provided = search_response.get('provided', 'no scope')
                                    logging.warning(f"Search API error: {error} - needed: {needed}, provided: {provided}")
                                    raise SlackApiError("Search API error", search_response.get('error', 'unknown error'))
                                    
                            # Process all collected matches
                            total_matches = 0
                            for match in search_results_list:
                                match_channel_id = match.get('channel', {}).get('id')
                                
                                # Only include matches from our target channels
                                if match_channel_id in channel_map:
                                    channel_name = channel_map[match_channel_id]
                                    
                                    # Initialize channel results if needed
                                    if channel_name not in search_results:
                                        search_results[channel_name] = []
                                    
                                    # Create message object with relevance score and permalink
                                    timestamp = match.get('ts', '')
                                    # Try to get permalink from search result, generate if not available
                                    permalink = match.get('permalink', '')
                                    if not permalink and timestamp:
                                        permalink = self._generate_permalink(match_channel_id, timestamp)
                                    
                                    message = {
                                        'text': match.get('text', ''),
                                        'user': match.get('user', ''),
                                        'timestamp': timestamp,
                                        'thread_ts': match.get('thread_ts', None),
                                        'relevance_score': match.get('score', 0) * 0.9,  # Use Slack's score
                                        'permalink': permalink,
                                        'from_search': True  # Mark as coming from search API
                                    }
                                    
                                    # Check for duplicates
                                    if not any(msg.get('timestamp') == message['timestamp'] 
                                             for msg in search_results[channel_name]):
                                        search_results[channel_name].append(message)
                                        total_matches += 1
                            
                            logging.info(f"Search API found {total_matches} relevant messages across {len(search_results)} channels")
                            break  # Success, exit retry loop
                        except SlackApiError as e:
                            error_msg = str(e).lower()
                            if "missing_scope" in error_msg:
                                logging.error(f"Missing scope for search: {e}")
                                if retry < max_retries:
                                    logging.info("Trying with different token...")
                                    # Switch token if possible (try the other token)
                                    if has_search_scope:
                                        # Already using best token, just break
                                        break
                                else:
                                    logging.error("Missing search:read scope on all available tokens")
                                    break
                            elif "ratelimited" in error_msg:
                                if retry < max_retries:
                                    logging.warning(f"Search API error, retrying: {e}")
                                    await asyncio.sleep(1)
                                else:
                                    logging.error(f"Search API error after retries: {e}")
                                    break
                            else:
                                logging.error(f"Search API error: {e}")
                                if retry < max_retries:
                                    await asyncio.sleep(1)
                                else:
                                    break
                except Exception as e:
                    logging.error(f"Error using search API: {str(e)}")
                    # Continue with history API if search fails
            
            # Step 2: Use history API to get recent messages from each channel (possibly filtered by search results)
            if use_history_api:
                logging.info("Using history API to complement search results")
                
                # Process each channel in parallel
                async def process_channel_history(channel_id, channel_name):
                    try:
                        logging.info(f"Processing channel: {channel_name} ({channel_id})")
                        
                        # Try to join the channel first (if not already a member)
                        join_succeeded = False
                        for join_retry in range(2):
                            try:
                                self.client.conversations_join(channel=channel_id)
                                logging.info(f"Joined channel: {channel_id}")
                                join_succeeded = True
                                break
                            except SlackApiError as e:
                                error_msg = str(e).lower()
                                # Add method_not_supported_for_channel_type to accepted errors
                                if ("already_in_channel" in error_msg or 
                                    "is_private" in error_msg or 
                                    "method_not_supported_for_channel_type" in error_msg):
                                    # This is actually a success case - we either can't join because:
                                    # 1. We're already in the channel
                                    # 2. It's a private channel (we're invited)
                                    # 3. It's a channel type that doesn't support joining (DMs, private channels)
                                    logging.info(f"Channel access confirmed for {channel_name} ({channel_id})")
                                    join_succeeded = True
                                    break
                                elif join_retry < 1:
                                    logging.warning(f"Error joining channel {channel_id}, retrying: {str(e)}")
                                    await asyncio.sleep(1)
                                else:
                                    logging.error(f"Error joining channel {channel_id}: {str(e)}")
                                    return [], 0  # Return empty results on error
                            except Exception as e:
                                logging.error(f"Unexpected error joining channel {channel_id}: {str(e)}")
                                if join_retry < 1:
                                    await asyncio.sleep(1)
                                else:
                                    return [], 0  # Return empty results
                        
                        # For private channels, we might still have access even if join failed
                        # So attempt to fetch history anyway if the channel looks private
                        if not join_succeeded and channel_name.startswith("team-"):
                            logging.info(f"Attempting to fetch history for private channel {channel_name} despite join failure")
                            join_succeeded = True  # Treat as if join succeeded to continue processing
                        
                        if not join_succeeded:
                            logging.error(f"Failed to join channel {channel_name} after retries")
                            return [], 0
                        
                        # Start with any existing messages from search results
                        existing_messages = search_results.get(channel_name, [])
                        existing_timestamps = {msg.get('timestamp') for msg in existing_messages}
                        
                        # Determine how many pages of history to fetch based on search results and deep_search setting
                        max_history_pages = params.get('max_pages', 1)  # Default to 1
                        
                        # For deep_search, use the specified pages or default to more
                        if deep_search:
                            max_history_pages = params.get('max_pages', 10)  # Default to 10 pages for deep search
                            logging.info(f"Deep search enabled - fetching up to {max_history_pages} pages of history")
                        # Otherwise use normal logic
                        elif len(existing_messages) >= 10:
                            max_history_pages = 1  # Just 1 page if we have good search results
                        elif len(existing_messages) >= 5:
                            max_history_pages = 1  # 1 page for decent search results
                        elif query:  # If there's a query
                            max_history_pages = 2  # 2 pages for query but few search results
                        else:
                            max_history_pages = 1  # 1 page for no query (just recent messages)
                            
                        history_messages = []
                        cursor = None
                        page_success = False
                        
                        for page in range(max_history_pages):
                            api_success = False
                            for history_retry in range(2):  # Try each page twice
                                try:
                                    history_args = {
                                        "channel": channel_id,
                                        "limit": 100 if deep_search else 50  # Get more messages per page for deep search
                                    }
                                    
                                    # STEP 4: Add time constraints to history API calls
                                    if oldest_timestamp is not None:
                                        history_args["oldest"] = str(oldest_timestamp)
                                        logging.info(f"Added oldest timestamp constraint: {oldest_timestamp}")
                                    
                                    if latest_timestamp is not None:
                                        history_args["latest"] = str(latest_timestamp)
                                        logging.info(f"Added latest timestamp constraint: {latest_timestamp}")
                                    
                                    if cursor:
                                        history_args["cursor"] = cursor
                                    
                                    # Determine the best client to use for history fetching
                                    # Try the user client first for private channels
                                    fetch_successful = False
                                    fetch_error = None
                                    
                                    # Try with user client first if available (best for private channels)
                                    if user_id and user_id in self.user_clients:
                                        try:
                                            user_client = self.user_clients[user_id]
                                            
                                            # Check if this is a private team- channel
                                            is_private = channel_name.startswith("team-") or "private" in channel_name
                                            
                                            # Check what scopes we have
                                            available_scopes = []
                                            if hasattr(self, 'user_scopes') and user_id in self.user_scopes:
                                                available_scopes = self.user_scopes[user_id]
                                            
                                            # Log available scopes for debugging
                                            if available_scopes:
                                                logging.info(f"User token has scopes: {','.join(available_scopes)}")
                                            
                                            # If this is a private channel and we have groups:history scope, we can access it
                                            can_access_private = is_private and "groups:history" in available_scopes
                                            # Check for public channel access
                                            can_access_public = "channels:history" in available_scopes
                                            
                                            # Log access capabilities
                                            if is_private:
                                                if can_access_private:
                                                    logging.info(f"User token has groups:history scope for private channel access")
                                                else:
                                                    logging.warning(f"User token missing groups:history scope for private channel")
                                                    
                                            # Try the appropriate method based on what we have
                                            if is_private and can_access_private:
                                                logging.info(f"Attempting private channel history fetch for {channel_name}")
                                                # Use conversations.history which works with groups:history scope
                                                response = user_client.conversations_history(**history_args)
                                                fetch_successful = True
                                            elif can_access_public:
                                                logging.info(f"Attempting public channel history fetch for {channel_name}")
                                                response = user_client.conversations_history(**history_args)
                                                fetch_successful = True
                                            elif "search:read" in available_scopes:
                                                # If only search access, let the client know we're using limited access
                                                logging.info(f"Limited user token access (search only)")
                                                # We'll use bot token for history in this case
                                                raise SlackApiError("limited_scope", {"error": "missing_history_scope"})
                                            else:
                                                raise SlackApiError("missing_scope", {"error": "no_usable_scopes"})
                                        except SlackApiError as ue:
                                            fetch_error = ue
                                            error_msg = str(ue).lower()
                                            
                                            # Provide better error message based on scope
                                            if "missing_scope" in error_msg:
                                                if hasattr(self, 'user_scopes') and user_id in self.user_scopes:
                                                    scopes = self.user_scopes[user_id]
                                                    logging.warning(f"User token missing required scope. Has: {','.join(scopes)}")
                                                else:
                                                    logging.warning(f"User token missing required scope: {ue}")
                                            
                                            logging.warning(f"User token failed for history: {ue} - falling back to bot token")
                                    
                                    # Fall back to bot token if user token failed or wasn't available
                                    if not fetch_successful:
                                        response = self.client.conversations_history(**history_args)
                                    
                                    # Process messages
                                    msgs_in_page = 0
                                    for msg in response.get('messages', []):
                                        msgs_in_page += 1
                                        
                                        # Skip if we already have this message from search
                                        if msg.get('ts') in existing_timestamps:
                                            continue
                                        
                                        # Skip system messages
                                        if msg.get('subtype') in ['channel_join', 'channel_leave']:
                                            continue
                                        
                                        # Calculate relevance if we have a query
                                        relevance_score = 0
                                        if query and query_keywords:
                                            relevance_score = self._calculate_relevance(msg.get('text', ''), query_keywords)
                                            
                                            # If deep search with query, only include messages that are relevant
                                            if deep_search and relevance_score < 0.1:
                                                continue
                                        
                                        # Create formatted message with permalink
                                        timestamp = msg.get('ts', '')
                                        message = {
                                            'text': msg.get('text', ''),
                                            'user': msg.get('user', ''),
                                            'timestamp': timestamp,
                                            'thread_ts': msg.get('thread_ts', None),
                                            'relevance_score': relevance_score,
                                            'permalink': self._generate_permalink(channel_id, timestamp),
                                            'from_search': False  # Mark as from history API
                                        }
                                        
                                        history_messages.append(message)
                                        existing_timestamps.add(msg.get('ts', ''))
                                    
                                    logging.info(f"History API page {page+1} for {channel_name}: {msgs_in_page} messages, {len(history_messages)} total")
                                    
                                    # Update cursor for next page
                                    cursor = response.get('response_metadata', {}).get('next_cursor')
                                    api_success = True
                                    page_success = True
                                    
                                    # If no cursor or no more results, break
                                    if not cursor:
                                        logging.info(f"No more messages to fetch for {channel_name}")
                                        break
                                        
                                    # Brief pause between pages to avoid rate limiting
                                    if deep_search and page < max_history_pages - 1:
                                        await asyncio.sleep(0.5)
                                        
                                    break  # Success, exit retry loop
                                    
                                except SlackApiError as e:
                                    error_msg = str(e).lower()
                                    
                                    # Log specific error types for debugging
                                    if "not_in_channel" in error_msg:
                                        logging.error(f"Bot not in channel {channel_name}: {error_msg}")
                                    elif "missing_scope" in error_msg:
                                        logging.error(f"Missing permission scope: {error_msg}")
                                    elif "channel_not_found" in error_msg:
                                        logging.error(f"Channel not found {channel_name}: {error_msg}")
                                    elif "is_archived" in error_msg:
                                        logging.error(f"Channel is archived: {error_msg}")
                                        return [], 0  # Don't retry for archived channels
                                        
                                    # For private channels, try harder to recover
                                    if channel_name.startswith("team-") or "private" in channel_name:
                                        # Try to get the most recent messages using user token
                                        if user_id and user_id in self.user_clients and history_retry == 0:
                                            try:
                                                logging.info(f"Retrying with user token for private channel {channel_name}")
                                                user_client = self.user_clients[user_id]
                                                response = user_client.conversations_history(**history_args)
                                                
                                                # Process messages (same as above)
                                                for msg in response.get('messages', []):
                                                    if msg.get('ts') in existing_timestamps:
                                                        continue
                                                    
                                                    if msg.get('subtype') in ['channel_join', 'channel_leave']:
                                                        continue
                                                    
                                                    relevance_score = 0
                                                    if query and query_keywords:
                                                        relevance_score = self._calculate_relevance(msg.get('text', ''), query_keywords)
                                                        
                                                        # For deep search, only include relevant messages
                                                        if deep_search and relevance_score < 0.1:
                                                            continue
                                                    
                                                    # Create message with permalink
                                                    timestamp = msg.get('ts', '')
                                                    message = {
                                                        'text': msg.get('text', ''),
                                                        'user': msg.get('user', ''),
                                                        'timestamp': timestamp,
                                                        'thread_ts': msg.get('thread_ts', None),
                                                        'relevance_score': relevance_score,
                                                        'permalink': self._generate_permalink(channel_id, timestamp),
                                                        'from_search': False
                                                    }
                                                    
                                                    history_messages.append(message)
                                                    existing_timestamps.add(timestamp)
                                                
                                                cursor = response.get('response_metadata', {}).get('next_cursor')
                                                api_success = True
                                                page_success = True
                                                break  # Success with user token
                                            except SlackApiError as ue:
                                                logging.error(f"User token also failed for history: {ue}")
                                                # Continue to standard retry logic
                                    
                                    if history_retry < 1:
                                        logging.warning(f"Error fetching history for {channel_name}, retrying: {str(e)}")
                                        await asyncio.sleep(1)
                                    else:
                                        logging.error(f"Error fetching history for {channel_name}: {str(e)}")
                                        break
                                except Exception as e:
                                    logging.error(f"Unexpected error fetching history for {channel_name}: {str(e)}")
                                    if history_retry < 1:
                                        await asyncio.sleep(1)
                                    else:
                                        break
                                        
                            if not api_success or not cursor:
                                break  # Exit page loop if API failed or no more pages
                        
                        # If we got at least one successful page, consider this a success
                        if page_success:
                            # Also cache these messages for future use
                            if not query and history_messages:
                                self.message_cache[channel_id] = history_messages
                                self.message_cache_time[channel_id] = time.time()
                            
                            logging.info(f"History API found {len(history_messages)} messages for {channel_name}")
                            return history_messages, len(existing_messages)
                        else:
                            # Complete failure, return empty results
                            logging.error(f"Failed to fetch any history for channel {channel_name}")
                            return [], 0
                        
                    except Exception as e:
                        logging.error(f"Error in process_channel_history for {channel_name}: {str(e)}")
                        return [], 0
                
                # Process all channels in parallel
                history_tasks = []
                for channel_id, channel_name in channel_map.items():
                    history_tasks.append(process_channel_history(channel_id, channel_name))
                
                # Wait for all tasks to complete, even if some fail
                history_results_list = await asyncio.gather(*history_tasks, return_exceptions=True)
                
                # Combine results, handling exceptions
                for i, (channel_id, channel_name) in enumerate(channel_map.items()):
                    result = history_results_list[i]
                    
                    # Check if the task raised an exception
                    if isinstance(result, Exception):
                        logging.error(f"Channel processing failed for {channel_name}: {str(result)}")
                        failed_channels.append(channel_name)
                        history_messages, search_count = [], 0
                    else:
                        history_messages, search_count = result
                    
                    history_results[channel_name] = history_messages
                    
                    # Ensure all channels have a baseline relevance score
                    if channel_name not in channel_relevance:
                        channel_relevance[channel_name] = 0.5  # Default baseline
                        
                    # Calculate channel relevance based on search and history
                    if channel_name in search_results and search_results[channel_name]:
                        # Higher relevance for channels with search matches
                        search_scores = [msg.get('relevance_score', 0) for msg in search_results[channel_name]]
                        avg_search_score = sum(search_scores) / len(search_scores) if search_scores else 0.3
                        # Boost existing relevance with search scores
                        channel_relevance[channel_name] = max(
                            channel_relevance[channel_name],
                            avg_search_score * 0.8 + 0.2  # Scale from 0.2 to 1.0
                        )
                    elif history_messages:
                        # Lower relevance for channels with only history matches
                        history_scores = [msg.get('relevance_score', 0) for msg in history_messages]
                        avg_history_score = (sum(history_scores) / len(history_scores)) * 0.7 if history_scores else 0.2
                        # Only update if better than existing
                        if avg_history_score > channel_relevance[channel_name]:
                            channel_relevance[channel_name] = avg_history_score
            
            # Step 3: Combine search and history results for each channel
            for channel_name in set(list(search_results.keys()) + list(history_results.keys())):
                # Get messages from both sources
                search_messages = search_results.get(channel_name, [])
                history_messages = history_results.get(channel_name, [])
                
                # Combine messages, avoiding duplicates
                all_messages = search_messages.copy()
                existing_timestamps = {msg.get('timestamp') for msg in all_messages}
                
                for msg in history_messages:
                    if msg.get('timestamp') not in existing_timestamps:
                        all_messages.append(msg)
                        existing_timestamps.add(msg.get('timestamp'))
                
                # Process threads for top messages - don't let thread errors stop processing
                try:
                    all_messages = await self._fetch_thread_replies(all_messages, channel_name, channel_map)
                except Exception as e:
                    logging.error(f"Error fetching thread replies for {channel_name}: {e}")
                    # Continue without thread replies
                
                # Sort by relevance if query provided, otherwise by timestamp
                if query:
                    all_messages.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
                else:
                    all_messages.sort(key=lambda x: float(x.get('timestamp', '0')), reverse=True)
                
                # Limit number of messages
                all_messages = all_messages[:max_return_limit]
                
                # Add channel relevance metadata
                combined_results[channel_name] = {
                    'messages': all_messages,
                    'relevance': channel_relevance.get(channel_name, 0.5),
                    'metadata': {
                        'total_messages': len(all_messages),
                        'from_search': len([m for m in all_messages if m.get('from_search', False)]),
                        'from_history': len([m for m in all_messages if not m.get('from_search', False)]),
                        'has_threads': any(m.get('replies') for m in all_messages if isinstance(m, dict)),
                        'query_match_count': sum(1 for m in all_messages if m.get('relevance_score', 0) > 0.3),
                        'processing_errors': channel_name in failed_channels,
                        'deep_search_enabled': deep_search,
                        'search_mode': search_mode
                    }
                }
                
                # Calculate average relevance score for messages in this channel
                if all_messages:
                    msg_relevance_scores = [m.get('relevance_score', 0) for m in all_messages if isinstance(m, dict)]
                    if msg_relevance_scores:
                        avg_msg_relevance = sum(msg_relevance_scores) / len(msg_relevance_scores)
                        # Update channel relevance using weighted average of previous and message relevance
                        combined_results[channel_name]['relevance'] = (
                            channel_relevance.get(channel_name, 0.5) * 0.7 + 
                            avg_msg_relevance * 0.3
                        )
                logging.info(f"Processed {channel_name} in {time.time() - start_time:.2f}s, returning {len(all_messages)} messages")
                
                # Add deep search indicator
                if deep_search:
                    combined_results[channel_name]['metadata']['deep_search'] = True
            
            # Step 4: Resolve user IDs to display names
            for channel_name, channel_data in combined_results.items():
                if isinstance(channel_data, dict) and 'messages' in channel_data:
                    messages = channel_data.get('messages', [])
                    await self._resolve_user_ids_in_messages(messages)
            
            # Step 5: Add global metadata
            results = combined_results.copy()
            results['_metadata'] = {
                'processing_time': time.time() - start_time,
                'channels_processed': len(channel_map),
                'channels_succeeded': len(channel_map) - len(failed_channels),
                'channels_failed': len(failed_channels),
                'failed_channels': failed_channels,
                'total_messages': sum(len(data.get('messages', [])) for data in combined_results.values()),
                'query_keywords': query_keywords,
                'channel_relevance': {k: v.get('relevance', 0.5) for k, v in combined_results.items()},
                'deep_search_enabled': deep_search,
                'search_mode': search_mode
            }
            
            logging.info(f"Total fetch_data execution time: {time.time() - start_time:.2f}s for {len(channel_map)} channels")
            logging.info(f"Successfully processed {len(channel_map) - len(failed_channels)} channels with {len(failed_channels)} failures")
            return results
            
        except Exception as e:
            logging.error(f"Error in fetch_data: {str(e)}", exc_info=True)
            return {'error': str(e)}
            
    async def _fetch_thread_replies(self, messages, channel_name, channel_map):
        """Fetch thread replies for top messages"""
        # Find the channel ID for this channel
        channel_id = None
        for cid, cname in channel_map.items():
            if cname == channel_name:
                channel_id = cid
                break
        
        if not channel_id:
            return messages
            
        # Get top messages by relevance that have thread_ts
        messages_with_threads = [
            msg for msg in messages 
            if msg.get('thread_ts') and isinstance(msg.get('thread_ts'), (str, float))
        ]
        
        # Sort by relevance and limit to top 5
        messages_with_threads.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
        messages_with_threads = messages_with_threads[:5]
        
        # Fetch thread replies for each message
        for msg in messages_with_threads:
            thread_ts = str(msg.get('thread_ts', ''))
            try:
                # Add retry logic for thread fetching
                max_retries = 2
                for retry in range(max_retries + 1):
                    try:
                        thread = self.client.conversations_replies(
                            channel=channel_id,
                            ts=thread_ts,
                            limit=5  # Limit to 5 replies
                        )
                        
                        # Check if we got valid replies
                        if len(thread.get('messages', [])) > 1:
                            replies = []
                            
                            # First message is the parent, skip it
                            for reply in thread.get('messages', [])[1:6]:  # Get up to 5 replies
                                if len(reply.get('text', '')) > 5:
                                    replies.append({
                                        'text': reply.get('text', '')[:200],  # Limit reply text length
                                        'user': reply.get('user', 'Unknown'),
                                        'timestamp': reply.get('ts', '')
                                    })
                            
                            if replies:
                                # Find the message in our results and add replies
                                for result_msg in messages:
                                    if result_msg.get('timestamp') == msg.get('timestamp'):
                                        result_msg['replies'] = replies
                                        break
                        break  # Success, exit retry loop
                    except SlackApiError as e:
                        if retry < max_retries:
                            # Add exponential backoff
                            wait_time = (2 ** retry) * 0.5  # 0.5s, then 1s
                            logging.warning(f"Retrying thread fetch after error: {e}. Waiting {wait_time}s...")
                            await asyncio.sleep(wait_time)
                        else:
                            raise  # Re-raise on final retry
            except Exception as e:
                logging.warning(f"Error fetching thread replies: {e}")
                
        return messages
            
    def format_for_llm(self, data: Dict[str, Any]) -> str:
        """Format Slack data for LLM consumption with improved structure and token efficiency."""
        # Handle case where data has an error
        if isinstance(data, dict) and 'error' in data:
            return f"Error retrieving Slack data: {data['error']}"
            
        # Check for the new metadata structure and extract messages
        if isinstance(data, dict) and '_metadata' in data:
            # Process metadata first
            metadata = data.get('_metadata', {})
            channels_data = {k: v for k, v in data.items() if k != '_metadata'}
            
            # Check if this was an "oldest message" search
            search_mode = metadata.get('search_mode', '')
            is_oldest_search = search_mode == 'oldest'
            
            # Create a summary of the metadata
            metadata_summary = [
                "Slack Data Summary:",
                f"- Channels processed: {metadata.get('channels_processed', 0)}",
                f"- Total messages: {metadata.get('total_messages', 0)}",
                f"- Processing time: {metadata.get('processing_time', 0):.2f}s"
            ]
            
            # For oldest message search, add special header
            if is_oldest_search:
                metadata_summary.insert(0, "🔍 OLDEST MESSAGES SEARCH RESULTS")
                metadata_summary.insert(1, "Showing earliest messages found in the channel(s)")
            
            # Extract channel-specific data
            channel_messages = {}
            for channel_name, channel_data in channels_data.items():
                if isinstance(channel_data, dict) and 'messages' in channel_data:
                    messages = channel_data.get('messages', [])
                    
                    # For oldest message search, ensure sorting by oldest first
                    if is_oldest_search and messages:
                        messages.sort(key=lambda x: float(x.get('timestamp', '0')))
                        
                    channel_messages[channel_name] = messages
                else:
                    channel_messages[channel_name] = channel_data  # Handle error messages
            
            # Use the new data format for formatting
            return self._format_channel_messages(channel_messages, metadata_summary, is_oldest_search)
        else:
            # Handle legacy data format (direct channel->messages mapping)
            return self._format_channel_messages(data, [], False)
            
    def _format_channel_messages(self, channel_data: Dict[str, Any], metadata_lines: List[str], is_oldest_search: bool = False) -> str:
        """Format channel messages with improved structure"""
        formatted = []
        
        # Add metadata if available
        if metadata_lines:
            formatted.extend(metadata_lines)
            formatted.append("-" * 40)
        
        try:
            for channel_name, messages in channel_data.items():
                # Check for error in channel data
                if isinstance(messages, dict) and 'error' in messages:
                    formatted.append(f"Error in channel {channel_name}: {messages['error']}")
                    continue
                    
                # Handle the new structure where messages are in a nested dict
                if isinstance(messages, dict) and 'messages' in messages:
                    channel_meta = messages.get('metadata', {})
                    relevance = messages.get('relevance', 0.5)
                    messages = messages.get('messages', [])
                    
                    # Add special header for oldest search
                    if is_oldest_search:
                        formatted.append(f"Channel: #{channel_name} (Showing Oldest Messages First)")
                    else:
                        # Regular channel header with relevance
                        formatted.append(f"Channel: #{channel_name} (Relevance: {relevance:.2f})")
                    
                    # Add channel metadata if available
                    if channel_meta:
                        meta_parts = []
                        if 'from_search' in channel_meta:
                            meta_parts.append(f"Search results: {channel_meta.get('from_search', 0)}")
                        if 'from_history' in channel_meta:
                            meta_parts.append(f"History results: {channel_meta.get('from_history', 0)}")
                        if 'query_match_count' in channel_meta:
                            meta_parts.append(f"Query matches: {channel_meta.get('query_match_count', 0)}")
                            
                        if meta_parts:
                            formatted.append(f"[{' | '.join(meta_parts)}]")
                else:
                    # Standard channel header for legacy format
                    formatted.append(f"Channel: #{channel_name}")
                
                formatted.append("-" * 40)
                
                # Verify messages is a list before proceeding
                if not isinstance(messages, list):
                    formatted.append(f"Error: Invalid message format. Expected list, got {type(messages)}")
                    formatted.append("-" * 40)
                    continue
                
                # Handle empty messages list
                if not messages:
                    formatted.append("No messages found in this channel.")
                    formatted.append("-" * 40)
                    continue
                
                # Sort messages by timestamp for oldest search, otherwise use regular sorting
                try:
                    if is_oldest_search:
                        # For oldest search, sort by timestamp (oldest first)
                        messages = sorted(
                            [msg for msg in messages if isinstance(msg, dict)],
                            key=lambda x: float(x.get('timestamp', '0'))
                        )
                        # Add special indicator for oldest message
                        if messages:
                            formatted.append("🔍 FIRST MESSAGE IN CHANNEL:")
                    elif any('relevance_score' in msg for msg in messages if isinstance(msg, dict)):
                        messages = sorted(
                            [msg for msg in messages if isinstance(msg, dict)],
                            key=lambda x: float(x.get('relevance_score', 0)),
                            reverse=True
                        )
                    else:
                        messages = sorted(
                            [msg for msg in messages if isinstance(msg, dict)],
                            key=lambda x: float(x.get('timestamp', '0')),
                            reverse=True
                        )
                except Exception as e:
                    logging.error(f"Error sorting messages: {e}")
                    # Continue with unsorted messages
                
                # Format messages as a compact table
                for i, msg in enumerate(messages):
                    # Skip if message is not a dictionary
                    if not isinstance(msg, dict):
                        formatted.append(f"[Invalid message format: {type(msg)}]")
                        continue
                    
                    # Format timestamp
                    ts = msg.get('timestamp', '')
                    if ts:
                        try:
                            from datetime import datetime
                            # Ensure ts is a float or can be converted to one
                            ts_float = float(ts) if isinstance(ts, (str, float, int)) else 0
                            ts = datetime.fromtimestamp(ts_float).strftime('%Y-%m-%d %H:%M')
                        except (ValueError, TypeError) as e:
                            logging.warning(f"Error formatting timestamp '{ts}' of type {type(ts)}: {e}")
                            # Keep original timestamp if conversion fails
                    
                    # Add relevance score for debugging if available
                    relevance_info = ""
                    if 'relevance_score' in msg:
                        try:
                            score = float(msg['relevance_score'])
                            if score > 0:
                                relevance_info = f" [relevance: {score:.2f}]"
                        except (ValueError, TypeError):
                            pass
                            
                    # Add search/history indicator
                    source_info = " [search]" if msg.get('from_search', False) else ""
                    
                    # Add message in compact format with better formatting
                    formatted.append(f"[{ts}] {msg.get('user', 'Unknown')}{relevance_info}{source_info}:")
                    formatted.append(f"{msg.get('text', '')}")
                    
                    # Add thread replies if any (in more compact format)
                    replies = msg.get('replies', [])
                    if isinstance(replies, list) and replies:
                        formatted.append("  Thread replies:")
                        for reply in replies:
                            if not isinstance(reply, dict):
                                continue
                                
                            reply_ts = reply.get('timestamp', '')
                            if reply_ts:
                                try:
                                    from datetime import datetime
                                    # Ensure reply_ts is a float or can be converted to one
                                    reply_ts_float = float(reply_ts) if isinstance(reply_ts, (str, float, int)) else 0
                                    reply_ts = datetime.fromtimestamp(reply_ts_float).strftime('%Y-%m-%d %H:%M')
                                except (ValueError, TypeError) as e:
                                    logging.warning(f"Error formatting reply timestamp '{reply_ts}' of type {type(reply_ts)}: {e}")
                                    # Keep original timestamp if conversion fails
                            
                            formatted.append(f"    [{reply_ts}] {reply.get('user', 'Unknown')}:")
                            formatted.append(f"    {reply.get('text', '')}")
                    
                    # Add separator between messages only if not the last message
                    if i < len(messages) - 1:
                        formatted.append("-" * 30)
                
                # Add simple channel stats with defensive programming
                try:
                    # Count valid messages, users, and threads
                    valid_msg_count = 0
                    users = set()
                    thread_count = 0
                    
                    for msg in messages:
                        # Skip invalid messages
                        if not isinstance(msg, dict):
                            continue
                            
                        valid_msg_count += 1
                        
                        # Add user if it's a string
                        user = msg.get('user', '')
                        if user and isinstance(user, str):
                            users.add(user)
                        
                        # Count threads properly, avoiding float error
                        replies = msg.get('replies', [])
                        if isinstance(replies, list) and len(replies) > 0:
                            thread_count += 1
                    
                    # Calculate stats
                    total_users = len(users)
                    
                    # Build stats string
                    stats = [
                        f"\nChannel Summary: {valid_msg_count} messages from {total_users} users",
                        f"Threads: {thread_count}"
                    ]
                    
                    formatted.append("=" * 40)
                    formatted.append(" | ".join(stats))
                    formatted.append("=" * 40)
                except Exception as e:
                    logging.error(f"Error calculating channel stats: {e}", exc_info=True)
                    formatted.append("=" * 40)
                    formatted.append("Error calculating channel statistics")
                    formatted.append("=" * 40)
            
            result = "\n".join(formatted)
            
            # If there was a query that returned no results, provide feedback
            if not result:
                return "No Slack messages found matching your query. Please try a different search term or channel."
                
            return result
            
        except Exception as e:
            logging.error(f"Error formatting Slack data: {str(e)}", exc_info=True)
            return f"Error formatting Slack data: {str(e)}"
    
    def get_form_fields(self) -> Dict[str, Any]:
        """Return form fields for Slack configuration"""
        return {
            'type': 'slack',
            'fields': [
                {
                    'type': 'text',
                    'label': 'Channel Name',
                    'name': 'channel',
                    'placeholder': 'Enter channel name ',
                    'optional': False,
                    'add_more': True
                },
                {
                    'type': 'checkbox',
                    'label': 'Super Deep Search (search entire channel history with maximum thoroughness)',
                    'name': 'deep_search',
                    'help_text': 'Note: Deep search is automatically enabled for all queries, but this option increases thoroughness even further',
                    'default': False
                },
                {
                    'type': 'number',
                    'label': 'Maximum Messages (limit results)',
                    'name': 'limit',
                    'placeholder': 'Default: 20 (deep search: 100)',
                    'optional': True
                },
                {
                    'type': 'number',
                    'label': 'Maximum History Pages',
                    'name': 'max_pages',
                    'placeholder': 'Default: 15 for queries, 5 for browsing',
                    'optional': True
                }
            ]
        }
    
    def _extract_keywords(self, query: str) -> List[str]:
        """Extract meaningful keywords from a query string for relevance scoring.
        
        Args:
            query: The search query string
            
        Returns:
            List of extracted keywords
        """
        if not query:
            return []
            
        # Normalize the query
        query = query.lower()
        
        # Remove common stopwords that don't add relevance
        stopwords = {
            'a', 'an', 'the', 'and', 'or', 'but', 'if', 'because', 'as', 'what',
            'which', 'this', 'that', 'these', 'those', 'then', 'just', 'so', 'than',
            'such', 'both', 'through', 'about', 'for', 'is', 'of', 'while', 'during',
            'to', 'from', 'in', 'on', 'at', 'by', 'with', 'without', 'between', 'into',
            'during', 'before', 'after', 'above', 'below', 'up', 'down', 'out', 'off',
            'over', 'under', 'again', 'further', 'then', 'once', 'here', 'there', 'when',
            'where', 'why', 'how', 'all', 'any', 'both', 'each', 'few', 'more', 'most',
            'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own', 'same', 'so',
            'than', 'too', 'very', 's', 't', 'can', 'will', 'don', 'should', 'now',
            'would', 'could', 'may', 'might', 'must', 'need', 'I', 'me', 'my', 'myself', 
            'we', 'our', 'ours', 'ourselves', 'you', 'your', 'yours', 'yourself',
            'he', 'him', 'his', 'himself', 'she', 'her', 'hers', 'herself', 'it', 'its',
            'itself', 'they', 'them', 'their', 'theirs', 'themselves', 'am', 'is',
            'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had', 'having',
            'do', 'does', 'did', 'doing'
        }
        
        # Split the query into words and remove punctuation
        import re
        words = re.findall(r'\b\w+\b', query)
        
        # Filter out stopwords and short words (less than 3 characters)
        keywords = [word for word in words if word not in stopwords and len(word) >= 3]
        
        # If we have no keywords after filtering, use all words
        if not keywords and words:
            keywords = words
            
        # Remove duplicates while preserving order
        seen = set()
        unique_keywords = [x for x in keywords if not (x in seen or seen.add(x))]
        
        return unique_keywords
    
    def _calculate_relevance(self, text: str, keywords: List[str]) -> float:
        """Calculate relevance score of a message based on keyword matching.
        
        Args:
            text: The message text
            keywords: List of search keywords
            
        Returns:
            Relevance score between 0.0 and 1.0
        """
        if not text or not keywords:
            return 0.0
            
        # Normalize text
        text = text.lower()
        
        # Count keyword occurrences
        matches = 0
        for keyword in keywords:
            if keyword in text:
                matches += 1
                
        # Calculate score based on proportion of matched keywords
        if matches == 0:
            return 0.0
            
        # Score based on percentage of keywords matched (0.3 to 0.9 range)
        score = 0.3 + (0.6 * matches / len(keywords))
        
        # Bonus for high keyword density
        text_length = max(1, len(text.split()))
        density = matches / text_length
        if density > 0.05:  # More than 5% density
            score += 0.05
            
        # Ensure score is capped at 1.0
        return min(1.0, score)

    async def fetch_complete_channel_history(self, channel_id: str, oldest_timestamp: float = None, 
                                            latest_timestamp: float = None) -> List[Dict[str, Any]]:
        """Fetch entire channel history with rate limit handling and optional time constraints"""
        messages = []
        cursor = None
        retry_count = 0
        max_retries = 5
        base_delay = 1  # Start with 1 second delay

        while True:
            try:
                history_args = self._build_history_args(
                    channel_id=channel_id,
                    limit=1000,  # Max allowed by Slack API
                    cursor=cursor,
                    oldest_timestamp=oldest_timestamp,
                    latest_timestamp=latest_timestamp
                )
                
                response = self.client.conversations_history(**history_args)
                messages.extend(response.get('messages', []))
                cursor = response.get('response_metadata', {}).get('next_cursor')
                
                # Reset retry counter on success
                retry_count = 0
                
                if not cursor:
                    break

                # Add dynamic delay between requests
                await asyncio.sleep(base_delay)

            except SlackApiError as e:
                if e.response['error'] == 'ratelimited':
                    retry_after = int(e.response.headers.get('Retry-After', 5))
                    logging.warning(f"Rate limited. Retrying after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    retry_count += 1
                    if retry_count > max_retries:
                        break
                else:
                    raise
        
        return messages

    async def fetch_history_by_time_ranges(self, channel_id: str, oldest_timestamp: float = None, 
                                          latest_timestamp: float = None) -> List[Dict[str, Any]]:
        """Fetch history in chronological chunks with optional time constraints"""
        messages = []
        latest = str(latest_timestamp) if latest_timestamp else None
        has_more = True
        
        while has_more:
            try:
                history_args = self._build_history_args(
                    channel_id=channel_id,
                    limit=1000,
                    oldest_timestamp=oldest_timestamp,
                    latest_timestamp=float(latest) if latest else None
                )
                history_args["inclusive"] = True  # Add inclusive parameter
                
                response = self.client.conversations_history(**history_args)
                new_messages = response.get('messages', [])
                if new_messages:
                    messages.extend(new_messages)
                    latest = new_messages[-1]['ts']
                    
                has_more = response.get('has_more', False)
                await asyncio.sleep(1)  # Conservative delay

            except SlackApiError as e:
                await self.handle_rate_limit(e)
        
        return messages

    async def handle_rate_limit(self, e: SlackApiError) -> None:
        """Handle rate limiting exceptions"""
        if e.response['error'] == 'ratelimited':
            retry_after = int(e.response.headers.get('Retry-After', 5))
            logging.warning(f"Rate limited. Waiting for {retry_after}s")
            await asyncio.sleep(retry_after)
        else:
            raise e

    async def deep_channel_search(self, channel_id: str, query: str, oldest_timestamp: float = None, 
                                 latest_timestamp: float = None) -> List[Dict[str, Any]]:
        """Combine search and history APIs with optional time constraints"""
        # 1. Get recent messages via history API with time constraints
        recent_messages = await self.fetch_complete_channel_history(
            channel_id, 
            oldest_timestamp=oldest_timestamp, 
            latest_timestamp=latest_timestamp
        )
        
        # 2. Search older messages beyond history limit
        search_results = []
        total_pages = 0
        cursor = None
        
        # Build search query with time constraints if provided
        search_query = f"in:{channel_id} {query}"
        
        while True:
            response = self.client.search_messages(
                query=search_query,
                cursor=cursor,
                count=100,
                sort='timestamp'
            )
            search_results.extend(response.get('messages', []))
            cursor = response.get('pagination', {}).get('next_cursor')
            
            if not cursor or total_pages >= 50:  # Safety limit
                break
                
            total_pages += 1
            await asyncio.sleep(2)  # Longer delay for search API

        return self.deduplicate_messages(recent_messages + search_results)

    def deduplicate_messages(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Remove duplicate messages from combined results"""
        seen = set()
        unique = []
        for msg in messages:
            identifier = f"{msg['ts']}-{msg.get('thread_ts')}"
            if identifier not in seen:
                seen.add(identifier)
                unique.append(msg)
        return unique

    def get_last_sync_time(self, channel_id: str) -> str:
        """Get the timestamp of last sync for a channel"""
        return self.last_sync_times.get(channel_id, "0")

    async def incremental_sync(self, channel_id: str) -> Dict[str, Any]:
        """Only fetch new messages since last sync"""
        last_sync = self.get_last_sync_time(channel_id)
        
        # Convert last_sync to float for the helper method
        oldest_timestamp = float(last_sync) if last_sync != "0" else None
        
        history_args = self._build_history_args(
            channel_id=channel_id,
            limit=1000,
            oldest_timestamp=oldest_timestamp
        )
        
        response = self.client.conversations_history(**history_args)
        
        # Update last sync time
        if response.get('messages'):
            self.last_sync_times[channel_id] = str(time.time())
            
        return response

    async def global_search(self, query: str, params: Optional[Dict[str, Any]] = None) -> GlobalSearchResult:
        """Perform a global search across all accessible Slack channels.
        
        This method searches all channels the bot/user has access to and returns
        results grouped by channel with relevance scores and follow-up suggestions.
        
        Args:
            query: The search query string
            params: Optional additional parameters:
                - user_id: User ID for using user token
                - limit: Maximum results per channel (default: 10)
                - max_channels: Maximum channels to return (default: 10)
                - include_dms: Whether to include DMs (default: False)
                
        Returns:
            GlobalSearchResult: Results grouped by channel with suggestions
            
        Raises:
            SlackApiError: If the search API fails
        """
        start_time = time.time()
        params = params or {}
        
        user_id = params.get('user_id')
        limit_per_channel = params.get('limit', 10)
        max_channels = params.get('max_channels', 10)
        include_dms = params.get('include_dms', False)
        
        logging.info(f"Starting global search for query: '{query}'")
        
        # Get the best client for search operations
        search_client = None
        if user_id and user_id in self.user_clients:
            search_client = self.user_clients[user_id]
            logging.info(f"Using user client for global search")
        elif self.default_user_client:
            search_client = self.default_user_client
            logging.info("Using default user client for global search")
        
        if not search_client:
            logging.error("No user token available for search API")
            return GlobalSearchResult(
                query=query,
                results={},
                channel_summaries=[],
                total_message_count=0,
                channels_searched=0,
                follow_up_suggestions=[
                    FollowUpSuggestion(
                        type="error",
                        value="no_token",
                        label="Search requires user token",
                        description="Please configure a Slack user token for search functionality"
                    )
                ],
                summary="Search failed: No user token available for the search API",
                processing_time_seconds=time.time() - start_time
            )
        
        try:
            # Use Slack's search API to find messages across all channels
            all_results: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            channel_metadata: Dict[str, Dict[str, Any]] = {}
            total_matches = 0
            
            # Perform paginated search
            cursor = None
            pages_fetched = 0
            max_pages = params.get('max_pages', 5)
            
            while pages_fetched < max_pages:
                search_args = {
                    "query": query,
                    "sort": "score",
                    "sort_dir": "desc",
                    "count": 100  # Max per page
                }
                
                if cursor:
                    search_args["cursor"] = cursor
                
                try:
                    response = search_client.search_messages(**search_args)
                    
                    if not response.get('ok'):
                        error = response.get('error', 'unknown error')
                        logging.error(f"Search API error: {error}")
                        break
                    
                    matches = response.get('messages', {}).get('matches', [])
                    logging.info(f"Page {pages_fetched + 1}: Found {len(matches)} matches")
                    
                    for match in matches:
                        channel_info = match.get('channel', {})
                        channel_id = channel_info.get('id', '')
                        channel_name = channel_info.get('name', channel_id)
                        
                        # Skip DMs if not requested
                        if not include_dms and channel_id.startswith('D'):
                            continue
                        
                        # Store channel metadata
                        if channel_id not in channel_metadata:
                            channel_metadata[channel_id] = {
                                'name': channel_name,
                                'id': channel_id,
                                'is_private': channel_info.get('is_private', False),
                                'scores': []
                            }
                        
                        # Create message object
                        message = {
                            'text': match.get('text', ''),
                            'user': match.get('user', ''),
                            'timestamp': match.get('ts', ''),
                            'thread_ts': match.get('thread_ts'),
                            'relevance_score': match.get('score', 0),
                            'permalink': match.get('permalink', ''),
                            'from_search': True
                        }
                        
                        all_results[channel_name].append(message)
                        channel_metadata[channel_id]['scores'].append(match.get('score', 0))
                        total_matches += 1
                    
                    # Check for more pages
                    cursor = response.get('messages', {}).get('pagination', {}).get('next_cursor')
                    if not cursor:
                        break
                    
                    pages_fetched += 1
                    await asyncio.sleep(0.5)  # Rate limiting
                    
                except SlackApiError as e:
                    error_msg = str(e).lower()
                    if "ratelimited" in error_msg:
                        retry_after = int(e.response.headers.get('Retry-After', 5))
                        logging.warning(f"Rate limited in global search. Waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        logging.error(f"Search API error: {e}")
                        break
                
                pages_fetched += 1
            
            # Build channel summaries sorted by relevance
            channel_summaries: List[ChannelSummary] = []
            for channel_id, metadata in channel_metadata.items():
                scores = metadata.get('scores', [])
                avg_score = sum(scores) / len(scores) if scores else 0
                channel_name = metadata.get('name', channel_id)
                messages = all_results.get(channel_name, [])
                
                # Get preview of top message
                top_preview = None
                if messages:
                    top_msg = max(messages, key=lambda m: m.get('relevance_score', 0))
                    top_preview = top_msg.get('text', '')[:150] + '...' if len(top_msg.get('text', '')) > 150 else top_msg.get('text', '')
                
                channel_summaries.append(ChannelSummary(
                    channel_id=channel_id,
                    channel_name=channel_name,
                    message_count=len(messages),
                    relevance_score=avg_score,
                    top_message_preview=top_preview
                ))
            
            # Sort by relevance score descending
            channel_summaries.sort(key=lambda x: x.relevance_score, reverse=True)
            
            # Limit to top channels
            channel_summaries = channel_summaries[:max_channels]
            
            # Limit messages per channel in results
            limited_results = {}
            for channel_name, messages in all_results.items():
                # Sort by relevance and limit
                sorted_messages = sorted(messages, key=lambda m: m.get('relevance_score', 0), reverse=True)
                limited_results[channel_name] = sorted_messages[:limit_per_channel]
            
            # Resolve user IDs to display names
            for channel_name, messages in limited_results.items():
                await self._resolve_user_ids_in_messages(messages)
            
            # Generate follow-up suggestions
            follow_up_suggestions = self._generate_follow_up_suggestions(
                query=query,
                channel_summaries=channel_summaries,
                total_messages=total_matches
            )
            
            # Build summary
            channels_with_results = len([c for c in channel_summaries if c.message_count > 0])
            summary = self._build_global_search_summary(
                query=query,
                total_messages=total_matches,
                channels_with_results=channels_with_results,
                top_channels=channel_summaries[:3]
            )
            
            processing_time = time.time() - start_time
            logging.info(f"Global search completed in {processing_time:.2f}s: {total_matches} messages across {channels_with_results} channels")
            
            return GlobalSearchResult(
                query=query,
                results=limited_results,
                channel_summaries=channel_summaries,
                total_message_count=total_matches,
                channels_searched=len(channel_metadata),
                follow_up_suggestions=follow_up_suggestions,
                summary=summary,
                processing_time_seconds=processing_time
            )
            
        except Exception as e:
            logging.error(f"Error in global_search: {str(e)}", exc_info=True)
            return GlobalSearchResult(
                query=query,
                results={},
                channel_summaries=[],
                total_message_count=0,
                channels_searched=0,
                follow_up_suggestions=[
                    FollowUpSuggestion(
                        type="error",
                        value="search_failed",
                        label="Search failed",
                        description=f"Error: {str(e)}"
                    )
                ],
                summary=f"Search failed: {str(e)}",
                processing_time_seconds=time.time() - start_time
            )
    
    def _generate_follow_up_suggestions(
        self,
        query: str,
        channel_summaries: List[ChannelSummary],
        total_messages: int
    ) -> List[FollowUpSuggestion]:
        """Generate follow-up suggestions for narrowing down search results.
        
        Args:
            query: The original search query
            channel_summaries: List of channel summaries with match counts
            total_messages: Total number of messages found
            
        Returns:
            List of follow-up suggestions
        """
        suggestions: List[FollowUpSuggestion] = []
        
        # Suggest top channels to narrow down
        for i, channel in enumerate(channel_summaries[:5]):
            if channel.message_count > 0:
                suggestions.append(FollowUpSuggestion(
                    type="channel",
                    value=channel.channel_name,
                    label=f"Focus on #{channel.channel_name} ({channel.message_count} matches)",
                    description=channel.top_message_preview
                ))
        
        # Suggest time-based filtering if many results
        if total_messages > 20:
            suggestions.append(FollowUpSuggestion(
                type="time_range",
                value="last_week",
                label="Limit to last week",
                description="Narrow results to the past 7 days"
            ))
            
            suggestions.append(FollowUpSuggestion(
                type="time_range",
                value="today",
                label="Show only today's messages",
                description="See what happened today"
            ))
        
        # Suggest keyword refinement based on query analysis
        query_keywords = self._extract_keywords(query)
        if len(query_keywords) >= 2:
            # Suggest focusing on specific keywords
            for keyword in query_keywords[:2]:
                suggestions.append(FollowUpSuggestion(
                    type="keyword",
                    value=keyword,
                    label=f"Focus on '{keyword}'",
                    description=f"Search specifically for '{keyword}'"
                ))
        
        # Add contextual suggestions based on query patterns
        query_lower = query.lower()
        
        if any(word in query_lower for word in ['meeting', 'schedule', 'calendar']):
            suggestions.append(FollowUpSuggestion(
                type="context",
                value="meetings",
                label="Look for meeting details",
                description="Find specific meeting times and attendees"
            ))
        
        if any(word in query_lower for word in ['bug', 'issue', 'error', 'problem']):
            suggestions.append(FollowUpSuggestion(
                type="context",
                value="issues",
                label="Find related issues",
                description="Search for related bug reports and solutions"
            ))
        
        if any(word in query_lower for word in ['deploy', 'release', 'ship']):
            suggestions.append(FollowUpSuggestion(
                type="context",
                value="deployments",
                label="Find deployment details",
                description="Search for deployment logs and release notes"
            ))
        
        # Limit total suggestions
        return suggestions[:8]
    
    def _build_global_search_summary(
        self,
        query: str,
        total_messages: int,
        channels_with_results: int,
        top_channels: List[ChannelSummary]
    ) -> str:
        """Build a human-readable summary of global search results.
        
        Args:
            query: The search query
            total_messages: Total messages found
            channels_with_results: Number of channels with matches
            top_channels: Top channels by relevance
            
        Returns:
            Human-readable summary string
        """
        if total_messages == 0:
            return f"No messages found matching '{query}'. Try different keywords or check if you have access to relevant channels."
        
        # Build summary
        summary_parts = [
            f"Found {total_messages} message{'s' if total_messages != 1 else ''} "
            f"across {channels_with_results} channel{'s' if channels_with_results != 1 else ''} "
            f"matching '{query}'."
        ]
        
        # Add top channels info
        if top_channels:
            channel_mentions = []
            for ch in top_channels[:3]:
                if ch.message_count > 0:
                    channel_mentions.append(f"#{ch.channel_name} ({ch.message_count})")
            
            if channel_mentions:
                summary_parts.append(f"Top channels: {', '.join(channel_mentions)}.")
        
        # Add suggestion to narrow down if many results
        if total_messages > 50:
            summary_parts.append(
                "Consider narrowing your search by selecting a specific channel or time range."
            )
        
        return " ".join(summary_parts)
    
    def _convert_global_result_to_dict(self, global_result: GlobalSearchResult) -> Dict[str, Any]:
        """Convert GlobalSearchResult to the dictionary format expected by the system.
        
        Args:
            global_result: The GlobalSearchResult from global_search()
            
        Returns:
            Dict in the standard fetch_data response format
        """
        # Build results in the standard channel -> messages format
        results = {}
        
        for channel_name, messages in global_result.results.items():
            results[channel_name] = {
                'messages': messages,
                'relevance': next(
                    (cs.relevance_score for cs in global_result.channel_summaries 
                     if cs.channel_name == channel_name),
                    0.5
                ),
                'metadata': {
                    'total_messages': len(messages),
                    'from_search': len(messages),
                    'from_history': 0,
                    'has_threads': any(m.get('thread_ts') for m in messages),
                    'query_match_count': len(messages),
                    'global_search': True
                }
            }
        
        # Add global metadata
        results['_metadata'] = {
            'processing_time': global_result.processing_time_seconds,
            'channels_processed': global_result.channels_searched,
            'channels_succeeded': len([cs for cs in global_result.channel_summaries if cs.message_count > 0]),
            'channels_failed': 0,
            'failed_channels': [],
            'total_messages': global_result.total_message_count,
            'query_keywords': self._extract_keywords(global_result.query),
            'channel_relevance': {
                cs.channel_name: cs.relevance_score 
                for cs in global_result.channel_summaries
            },
            'global_search': True,
            'summary': global_result.summary,
            'follow_up_suggestions': [
                {
                    'type': s.type,
                    'value': s.value,
                    'label': s.label,
                    'description': s.description
                }
                for s in global_result.follow_up_suggestions
            ],
            'channel_summaries': [
                {
                    'channel_id': cs.channel_id,
                    'channel_name': cs.channel_name,
                    'message_count': cs.message_count,
                    'relevance_score': cs.relevance_score,
                    'top_message_preview': cs.top_message_preview
                }
                for cs in global_result.channel_summaries
            ]
        }
        
        return results

    async def _process_thread_urls(self, thread_urls: List[str], params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process Slack thread URLs and return summarized thread content
        
        Args:
            thread_urls: List of Slack thread URLs to process
            params: Additional parameters including query
            
        Returns:
            Dictionary containing thread summaries in format expected by format_for_llm
        """
        try:
            from services.slack_service import fetch_and_summarize_slack_thread
            
            results = {}
            query = params.get('query', '')
            successful_threads = 0
            failed_threads = 0
            
            logging.info(f"Processing {len(thread_urls)} Slack thread URLs")
            
            for i, thread_url in enumerate(thread_urls):
                try:
                    logging.info(f"Processing thread URL {i+1}/{len(thread_urls)}: {thread_url}")
                    
                    # Get thread summary
                    thread_summary = fetch_and_summarize_slack_thread(thread_url)
                    
                    # Extract channel name from URL for better organization
                    channel_name = f"thread_{i+1}"
                    try:
                        # Try to parse channel name from URL
                        from urllib.parse import urlparse
                        parsed_url = urlparse(thread_url)
                        if 'archives' in parsed_url.path:
                            path_parts = parsed_url.path.strip('/').split('/')
                            if len(path_parts) >= 2:
                                channel_id = path_parts[1]
                                # Try to get channel name from cache or API
                                try:
                                    channel_info = self.client.conversations_info(channel=channel_id)
                                    channel_name = f"thread_from_{channel_info['channel']['name']}"
                                except:
                                    channel_name = f"thread_from_{channel_id}"
                    except:
                        pass  # Keep default name if parsing fails
                    
                    # Format the thread summary as a single "message" for consistency
                    # This matches the format expected by format_for_llm
                    thread_message = {
                        "text": thread_summary,
                        "user": "Thread Summary",
                        "timestamp": str(time.time()),
                        "thread_url": thread_url,
                        "type": "thread_summary"
                    }
                    
                    # Store in the format expected by format_for_llm
                    results[channel_name] = [thread_message]
                    successful_threads += 1
                    
                    logging.info(f"Successfully processed thread {i+1}")
                    
                except Exception as e:
                    logging.error(f"Error processing thread URL {thread_url}: {e}")
                    
                    # Store error in the same format
                    error_channel_name = f"thread_{i+1}_error"
                    error_message = {
                        "text": f"Error processing thread: {str(e)}",
                        "user": "System Error",
                        "timestamp": str(time.time()),
                        "thread_url": thread_url,
                        "type": "thread_error"
                    }
                    results[error_channel_name] = [error_message]
                    failed_threads += 1
            
            # Add metadata in the format expected by format_for_llm
            results["_metadata"] = {
                "source": "slack_threads",
                "thread_count": len(thread_urls),
                "successful_threads": successful_threads,
                "failed_threads": failed_threads,
                "query": query,
                "channels_processed": len([k for k in results.keys() if k != "_metadata"]),  # Count non-metadata keys
                "total_messages": sum(len(messages) for key, messages in results.items() if key != "_metadata"),
                "processing_time": 0.0  # Will be calculated by caller if needed
            }
            
            logging.info(f"Thread processing complete: {successful_threads} successful, {failed_threads} failed")
            return results
            
        except ImportError:
            logging.error("Could not import slack_service for thread processing")
            # Return error in expected format
            return {
                "thread_import_error": [{
                    "text": "Error: Thread processing functionality not available - could not import slack_service",
                    "user": "System Error",
                    "timestamp": str(time.time()),
                    "type": "import_error"
                }],
                "_metadata": {
                    "source": "slack_threads",
                    "error": "Import error",
                    "channels_processed": 1,
                    "total_messages": 1,
                    "processing_time": 0.0
                }
            }
        except Exception as e:
            logging.error(f"Error in _process_thread_urls: {e}")
            # Return error in expected format
            return {
                "thread_processing_error": [{
                    "text": f"Error processing thread URLs: {str(e)}",
                    "user": "System Error", 
                    "timestamp": str(time.time()),
                    "type": "processing_error"
                }],
                "_metadata": {
                    "source": "slack_threads",
                    "error": str(e),
                    "channels_processed": 1,
                    "total_messages": 1,
                    "processing_time": 0.0
                }
            }