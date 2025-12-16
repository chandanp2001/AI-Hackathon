from typing import Dict, Any, List, Optional
from ..flow_controller.controller import FlowController
from ..data_sources.base import DataSourceRegistry
from ..llm.context_manager import ContextManager
import logging
import json
import asyncio
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from config import SLACK_BOT_TOKEN, SLACK_USER_TOKEN
import requests
import re
import time
import threading
import os
import pickle
# Import the new thread summarization function
from services.slack_service import fetch_and_summarize_slack_thread

logger = logging.getLogger(__name__)

class SlackHandler:
    """Handles all Slack interactions"""
    
    def __init__(
        self,
        flow_controller: FlowController,
        source_registry: DataSourceRegistry,
        context_manager: ContextManager
    ):
        """Initialize the Slack handler"""
        self.flow_controller = flow_controller
        self.source_registry = source_registry
        self.context_manager = context_manager
        self.client = WebClient(token=SLACK_BOT_TOKEN)
        
        # Store user states between interactions
        self.user_states = {}
        
        # New: Store thread contexts for follow-up queries with persistence
        self.thread_contexts = {}
        self.thread_contexts_file = "thread_contexts.pkl"
        
        # Load any existing thread contexts
        self._load_thread_contexts()
        
        # Caches for performance
        self.channel_name_to_id_cache = {}  # Cache channel name -> ID
        self.channel_cache = {}  # Cache for channel prefixes
        self.channel_cache_time = {}  # Last time each prefix was fetched
        self.channels_prefetched = False  # Flag to track prefetch status
        
        # Add cache for user ID to name mapping
        self.user_id_to_name_cache = {}
        
        self.channel_cache_time = {}  # Time when each prefix cache was last updated
        self.channels_prefetched = False  # Flag to track if channels have been prefetched
        self.prefetch_in_progress = False  # Lock to prevent multiple prefetch operations
        
        # Set up periodic cleanup task for thread contexts
        self._setup_cleanup_task()

    def _load_thread_contexts(self):
        """Load thread contexts from disk if available"""
        try:
            if os.path.exists(self.thread_contexts_file):
                with open(self.thread_contexts_file, 'rb') as f:
                    self.thread_contexts = pickle.load(f)
                logger.info(f"Loaded {len(self.thread_contexts)} user thread contexts from {self.thread_contexts_file}")
                
                # Log details about loaded contexts
                total_threads = sum(len(threads) for threads in self.thread_contexts.values())
                logger.info(f"Total thread contexts loaded: {total_threads}")
                
                # Log a sample of users and their thread counts
                for user_id, threads in list(self.thread_contexts.items())[:5]:
                    logger.info(f"User {user_id} has {len(threads)} thread contexts")
        except Exception as e:
            logger.error(f"Error loading thread contexts: {str(e)}")
            self.thread_contexts = {}

    def _save_thread_contexts(self):
        """Save thread contexts to disk for persistence"""
        try:
            with open(self.thread_contexts_file, 'wb') as f:
                pickle.dump(self.thread_contexts, f)
            logger.info(f"Saved {len(self.thread_contexts)} user thread contexts to {self.thread_contexts_file}")
        except Exception as e:
            logger.error(f"Error saving thread contexts: {str(e)}")

    def _setup_cleanup_task(self):
        """Set up a periodic task to clean up old thread contexts"""
        import asyncio
        import threading
        
        def run_cleanup_loop():
            """Run cleanup loop in a separate thread with its own event loop"""
            # Create a new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            async def cleanup_loop():
                while True:
                    await asyncio.sleep(3600)  # Run every hour
                    await self._cleanup_thread_contexts()
            
            # Run the cleanup loop
            try:
                loop.run_until_complete(cleanup_loop())
            except Exception as e:
                logger.error(f"Error in thread context cleanup loop: {str(e)}")
            finally:
                loop.close()
        
        # Create and start a daemon thread for the cleanup task
        cleanup_thread = threading.Thread(target=run_cleanup_loop)
        cleanup_thread.daemon = True  # Daemon thread will exit when main thread exits
        cleanup_thread.start()
        logger.info("Started thread context cleanup background thread")

    async def _cleanup_thread_contexts(self):
        """Remove old thread contexts to prevent memory leaks"""
        try:
            current_time = time.time()
            expiration_time = 24 * 60 * 60  # 24 hours in seconds
            
            # Track stats for logging
            total_before = sum(len(contexts) for contexts in self.thread_contexts.values())
            removed_count = 0
            
            # Clean up expired contexts
            for user_id in list(self.thread_contexts.keys()):
                # Find expired threads for this user
                expired_threads = []
                for thread_ts, context in self.thread_contexts[user_id].items():
                    last_interaction = context.get("last_interaction", 0)
                    if current_time - last_interaction > expiration_time:
                        expired_threads.append(thread_ts)
                        removed_count += 1
                
                # Remove expired contexts
                for thread_ts in expired_threads:
                    del self.thread_contexts[user_id][thread_ts]
                    
                # If user has no more contexts, remove the user entry
                if not self.thread_contexts[user_id]:
                    del self.thread_contexts[user_id]
            
            total_after = sum(len(contexts) for contexts in self.thread_contexts.values())
            logger.info(f"Thread context cleanup: removed {removed_count} expired contexts, {total_before} -> {total_after}")
            
            # Save updated thread contexts
            self._save_thread_contexts()
        except Exception as e:
            logger.error(f"Error cleaning up thread contexts: {str(e)}")
    
    async def register_user_token(self, user_id: str, token: str) -> bool:
        """
        Register a user's token with the SlackDataSource for improved performance.
        This allows the system to make API calls as the user when needed.
        """
        try:
            if not user_id or not token:
                logger.warning("Cannot register empty user token")
                return False
                
            # Get the SlackDataSource
            slack_source = self.source_registry.get_source("slack")
            if not slack_source or not hasattr(slack_source, "register_user_token"):
                logger.warning("SlackDataSource not available or doesn't support user tokens")
                return False
                
            # Register the token with the data source
            result = slack_source.register_user_token(user_id, token)
            
            if result:
                logger.info(f"Successfully registered user token for {user_id}")
                
                # After registering a user token, prefetch channels using it for better results
                await self.prefetch_channels(user_id)
            else:
                logger.warning(f"Failed to register user token for {user_id}")
                
            return result
            
        except Exception as e:
            logger.error(f"Error registering user token: {e}")
            return False
            
    async def prefetch_channels(self, user_id: str = None):
        """
        Prefetch all channels into cache on startup.
        This avoids delays when users are selecting channels.
        If user_id is provided, will use that user's token for fetching.
        """
        if self.prefetch_in_progress:
            logger.info("Channel prefetch already in progress, skipping")
            return
            
        # If we already have a populated cache with a significant number of channels, skip prefetching
        if self.channels_prefetched and len(self.channel_name_to_id_cache) > 100:
            logger.info(f"Skipping prefetch - already have {len(self.channel_name_to_id_cache)} channels cached")
            return
            
        try:
            self.prefetch_in_progress = True
            logger.info(f"Starting background prefetch of all Slack channels{' using user token' if user_id else ''}")
            
            # Get the slack data source
            slack_source = self.source_registry.get_source("slack")
            if not slack_source:
                logger.warning("Could not find slack data source for prefetching")
                return
                
            # Use the SlackDataSource's _fetch_all_channels method for efficiency
            if hasattr(slack_source, "_fetch_all_channels"):
                await slack_source._fetch_all_channels()
                
                # Copy the cache to our local cache
                if hasattr(slack_source, "channel_cache"):
                    # Save how many channels we had before
                    prev_count = len(self.channel_name_to_id_cache)
                    
                    # Update our cache with the new channels
                    self.channel_name_to_id_cache.update(slack_source.channel_cache)
                    
                    # Log how many new channels were added
                    new_count = len(self.channel_name_to_id_cache)
                    logger.info(f"Prefetched {len(slack_source.channel_cache)} channels into cache (added {new_count - prev_count} new channels)")
                    self.channels_prefetched = True
            else:
                # Fallback to our own implementation if the data source doesn't have the method
                await self._prefetch_channels_direct(user_id)
                
        except Exception as e:
            logger.error(f"Error prefetching channels: {e}")
        finally:
            self.prefetch_in_progress = False
            
    async def _prefetch_channels_direct(self, user_id: str = None):
        """Direct implementation of channel prefetching using the Slack API"""
        try:
            logger.info("Starting direct prefetch of channels")
            matching_channels = []
            dm_channels = []  # Separate list for DM channels
            cursor = None
            current_time = time.time()
            
            # Get the best client to use (user client if available)
            client = self.client  # Default to bot client
            slack_source = self.source_registry.get_source("slack")
            if user_id and slack_source and hasattr(slack_source, "get_client_for_user"):
                client = slack_source.get_client_for_user(user_id)
                logger.info(f"Using user client for direct channel prefetch")
            
            # STEP 1: First fetch regular channels
            logger.info("Fetching public and private channels...")
            
            # Set up API query params for regular channels
            regular_search_args = {
                "types": "public_channel,private_channel",  # Only regular channels
                "exclude_archived": True,
                "limit": 1000
            }
            
            has_more = True
            page_count = 0
            
            while has_more:
                page_count += 1
                
                if cursor:
                    regular_search_args["cursor"] = cursor
                
                # Make the API call
                result = client.conversations_list(**regular_search_args)
                channels = result.get("channels", [])
                matching_channels.extend(channels)
                
                # Get cursor for next page
                metadata = result.get("response_metadata", {})
                cursor = metadata.get("next_cursor")
                
                # If cursor is empty or empty string, we've reached the end
                if not cursor:
                    has_more = False
                    
                # Log progress
                logger.info(f"Prefetched {len(matching_channels)} regular channels so far on page {page_count}...")
                
                # Pause briefly to avoid rate limiting
                await asyncio.sleep(0.5)
            
            # STEP 2: Now fetch DM channels using users.conversations which works better with im:read scope
            logger.info("Fetching DM channels using users.conversations API with im:read scope...")
            
            # Reset cursor for new pagination
            cursor = None
            has_more = True
            dm_page_count = 0
            
            # Set up query specifically for DM channels
            dm_search_args = {
                "types": "im",  # Only direct messages
                "exclude_archived": True,
                "limit": 200  # Lower limit for more stable results
            }
            
            try:
                # First try the users.conversations method which is better for DMs
                logger.info("Attempting to use users.conversations API for DMs (better with im:read scope)")
                
                dm_cursor = None
                dm_has_more = True
                
                while dm_has_more:
                    dm_page_count += 1
                    
                    # Set up query params
                    users_convo_args = {
                        "types": "im",  # Specifically request DMs
                        "exclude_archived": False,  # Include archived for better coverage
                        "limit": 500
                    }
                    
                    if dm_cursor:
                        users_convo_args["cursor"] = dm_cursor
                    
                    try:
                        # Make the API call - users.conversations is better for fetching user's DMs
                        users_convo_result = client.users_conversations(**users_convo_args)
                        dm_batch = users_convo_result.get("channels", [])
                        
                        # Count DMs in this batch
                        actual_dms = [ch for ch in dm_batch if ch.get("id", "").startswith("D")]
                        dm_channels.extend(actual_dms)
                        
                        # Get cursor for next page
                        dm_metadata = users_convo_result.get("response_metadata", {})
                        dm_cursor = dm_metadata.get("next_cursor")
                        
                        # If cursor is empty, we've reached the end
                        if not dm_cursor:
                            dm_has_more = False
                            
                        # Log progress with detailed count of actual DMs
                        logger.info(f"Fetched {len(actual_dms)} DM channels in batch {dm_page_count} using users.conversations, total: {len(dm_channels)}")
                        
                        # Pause briefly to avoid rate limiting
                        await asyncio.sleep(0.5)
                        
                    except SlackApiError as e:
                        error_msg = str(e).lower()
                        if "not_allowed_token_type" in error_msg:
                            logger.warning(f"Token does not have permission for users.conversations: {str(e)}")
                        elif "missing_scope" in error_msg:
                            logger.warning(f"Token missing required scope for users.conversations: {str(e)}")
                        else:
                            logger.warning(f"Error fetching DM channels with users.conversations: {str(e)}")
                        dm_has_more = False  # Stop pagination on error
                        
                logger.info(f"Completed users.conversations method, found {len(dm_channels)} DM channels")
                
            except Exception as e:
                logger.warning(f"Could not use users.conversations method for DMs: {str(e)}")
            
            # Fallback: If users.conversations didn't work well, try conversations.list specifically for DMs
            if len(dm_channels) < 100:  # If we didn't get many DMs, try fallback method
                logger.info("users.conversations method didn't find many DMs, trying conversations.list fallback...")
                
                # Reset cursor for new pagination
                cursor = None
                has_more = True
                list_dm_count = 0
                
                while has_more:
                    list_dm_count += 1
                    
                    if cursor:
                        dm_search_args["cursor"] = cursor
                    
                    try:
                        # Make the API call specifically for DMs
                        dm_result = client.conversations_list(**dm_search_args)
                        dm_batch = dm_result.get("channels", [])
                        
                        # Count actual DMs in this batch
                        actual_dms = [ch for ch in dm_batch if ch.get("id", "").startswith("D")]
                        
                        # Add only the DMs that aren't already in our list
                        existing_dm_ids = {ch.get("id") for ch in dm_channels}
                        new_dms = [ch for ch in actual_dms if ch.get("id") not in existing_dm_ids]
                        dm_channels.extend(new_dms)
                        
                        # Get cursor for next page
                        dm_metadata = dm_result.get("response_metadata", {})
                        cursor = dm_metadata.get("next_cursor")
                        
                        # If cursor is empty, we've reached the end
                        if not cursor:
                            has_more = False
                            
                        # Log progress
                        logger.info(f"Fetched {len(new_dms)} additional DM channels in batch {list_dm_count} using conversations.list, total: {len(dm_channels)}")
                        
                        # Pause briefly to avoid rate limiting
                        await asyncio.sleep(0.5)
                        
                    except SlackApiError as e:
                        error_msg = str(e).lower()
                        if "not_allowed_token_type" in error_msg:
                            logger.warning(f"Token does not have permission to list DMs: {str(e)}")
                        elif "missing_scope" in error_msg:
                            logger.warning(f"Token missing required scope for DM listing: {str(e)}")
                        else:
                            logger.warning(f"Error fetching DM channels: {str(e)}")
                        has_more = False  # Stop pagination on error
            
            # Count DM channels with user info vs without
            dms_with_user = sum(1 for ch in dm_channels if ch.get("user"))
            dms_without_user = sum(1 for ch in dm_channels if not ch.get("user"))
            logger.info(f"DM channel stats: {len(dm_channels)} total, {dms_with_user} with user info, {dms_without_user} without user info")
                
            # Add DM channels to the main channel list
            matching_channels.extend(dm_channels)
            
            # Update cache
            self.channel_cache[""] = matching_channels  # Store all channels under empty prefix
            self.channel_cache_time[""] = current_time
            
            # Update name to ID cache for faster lookups
            dm_count = 0
            regular_count = 0
            
            for channel in matching_channels:
                try:
                    channel_id = channel.get("id", "")
                    if not channel_id:
                        continue
                        
                    # Handle DM channels (IDs starting with D) differently
                    if channel_id.startswith("D"):
                        # DM channels don't have a name, they have a user field
                        user_id = channel.get("user", "")
                        if user_id:
                            dm_key = f"dm_{user_id}".lower()
                            self.channel_name_to_id_cache[dm_key] = channel_id
                            
                            # Also store by ID for direct lookups
                            self.channel_name_to_id_cache[channel_id.lower()] = channel_id
                            dm_count += 1
                        else:
                            # Fallback if no user ID
                            dm_key = f"dm_{channel_id}".lower()
                            self.channel_name_to_id_cache[dm_key] = channel_id
                            self.channel_name_to_id_cache[channel_id.lower()] = channel_id
                            dm_count += 1
                    else:
                        # Regular channels have a name field
                        channel_name = channel.get("name", "").lower()
                        if channel_name and channel_id:
                            self.channel_name_to_id_cache[channel_name] = channel_id
                            regular_count += 1
                except Exception as e:
                    logger.warning(f"Error processing channel during prefetch: {e}")
                    continue
            
            # Log all the DM channel IDs for debugging (limit to first 20)
            dm_ids = [ch.get("id") for ch in dm_channels][:20]
            logger.info(f"Sample of DM channel IDs: {dm_ids}")
            
            self.channels_prefetched = True
            logger.info(f"Direct prefetch completed. Cached {len(self.channel_name_to_id_cache)} channels ({regular_count} regular, {dm_count} DMs)")
            
        except SlackApiError as e:
            error_msg = str(e).lower()
            if "ratelimited" in error_msg:
                logger.error(f"Rate limited during channel prefetch: {str(e)}")
            else:
                logger.error(f"Slack API error during channel prefetch: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error prefetching channels: {str(e)}")
        
    async def _lookup_channel_id(self, channel_name, user_id=None):
        """Look up a channel ID from a channel name"""
        if not channel_name:
            return None
            
        channel_name = channel_name.lower()
        
        # If the input is already a DM channel ID, return it directly
        if channel_name.upper().startswith('D'):
            logger.info(f"Input appears to be a DM channel ID: {channel_name.upper()}")
            
            # Add to cache for future lookups
            self.channel_name_to_id_cache[channel_name.lower()] = channel_name.upper()
            return channel_name.upper()
        
        # First check our local cache
        if channel_name in self.channel_name_to_id_cache:
            channel_id = self.channel_name_to_id_cache[channel_name]
            logger.info(f"Found channel ID in local cache: {channel_name} -> {channel_id}")
            return channel_id
            
        # Handle direct message channels differently - they start with 'D'
        # Direct message "names" might be usernames or user IDs
        if channel_name.startswith('@') or channel_name.startswith('u'):
            logger.info(f"Detected potential direct message reference: {channel_name}")
            # If it's a username, we need to look up the user ID first
            if channel_name.startswith('@'):
                username = channel_name[1:]  # Remove the @ symbol
                try:
                    # Try to find the user by username
                    user_list = self.client.users_list()
                    for user in user_list.get("members", []):
                        if (user.get("name", "").lower() == username.lower() or 
                            user.get("profile", {}).get("display_name", "").lower() == username.lower()):
                            user_to_dm = user.get("id")
                            logger.info(f"Found user ID {user_to_dm} for username {username}")
                            
                            # Now open a DM channel with this user
                            try:
                                dm_channel = self.client.conversations_open(users=user_to_dm)
                                dm_id = dm_channel.get("channel", {}).get("id")
                                if dm_id:
                                    logger.info(f"Opened DM channel with {username}: {dm_id}")
                                    # Cache for future lookups
                                    self.channel_name_to_id_cache[channel_name] = dm_id
                                    self.channel_name_to_id_cache[dm_id.lower()] = dm_id  # Also cache the direct ID
                                    return dm_id
                            except SlackApiError as dm_error:
                                logger.warning(f"Error opening DM channel: {dm_error}")
                except SlackApiError as user_error:
                    logger.warning(f"Error looking up user: {user_error}")
            
            # If channel_name is already a user ID (starts with 'U')
            elif channel_name.upper().startswith('U'):
                user_to_dm = channel_name.upper()
                try:
                    # Try to open a DM channel with this user ID
                    dm_channel = self.client.conversations_open(users=user_to_dm)
                    dm_id = dm_channel.get("channel", {}).get("id")
                    if dm_id:
                        logger.info(f"Opened DM channel with user ID {user_to_dm}: {dm_id}")
                        # Cache for future lookups
                        self.channel_name_to_id_cache[channel_name] = dm_id
                        self.channel_name_to_id_cache[dm_id.lower()] = dm_id  # Also cache the direct ID
                        return dm_id
                except SlackApiError as dm_error:
                    logger.warning(f"Error opening DM channel: {dm_error}")
            
        # Special handling for team- private channels
        is_team_channel = channel_name.startswith("team-")
        if is_team_channel:
            logger.info(f"Detected team- private channel: {channel_name}, using specialized lookup")
        
        # If not in cache, try to look it up from the Slack API
        try:
            # Get the Slack source to leverage its channel lookup capabilities
            slack_source = self.source_registry.get_source("slack")
            if slack_source and hasattr(slack_source, "_get_channel_id"):
                # Try to use the slack source's channel ID lookup which has better caching
                channel_id = slack_source._get_channel_id(channel_name, user_id)
                if channel_id:
                    # Cache for future lookups
                    self.channel_name_to_id_cache[channel_name] = channel_id
                    return channel_id
            
            # If that fails, try a direct lookup with the current token
            # Try to use user token if available for better results
            client = self.client
            
            # Check if we have a user token to use instead
            if user_id and slack_source and hasattr(slack_source, "get_client_for_user"):
                user_client = slack_source.get_client_for_user(user_id)
                if user_client:
                    logger.info(f"Using user client for channel lookup: {channel_name}")
                    client = user_client
            
            # If we don't have a user client but have a config token, use it
            if client == self.client and SLACK_USER_TOKEN:
                try:
                    from slack_sdk import WebClient
                    # Strip any quotes that might have been included in the token
                    clean_token = SLACK_USER_TOKEN.strip().strip('"\'')
                    test_client = WebClient(token=clean_token)
                    
                    # Test if the token works
                    auth_test = test_client.auth_test()
                    if auth_test.get('ok'):
                        logger.info(f"Using config SLACK_USER_TOKEN for channel lookup")
                        client = test_client
                    else:
                        error_msg = auth_test.get('error', 'unknown error')
                        logger.warning(f"Config token validation failed for channel lookup: {error_msg}")
                except Exception as e:
                    logger.warning(f"Error creating client with config token: {e}")
            
            # Try an alternative approach for private channels if we have a user token
            try:
                # First try conversations_list which is more reliable for private channels
                result = client.conversations_list(
                    types="public_channel,private_channel,im",  # Include im (DMs) here
                    exclude_archived=True,
                    limit=1000  # Increased for better coverage
                )
                
                # Look for exact and then partial matches
                # Priority matching for team- channels
                if is_team_channel:
                    # First search specifically for team channels by exact match
                    for channel in result.get("channels", []):
                        ch_name = channel.get("name", "").lower()
                        ch_id = channel.get("id")
                        is_private = channel.get("is_private", False)
                        
                        # Update cache regardless
                        if ch_name and ch_id:
                            self.channel_name_to_id_cache[ch_name] = ch_id
                        
                        # Prioritize exact match for team channels
                        if ch_name == channel_name:
                            logger.info(f"Found team channel via exact match: {channel_name} -> {ch_id} (private: {is_private})")
                            return ch_id
                
                # For non-team channels or if team channel not found by exact match
                # Look for exact matches first (for all channel types)
                for channel in result.get("channels", []):
                    ch_name = channel.get("name", "").lower()
                    ch_id = channel.get("id")
                    
                    # Update our cache with all returned channels
                    if ch_name and ch_id:
                        self.channel_name_to_id_cache[ch_name] = ch_id
                    
                    # Directly cache all DM channels by ID
                    if ch_id and ch_id.startswith("D"):
                        self.channel_name_to_id_cache[ch_id.lower()] = ch_id
                    
                    # Check for exact match
                    if ch_name == channel_name:
                        logger.info(f"Found channel ID via direct API: {channel_name} -> {ch_id}")
                        return ch_id
                
                # Try partial matches if no exact match
                for channel in result.get("channels", []):
                    ch_name = channel.get("name", "").lower()
                    ch_id = channel.get("id")
                    
                    # For team- channels, be more lenient with partial matches
                    if is_team_channel:
                        # If we're looking for team-something, match with more flexibility
                        if ch_name.startswith("team-") and (channel_name in ch_name or ch_name in channel_name):
                            self.channel_name_to_id_cache[channel_name] = ch_id
                            logger.info(f"Found team channel via partial match: {channel_name} -> {ch_id} (actual name: {ch_name})")
                            return ch_id
                    elif (channel_name in ch_name or ch_name in channel_name) and ch_id:
                        self.channel_name_to_id_cache[channel_name] = ch_id
                        logger.info(f"Found channel ID via partial match: {channel_name} -> {ch_id} (actual name: {ch_name})")
                        return ch_id
                
                # If we get here, the channel wasn't found through normal list methods
                # Try direct search methods - works better for some private channels
                logger.info(f"Channel not found in list, trying search methods: {channel_name}")
                
                # For private team channels, try a different approach
                if is_team_channel:
                    logger.info(f"Trying specialized approaches for team- private channel: {channel_name}")
                    
                    # Try getting the channels the bot is a member of
                    try:
                        # List channels the bot is specifically a member of
                        member_result = client.users_conversations(
                            types="private_channel",
                            exclude_archived=True,
                            limit=200
                        )
                        
                        # Check each channel the bot is a member of
                        for channel in member_result.get("channels", []):
                            ch_name = channel.get("name", "").lower()
                            ch_id = channel.get("id")
                            
                            # Cache for future lookups
                            if ch_name and ch_id:
                                self.channel_name_to_id_cache[ch_name] = ch_id
                                
                            # Look for the exact team channel
                            if ch_name == channel_name:
                                logger.info(f"Found team channel in member list: {channel_name} -> {ch_id}")
                                return ch_id
                            
                            # Try partial match for team channels
                            if ch_name.startswith("team-") and (channel_name in ch_name or ch_name in channel_name):
                                logger.info(f"Found similar team channel in member list: {channel_name} -> {ch_id} (actual: {ch_name})")
                                return ch_id
                    except Exception as member_error:
                        logger.warning(f"Error getting member channels: {member_error}")
                
                # Try different search methods
                try:
                    # Try with search method if available
                    if hasattr(client, "search_messages"):
                        search_result = client.search_messages(
                            query=f"in:#{channel_name}",
                            count=1
                        )
                        # Parse the search results to find the channel ID
                        matches = search_result.get("messages", {}).get("matches", [])
                        for match in matches:
                            ch_id = match.get("channel", {}).get("id")
                            if ch_id:
                                logger.info(f"Found channel ID via search: {channel_name} -> {ch_id}")
                                self.channel_name_to_id_cache[channel_name] = ch_id
                                return ch_id
                except Exception as search_error:
                    logger.warning(f"Search method failed: {search_error}")
                
                # As last resort, check if it's directly a channel ID
                if channel_name.upper().startswith('C') and len(channel_name) >= 9:
                    # This looks like a channel ID, try to verify it
                    try:
                        test_id = channel_name.upper()
                        info_result = client.conversations_info(channel=test_id)
                        if info_result.get("ok"):
                            logger.info(f"Input was already a channel ID: {test_id}")
                            # Update cache with correct name
                            actual_name = info_result.get("channel", {}).get("name", "").lower()
                            if actual_name:
                                self.channel_name_to_id_cache[actual_name] = test_id
                            return test_id
                    except Exception as id_error:
                        logger.warning(f"Failed to verify as channel ID: {id_error}")
            
            except SlackApiError as list_error:
                logger.warning(f"conversations_list error: {list_error}")
                # Continue to other methods
                    
        except SlackApiError as e:
            if "ratelimited" in str(e).lower():
                logger.warning(f"Rate limited during direct API lookup: {e}")
            else:
                logger.warning(f"Slack API error in direct lookup for channel: {e}")
        except Exception as e:
            logger.warning(f"Unexpected error in direct API lookup for channel: {e}")
            
        # If all lookup methods failed, provide more detailed error message
        logger.warning(f"Channel validation failed: {channel_name}")
        
        # Better error message with troubleshooting help
        error_msg = f"Channel #{channel_name} not found. Please check:\n"
        error_msg += "1. The channel name is spelled correctly (case-sensitive)\n"
        error_msg += "2. The bot has been invited to the channel with /invite @YourBot\n"
        error_msg += "3. For private channels, use the channel ID instead (starts with C)\n"
        
        if is_team_channel:
            error_msg += "\nThis appears to be a team- private channel. Make sure:\n"
            error_msg += "- You've invited the bot to this private channel\n"
            error_msg += "- The exact channel name is used (team-name, not Team-Name)\n"
            error_msg += "- As a workaround, try using the channel ID directly\n"
        
        error_msg += "\nTo find a channel ID: Right-click on the channel in Slack and select 'Copy Link'. The ID is the part after the last / in the URL."
        
        raise ValueError(error_msg)
        
    async def _get_channels_by_prefix(self, prefix=""):
        """Get channels matching the given prefix"""
        prefix = prefix.lower()
        current_time = time.time()
        
        # Use an empty string as key for "all channels" request when prefix is empty
        cache_key = prefix if prefix else ""
        
        # Check if we have a fresh cache for this prefix (less than 5 minutes old)
        if cache_key in self.channel_cache and cache_key in self.channel_cache_time:
            if current_time - self.channel_cache_time[cache_key] < 300:  # 5 minutes
                logger.info(f"Using cached channel list for prefix '{prefix}' ({len(self.channel_cache[cache_key])} channels)")
                return self.channel_cache[cache_key]
        
        # If we're fetching without a prefix, we'll be more conservative (only do this if absolutely needed)
        if not prefix:
            logger.info("Fetching ALL channels from Slack API - this might cause rate limiting")
        else:
            logger.info(f"Fetching channels matching prefix '{prefix}' from Slack API")
            
        try:
            matching_channels = []
            cursor = None
            
            # Set up API query params
            search_args = {
                "types": "public_channel,private_channel",
                "exclude_archived": True,
                "limit": 200  # Reduced from 1000 to avoid rate limiting
            }
            
            # Try to avoid using too many API calls
            max_pages = 3 if prefix else 1  # Only fetch one page if no prefix
            current_page = 0
            
            while cursor is not None or current_page == 0:
                current_page += 1
                
                if cursor:
                    search_args["cursor"] = cursor
                
                # Make the API call
                result = self.client.conversations_list(**search_args)
                channels = result.get("channels", [])
                
                # Filter channels by prefix (client-side filtering)
                if prefix:
                    prefix_matches = [
                        channel for channel in channels 
                        if channel.get("name", "").lower().startswith(prefix)
                    ]
                    matching_channels.extend(prefix_matches)
                else:
                    # If no prefix, just add all channels
                    matching_channels.extend(channels)
                
                # Get cursor for next page
                metadata = result.get("response_metadata", {})
                cursor = metadata.get("next_cursor")
                
                # Stop after max_pages to avoid rate limiting
                if current_page >= max_pages:
                    break
            
            # Update cache for this prefix
            self.channel_cache[cache_key] = matching_channels
            self.channel_cache_time[cache_key] = current_time
            
            # Update name to ID cache for faster lookups later
            for channel in matching_channels:
                channel_name = channel.get("name", "").lower()
                channel_id = channel.get("id", "")
                if channel_name and channel_id:
                    self.channel_name_to_id_cache[channel_name] = channel_id
            
            logger.info(f"Found {len(matching_channels)} channels matching prefix '{prefix}'")
            return matching_channels
            
        except SlackApiError as e:
            error_msg = str(e).lower()
            if "ratelimited" in error_msg:
                logger.error(f"Rate limited by Slack API: {str(e)}")
                # If we're rate limited, try to use whatever we have cached
                if cache_key in self.channel_cache:
                    logger.warning("Using outdated cache due to rate limiting")
                    return self.channel_cache[cache_key]
                # If no cache, try with a different approach
                if prefix and len(prefix) >= 2:
                    # Try to use a shorter prefix from the cache
                    shorter_prefix = prefix[0]
                    if shorter_prefix in self.channel_cache:
                        # Filter the shorter prefix cache client-side
                        filtered = [
                            channel for channel in self.channel_cache[shorter_prefix]
                            if channel.get("name", "").lower().startswith(prefix)
                        ]
                        logger.warning(f"Using cache with shorter prefix '{shorter_prefix}' - found {len(filtered)} matches")
                        return filtered
            elif "not_in_channel" in error_msg or "channel_not_found" in error_msg:
                logger.error(f"Channel access error: {str(e)}")
                # Return empty list - bot might not have access to these channels
                return []
            else:
                # Generic Slack API error
                logger.error(f"Slack API error: {str(e)}")
                # Try to use cache if available
                if cache_key in self.channel_cache:
                    return self.channel_cache[cache_key]
                return []
        except Exception as e:
            # Any other error
            logger.error(f"Unexpected error fetching channels: {str(e)}")
            # Try to use cache if available
            if cache_key in self.channel_cache:
                return self.channel_cache[cache_key]
            return []
            
    async def handle_slash_command(self, command_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the /test command with combined modal"""
        try:
            user_id = command_data["user_id"]
            channel_id = command_data["channel_id"]
            response_url = command_data.get("response_url")

            logger.info(f"Slash command received in channel: {channel_id}")

            # Check if this is a DM channel
            is_dm = channel_id.startswith('D')
            logger.info(f"Is DM channel: {is_dm}")

            # For DM channels, don't try to open a new one - use the original channel
            # This fixes the issue where we were creating new DM channels unnecessarily
            if is_dm:
                logger.info(f"Using original DM channel: {channel_id}")
            
            # Initialize user state
            self.user_states[user_id] = {
                "sources": [],
                "prompt": "",
                "channel_id": channel_id,
                "slack_channel_id": channel_id,
                "source_data": {},
                "is_dm": is_dm,
                "response_url": response_url  # Store response_url for fallback use
            }

            # Compose initial message blocks
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Configure Your Analysis*\nAdd data sources and enter your analysis prompt."
                    }
                },
                {
                    "type": "input",
                    "block_id": "prompt_block",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "prompt_input",
                        "placeholder": {"type": "plain_text", "text": "Enter your analysis prompt..."}
                    },
                    "label": {"type": "plain_text", "text": "Analysis Prompt"}
                },
                {
                    "type": "actions",
                    "block_id": "source_actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "➕ Add Data Source", "emoji": True},
                            "action_id": "add_source",
                            "style": "primary"
                        }
                    ]
                },
                {
                    "type": "divider"
                },
                {
                    "type": "actions",
                    "block_id": "submit_block",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Submit Analysis", "emoji": True},
                            "action_id": "submit_prompt",
                            "style": "primary"
                        }
                    ]
                }
            ]

            # For DM channels, try direct message first, but use response_url as fallback
            try:
                self.client.chat_postMessage(
                    channel=channel_id,
                    blocks=blocks,
                    text="Analysis setup started"  # Fallback text
                )
                logger.info(f"Successfully posted message to channel: {channel_id}")
            except SlackApiError as e:
                error_msg = str(e).lower()
                if "channel_not_found" in error_msg and response_url:
                    logger.info(f"Using response_url fallback for DM channel {channel_id}")
                    requests.post(
                        response_url,
                        json={
                            "blocks": blocks,
                            "text": "Analysis setup started"
                        },
                        headers={"Content-Type": "application/json"}
                    )
                else:
                    raise

            return {"text": "Analysis setup started"}

        except Exception as e:
            logger.error(f"Error handling slash command: {str(e)}")
            return {"text": f"Error: {str(e)}"}
    
    async def options_load_handler(self, interaction_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle options_load requests for external select menus"""
        try:
            # Extract search value from different possible formats
            value = ""
            action_id = ""
            user_id = None
            
            if interaction_data.get("type") == "block_suggestions":
                value = interaction_data.get("value", "").lower()
                action_id = interaction_data.get("action_id", "")
                user_id = interaction_data.get("user", {}).get("id")
            elif "value" in interaction_data:
                value = interaction_data.get("value", "").lower()
                action_id = interaction_data.get("action_id", "")
                user_id = interaction_data.get("user_id")
            elif "payload" in interaction_data:
                try:
                    if isinstance(interaction_data["payload"], str):
                        payload = json.loads(interaction_data["payload"])
                        value = payload.get("value", "").lower()
                        action_id = payload.get("action_id", "")
                        user_id = payload.get("user", {}).get("id")
                except:
                    pass
            
            logger.info(f"Loading options for action_id: {action_id}, search value: '{value}', user_id: {user_id}")
            
            # Handle Slack channel search with direct input
            if action_id == "channel" or action_id == "channel_additional":
                try:
                    # Make sure we have prefetched channels first if possible
                    if not self.channels_prefetched and not self.prefetch_in_progress:
                        # We can prefetch in the background
                        # This will help for future requests but won't block the current one
                        await self.prefetch_channels(user_id)
                    
                    # If user hasn't typed enough, just suggest direct entry
                    # This avoids unnecessary API calls
                    if not value or len(value) < 2:
                        return {
                            "options": [
                                {
                                    "text": {"type": "plain_text", "text": "Type more characters to search..."},
                                    "value": "help_text"
                                }
                            ]
                        }
                    
                    # If user has typed something, use direct text matching and our cache
                    channel_suggestions = []
                    
                    # First, offer the exact text they typed as a direct entry option
                    channel_suggestions.append({
                        "text": {"type": "plain_text", "text": f"#{value} (Use exactly what you typed)"},
                        "value": value
                    })
                    
                    # Check our channel name to ID cache for matches
                    matching_channels = []
                    
                    # Check if we have a well-populated cache first
                    if len(self.channel_name_to_id_cache) > 100:
                        # Use our large prefetched cache for fast lookup
                        for cached_name, cached_id in self.channel_name_to_id_cache.items():
                            if value in cached_name:
                                matching_channels.append({
                                    "name": cached_name,
                                    "id": cached_id
                                })
                                
                        # If we found a lot of matches, sort them by relevance 
                        # (exact match first, then prefix match, then contains match)
                        if len(matching_channels) > 3:
                            # Sort by relevance: exact match first, then starts with, then contains
                            exact_matches = [ch for ch in matching_channels if ch['name'] == value]
                            prefix_matches = [ch for ch in matching_channels if ch['name'].startswith(value) and ch['name'] != value]
                            other_matches = [ch for ch in matching_channels if value in ch['name'] and not ch['name'].startswith(value)]
                            
                            # Combine in order of relevance
                            matching_channels = exact_matches + prefix_matches + other_matches
                            
                        logger.info(f"Found {len(matching_channels)} matching channels in cache for '{value}'")
                    else:
                        # If our cache isn't well populated, use what we have
                        for cached_name, cached_id in self.channel_name_to_id_cache.items():
                            if value in cached_name:
                                matching_channels.append({
                                    "name": cached_name,
                                    "id": cached_id
                                })
                    
                    # Add matching channels from cache to suggestions (limited to 9 to leave room for the manual option)
                    for channel in matching_channels[:9]:
                        channel_suggestions.append({
                            "text": {"type": "plain_text", "text": f"#{channel['name']}"},
                            "value": channel['name']  # Use name as value
                        })
                        
                    # Only if needed and we have very few results, make a direct API call
                    if len(channel_suggestions) < 2 and len(value) >= 2:
                        try:
                            # Get the best client to use (user client if available)
                            client = self.client  # Default to bot client
                            slack_source = self.source_registry.get_source("slack")
                            if user_id and slack_source and hasattr(slack_source, "get_client_for_user"):
                                client = slack_source.get_client_for_user(user_id)
                                logger.info(f"Using user client for channel search")
                        
                            # Make a single limited API call
                            result = client.conversations_list(
                                types="public_channel,private_channel",  # Include private channels too
                                exclude_archived=True,
                                limit=20  # Increased limit for better results
                            )
                            
                            all_channels = result.get("channels", [])
                            
                            # Find channels matching search term
                            for channel in all_channels:
                                channel_name = channel.get("name", "").lower()
                                channel_id = channel.get("id", "")
                                
                                # Add to our cache for future use
                                if channel_name and channel_id:
                                    self.channel_name_to_id_cache[channel_name] = channel_id
                                
                                if value in channel_name:
                                    channel_suggestions.append({
                                        "text": {"type": "plain_text", "text": f"#{channel_name}"},
                                        "value": channel_name  # Use name as value for consistency
                                    })
                        except SlackApiError as e:
                            logger.warning(f"API error when fetching channels: {e}")
                            # Continue without API results
                    
                    # If we still didn't find anything, offer manual entry
                    if len(channel_suggestions) <= 1:
                        channel_suggestions.append({
                            "text": {"type": "plain_text", "text": f"#{value} (Use this channel name)"},
                            "value": value
                        })
                        
                        channel_suggestions.append({
                            "text": {"type": "plain_text", "text": "No channels found - type more specifically"},
                            "value": "not_found"
                        })
                    
                    # Return our suggestions (limit to 10 for UI)
                    return {"options": channel_suggestions[:10]}
                        
                except Exception as e:
                    logger.error(f"Error in channel search: {str(e)}")
                    return {
                        "options": [
                            {
                                "text": {"type": "plain_text", "text": f"Enter channel name manually: {value}"},
                                "value": value
                            }
                        ]
                    }
            
            # Default response
            return {
                "options": [
                    {
                        "text": {"type": "plain_text", "text": "No options available"},
                        "value": "none"
                    }
                ]
            }
            
        except Exception as e:
            logger.error(f"Error handling options_load: {str(e)}")
            # Even on error, allow the user to enter the channel name manually
            return {
                "options": [
                    {
                        "text": {"type": "plain_text", "text": f"Enter manually: {value if value else 'type channel name'}"},
                        "value": value if value else "manual_entry"
                    }
                ]
            }
        
    async def handle_prompt_submission(self, interaction_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle final prompt submission and generate analysis"""
        try:
            user_id = interaction_data["user"]["id"]
            # Get the current channel where the user is interacting
            interaction_channel_id = interaction_data["channel"]["id"]
            
            # Get thread_ts if present (for DM channels)
            thread_ts = None
            # Extract thread_ts from message for DM channels
            if interaction_channel_id.startswith('D'):
                # Try to get from container.message_ts
                if interaction_data.get("container", {}).get("message_ts"):
                    thread_ts = interaction_data["container"]["message_ts"]
                # Or from message.ts
                elif interaction_data.get("message", {}).get("ts"):
                    thread_ts = interaction_data["message"]["ts"]
                
                logger.info(f"Extracted thread_ts for DM channel: {thread_ts}")
            
            # Get user state which should have the correct channel information
            user_state = self.user_states.get(user_id)

            if not user_state:
                return {"text": "Session expired. Please start over with /test"}
            
            # Use the channel ID from user state if available (which should be the DM channel ID)
            response_channel_id = user_state.get("channel_id") or interaction_channel_id
            is_dm = user_state.get("is_dm", response_channel_id.startswith('D'))
            
            # Get the response_url for fallback in DM channels
            response_url = user_state.get("response_url")
            
            logger.info(f"Processing prompt submission: user={user_id}, channel={response_channel_id}, is_dm={is_dm}, thread_ts={thread_ts}")

            # Check if this is a modal view submission or message-based button click
            container_type = interaction_data.get("container", {}).get("type", "")
            is_modal = container_type == "view"
            view_id = interaction_data.get("container", {}).get("view_id") if is_modal else None
            
            # For modal views, update the view to show processing status
            if is_modal and view_id:
                try:
                    self.client.views_update(
                        view_id=view_id,
                        view={
                            "type": "modal",
                            "title": {"type": "plain_text", "text": "Processing"},
                            "close": {"type": "plain_text", "text": "Close"},
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": "🔍 *Processing your request...*\nYour results will appear shortly in the channel."
                                    }
                                }
                            ]
                        }
                    )
                except Exception as e:
                    logger.error(f"Error updating modal view: {str(e)}")

            # Ensure we're using the correct channel for responses (from user state)
            logger.info(f"Using channel {response_channel_id} for response")

            # Check if this is a direct message channel
            if is_dm:
                logger.info(f"Processing prompt submission in direct message channel: {response_channel_id}")
                if response_url:
                    logger.info(f"Response URL available for fallback: {response_url}")
                else:
                    logger.warning("No response_url available for DM channel fallback")
                
            if not response_channel_id:
                logger.error("No valid channel_id available for prompt submission")
                return {"text": "Error: Could not find a valid channel to post the response. Please try again with /test"}

            # Get prompt from either prompt_text (set in handle_interaction) or from state values
            prompt = ""
            if "prompt_text" in interaction_data:
                prompt = interaction_data["prompt_text"]
            else:
                prompt = interaction_data.get("state", {}).get("values", {}).get("prompt_block", {}).get("prompt", {}).get("value", "")
                # Try prompt_input if prompt isn't found
                if not prompt:
                    prompt = interaction_data.get("state", {}).get("values", {}).get("prompt_block", {}).get("prompt_input", {}).get("value", "")

            if not prompt:
                return {"text": "Please enter a prompt"}

            # VALIDATION STEP 1: Check if any data sources are provided
            if not user_state.get('source_data') or len(user_state.get('source_data', {})) == 0:
                return {
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "⚠️ *Validation Error*: Please select at least one data source before submitting your prompt."
                            }
                        },
                        {
                            "type": "divider"
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Add Data Source", "emoji": True},
                                    "action_id": "add_source",
                                    "style": "primary"
                                }
                            ]
                        }
                    ]
                }

            # Determine if this is a direct question
            is_direct = self._is_direct_question(prompt)
            
            # Send an initial message to the main channel to create a new thread
            if is_direct:
                initial_msg = f"🔍 Analyzing: *{prompt}*\nLooking for a direct answer to this question across {len(user_state['source_data'])} data sources..."
            else:
                initial_msg = f"🔍 Analyzing: *{prompt}*\nProcessing this query across {len(user_state['source_data'])} data sources..."
            
            # For DM channels, check if we already have a thread_ts and use it
            # Otherwise, post initial message to main channel to create a new thread
            new_thread_ts = None
            
            if is_dm and thread_ts:
                # For DM channels with existing thread_ts, use it directly
                new_thread_ts = thread_ts
                logger.info(f"Using existing thread_ts for DM channel: {new_thread_ts}")
                
                # Send an initial message in the thread to acknowledge
                await self.post_message(
                    response_channel_id,
                    text=initial_msg,
                    thread_ts=new_thread_ts,
                    return_ts=False
                )
            else:
                # Post initial message to main channel (no thread_ts)
                # For DM channels, our modified post_message will use response_url as fallback if needed
                initial_response = await self.post_message(
                    response_channel_id,
                    text=initial_msg,
                    # No thread_ts here - this creates a message in the main channel
                    return_ts=True  # Add a parameter to return the timestamp
                )
                
                # Get the timestamp of the initial message to use as the parent of the thread
                new_thread_ts = initial_response.get("ts") if initial_response else None
            
            # For DM channels, we need to handle missing thread_ts when using response_url
            dm_threading_available = True
            
            if not new_thread_ts:
                logger.warning("Failed to get timestamp for new thread, falling back to main channel")
                
                # For DM channels with response_url, we can't get thread_ts when using response_url
                # We'll have to post all messages to the main channel without threading
                if is_dm and response_url:
                    logger.info("DM channel with response_url - threading may not be available")
                    dm_threading_available = False
            
            # Process data from all sources
            analysis_data = {}
            validation_errors = []
            
            for source_name, source_data in user_state["source_data"].items():
                source_instance = self.source_registry.get_source(source_name)
                try:
                    # VALIDATION STEP 2: Validate each data source
                    # Validate inputs
                    validation_result = await source_instance.validate_inputs(source_data)
                    if not validation_result:
                        # Provide specific error messages based on source type
                        if source_name.lower() == 'slack':
                            error_msg = "Please enter correct channel name or ID"
                            validation_errors.append(f"Slack validation error: {error_msg}")
                            continue
                        elif source_name.lower() == 'jira':
                            error_msg = "Please enter correct JQL query or project key"
                            validation_errors.append(f"Jira validation error: {error_msg}")
                            continue
                        elif source_name.lower() == 'google_docs':
                            error_msg = "Please enter correct Google Doc URL"
                            validation_errors.append(f"Google Docs validation error: {error_msg}")
                            continue
                        elif source_name.lower() == 'google_sheets':
                            error_msg = "Please enter correct Google Sheets URL"
                            validation_errors.append(f"Google Sheets validation error: {error_msg}")
                            continue
                        elif source_name.lower() == 'tableau':
                            error_msg = "Please enter correct Tableau dashboard URL"
                            validation_errors.append(f"Tableau validation error: {error_msg}")
                            continue
                        else:
                            validation_errors.append(f"Invalid inputs for {source_name}")
                            # Skip invalid source
                            continue
                    
                    # Special handling for different data sources
                    # For Slack, we handle multiple channels and add query
                    if source_name.lower() == 'slack':
                        # Check if we have thread URLs (new format)
                        thread_urls = source_data.get('thread_urls')
                        if thread_urls:
                            # Handle thread URLs - pass them directly to the data source
                            logger.info(f"Processing Slack thread URLs: {thread_urls}")
                            source_data_with_query = {
                                **source_data,
                                'query': prompt,
                                'user_id': user_id
                            }
                            data = await source_instance.fetch_data(source_data_with_query)
                        else:
                            # Handle regular channel format (existing logic)
                            # Ensure we have a valid slack source_data with channel
                            channel_data = source_data.get('channel')
                            if not channel_data:
                                validation_errors.append("No channels specified for Slack source")
                                continue
                                
                            # Log channel data for debugging
                            logger.info(f"Processing Slack source with channel data: {json.dumps(channel_data, indent=2)}")
                            
                            # Ensure we're handling a list of channels properly
                            if not isinstance(channel_data, list):
                                channel_data = [channel_data]
                                logger.info(f"Converting single channel to list format: {json.dumps(channel_data, indent=2)}")
                            
                            logger.info(f"Processing {len(channel_data)} Slack channels: {', '.join([ch.get('name', 'unnamed') for ch in channel_data if isinstance(ch, dict) and 'name' in ch])}")
                            
                            # Validate each channel has an ID if possible
                            validated_channels = []
                            channel_errors = []
                            
                            for i, ch in enumerate(channel_data):
                                if isinstance(ch, dict):
                                    if 'id' in ch and ch['id']:
                                        # Channel has ID already, use it as is
                                        validated_channels.append(ch)
                                        logger.info(f"Channel {i+1}: {ch.get('name', 'unnamed')} has ID {ch['id']}")
                                    elif 'name' in ch:
                                        # Try to look up ID for the name
                                        channel_name = ch['name']
                                        try:
                                            channel_id = await self._lookup_channel_id(channel_name, user_id)
                                            if channel_id:
                                                ch['id'] = channel_id
                                                validated_channels.append(ch)
                                                logger.info(f"Channel {i+1}: {channel_name} found ID {channel_id}")
                                            else:
                                                logger.warning(f"Channel {i+1}: {channel_name} - could not find ID")
                                                validated_channels.append(ch)  # Include anyway for backward compatibility
                                        except ValueError as ve:
                                            # Capture detailed error message
                                            logger.warning(f"Error looking up channel ID for {channel_name}: {str(ve)}")
                                            channel_errors.append(f"Channel '{channel_name}': {str(ve)}")
                                            # Don't add this channel as it's invalid
                                    else:
                                        # String format - look up ID if needed
                                        channel_name = str(ch).lower()
                                        try:
                                            channel_id = await self._lookup_channel_id(channel_name, user_id)
                                            if channel_id:
                                                validated_channels.append({"name": channel_name, "id": channel_id})
                                                logger.info(f"Channel {i+1}: {channel_name} found ID {channel_id}")
                                            else:
                                                validated_channels.append({"name": channel_name})
                                                logger.warning(f"Channel {i+1}: {channel_name} - could not find ID")
                                        except ValueError as ve:
                                            # Capture detailed error message
                                            logger.warning(f"Error looking up channel ID for {channel_name}: {str(ve)}")
                                            channel_errors.append(f"Channel '{channel_name}': {str(ve)}")
                                            # Don't add this channel as it's invalid
                            
                            # If we have channel errors, add them to validation errors
                            if channel_errors:
                                error_msg = "Slack channel validation errors:\n• " + "\n• ".join(channel_errors)
                                validation_errors.append(error_msg)
                                
                                # If no valid channels, skip this source
                                if not validated_channels:
                                    logger.warning("No valid Slack channels found, skipping Slack source")
                                    continue
                            
                            # Use the validated channels
                            logger.info(f"Using {len(validated_channels)} validated channels for Slack source")
                            
                            # Check if we have a user token for this user in the source
                            slack_source = source_instance
                            user_has_token = False
                            if user_id and hasattr(slack_source, 'user_tokens') and user_id in slack_source.user_tokens:
                                user_has_token = True
                                logger.info(f"User {user_id} has registered token for search operations")
                            else:
                                # If we need to register a token
                                logger.info(f"User {user_id} does not have a registered token for search operations")
                                
                                # Try to use the config token
                                if SLACK_USER_TOKEN and hasattr(slack_source, 'register_user_token'):
                                    try:
                                        # Strip any quotes that might have been included in the token
                                        clean_token = SLACK_USER_TOKEN.strip().strip('"\'')
                                        result = slack_source.register_user_token(user_id, clean_token)
                                        if result:
                                            logger.info(f"Registered token from config for user {user_id} for search operations")
                                            user_has_token = True
                                        else:
                                            logger.warning(f"Failed to register config token for user {user_id}")
                                    except Exception as e:
                                        logger.error(f"Error registering config token for user {user_id}: {e}")
                            
                            # Pass all necessary data including the current channel ID for context
                            source_data_with_query = {
                                **source_data, 
                                'channel': validated_channels,  # Replace with validated list
                                'query': prompt,
                                'user_id': user_id,  # Pass user ID to enable user token usage
                                'current_channel_id': response_channel_id  # Pass the current channel ID for context
                            }
                            
                            # If we have a registered token, make sure we pass it
                            if user_has_token and hasattr(slack_source, 'user_tokens') and user_id in slack_source.user_tokens:
                                source_data_with_query['user_token'] = slack_source.user_tokens.get(user_id)
                                logger.info(f"Including user token in API call for search operations")
                            
                            data = await source_instance.fetch_data(source_data_with_query)
                    
                    # For Jira, handle multiple project keys
                    elif source_name.lower() == 'jira':
                        # Get JQL query
                        jql_query = source_data.get('jql', '')
                        
                        # Make sure we have a project key for backwards compatibility
                        project_keys = source_data.get('project_key', [])
                        if not project_keys:
                            # Extract project from JQL
                            project_key = self._extract_project_from_jql(jql_query)
                            if project_key:
                                project_keys = [project_key]
                            else:
                                validation_errors.append("Could not extract a project key from the JQL query")
                                continue
                                
                        # Always ensure project_key is a list
                        if not isinstance(project_keys, list):
                            project_keys = [project_keys]
                            
                        # Create a source_data object with both JQL and project keys  
                        source_data_with_query = {
                            'project_key': project_keys,
                            'jql': jql_query,
                            'query': prompt  # Also pass prompt for relevance filtering
                        }
                        
                        logger.info(f"Fetching JIRA data with JQL: {jql_query} and project keys: {project_keys}")
                        data = await source_instance.fetch_data(source_data_with_query)
                    
                    # For DevRev, handle project keys and convert JQL to GraphQL
                    elif source_name.lower() == 'devrev':
                        # Get JQL query if provided
                        jql_query = source_data.get('jql_query', '')
                        
                        # Make sure we have project keys for backwards compatibility
                        project_keys = source_data.get('project_key', [])
                        if not project_keys and not jql_query:
                            validation_errors.append("No project key or JQL query provided for DevRev")
                            continue
                                
                        # Always ensure project_key is a list
                        if not isinstance(project_keys, list):
                            project_keys = [project_keys]
                            
                        # Create a source_data object with both JQL query and project keys
                        source_data_with_query = {
                            'project_key': project_keys,
                            'jql_query': jql_query,
                            'query': prompt  # Pass prompt for relevance filtering
                        }
                        
                        logger.info(f"Fetching DevRev data with JQL: {jql_query} and project keys: {project_keys}")
                        data = await source_instance.fetch_data(source_data_with_query)
                    
                    # For Google docs/sheets, include the prompt as a query parameter  
                    elif source_name.lower() in ['google_docs', 'google_sheets']:
                        source_data_with_query = {**source_data, 'query': prompt}
                        
                        # VALIDATION STEP 3: Validate document/sheet URLs
                        if source_name.lower() == 'google_docs' and not self._validate_google_url(source_data, 'document'):
                            error_msg = "Please enter a valid Google Docs URL (should contain '/document/d/')"
                            validation_errors.append(error_msg)
                            continue
                            
                        if source_name.lower() == 'google_sheets' and not self._validate_google_url(source_data, 'spreadsheet'):
                            error_msg = "Please enter a valid Google Sheets URL (should contain '/spreadsheets/d/')"
                            validation_errors.append(error_msg)
                            continue
                            
                        data = await source_instance.fetch_data(source_data_with_query)
                    
                    # For Tableau, validate the URL format
                    elif source_name.lower() == 'tableau':
                        # VALIDATION STEP 4: Validate Tableau URL
                        if not self._validate_tableau_url(source_data):
                            error_msg = "Please enter a valid Tableau dashboard URL"
                            validation_errors.append(error_msg)
                            continue
                            
                        source_data_with_query = {**source_data, 'query': prompt}
                        data = await source_instance.fetch_data(source_data_with_query)
                    else:
                        # For other sources, use standard parameters
                        data = await source_instance.fetch_data(source_data)
                    
                    # Store the raw data - let the context manager handle formatting
                    analysis_data[source_name] = data
                    
                    # Log success for this source
                    logger.info(f"Successfully fetched data from {source_name}")
                    
                except Exception as e:
                    error_msg = f"Error processing {source_name}: {str(e)}"
                    validation_errors.append(error_msg)
                    logger.error(error_msg, exc_info=True)  # Added exc_info for better debugging
            
            # VALIDATION STEP 5: Check if we have any valid data sources after validation
            if not analysis_data:
                error_message = "No valid data sources available for analysis. Please check the following errors:\n• " + "\n• ".join(validation_errors)
                return {
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"⚠️ *Validation Error*: {error_message}"
                            }
                        },
                        {
                            "type": "divider"
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Fix Errors", "emoji": True},
                                    "action_id": "retry_submission",
                                    "style": "primary"
                                },
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Start Over", "emoji": True},
                                    "action_id": "reload_page"
                                }
                            ]
                        }
                    ]
                }
                
            # If there were validation errors, report them
            if validation_errors:
                error_message = "The following errors occurred during data retrieval:\n• " + "\n• ".join(validation_errors)
                
                # If we have no valid data sources, return an error
                if not analysis_data:
                    return {
                        "response_type": "ephemeral",
                        "replace_original": False,
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"⚠️ *Validation Error*: {error_message}"
                                }
                            },
                            {
                                "type": "divider"
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Fix Errors", "emoji": True},
                                        "action_id": "retry_submission",
                                        "style": "primary"
                                    },
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Start Over", "emoji": True},
                                        "action_id": "reload_page" 
                                    }
                                ]
                            }
                        ]
                    }
                
                # Otherwise, notify about partial data retrieval
                await self.post_message(
                    response_channel_id,
                    text=f"⚠️ Warning: {error_message}\n\nProceeding with analysis using available data sources.",
                    thread_ts=new_thread_ts  # Post in the new thread
                )

            # Generate analysis
            analysis_result = await self.context_manager.generate_analysis(
                source_data=analysis_data, 
                query=prompt
            )

            # Format the analysis response nicely for Slack
            if isinstance(analysis_result, dict):
                response_text = analysis_result.get("response", "")
                confidence = analysis_result.get("confidence", 0)
                metadata = analysis_result.get("metadata", {})
                
                # Replace user IDs with real names
                response_text = await self._replace_user_ids_with_names(response_text)
                
                # Add title with source information
                source_count = metadata.get("source_count", len(analysis_data.keys()))
                source_names = ", ".join(analysis_data.keys())
                title = f"*Analysis Results from {source_count} source{'' if source_count == 1 else 's'}*"
                if source_count > 0:
                    title += f"\n_Sources: {source_names}_\n\n"
                
                # Fix Slack markdown formatting
                # Ensure header formatting is Slack-compatible (transform ### to *bold*)
                formatted_response = ""
                for line in response_text.split('\n'):
                    # Convert markdown headers to Slack bold
                    if line.startswith('###'):
                        line = f"{line.lstrip('# ')}"
                    elif line.startswith('##'):
                        line = f"{line.lstrip('# ')}"
                    elif line.startswith('#'):
                        line = f"{line.lstrip('# ')}"
                    
                    # Ensure lists are properly formatted for Slack
                    if (line.strip().startswith('- ') or 
                        line.strip().startswith('* ') or 
                        re.match(r'^\d+\.\s', line.strip())):  # Match numbered lists like "1. Item"
                        # Make sure there's proper spacing for list items
                        if formatted_response and not formatted_response.endswith('\n'):
                            formatted_response += '\n'
                    
                    # Enhance numbered list items with bullet points for better visibility in Slack
                    numbered_list_match = re.match(r'^(\d+)\.(\s.*)$', line.strip())
                    if numbered_list_match:
                        number = numbered_list_match.group(1)
                        content = numbered_list_match.group(2)
                        line = f"• {number}{content}"
                    
                    formatted_response += line + '\n'
                
                # Format confidence level
                confidence_text = ""
                if confidence > 0:
                    confidence_percent = int(confidence * 100)
                    confidence_text = f"\n*Confidence: {confidence_percent}%*"
                
                formatted_analysis = f"{title}{formatted_response}{confidence_text}"
            else:
                # Fallback if analysis is not a dict
                formatted_analysis = str(analysis_result)

            # Send response to the interaction channel, using the thread if available
            thread_option = {}
            if new_thread_ts and dm_threading_available:
                thread_option["thread_ts"] = new_thread_ts
            elif is_dm and thread_ts:  # Use the original thread_ts if this is a DM
                thread_option["thread_ts"] = thread_ts
                new_thread_ts = thread_ts  # Update new_thread_ts for later use
                logger.info(f"Using original thread_ts for DM channel response: {thread_ts}")
            
            # For DM channels using response_url, we need custom handling
            if is_dm and response_url:
                try:
                    logger.info(f"Posting follow-up analysis to DM channel using response_url with thread_ts: {thread_option.get('thread_ts', '')}")
                    
                    # Create payload for response_url
                    payload = {
                        "text": formatted_analysis,
                        "response_type": "in_channel"  # Make visible in channel
                    }
                    
                    # Always add thread_ts parameter if available to ensure threading in DM
                    if thread_option.get("thread_ts"):
                        payload["thread_ts"] = thread_option["thread_ts"]
                    
                    # Post using response_url
                    response = requests.post(
                        response_url,
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    )
                    
                    if response.status_code == 200:
                        logger.info("Successfully posted follow-up analysis to DM using response_url")
                        # Try to get timestamp from the response if possible 
                        try:
                            response_json = response.json() if response.text else {}
                            if isinstance(response_json, dict) and response_json.get("ts"):
                                # Got a timestamp from the response - use this for threading in future messages
                                if not new_thread_ts:  # If we didn't have a thread_ts before, use this one
                                    new_thread_ts = response_json.get("ts")
                                dm_threading_available = True
                                logger.info(f"Obtained thread timestamp from response_url response: {new_thread_ts}")
                        except Exception as e:
                            logger.warning(f"Error parsing response: {str(e)}")
                    else:
                        logger.error(f"Error posting to response_url: {response.status_code} {response.text}")
                except Exception as e:
                    logger.error(f"Error posting to response_url: {str(e)}")
                    
                    # Fall back to post_message method
                    await self.post_message(
                        response_channel_id,
                        text=formatted_analysis,
                        **(thread_option if thread_option else {})
                    )
            else:
                # Use standard post_message for non-DM channels or DM channels where we have thread_ts
                await self.post_message(
                    response_channel_id,
                    text=formatted_analysis,
                    **(thread_option if thread_option else {})
                )

            # Store thread context for future follow-up queries
            if new_thread_ts:
                # Initialize user's thread contexts if not exists
                if user_id not in self.thread_contexts:
                    self.thread_contexts[user_id] = {}
                    logger.info(f"Created new thread contexts entry for user {user_id}")
                
                # Determine the command source
                command_source = "genius"  # Default to genius for Razorpay Genius app
                app_identifier = "razorpay_genius"  # App identifier
                command_type = interaction_data.get("command", {}).get("command", "")
                
                # Extract command type if available in interaction data
                if command_type:
                    logger.info(f"Detected command type: {command_type}")
                    if command_type.startswith("/genius"):
                        command_source = "genius"
                    elif command_type.startswith("/test"):
                        command_source = "test"
                
                # Flag for direct message channels
                is_dm = response_channel_id.startswith('D')
                
                # Store the sources and other context for this thread
                self.thread_contexts[user_id][new_thread_ts] = {
                    "sources": user_state.get("sources", []).copy(),
                    "source_data": user_state.get("source_data", {}).copy(),
                    "channel_id": response_channel_id,
                    "last_interaction": time.time(),
                    "original_query": prompt,
                    # Mark this thread as coming from the /genius command specifically
                    "command_source": command_source,
                    "app_identifier": app_identifier,
                    # Add direct message flag
                    "is_dm": is_dm,
                    # Store response_url for future use in DM channels
                    "response_url": response_url if is_dm else None,
                    # Store the thread_ts for threaded replies
                    "thread_ts": new_thread_ts,
                    # Add conversation history tracking
                    "conversation_history": [
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": response_text}
                    ]
                }
                
                # Log details about stored context
                source_count = len(user_state.get("sources", []))
                source_names = [s.get('name', 'unknown') for s in user_state.get("sources", [])]
                logger.info(f"Stored thread context for user {user_id} in thread {new_thread_ts}")
                logger.info(f"Thread context details: {source_count} sources: {source_names}")
                logger.info(f"Thread command source: {command_source}, app identifier: {app_identifier}")
                if is_dm:
                    logger.info(f"Thread is in direct message channel: {response_channel_id}")
                logger.info(f"All thread_ts stored for user {user_id}: {list(self.thread_contexts[user_id].keys())}")
                
                # Save thread contexts to disk for persistence
                self._save_thread_contexts()

            # Clear user state
            del self.user_states[user_id]
            
            # Now that processing is complete, if this was from a modal view, close it
            if is_modal and view_id:
                try:
                    logger.info("Analysis complete, closing modal view")
                    self.client.views_update(
                        view_id=view_id,
                        view={
                            "type": "modal",
                            "title": {"type": "plain_text", "text": "Processing Complete"},
                            "close": {"type": "plain_text", "text": "Close"},
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": "✅ *Analysis complete!*\nYour results have been posted to the channel."
                                    }
                                }
                            ]
                        }
                    )
                except Exception as e:
                    logger.error(f"Error closing modal view: {str(e)}")
            
            return {"text": "Analysis complete"}

        except Exception as e:
            logger.error(f"Error handling prompt: {str(e)}")
            return {
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"❌ Error: {str(e)}"
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Retry", "emoji": True},
                                "action_id": "retry_submission"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Reset", "emoji": True},
                                "action_id": "reload_page"
                            }
                        ]
                    }
                ]
            }

    async def handle_interaction(self, interaction_data: Dict[str, Any]) -> Dict[str, Any]:
        """Handle interactive components"""
        try:
            # Extract common data
            user_id = interaction_data.get("user", {}).get("id")
            channel_id = interaction_data.get("channel", {}).get("id")
            
            # For block_actions and view_submission, get the action_id
            action_id = ""
            if interaction_data.get("type") == "block_actions":
                actions = interaction_data.get("actions", [])
                if actions:
                    action_id = actions[0].get("action_id", "")

            logger.info(f"Processing action: {action_id} from user: {user_id} in channel: {channel_id}")

            # Handle "Add Data Source" button click
            if action_id == "add_source":
                logger.info(f"Handling add_source action for user {user_id}")
                
                # Get the user state to ensure we're using the correct channel (especially for DMs)
                user_state = self.user_states.get(user_id, {})
                is_dm = user_state.get("is_dm", channel_id.startswith('D'))
                
                # Use the channel from user state if available, otherwise use the interaction channel
                response_channel_id = user_state.get("channel_id", channel_id)
                logger.info(f"Using channel for response: {response_channel_id}, is_dm: {is_dm}")
                
                # Extract response_url from the interaction if available
                response_url = interaction_data.get("response_url")
                logger.info(f"Response URL available: {bool(response_url)}")
                
                # Get available sources from registry
                sources = self.source_registry.get_sources()
                logger.info(f"Available sources: {sources}")
                
                source_options = [
                    {
                        "text": {"type": "plain_text", "text": source.capitalize()},
                        "value": source
                    } for source in sources
                ]

                # Generate unique source ID
                source_id = f"src_{int(time.time() * 1000)}"
                
                # Get trigger_id from interaction data
                trigger_id = interaction_data.get("trigger_id")
                logger.info(f"Using trigger_id: {trigger_id}")
                
                # Create modal view
                modal_view = {
                    "type": "modal",
                    "callback_id": "add_source_modal",
                    "private_metadata": json.dumps({
                        "source_id": source_id,
                        "channel_id": response_channel_id,
                        "is_dm": is_dm,
                        "response_url": response_url
                    }),
                    "title": {
                        "type": "plain_text",
                        "text": "Add Data Source"
                    },
                    "submit": {
                        "type": "plain_text",
                        "text": "Add"
                    },
                    "close": {
                        "type": "plain_text",
                        "text": "Cancel"
                    },
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "*Select a data source type:*"
                            }
                        },
                        {
                            "type": "input",
                            "block_id": f"source_type_block_{source_id}",
                            "element": {
                                "type": "static_select",
                                "action_id": f"source_type_{source_id}",
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "Select source type"
                                },
                                "options": source_options
                            },
                            "label": {
                                "type": "plain_text",
                                "text": "Source Type"
                            }
                        },
                        {
                            "type": "input",
                            "block_id": f"source_value_block_{source_id}",
                            "element": {
                                "type": "plain_text_input",
                                "action_id": f"source_value_{source_id}",
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "Slack: channel name | Jira: JQL query | Google Doc & Sheets: full URL"
                                }
                            },
                            "label": {
                                "type": "plain_text",
                                "text": "Source Details"
                            }
                        }
                    ]
                }
                
                # Update user state if not already initialized
                if user_id not in self.user_states:
                    self.user_states[user_id] = {
                        "sources": [],
                        "channel_id": response_channel_id,
                        "slack_channel_id": response_channel_id,
                        "source_data": {},
                        "is_dm": is_dm,
                        "response_url": response_url
                    }
                # If already initialized, make sure the channel IDs are consistent
                else:
                    self.user_states[user_id]["channel_id"] = response_channel_id
                    self.user_states[user_id]["slack_channel_id"] = response_channel_id
                    self.user_states[user_id]["is_dm"] = is_dm
                    if response_url:
                        self.user_states[user_id]["response_url"] = response_url
                
                # Open the modal with views_open API
                try:
                    logger.info(f"Opening modal with trigger_id: {trigger_id}")
                    result = self.client.views_open(
                        trigger_id=trigger_id,
                        view=modal_view
                    )
                    logger.info(f"Modal opened successfully: {result.get('ok', False)}")
                    return {"text": "Opening data source selector..."}
                except Exception as e:
                    logger.error(f"Error opening modal: {e}")
                    return {
                        "text": f"Error opening modal: {e}",
                        "replace_original": False
                    }

            # Add a new handler for the modal submission
            elif interaction_data.get("type") == "view_submission" and interaction_data.get("view", {}).get("callback_id") == "add_source_modal":
                view = interaction_data.get("view", {})
                private_metadata = json.loads(view.get("private_metadata", "{}"))
                source_id = private_metadata.get("source_id")
                channel_id = private_metadata.get("channel_id")
                is_dm = private_metadata.get("is_dm", channel_id.startswith('D'))
                response_url = private_metadata.get("response_url")
                
                # Get the values from the modal
                state_values = view.get("state", {}).get("values", {})
                
                # Extract source type and value
                source_type = ""
                source_value = ""
                
                for block_id, block_data in state_values.items():
                    if block_id.startswith("source_type_block_"):
                        for action_id, action_data in block_data.items():
                            if action_id.startswith("source_type_"):
                                source_type = action_data.get("selected_option", {}).get("value", "")
                    
                    if block_id.startswith("source_value_block_"):
                        for action_id, action_data in block_data.items():
                            if action_id.startswith("source_value_"):
                                source_value = action_data.get("value", "")
                
                logger.info(f"Modal submitted with source_type: {source_type}, source_value: {source_value}")
                
                # Initialize user state if needed
                if user_id not in self.user_states:
                    self.user_states[user_id] = {
                        "sources": [],
                        "channel_id": channel_id,
                        "source_data": {},
                        "is_dm": is_dm,
                        "response_url": response_url
                    }
                
                # Add the new source to user state
                if "sources" not in self.user_states[user_id]:
                    self.user_states[user_id]["sources"] = []
                    
                self.user_states[user_id]["sources"].append({
                    "id": source_id,
                    "type": source_type,
                    "value": source_value
                })
                
                # Now we need to update the original message with the new source
                # Get the original message
                try:
                    original_message = self.client.conversations_history(
                        channel=channel_id,
                        limit=10
                    )
                    
                    message_to_update = None
                    for msg in original_message.get("messages", []):
                        if msg.get("blocks"):
                            for block in msg.get("blocks", []):
                                if block.get("block_id") == "prompt_block":
                                    message_to_update = msg
                                    break
                            if message_to_update:
                                break
                    
                    if not message_to_update:
                        # If we couldn't find the original message, just acknowledge
                        return {"response_action": "update", "view": {
                            "type": "modal",
                            "title": {"type": "plain_text", "text": "Success"},
                            "close": {"type": "plain_text", "text": "Close"},
                            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Source added successfully!"}}]
                        }}
                    
                    # Get the blocks from the original message
                    blocks = message_to_update.get("blocks", [])
                    
                    # Create new source display block
                    new_source_display = [
                        {
                            "type": "section",
                            "block_id": f"source_header_{source_id}",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Data Source: {source_type.capitalize()}*"
                            },
                            "accessory": {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "×",
                                    "emoji": True
                                },
                                "value": source_id,
                                "style": "danger",
                                "action_id": f"remove_source_{source_id}"
                            }
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": self._get_source_display_text(source_type, source_value)
                            }
                        },
                        {
                            "type": "divider"
                        }
                    ]
                    
                    # Find where to insert the new blocks (before submit_block)
                    submit_index = -1
                    for i, block in enumerate(blocks):
                        if block.get("block_id") == "submit_block":
                            submit_index = i
                            break
                    
                    # Insert new blocks before submit button
                    if submit_index != -1:
                        for block in reversed(new_source_display):
                            blocks.insert(submit_index, block)
                    else:
                        # If no submit block found, add to end
                        blocks.extend(new_source_display)
                        # And ensure we have a submit button
                        blocks.append({
                            "type": "actions",
                            "block_id": "submit_block",
                            "elements": [{
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Submit Analysis"},
                                "action_id": "submit_prompt",
                                "style": "primary"
                            }]
                        })
                    
                    # Update the original message with new blocks
                    try:
                        update_result = self.client.chat_update(
                            channel=channel_id,
                            ts=message_to_update.get("ts"),
                            blocks=blocks
                        )
                        logger.info("Message updated successfully")
                    except Exception as e:
                        logger.error(f"Error updating message: {str(e)}")
                
                except SlackApiError as e:
                    error_msg = str(e).lower()
                    if "channel_not_found" in error_msg and is_dm and response_url:
                        logger.info(f"Channel not found for DM, using response_url instead")
                        # For DM channels, use response_url to update the message
                        try:
                            # Create base message blocks for a fresh update
                            blocks = [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": "*Configure Your Analysis*\nAdd data sources and enter your analysis prompt."
                                    }
                                },
                                {
                                    "type": "input",
                                    "block_id": "prompt_block",
                                    "element": {
                                        "type": "plain_text_input",
                                        "action_id": "prompt_input",
                                        "placeholder": {"type": "plain_text", "text": "Enter your analysis prompt..."}
                                    },
                                    "label": {"type": "plain_text", "text": "Analysis Prompt"}
                                }
                            ]
                            
                            # Add all sources from user state
                            sources = self.user_states[user_id].get("sources", [])
                            for source in sources:
                                source_id = source.get("id")
                                source_type = source.get("type")
                                source_value = source.get("value")
                                if source_id and source_type and source_value:
                                    blocks.extend([
                                        {
                                            "type": "section",
                                            "block_id": f"source_header_{source_id}",
                                            "text": {
                                                "type": "mrkdwn",
                                                "text": f"*Data Source: {source_type.capitalize()}*"
                                            },
                                            "accessory": {
                                                "type": "button",
                                                "text": {
                                                    "type": "plain_text",
                                                    "text": "×",
                                                    "emoji": True
                                                },
                                                "value": source_id,
                                                "style": "danger",
                                                "action_id": f"remove_source_{source_id}"
                                            }
                                        },
                                        {
                                            "type": "section",
                                            "text": {
                                                "type": "mrkdwn",
                                                "text": self._get_source_display_text(source_type, source_value)
                                            }
                                        },
                                        {
                                            "type": "divider"
                                        }
                                    ])
                            
                            # Add the action buttons
                            blocks.extend([
                                {
                                    "type": "actions",
                                    "block_id": "source_actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "➕ Add Data Source", "emoji": True},
                                            "action_id": "add_source",
                                            "style": "primary"
                                        }
                                    ]
                                },
                                {
                                    "type": "divider"
                                },
                                {
                                    "type": "actions",
                                    "block_id": "submit_block",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "Submit Analysis", "emoji": True},
                                            "action_id": "submit_prompt",
                                            "style": "primary"
                                        }
                                    ]
                                }
                            ])
                            
                            # Post using response_url
                            requests.post(
                                response_url,
                                json={
                                    "blocks": blocks,
                                    "text": "Analysis setup with sources",
                                    "replace_original": True
                                },
                                headers={"Content-Type": "application/json"}
                            )
                            logger.info("Successfully updated message using response_url")
                        except Exception as post_error:
                            logger.error(f"Error updating message using response_url: {str(post_error)}")
                    else:
                        logger.error(f"Error getting conversation history: {str(e)}")
                
                # Return response to close the modal
                return {"response_action": "clear"}

            # Ensure user state is initialized if it doesn't exist
            if action_id != "next_step" and user_id not in self.user_states:
                logger.warning(f"User state not found for {user_id}, initializing a new state")
                # Initialize with default values
                self.user_states[user_id] = {
                    "selected_sources": ["slack"],  # Default to slack since that's what we're handling
                    "current_source_index": 0,
                    "source_data": {},
                    "slack_channel_id": channel_id,
                    "slack_channels": []  # Direct channel storage - no temp data needed
                }
                logger.info(f"Created new user state for {user_id}: {json.dumps(self.user_states[user_id])}")

            # Handle source type selection in the modal
            elif action_id.startswith("source_type_") and interaction_data.get("view", {}).get("type") == "modal":
                source_id = action_id.replace("source_type_", "")
                selected_value = ""
                
                # Get the selected value
                for action in interaction_data.get("actions", []):
                    if action.get("action_id") == action_id:
                        selected_value = action.get("selected_option", {}).get("value", "")
                        break
                
                if not selected_value:
                    return {"text": "Please select a valid source type"}
                    
                # Update the source value input placeholder based on the selected source type
                view = interaction_data.get("view", {})
                blocks = view.get("blocks", [])
                
                for i, block in enumerate(blocks):
                    if block.get("block_id") == f"source_value_block_{source_id}":
                        if selected_value == "jira":
                            blocks[i]["element"]["placeholder"] = {
                                "type": "plain_text",
                                "text": "Enter a project key (e.g., DEMO, JIRA, ENG)"
                            }
                            # Update the label to be more specific
                            blocks[i]["label"] = {
                                "type": "plain_text",
                                "text": "Project Key"
                            }
                        elif selected_value == "slack":
                            blocks[i]["element"]["placeholder"] = {
                                "type": "plain_text",
                                "text": "Enter a single channel name without the # (e.g., general)"
                            }
                            # Update the label to be more specific
                            blocks[i]["label"] = {
                                "type": "plain_text",
                                "text": "Channel Name"
                            }
                        elif selected_value == "google_docs":
                            blocks[i]["element"]["placeholder"] = {
                                "type": "plain_text",
                                "text": "Enter the full Google Doc URL (e.g., https://docs.google.com/document/d/...)"
                            }
                            # Update the label to be more specific
                            blocks[i]["label"] = {
                                "type": "plain_text",
                                "text": "Google Doc URL"
                            }
                        elif selected_value == "google_sheets":
                            blocks[i]["element"]["placeholder"] = {
                                "type": "plain_text",
                                "text": "Enter the full Google Sheet URL (e.g., https://docs.google.com/spreadsheets/d/...)"
                            }
                            # Update the label to be more specific
                            blocks[i]["label"] = {
                                "type": "plain_text",
                                "text": "Google Sheet URL"
                            }
                        elif selected_value == "tableau":
                            blocks[i]["element"]["placeholder"] = {
                                "type": "plain_text",
                                "text": "Enter the full Tableau dashboard URL"
                            }
                            # Update the label to be more specific
                            blocks[i]["label"] = {
                                "type": "plain_text",
                                "text": "Dashboard URL"
                            }
                        
                        # If it's Jira, add the JQL query field
                        if selected_value == "jira" and len(blocks) <= 3:
                            blocks.insert(i+1, {
                                "type": "input",
                                "block_id": f"source_jql_block_{source_id}",
                                "element": {
                                    "type": "plain_text_input",
                                    "action_id": f"source_jql_{source_id}",
                                    "placeholder": {
                                        "type": "plain_text",
                                        "text": "Enter a JQL query to filter issues (e.g., type = Bug AND status = Open)"
                                    }
                                },
                                "label": {
                                    "type": "plain_text",
                                    "text": "JQL Query"
                                },
                                "optional": True
                            })
                        
                        # Update the view
                        return {
                            "response_type": "ephemeral",
                            "replace_original": False,
                            "view": {
                                "type": "modal",
                                "callback_id": "add_source_modal",
                                "private_metadata": view.get("private_metadata", "{}"),
                                "title": {
                                    "type": "plain_text",
                                    "text": "Add Data Source"
                                },
                                "submit": {
                                    "type": "plain_text",
                                    "text": "Add"
                                },
                                "close": {
                                    "type": "plain_text",
                                    "text": "Cancel"
                                },
                                "blocks": blocks
                            }
                        }
                
                # If we couldn't update the view, just acknowledge
                return {"text": "Source type selected"}

        # Handle other action_ids
        # ... (rest of the existing handle_interaction method)

            # Ensure user state is initialized if it doesn't exist
            if action_id != "next_step" and user_id not in self.user_states:
                logger.warning(f"User state not found for {user_id}, initializing a new state")
                # Initialize with default values
                self.user_states[user_id] = {
                    "selected_sources": ["slack"],  # Default to slack since that's what we're handling
                    "current_source_index": 0,
                    "source_data": {},
                    "slack_channel_id": channel_id,
                    "slack_channels": []  # Direct channel storage - no temp data needed
                }
                logger.info(f"Created new user state for {user_id}: {json.dumps(self.user_states[user_id])}")
            
            # Log the current user state for debugging
            if user_id in self.user_states:
                logger.info(f"Current user state: {json.dumps(self.user_states[user_id])}")
            else:
                logger.warning(f"No user state available for {user_id}")

            # NEW: Handle "Add Data Source" button click
            if action_id == "add_source":
                logger.info(f"Handling add_source action for user {user_id}")
                
                # Initialize sources array if it doesn't exist
                if "sources" not in self.user_states[user_id]:
                    self.user_states[user_id]["sources"] = []
                
                # Add a new source with a unique ID
                source_id = f"src_{int(time.time() * 1000)}"
                self.user_states[user_id]["sources"].append({
                    "id": source_id,
                    "type": "",
                    "value": ""
                })
                
                logger.info(f"Added new source with ID {source_id}. Current sources: {json.dumps(self.user_states[user_id]['sources'])}")
                
                # Get the message blocks to update
                message = interaction_data.get("message", {})
                blocks = message.get("blocks", [])
                
                # Modified source type selection with dynamic sources
                sources = self.source_registry.get_sources()
                source_options = [
                    {
                        "text": {"type": "plain_text", "text": source.capitalize()},
                        "value": source
                    } for source in sources
                ]

                # Updated source type block
                new_source_blocks = [
                    {
                        "type": "section",
                        "block_id": f"source_header_{source_id}",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Data Source*"
                        },
                        "accessory": {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": "×",
                                "emoji": True
                            },
                            "value": source_id,
                            "style": "danger",
                            "action_id": f"remove_source_{source_id}"
                        }
                    },
                    {
                        "type": "input",
                        "block_id": f"source_type_block_{source_id}",
                        "element": {
                            "type": "static_select",
                            "action_id": f"source_type_{source_id}",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Select source type"
                            },
                            "options": source_options
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Source Type"
                        }
                    }
                ]

                # Add initial value input
                new_source_blocks.append({
                    "type": "input",
                    "block_id": f"source_value_block_{source_id}",
                    "element": {
                        "type": "plain_text_input",
                        "action_id": f"source_value_{source_id}",
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Slack: channel name (e.g., general) | Jira: JQL query | Google: full URL"
                        }
                    },
                    "label": {
                        "type": "plain_text",
                        "text": "Source Details"
                    }
                })
                
                # Add a divider
                new_source_blocks.append({
                    "type": "divider"
                })
                
                # Find the submit block index to insert our new source block before it
                submit_block_index = -1
                for i, block in enumerate(blocks):
                    if block.get("block_id") == "submit_block":
                        submit_block_index = i
                        break
                
                # Insert the new source blocks before the submit button
                if submit_block_index >= 0:
                    for block in reversed(new_source_blocks):
                        blocks.insert(submit_block_index, block)
                else:
                    # If submit block not found, add to the end
                    blocks.extend(new_source_blocks)
                    # And add a submit button if needed
                    blocks.append({
                        "type": "actions",
                        "block_id": "submit_block",
                        "elements": [{
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Submit Analysis"},
                            "action_id": "submit_prompt",
                            "style": "primary"
                        }]
                    })
                
                # Send the updated UI
                return {
                    "response_type": "in_channel",
                    "replace_original": True,
                    "blocks": blocks
                }
            
            # Handle source type selection to update input placeholder and add conditional fields
            elif action_id.startswith("source_type_"):
                source_id = action_id.replace("source_type_", "")
                logger.info(f"Handling source type selection for source {source_id}")
                
                # Get the selected value
                selected_value = ""
                for action in interaction_data.get("actions", []):
                    if action.get("action_id") == action_id:
                        selected_value = action.get("selected_option", {}).get("value", "")
                        break
                
                if not selected_value:
                    logger.warning(f"No selection value found for {action_id}")
                    return {"text": "Please select a valid source type"}
                
                # Update the source type in user state
                if "sources" in self.user_states[user_id]:
                    for source in self.user_states[user_id]["sources"]:
                        if source["id"] == source_id:
                            source["type"] = selected_value
                            logger.info(f"Updated source {source_id} type to {selected_value}")
                
                # Get the message blocks to update the input placeholder
                message = interaction_data.get("message", {})
                blocks = message.get("blocks", [])
                
                # Find the source value input block
                for i, block in enumerate(blocks):
                    if block.get("block_id") == f"source_value_block_{source_id}":
                        input_element = block.get("element", {})
                        if input_element.get("action_id") == f"source_value_{source_id}":
                            # Update the placeholder based on source type
                            if selected_value == "jira":
                                blocks[i]["element"]["placeholder"] = {
                                    "type": "plain_text",
                                    "text": "Enter a project key (e.g., DEMO, JIRA, ENG)"
                                }
                                
                                # Add JQL input block
                                jql_block = {
                                    "type": "input",
                                    "block_id": f"source_jql_block_{source_id}",
                                    "element": {
                                        "type": "plain_text_input",
                                        "action_id": f"source_jql_{source_id}",
                                        "placeholder": {
                                            "type": "plain_text",
                                            "text": "Enter a JQL query to filter issues (e.g., type = Bug AND status = Open)"
                                        }
                                    },
                                    "label": {
                                        "type": "plain_text",
                                        "text": "JQL Query"
                                    },
                                    "optional": True
                                }
                                # Find and insert after value block
                                blocks.insert(i+1, jql_block)
                                
                            elif selected_value == "slack":
                                blocks[i]["element"]["placeholder"] = {
                                    "type": "plain_text",
                                    "text": "Enter a single channel name without the # (e.g., general)"
                                }
                            elif selected_value == "google_docs":
                                blocks[i]["element"]["placeholder"] = {
                                    "type": "plain_text",
                                    "text": "Enter the full Google Doc URL including document ID"
                                }
                            elif selected_value == "google_sheets":
                                blocks[i]["element"]["placeholder"] = {
                                    "type": "plain_text",
                                    "text": "Enter the full Google Sheet URL including spreadsheet ID"
                                }
                            elif selected_value == "tableau":
                                blocks[i]["element"]["placeholder"] = {
                                    "type": "plain_text",
                                    "text": "Enter the full Tableau dashboard URL with view name"
                                }
                            
                            logger.info(f"Updated placeholder for source {source_id} input based on type {selected_value}")
                
                # Send the updated UI
                return {
                    "response_type": "in_channel",
                    "replace_original": True,
                    "blocks": blocks
                }

            # NEW: Handle removing a source
            elif action_id.startswith("remove_source_"):
                source_id = action_id.replace("remove_source_", "")
                logger.info(f"Removing source with ID: {source_id}")
                return await self.handle_remove_field(user_id, interaction_data, source_id)
                
                # Remove the source from the user's state
                if "sources" in self.user_states[user_id]:
                    self.user_states[user_id]["sources"] = [
                        src for src in self.user_states[user_id]["sources"] if src["id"] != source_id
                    ]
                
                # Get the message blocks to update
                message = interaction_data.get("message", {})
                blocks = message.get("blocks", [])
                
                # Find and remove the blocks for this source
                updated_blocks = []
                i = 0
                while i < len(blocks):
                    block = blocks[i]
                    block_id = block.get("block_id", "")
                    
                    # Skip this source's blocks
                    if (block_id == f"source_header_{source_id}" or 
                        block_id == f"source_type_block_{source_id}" or
                        block_id == f"source_value_block_{source_id}"):
                        i += 1
                        # Skip the divider after this source too
                        if i < len(blocks) and blocks[i].get("type") == "divider":
                            i += 1
                        continue
                    
                    updated_blocks.append(block)
                    i += 1
                
                # Send the updated UI
                return {
                    "response_type": "in_channel",
                    "replace_original": True,
                    "blocks": updated_blocks
                }
                
            # Add handler for source value input 
            elif action_id.startswith("source_value_"):
                source_id = action_id.replace("source_value_", "")
                logger.info(f"Handling source value input for source {source_id}")
                
                # Get the input value
                input_value = ""
                state_values = interaction_data.get("state", {}).get("values", {})
                
                # Find the block containing this input
                for block_id, block_data in state_values.items():
                    if block_id == f"source_inputs_{source_id}" and action_id in block_data:
                        input_value = block_data[action_id].get("value", "")
                        break
                
                # Update the source value in user state
                if "sources" in self.user_states[user_id]:
                    for source in self.user_states[user_id]["sources"]:
                        if source["id"] == source_id:
                            source["value"] = input_value
                            logger.info(f"Updated source {source_id} value to {input_value}")
                
                # No need to update UI here, just acknowledge
                return {"text": "Source details updated"}

            if action_id == "next_step":
                # Get selected sources from state values
                selected_sources = []
                state_values = interaction_data.get("state", {}).get("values", {})
                source_selection = state_values.get("source_selection", {}).get("select_sources", {})
                if source_selection:
                    selected_options = source_selection.get("selected_options", [])
                    selected_sources = [opt["value"] for opt in selected_options]
                    logger.info(f"Selected sources: {selected_sources}")

                if not selected_sources:
                    logger.warning("No sources selected")
                    return {"text": "Please select at least one data source"}

                # Store selected sources in user state
                self.user_states[user_id] = {
                    "selected_sources": selected_sources,
                    "current_source_index": 0,
                    "source_data": {},
                    "slack_channel_id": channel_id  # Store the channel_id
                }
                logger.info(f"User state created: {json.dumps(self.user_states[user_id], indent=2)}")
            

                # Show form for the first source
                form_response = await self.show_source_form(user_id, channel_id)
                
                # Send the response using response_url
                if response_url:
                    try:
                        requests.post(
                            response_url,
                            json=form_response,
                            headers={"Content-Type": "application/json"}
                        )
                        return {"text": "Processing your request..."}
                    except Exception as e:
                        logger.error(f"Error sending response to response_url: {str(e)}")
                        return form_response
                return form_response
                        
                        # Modify reload_page case
            elif action_id == "reload_page":
                logger.info(f"User {user_id} requested full reset")
                # Clear user state
                if user_id in self.user_states:
                    del self.user_states[user_id]
                
                # Return to initial source selection
                return await self.handle_slash_command({
                    "channel_id": channel_id,
                    "user_id": user_id
                })

            # Handle "Add Another" button clicks for multi-value fields
            elif action_id.startswith("add_more_"):
                # Extract the field name that needs another input
                field_name = action_id.replace("add_more_", "")
                
                # Store channel_id in user state for future use
                if channel_id and user_id in self.user_states:
                    self.user_states[user_id]["slack_channel_id"] = channel_id
                    logger.info(f"Stored channel_id in user_state during add_more: {channel_id}")
                
                # Retrieve the current source being configured
                current_source = self.user_states[user_id]["selected_sources"][self.user_states[user_id]["current_source_index"]]
                source_instance = self.source_registry.get_source(current_source)
                form_config = source_instance.get_form_fields()
                
                # Find the field configuration
                form_fields = form_config.get('fields', []) if isinstance(form_config, dict) else form_config
                target_field = next((f for f in form_fields if f.get('name') == field_name), None)
                
                if not target_field:
                    return {"text": f"Error: Could not find field configuration for {field_name}"}
                
                # Get the current form value
                state_values = interaction_data.get("state", {}).get("values", {})
                
                # For each block in the form, extract the field values
                for block_id, block_data in state_values.items():
                    for current_action_id, action_data in block_data.items():
                        # Extract the field name from block_id
                        current_field_name = block_id.replace('_block', '')
                        
                        # Skip if this isn't the field we're interested in
                        if current_field_name != field_name:
                            continue
                            
                        # Special handling for channel field
                        if current_field_name == "channel" and current_source.lower() == 'slack':
                            value = action_data.get("value", "").strip()
                            if not value:
                                continue
                                
                            # Check if the input is already a channel ID
                            if value.upper().startswith('C'):
                                logger.info(f"Input '{value}' appears to be a channel ID, using directly")
                                channel_id_value = value.upper()
                                channel_value = {
                                    "name": f"channel_{channel_id_value}",
                                    "id": channel_id_value
                                }
                                
                                # Try to get the channel name
                                try:
                                    channel_info = self.client.conversations_info(channel=channel_id_value)
                                    if channel_info and channel_info.get('ok'):
                                        channel_name = channel_info.get('channel', {}).get('name', '')
                                        if channel_name:
                                            logger.info(f"Found channel name for ID {channel_id_value}: {channel_name}")
                                            channel_value["name"] = channel_name
                                            # Also update cache for future use
                                            self.channel_name_to_id_cache[channel_name.lower()] = channel_id_value
                                except Exception as e:
                                    logger.warning(f"Error getting channel name for ID {channel_id_value}: {e}")
                            else:
                                # Handle as channel name
                                channel_name = value.lower()
                                channel_value = {"name": channel_name}
                                
                                # Try lookup
                                found_channel_id = await self._lookup_channel_id(channel_name)
                                if found_channel_id:
                                    channel_value["id"] = found_channel_id
                                    logger.info(f"Using looked up channel ID during add_more: {found_channel_id} for {channel_name}")
                                
                            # Store directly in the user state - no temp data
                            if "slack_channels" not in self.user_states[user_id]:
                                self.user_states[user_id]["slack_channels"] = []
                            
                            # Add to the list - avoid duplicates by ID if possible
                            if "id" in channel_value:
                                # Check if we already have this channel by ID
                                existing_ids = [ch.get("id") for ch in self.user_states[user_id]["slack_channels"] 
                                               if isinstance(ch, dict) and "id" in ch]
                                
                                if channel_value["id"] not in existing_ids:
                                    self.user_states[user_id]["slack_channels"].append(channel_value)
                                    logger.info(f"Added channel {channel_value['name']} with ID {channel_value['id']} to user state")
                            else:
                                # No ID, just add by name (potential for duplicates, but we'll handle it)
                                self.user_states[user_id]["slack_channels"].append(channel_value)
                                logger.info(f"Added channel {channel_value['name']} without ID to user state")

                # Show the "add more" form
                blocks = [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"Configure {current_source}",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Adding another {target_field.get('label', field_name)}*"
                        }
                    },
                    {
                        "type": "divider"
                    }
                ]
                
                # Show current channels for Slack
                if field_name == "channel" and current_source.lower() == "slack" and "slack_channels" in self.user_states[user_id]:
                    channels = self.user_states[user_id]["slack_channels"]
                    
                    if channels:
                        # Format channels for display
                        display_items = []
                        for ch in channels:
                            if isinstance(ch, dict):
                                name = ch.get("name", "")
                                ch_id = ch.get("id", "")
                                if ch_id:
                                    display_items.append(f"• #{name} ({ch_id})")
                                else:
                                    display_items.append(f"• #{name}")
                            else:
                                display_items.append(f"• #{ch}")
                                
                        blocks.append({
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Current {target_field.get('label', field_name)}s:*\n" + 
                                       "\n".join(display_items) + 
                                       f"\n*Total: {len(channels)} channels*"
                            }
                        })
                
                # Add appropriate input field
                input_element = None
                if current_source.lower() == 'slack' and field_name == 'channel':
                    input_element = {
                        "type": "plain_text_input",
                        "action_id": field_name,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Enter channel name (e.g., 'general' or 'general, random')",
                            "emoji": True
                        }
                    }
                    # Add improved help text for channel selection
                    blocks.append({
                        "type": "context",
                        "block_id": "hsuGB",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": ":warning: *IMPORTANT*: Type the EXACT channel name (without the #) - case sensitive. Example: 'general', not 'General' or '#general'.\nYou can enter multiple channels separated by commas, for example: 'general, random, engineering'",
                                "verbatim": False
                            }
                        ]
                    })
                    
                    # Add a list of available channels as examples
                    try:
                        # Get a small sample of channels from the API
                        sample_result = self.client.conversations_list(
                            types="public_channel",
                            exclude_archived=True,
                            limit=5
                        )
                        
                        # Extract channel names
                        sample_channels = []
                        for ch in sample_result.get("channels", []):
                            sample_channels.append(ch.get("name", ""))
                        
                        if sample_channels:
                            blocks.append({
                                "type": "context",
                                "elements": [
                                    {
                                        "type": "mrkdwn",
                                        "text": f"Available channels include: *{', '.join(sample_channels)}*"
                                    }
                                ]
                            })
                    except Exception as e:
                        logger.warning(f"Error fetching sample channels: {e}")
                else:
                    input_element = {
                        "type": "plain_text_input",
                        "action_id": f"{field_name}_additional",
                        "placeholder": {
                            "type": "plain_text",
                            "text": target_field.get('placeholder', f"Enter another {field_name}")
                        }
                    }
                
                blocks.extend([
                    {
                        "type": "input",
                        "block_id": f"{field_name}_additional_block",
                        "element": input_element,
                        "label": {
                            "type": "plain_text",
                            "text": f"Additional {target_field.get('label', field_name)}"
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Add & Continue", "emoji": True},
                                "style": "primary",
                                "action_id": f"add_additional_{field_name}"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Done Adding", "emoji": True},
                                "action_id": "return_to_form"
                            }
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "If you're experiencing issues or session errors:"
                            }
                        ]
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "🔄 Reload Page", "emoji": True},
                                "style": "danger",
                                "action_id": "reload_page"
                            }
                        ]
                    }
                ])
                
                return {
                    "response_type": "in_channel",
                    "replace_original": True,
                    "blocks": blocks,
                    "text": f"Adding another {target_field.get('label', field_name)}"
                }
            
                        # Add to handle_interaction's action_id checks
            elif action_id == "retry_submission":
                # Preserve state and reshow previous form
                return await self.show_source_form(user_id, channel_id)

            # Handle "Add & Continue" actions for multi-value fields
            elif action_id.startswith("add_additional_"):
                field_name = action_id.replace("add_additional_", "")
                current_source = self.user_states[user_id]["selected_sources"][self.user_states[user_id]["current_source_index"]]
                
                # Store channel_id in user state for future use
                if channel_id and user_id in self.user_states:
                    self.user_states[user_id]["slack_channel_id"] = channel_id
                    logger.info(f"Stored channel_id in user_state during add_additional: {channel_id}")
                
                # Get the new value
                state_values = interaction_data.get("state", {}).get("values", {})
                additional_block = f"{field_name}_additional_block"
                additional_action = f"{field_name}_additional"
                
                if additional_block in state_values and additional_action in state_values[additional_block]:
                    action_data = state_values[additional_block][additional_action]
                    
                    # Get value directly from plain text input
                    new_value = action_data.get("value", "")
                    
                    # For Slack channel, handle plain text inputs specially
                    if field_name == "channel" and current_source.lower() == 'slack':
                        value = new_value.strip()
                        if value:
                            # Check if the input is already a channel ID (starts with C)
                            if value.upper().startswith('C'):
                                logger.info(f"Input '{value}' appears to be a channel ID, using directly")
                                channel_id_value = value.upper()  # Ensure proper format
                                channel_value = {
                                    "name": f"channel_{channel_id_value}",  # Temporary name
                                    "id": channel_id_value
                                }
                                
                                
                                # Try to get the channel name for better display
                                try:
                                    channel_info = self.client.conversations_info(channel=channel_id_value)
                                    if channel_info and channel_info.get('ok'):
                                        channel_name = channel_info.get('channel', {}).get('name', '')
                                        if channel_name:
                                            logger.info(f"Found channel name for ID {channel_id_value}: {channel_name}")
                                            channel_value["name"] = channel_name
                                            # Also update cache for future use
                                            self.channel_name_to_id_cache[channel_name.lower()] = channel_id_value
                                except Exception as e:
                                    logger.warning(f"Error getting channel name for ID {channel_id_value}: {e}")
                            else:
                                # Handle as channel name
                                channel_name = value.lower()
                                channel_value = {"name": channel_name}
                                
                                # Try to look up the channel ID using our helper method
                                found_channel_id = await self._lookup_channel_id(channel_name)
                                if found_channel_id:
                                    channel_value["id"] = found_channel_id
                                    logger.info(f"Using looked up channel ID: {found_channel_id} for {channel_name}")
                            
                            # Store directly in the user state - no temp data
                            if "slack_channels" not in self.user_states[user_id]:
                                self.user_states[user_id]["slack_channels"] = []
                            
                            # Add to the list - avoid duplicates by ID if possible
                            if "id" in channel_value:
                                # Check if we already have this channel by ID
                                existing_ids = [ch.get("id") for ch in self.user_states[user_id]["slack_channels"] 
                                               if isinstance(ch, dict) and "id" in ch]
                                
                                if channel_value["id"] not in existing_ids:
                                    self.user_states[user_id]["slack_channels"].append(channel_value)
                                    logger.info(f"Added additional channel {channel_value['name']} with ID {channel_value['id']} to user state")
                            else:
                                # No ID, just add by name (potential for duplicates, but we'll handle it)
                                self.user_states[user_id]["slack_channels"].append(channel_value)
                                logger.info(f"Added additional channel {channel_value['name']} without ID to user state")
                
                # Show the same "add more" form again with updated values
                source_instance = self.source_registry.get_source(current_source)
                form_config = source_instance.get_form_fields()
                form_fields = form_config.get('fields', []) if isinstance(form_config, dict) else form_config
                target_field = next((f for f in form_fields if f.get('name') == field_name), None)
                
                blocks = [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": f"Configure {current_source}",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Adding another {target_field.get('label', field_name)}*"
                        }
                    },
                    {
                        "type": "divider"
                    }
                ]
                
                # Show current channels for Slack
                if field_name == "channel" and current_source.lower() == "slack" and "slack_channels" in self.user_states[user_id]:
                    channels = self.user_states[user_id]["slack_channels"]
                    
                    if channels:
                        # Format channels for display
                        display_items = []
                        for ch in channels:
                            if isinstance(ch, dict):
                                name = ch.get("name", "")
                                ch_id = ch.get("id", "")
                                if ch_id:
                                    display_items.append(f"• #{name} ({ch_id})")
                                else:
                                    display_items.append(f"• #{name}")
                            else:
                                display_items.append(f"• #{ch}")
                                
                        blocks.append({
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Current {target_field.get('label', field_name)}s:*\n" + 
                                       "\n".join(display_items) + 
                                       f"\n*Total: {len(channels)} channels*"
                            }
                        })
                
                # Add appropriate input field
                input_element = None
                if current_source.lower() == 'slack' and field_name == 'channel':
                    input_element = {
                        "type": "plain_text_input",
                        "action_id": field_name,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Enter channel name (e.g., 'general' or 'general, random')",
                            "emoji": True
                        }
                    }
                    # Add improved help text for channel selection
                    blocks.append({
                        "type": "context",
                        "block_id": "hsuGB",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": ":warning: *IMPORTANT*: Type the EXACT channel name (without the #) - case sensitive. Example: 'general', not 'General' or '#general'.\nYou can enter multiple channels separated by commas, for example: 'general, random, engineering'",
                                "verbatim": False
                            }
                        ]
                    })
                    
                    # Add a list of available channels as examples
                    try:
                        # Get a small sample of channels from the API
                        sample_result = self.client.conversations_list(
                            types="public_channel",
                            exclude_archived=True,
                            limit=5
                        )
                        
                        # Extract channel names
                        sample_channels = []
                        for ch in sample_result.get("channels", []):
                            sample_channels.append(ch.get("name", ""))
                        
                        if sample_channels:
                            blocks.append({
                                "type": "context",
                                "elements": [
                                    {
                                        "type": "mrkdwn",
                                        "text": f"Available channels include: *{', '.join(sample_channels)}*"
                                    }
                                ]
                            })
                    except Exception as e:
                        logger.warning(f"Error fetching sample channels: {e}")
                else:
                    input_element = {
                        "type": "plain_text_input",
                        "action_id": f"{field_name}_additional",
                        "placeholder": {
                            "type": "plain_text",
                            "text": target_field.get('placeholder', f"Enter another {field_name}")
                        }
                    }
                
                blocks.extend([
                    {
                        "type": "input",
                        "block_id": f"{field_name}_additional_block",
                        "element": input_element,
                        "label": {
                            "type": "plain_text",
                            "text": f"Additional {target_field.get('label', field_name)}"
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Add & Continue", "emoji": True},
                                "style": "primary",
                                "action_id": f"add_additional_{field_name}"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Done Adding", "emoji": True},
                                "action_id": "return_to_form"
                            }
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "If you're experiencing issues or session errors:"
                            }
                        ]
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "🔄 Reload Page", "emoji": True},
                                "style": "danger",
                                "action_id": "reload_page"
                            }
                        ]
                    }
                ])
                
                return {
                    "response_type": "in_channel",
                    "replace_original": True,
                    "blocks": blocks,
                    "text": f"Adding another {target_field.get('label', field_name)}"
                }

            # Handle returning to the main form after adding multiple values
            elif action_id == "return_to_form":
                # Store channel_id in user state for future use
                if channel_id and user_id in self.user_states:
                    self.user_states[user_id]["slack_channel_id"] = channel_id
                    logger.info(f"Stored channel_id in user_state during return_to_form: {channel_id}")
                
                # Just show the form again - no need to process temp data
                form_response = await self.show_source_form(user_id, channel_id)
                return form_response
            

            elif action_id == "submit_source_form":
                # Process form submission
                try:
                    # Use the correct method signature that only needs interaction_data
                    form_response = await self.handle_source_form_submission(interaction_data)
                    if response_url:
                        try:
                            requests.post(
                                response_url,
                                json=form_response,
                                headers={"Content-Type": "application/json"}
                            )
                            return {"text": "Processing your request..."}
                        except Exception as e:
                            logger.error(f"Error sending response to response_url: {str(e)}")
                            return form_response
                    return form_response
                except TypeError as e:
                    # If there's a TypeError, it might be a method signature mismatch
                    logger.error(f"Method signature error: {str(e)}")
                    # Extract user_id and create form_data from interaction_data
                    user_id = interaction_data.get("user", {}).get("id")
                    return {"text": f"Error processing form: {str(e)}"}

            elif action_id == "submit_prompt":
                # Process final prompt submission
                try:
                    # Get prompt text from the prompt input
                    prompt_text = ""
                    state_values = interaction_data.get("state", {}).get("values", {})
                    
                    if "prompt_block" in state_values:
                        if "prompt_input" in state_values["prompt_block"]:
                            prompt_text = state_values["prompt_block"]["prompt_input"].get("value", "")
                        elif "prompt" in state_values["prompt_block"]:
                            prompt_text = state_values["prompt_block"]["prompt"].get("value", "")
                    
                    if not prompt_text:
                        # Display error in the UI
                        return {
                            "response_type": "ephemeral",
                            "replace_original": False,
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": "⚠️ *Validation Error*: Please enter a prompt before submitting"
                                    }
                                },
                                {
                                    "type": "divider"
                                },
                                {
                                    "type": "actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "Retry", "emoji": True},
                                            "action_id": "retry_submission",
                                            "style": "primary"
                                        }
                                    ]
                                }
                            ]
                        }
                    
                    # Process each source
                    user_data = self.user_states.get(user_id, {})
                    logger.info(f"User state at submission: {json.dumps(user_data)}")
                    
                    # Make sure we have user state
                    if not user_data:
                        logger.warning(f"No user state found for user {user_id}")
                        return {
                            "response_type": "ephemeral",
                            "replace_original": False,
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": "⚠️ *Session Error*: Your session has expired. Please start a new session with /test command."
                                    }
                                },
                                {
                                    "type": "actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "Start New Session", "emoji": True},
                                            "action_id": "reload_page",
                                            "style": "primary"
                                        }
                                    ]
                                }
                            ]
                        }
                    
                    # Get channel_id and check if it's a DM channel
                    channel_id = interaction_data.get("channel", {}).get("id")
                    is_dm = channel_id.startswith('D')
                    
                    # For DM channels, extract thread_ts if available
                    thread_ts = None
                    if is_dm:
                        # Try to extract thread_ts from various places in the payload
                        if interaction_data.get("container", {}).get("message_ts"):
                            thread_ts = interaction_data["container"]["message_ts"]
                        elif interaction_data.get("message", {}).get("ts"):
                            thread_ts = interaction_data["message"]["ts"]
                        
                        logger.info(f"Extracted thread_ts for DM channel: {thread_ts}")
                    
                    # Initialize source_data in user state if it doesn't exist
                    if "source_data" not in user_data:
                        user_data["source_data"] = {}
                    
                    # VALIDATION: Check if we have any sources added
                    if "sources" not in user_data or not user_data["sources"]:
                        logger.warning(f"No sources added for user {user_id}")
                        # Display error in the UI
                        return {
                            "response_type": "ephemeral",
                            "replace_original": False,
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": "⚠️ *Validation Error*: Please add at least one data source before submitting your prompt."
                                    }
                                },
                                {
                                    "type": "divider"
                                },
                                {
                                    "type": "actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "Add Data Source", "emoji": True},
                                            "action_id": "add_source",
                                            "style": "primary"
                                        }
                                    ]
                                }
                            ]
                        }
                    
                    # Process sources if they exist in the user state
                    validation_errors = []
                    if "sources" in user_data and user_data["sources"]:
                        # Process each source
                        for source in user_data["sources"]:
                            source_type = source.get("type", "")
                            source_value = source.get("value", "")
                            
                            # VALIDATION: Check if source has type and value
                            if not source_type:
                                continue
                            if not source_value:
                                logger.warning(f"Missing value for source type {source_type}")
                                validation_errors.append(f"Missing value for {source_type} data source")
                                continue
                                
                            logger.info(f"Processing source: {source_type} - {source_value}")
                            
                            # Make sure the source type exists in source_data
                            if source_type not in user_data["source_data"]:
                                user_data["source_data"][source_type] = {}
                            
                            # Process Slack sources
                            if source_type.lower() == "slack":
                                # Check if the source_value is a Slack thread URL
                                if self._is_slack_thread_url(source_value.strip()):
                                    logger.info(f"Detected Slack thread URL: {source_value}")
                                    # Handle as thread URL - store it for thread summarization
                                    if "thread_urls" not in user_data["source_data"][source_type]:
                                        user_data["source_data"][source_type]["thread_urls"] = []
                                    user_data["source_data"][source_type]["thread_urls"].append(source_value.strip())
                                    logger.info(f"Added Slack thread URL: {source_value}")
                                else:
                                    # Handle multiple comma-separated channels (existing logic)
                                    channel_names = [name.strip() for name in source_value.split(',') if name.strip()]
                                    
                                    # VALIDATION: Check if channel names are provided
                                    if not channel_names:
                                        logger.warning("No channel names provided for Slack source")
                                        validation_errors.append("Please enter at least one valid Slack channel name")
                                        continue
                                    
                                    channels = []
                                    channel_error = None
                                    
                                    for channel_name in channel_names:
                                        try:
                                            channel_id = await self._lookup_channel_id(channel_name, user_id)
                                            if channel_id:
                                                channels.append({"name": channel_name, "id": channel_id})
                                            else:
                                                channels.append({"name": channel_name})
                                        except Exception as e:
                                            logger.warning(f"Error looking up channel ID for {channel_name}: {e}")
                                            channel_error = f"Invalid channel: '{channel_name}'. {str(e)}"
                                            validation_errors.append(channel_error)
                                            break
                                    
                                    if channel_error:
                                        continue
                                    
                                    # Store the channels in source_data
                                    if "channel" not in user_data["source_data"][source_type]:
                                        user_data["source_data"][source_type]["channel"] = channels
                                    else:
                                        # Add to existing channels
                                        existing_channels = user_data["source_data"][source_type]["channel"]
                                        if not isinstance(existing_channels, list):
                                            existing_channels = [existing_channels]
                                        user_data["source_data"][source_type]["channel"] = existing_channels + channels

                            # Process Jira sources
                            elif source_type.lower() == "jira":
                                # VALIDATION: Check JQL syntax at a basic level
                                if "project" not in source_value.lower() and "=" not in source_value:
                                    logger.warning(f"Potentially invalid JQL query: {source_value}")
                                    validation_errors.append("Your JQL query might be invalid. Make sure it follows JQL syntax (e.g., 'project = KEY').")
                                    # We continue anyway but note the warning
                                
                                # Store the JQL query in source_data
                                if "jql" not in user_data["source_data"][source_type]:
                                    user_data["source_data"][source_type]["jql"] = source_value
                                    
                                # Make sure project_key exists (required by the Jira source)
                                if "project_key" not in user_data["source_data"][source_type]:
                                    # Extract project key from JQL if possible, otherwise use a default
                                    project_key = self._extract_project_from_jql(source_value)
                                    user_data["source_data"][source_type]["project_key"] = [project_key]
                                
                                # Add to the project keys list
                                if isinstance(user_data["source_data"][source_type]["project_key"], list):
                                    if source_value not in user_data["source_data"][source_type]["project_key"]:
                                        user_data["source_data"][source_type]["project_key"].append(source_value)
                                else:
                                    user_data["source_data"][source_type]["project_key"] = [source_value]
                                
                                # Store JQL if provided
                                jql = source.get("jql", "")
                                if jql:
                                    if "jql" not in user_data["source_data"][source_type]:
                                        user_data["source_data"][source_type]["jql"] = {}
                                    user_data["source_data"][source_type]["jql"][source_value] = jql
                                    
                            # Process Google Docs sources
                            elif source_type.lower() == "google_docs":
                                # VALIDATION: Check Google Doc URL format
                                if not ('docs.google.com' in source_value and '/document/d/' in source_value):
                                    logger.warning(f"Invalid Google Docs URL: {source_value}")
                                    validation_errors.append("Please enter a valid Google Docs URL. It should contain 'docs.google.com/document/d/'")
                                    continue
                                
                                # Store the document URL in source_data
                                urls = source_value.split(',')
                                clean_urls = [url.strip() for url in urls if url.strip()]
                                
                                if "document_urls" not in user_data["source_data"][source_type]:
                                    user_data["source_data"][source_type]["document_urls"] = clean_urls
                                else:
                                    # Add to existing URLs
                                    existing_urls = user_data["source_data"][source_type]["document_urls"]
                                    if not isinstance(existing_urls, list):
                                        existing_urls = [existing_urls]
                                    user_data["source_data"][source_type]["document_urls"] = existing_urls + clean_urls
                                    
                                logger.info(f"Added Google Docs URLs: {clean_urls}")
                                
                            # Process Google Sheets sources
                            elif source_type.lower() == "google_sheets":
                                # VALIDATION: Check Google Sheets URL format
                                if not ('docs.google.com' in source_value and '/spreadsheets/d/' in source_value):
                                    logger.warning(f"Invalid Google Sheets URL: {source_value}")
                                    validation_errors.append("Please enter a valid Google Sheets URL. It should contain 'docs.google.com/spreadsheets/d/'")
                                    continue
                                
                                # Store the spreadsheet URL in source_data
                                urls = source_value.split(',')
                                clean_urls = [url.strip() for url in urls if url.strip()]
                                
                                if "spreadsheet_urls" not in user_data["source_data"][source_type]:
                                    user_data["source_data"][source_type]["spreadsheet_urls"] = clean_urls
                                else:
                                    # Add to existing URLs
                                    existing_urls = user_data["source_data"][source_type]["spreadsheet_urls"]
                                    if not isinstance(existing_urls, list):
                                        existing_urls = [existing_urls]
                                    user_data["source_data"][source_type]["spreadsheet_urls"] = existing_urls + clean_urls
                                    
                                logger.info(f"Added Google Sheets URLs: {clean_urls}")
                                
                            # Process Tableau sources
                            elif source_type.lower() == "tableau":
                                # VALIDATION: Check Tableau URL format
                                if not ('tableau' in source_value.lower() and '/views/' in source_value.lower()):
                                    logger.warning(f"Invalid Tableau URL: {source_value}")
                                    validation_errors.append("Please enter a valid Tableau dashboard URL. It should contain 'tableau' and '/views/'")
                                    continue
                                
                                # Store the dashboard URL in source_data
                                urls = source_value.split(',')
                                clean_urls = [url.strip() for url in urls if url.strip()]
                                
                                if "view_url" not in user_data["source_data"][source_type]:
                                    user_data["source_data"][source_type]["view_url"] = clean_urls[0] if clean_urls else ""
                                else:
                                    # Update existing URL - for Tableau we'll just use one URL at a time
                                    user_data["source_data"][source_type]["view_url"] = clean_urls[0] if clean_urls else user_data["source_data"][source_type]["view_url"]
                                    
                                logger.info(f"Added Tableau Dashboard URL: {clean_urls[0] if clean_urls else 'No URL provided'}")
                            
                            # Process DevRev sources
                            elif source_type.lower() == "devrev":
                                # Check if the value looks like a JQL query
                                is_jql_query = any(op in source_value for op in ["=", "AND", "OR", "<", ">"])
                                
                                if is_jql_query:
                                    logger.info(f"DevRev value appears to be a JQL query: {source_value[:50]}...")
                                    # Store as jql_query parameter - this is the key parameter missing before
                                    user_data["source_data"][source_type]["jql_query"] = source_value
                                    
                                    # Try to extract project key from JQL if possible
                                    project_match = re.search(r'project\s*=\s*(\w+)', source_value, re.IGNORECASE)
                                    if project_match:
                                        project_key = project_match.group(1)
                                        # Also store as project_key for compatibility
                                        user_data["source_data"][source_type]["project_key"] = [project_key]
                                        logger.info(f"Extracted project {project_key} from DevRev JQL query")
                                else:
                                    # Treat as project key/ID
                                    project_keys = [k.strip() for k in source_value.split(',') if k.strip()]
                                    user_data["source_data"][source_type]["project_key"] = project_keys
                                    logger.info(f"Added DevRev project key(s): {project_keys}")
                    
                    # Log the final source data structure for debugging
                    logger.info(f"Final source_data after processing: {json.dumps(user_data['source_data'])}")
                    
                    # If there are validation errors, display them
                    if validation_errors:
                        error_text = "⚠️ *Validation Errors:*\n"
                        for i, error in enumerate(validation_errors):
                            error_text += f"• {error}\n"
                            
                        return {
                            "response_type": "ephemeral",
                            "replace_original": False,
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn",
                                        "text": error_text
                                    }
                                },
                                {
                                    "type": "divider"
                                },
                                {
                                    "type": "actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "Fix Errors", "emoji": True},
                                            "action_id": "retry_submission",
                                            "style": "primary"
                                        },
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "Start Over", "emoji": True},
                                            "action_id": "reload_page"
                                        }
                                    ]
                                }
                            ]
                        }
                    
                    # VALIDATION: Final check that we have at least one data source with data
                    if not user_data["source_data"]:
                        logger.warning("No data sources available for analysis")
                        return {
                            "response_type": "ephemeral",
                            "replace_original": False,
                            "blocks": [
                                {
                                    "type": "section",
                                    "text": {
                                        "type": "mrkdwn", 
                                        "text": "⚠️ *Validation Error*: Please add at least one valid data source before submitting your prompt."
                                    }
                                },
                                {
                                    "type": "divider"
                                },
                                {
                                    "type": "actions",
                                    "elements": [
                                        {
                                            "type": "button",
                                            "text": {"type": "plain_text", "text": "Add Data Source", "emoji": True},
                                            "action_id": "add_source",
                                            "style": "primary"
                                        }
                                    ]
                                }
                            ]
                        }
                    
                    # Show processing message
                    # For DM channels, we need to use response_url instead of chat.postEphemeral
                    is_dm = channel_id.startswith('D')
                    response_url = interaction_data.get("response_url")
                    
                    if is_dm and response_url:
                        # Use response_url for DM channels
                        try:
                            logger.info(f"Using response_url for processing message in DM channel")
                            payload = {
                                "text": "🔍 Processing your prompt with the available data sources...",
                                "replace_original": False,
                                "response_type": "ephemeral"
                            }
                            
                            response = requests.post(
                                response_url,
                                json=payload,
                                headers={"Content-Type": "application/json"}
                            )
                            
                            if response.status_code == 200:
                                logger.info("Successfully sent processing message using response_url")
                            else:
                                logger.error(f"Error posting to response_url: {response.status_code} {response.text}")
                        except Exception as e:
                            logger.error(f"Error using response_url for processing message: {str(e)}")
                    else:
                        # Use chat.postEphemeral for non-DM channels
                        try:
                            self.client.chat_postEphemeral(
                                channel=channel_id,
                                user=user_id,
                                text="🔍 Processing your prompt with the available data sources..."
                            )
                        except Exception as e:
                            logger.error(f"Error sending ephemeral message: {str(e)}")
                    
                    # Update interaction_data with the collected data for handle_prompt_submission
                    interaction_data["prompt_text"] = prompt_text
                    
                    # Call the handle_prompt_submission method with the processed data
                    result = await self.handle_prompt_submission(interaction_data)
                    
                    # Check if this is a view_submission (modal) and close it
                    container_type = interaction_data.get("container", {}).get("type", "")
                    if container_type == "view":
                        logger.info("Closing modal after successful submission")
                        return {"response_action": "clear"}
                    else:
                        # For message-based interactions, return the result
                        return result
                    
                except Exception as e:
                    logger.error(f"Error processing prompt submission: {e}", exc_info=True)
                    # Improved error display
                    error_response = {
                        "response_type": "ephemeral",
                        "replace_original": False,
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f"❌ *Error*: {str(e)}"
                                }
                            },
                            {
                                "type": "divider"
                            },
                            {
                                "type": "actions",
                                "elements": [
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Retry", "emoji": True},
                                        "action_id": "retry_submission",
                                        "style": "primary"
                                    },
                                    {
                                        "type": "button",
                                        "text": {"type": "plain_text", "text": "Reset", "emoji": True}, 
                                        "action_id": "reload_page"
                                    }
                                ]
                            }
                        ]
                    }
                    
                    # For DM channels, try using response_url if available
                    is_dm = channel_id.startswith('D')
                    response_url = interaction_data.get("response_url")
                    if is_dm and response_url:
                        try:
                            logger.info(f"Using response_url to post error message in DM channel")
                            response = requests.post(
                                response_url,
                                json=error_response,
                                headers={"Content-Type": "application/json"}
                            )
                            
                            if response.status_code == 200:
                                logger.info("Successfully sent error message using response_url")
                                return {"text": "Error handled via response_url"}
                            else:
                                logger.error(f"Error posting to response_url: {response.status_code} {response.text}")
                        except Exception as e2:
                            logger.error(f"Error using response_url for error message: {str(e2)}")
                    
                    return error_response
        
        except Exception as e:
            logger.error(f"Error processing Slack interaction: {str(e)}", exc_info=True)
            return {"text": f"⚠️ Error processing Slack interaction: {str(e)}"}
        
    def _is_direct_question(self, query: str) -> bool:
        """Determine if the query is a direct question needing a specific answer"""
        # Normalize query
        query = query.lower().strip()
        
        # Check for direct question patterns
        direct_patterns = [
            # Who/what/when/where/which patterns
            r'^who\s+(?:is|are|was|were)\s+',
            r'^what\s+(?:is|are|was|were)\s+',
            r'^when\s+(?:is|are|was|were|did|will|should)\s+',
            r'^where\s+(?:is|are|was|were|did|will|should)\s+',
            r'^which\s+\w+\s+(?:is|are|was|were|did|will|should)\s+',
            # How many/much patterns
            r'^how\s+many\s+',
            r'^how\s+much\s+',
            # Did/has/have patterns (yes/no questions)
            r'^did\s+',
            r'^has\s+',
            r'^have\s+',
            r'^is\s+',
            r'^are\s+',
            r'^can\s+',
            r'^could\s+',
            r'^will\s+',
            r'^would\s+',
            r'^should\s+',
            # Name/list/identify patterns
            r'^name\s+(?:the|all|some|)\s+',
            r'^list\s+(?:the|all|some|)\s+',
            r'^identify\s+(?:the|all|some|)\s+',
            r'^find\s+(?:the|all|some|)\s+',
        ]
        
        for pattern in direct_patterns:
            if re.search(pattern, query):
                return True
                
        # Check for question marks with key question words
        if '?' in query and any(word in query for word in ['who', 'what', 'when', 'where', 'which', 'how', 'why']):
            return True
            
        return False 

    def _format_channel_for_display(self, channel_value):
        """Format a channel object or string for display in UI"""
        if isinstance(channel_value, dict):
            # Format channel object
            name = channel_value.get("name", "")
            channel_id = channel_value.get("id", "")
            if channel_id:
                if name.startswith("channel_"):
                    # This is a raw ID, just show that
                    return f"#{channel_id}"
                return f"#{name} ({channel_id})"
            else:
                return f"#{name}"
        else:
            # Handle string value
            return f"#{channel_value}" if channel_value else ""
    
    def _format_channels_for_display(self, channel_values):
        """Format a list of channel objects or strings for display in UI"""
        if not channel_values:
            return "No channels configured yet"
            
        if not isinstance(channel_values, list):
            # Single channel case
            return self._format_channel_for_display(channel_values)
            
        # Multiple channels case - handle each one
        display_items = []
        
        for val in channel_values:
            formatted = self._format_channel_for_display(val)
            if formatted:
                display_items.append(formatted)
                
        # Add a count for clarity
        if len(display_items) > 0:
            display_text = "\n".join([f"• {item}" for item in display_items])
            count_text = f"*Total: {len(display_items)} channels*"
            return f"{display_text}\n{count_text}"
        else:
            return "No channels configured yet"
            
    def _debug_channels(self, temp_channels, form_channels):
        """Debug helper to log channel merging details"""
        logger.info("---- CHANNEL DEBUG INFO ----")
        logger.info(f"Temp channels ({len(temp_channels) if isinstance(temp_channels, list) else 1}): {json.dumps(temp_channels, indent=2)}")
        logger.info(f"Form channels ({len(form_channels) if isinstance(form_channels, list) else 1}): {json.dumps(form_channels, indent=2)}")
        
        # Convert both to lists if they aren't already
        temp_list = temp_channels if isinstance(temp_channels, list) else [temp_channels]
        form_list = form_channels if isinstance(form_channels, list) else [form_channels]
        
        # Extract IDs and names for easier debugging
        temp_ids = [ch.get('id') for ch in temp_list if isinstance(ch, dict) and 'id' in ch]
        temp_names = [ch.get('name') for ch in temp_list if isinstance(ch, dict) and 'name' in ch]
        form_ids = [ch.get('id') for ch in form_list if isinstance(ch, dict) and 'id' in ch]
        form_names = [ch.get('name') for ch in form_list if isinstance(ch, dict) and 'name' in ch]
        
        logger.info(f"Temp channel IDs: {temp_ids}")
        logger.info(f"Temp channel names: {temp_names}")
        logger.info(f"Form channel IDs: {form_ids}")
        logger.info(f"Form channel names: {form_names}")
        logger.info("---- END DEBUG INFO ----")
        
    def _merge_channel_lists(self, temp_channels, form_channels):
        """Specialized method to properly merge channel lists"""
        # Debug the input data
        self._debug_channels(temp_channels, form_channels)
        
        # Convert both to lists if they aren't already
        temp_list = temp_channels if isinstance(temp_channels, list) else [temp_channels]
        form_list = form_channels if isinstance(form_channels, list) else [form_channels]
        
        # Create a dictionary to track unique channels by ID
        channels_by_id = {}
        
        # First add all form channels
        for ch in form_list:
            if isinstance(ch, dict) and 'id' in ch:
                channels_by_id[ch['id']] = ch
            else:
                # For channels without IDs, add them directly to the result list
                channels_by_id[f"noID_{json.dumps(ch)}"] = ch
        
        # Then add all temp channels, only if they don't overlap by ID
        for ch in temp_list:
            if isinstance(ch, dict) and 'id' in ch:
                if ch['id'] not in channels_by_id:
                    channels_by_id[ch['id']] = ch
            else:
                # For channels without IDs, we have no reliable way to deduplicate
                # so just add them
                channels_by_id[f"noID_{json.dumps(ch)}_{len(channels_by_id)}"] = ch
        
        # Convert back to a list
        result = list(channels_by_id.values())
        
        # Log results
        logger.info(f"After merging: Combined {len(form_list)} form channels with {len(temp_list)} temp channels = {len(result)} total channels")
        logger.info(f"Final channel list: {json.dumps(result, indent=2)}")
        
        return result

    async def handle_project_key_submission(self, user_id: str, project_key: str):
        """Handle submission of a project key for JIRA"""
        current_state = self.user_states.get(user_id, {})
        
        # Get existing project keys or initialize an empty list
        if 'source_data' not in current_state:
            current_state['source_data'] = {}
        if 'jira' not in current_state['source_data']:
            current_state['source_data']['jira'] = {}
        
        # Initialize or get the project_key list
        if 'project_key' not in current_state['source_data']['jira']:
            current_state['source_data']['jira']['project_key'] = []
        elif isinstance(current_state['source_data']['jira']['project_key'], str):
            # Convert to list if it's a string
            current_state['source_data']['jira']['project_key'] = [current_state['source_data']['jira']['project_key']]
        
        # Add the new project key to the list
        project_key_list = current_state['source_data']['jira']['project_key']
        if project_key not in project_key_list:
            project_key_list.append(project_key)
            
        # Update the state
        current_state['source_data']['jira']['project_key'] = project_key_list
        self.user_states[user_id] = current_state
        
        # Log the updated state
        logging.info(f"Updated project key list for user {user_id}: {project_key_list}")
        
        return current_state

    async def process_source_form_data(self, user_id: str, form_data: Dict[str, Any]):
        """Handle the submission of a data source configuration form
        
        Args:
            user_id: The Slack user ID
            form_data: Form data submitted by the user
        """
        if user_id not in self.user_states:
            logging.warning(f"User state not found for {user_id}")
            return {"error": "User session expired. Please try again."}
            
        current_state = self.user_states[user_id]
        current_source_index = current_state.get('current_source_index', 0)
        selected_sources = current_state.get('selected_sources', [])
        
        if current_source_index >= len(selected_sources):
            return {"error": "Invalid source index"}
            
        current_source = selected_sources[current_source_index]
        
        # Process the form based on source type
        logging.info(f"Processing submission for source: {current_source}")
        logging.info(f"Processed form data: {json.dumps(form_data, indent=2)}")
        
        # Initialize source_data if not present
        if 'source_data' not in current_state:
            current_state['source_data'] = {}
            
        # Handle JIRA project_key specially to ensure it's a list
        if current_source == 'jira' and 'project_key' in form_data:
            await self.handle_project_key_submission(user_id, form_data['project_key'])
            
            # Remove project_key from form_data since we processed it separately
            form_data_copy = form_data.copy()
            if 'project_key' in form_data_copy:
                del form_data_copy['project_key']
                
            # Add other form fields (like days, etc.)
            if current_source not in current_state['source_data']:
                current_state['source_data'][current_source] = {}
                
            current_state['source_data'][current_source].update(form_data_copy)
        else:
            # For other sources, just add the form data as is
            current_state['source_data'][current_source] = form_data
            
        logging.info(f"Updated user state with new data for {current_source}")
        
        # Save channel ID in user state (for later reference)
        if 'slack_channel_id' in current_state:
            logging.info(f"Stored channel_id in user_state: {current_state['slack_channel_id']}")
        
        # Move to the next source
        current_state['current_source_index'] = current_source_index + 1
        logging.info(f"Moving to next source (index: {current_state['current_source_index']})")
        
        # Save the updated state
        self.user_states[user_id] = current_state
        
        return current_state

    async def handle_add_additional_project_key(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle adding an additional project key for JIRA"""
        # Extract the additional project key from the payload
        try:
            state_values = payload.get('state', {}).get('values', {})
            block_id = 'project_key_additional_block'
            action_id = 'project_key_additional'
            if block_id in state_values and action_id in state_values[block_id]:
                additional_project_key = state_values[block_id][action_id].get('value', '').strip()
                
                if not additional_project_key:
                    return self.create_error_message("Please enter a valid project key")
                    
                # Check if JIRA data source is initialized in the flow controller
                jira_source = self.source_registry.get_source('jira')
                if not jira_source:
                    return self.create_error_message("JIRA data source not available")
                    
                # Validate the project key
                is_valid = await self._validate_jira_project_key(additional_project_key)
                if not is_valid:
                    return self.create_error_message(f"Invalid JIRA project key: {additional_project_key}")
                
                # Add to the user's project key list
                await self.handle_project_key_submission(user_id, additional_project_key)
                
                # Get current state to check the channel ID
                current_state = self.user_states.get(user_id, {})
                channel_id = current_state.get('slack_channel_id')
                logging.info(f"Stored channel_id in user_state during add_more: {channel_id}")
                
                # Get the updated list of project keys
                project_keys = current_state.get('source_data', {}).get('jira', {}).get('project_key', [])
                if isinstance(project_keys, str):
                    project_keys = [project_keys]
                    
                # Create success message with the list of all project keys
                project_keys_list = ", ".join(project_keys)
                success_message = {
                    "response_type": "in_channel",
                    "replace_original": True,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": "Configure jira",
                                "emoji": True
                            }
                        },
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f"*Project Key Added Successfully*\nCurrent project keys: {project_keys_list}"
                            }
                        },
                        {
                            "type": "divider"
                        },
                        {
                            "type": "input",
                            "block_id": "project_key_additional_block",
                            "element": {
                                "type": "plain_text_input",
                                "action_id": "project_key_additional",
                                "placeholder": {
                                    "type": "plain_text",
                                    "text": "Enter JIRA project key or ID"
                                }
                            },
                            "label": {
                                "type": "plain_text",
                                "text": "Additional Project Key/ID"
                            }
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Add & Continue",
                                        "emoji": True
                                    },
                                    "style": "primary",
                                    "action_id": "add_additional_project_key"
                                },
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "Done Adding",
                                        "emoji": True
                                    },
                                    "action_id": "return_to_form"
                                }
                            ]
                        },
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": "If you're experiencing issues or session errors:"
                                }
                            ]
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {
                                        "type": "plain_text",
                                        "text": "🔄 Reload Page",
                                        "emoji": True
                                    },
                                    "style": "danger",
                                    "action_id": "reload_page"
                                }
                            ]
                        }
                    ],
                    "text": "Adding another Project Key/ID"
                }
                
                return success_message
                
            else:
                return self.create_error_message("Could not find the project key field in the form")
                
        except Exception as e:
            logging.error(f"Error adding additional project key: {str(e)}")
            return self.create_error_message(f"Error adding project key: {str(e)}")

    async def _validate_jira_project_key(self, project_key: str) -> bool:
        """Validate if a JIRA project key exists.
        
        Args:
            project_key: The JIRA project key to validate
            
        Returns:
            Boolean indicating if the project key is valid
        """
        try:
            # Get JIRA data source
            jira_source = self.source_registry.get_source('jira')
            if not jira_source:
                logging.error("JIRA data source not available")
                return False
            
            # New: Check project existence
            project = await jira_source.client.project(project_key)
            return bool(project)
        except Exception as e:
            logger.error(f"JIRA validation error: {str(e)}")
            return False
                
            # Use the JIRA data source's validation method
            if hasattr(jira_source, '_validate_project_key'):
                return jira_source._validate_project_key(project_key)
                
            # Fallback validation if the method doesn't exist
            # This is less efficient but will work with older versions
            try:
                # Try a simple JQL query to see if the project exists
                response = jira_source.client.search_issues(f"project = {project_key}", maxResults=1)
                # If we get here, the project exists
                return True
            except Exception as e:
                logging.error(f"Project key validation failed: {str(e)}")
                return False
                
        except Exception as e:
            logging.error(f"Error validating JIRA project key: {str(e)}")
            return False

    async def _validate_devrev_project_key(self, project_key: str) -> bool:
        """Validate if a DevRev project key/ID exists.
        
        Args:
            project_key: The DevRev project key/ID to validate
            
        Returns:
            Boolean indicating if the project key is valid
        """
        try:
            # Get DevRev data source
            devrev_source = self.source_registry.get_source('devrev')
            if not devrev_source:
                logger.error("DevRev data source not available")
                return False
            
            # Check if the source has available projects method
            if hasattr(devrev_source, '_get_available_projects'):
                available_projects = devrev_source._get_available_projects()
                available_ids = [p.get("id") for p in available_projects if p.get("id")]
                available_names = [p.get("name") for p in available_projects if p.get("name")]
                
                # Check if project key matches any ID or name
                if project_key in available_ids or project_key in available_names:
                    return True
                
                # Try partial matching
                for proj in available_projects:
                    if (project_key.lower() in proj.get("id", "").lower() or 
                        project_key.lower() in proj.get("name", "").lower()):
                        return True
                
                logger.warning(f"DevRev project '{project_key}' not found in available projects")
                return False
            
            # If _get_available_projects doesn't exist, try to use validate_inputs
            test_input = {'project_key': project_key}
            if hasattr(devrev_source, 'validate_inputs'):
                is_valid = await devrev_source.validate_inputs(test_input)
                return is_valid
            
            # No validation method available
            logger.warning("No validation method available for DevRev projects")
            return True  # Default to accepting the input
                
        except Exception as e:
            logger.error(f"Error validating DevRev project key: {str(e)}")
            return False

    async def handle_action(self, action_id: str, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle a button or menu action from Slack
        
        Args:
            action_id: The ID of the action that was triggered
            user_id: The Slack user ID of the user who triggered the action
            payload: The full payload from Slack containing action details
        """
        logging.info(f"Processing action: {action_id} from user: {user_id}")
        
        # Get or initialize user state
        if user_id not in self.user_states:
            logging.warning(f"User state not found for {user_id}, initializing a new state")
            self.user_states[user_id] = self._create_new_user_state(user_id)
        
        logging.info(f"Current user state: {json.dumps(self.user_states[user_id])}")
        
        # Handle different actions
        if action_id == "select_sources":
            return await self.handle_select_sources(user_id, payload)
        elif action_id == "next_step":
            return await self.handle_next_step(user_id, payload)
        elif action_id == "submit_source_form":
            return await self.handle_submit_source_form(user_id, payload)
        elif action_id == "submit_prompt":
            return await self.handle_submit_prompt(user_id, payload)
        elif action_id == "add_more_slack_channel":
            return await self.handle_add_more_slack_channel(user_id, payload)
        elif action_id == "add_more_project_key":
            return await self.handle_add_more_project_key(user_id, payload)
        elif action_id == "add_additional_project_key":
            return await self.handle_add_additional_project_key(user_id, payload)
        elif action_id == "return_to_form":
            return await self.handle_return_to_form(user_id, payload)
        elif action_id == "add_selected_channel":
            return await self.handle_add_selected_channel(user_id, payload)
        elif action_id == "reload_page":
            return await self.handle_reload_page(user_id, payload)
        elif action_id.startswith("add_field_"):
            return await self.handle_add_field(user_id, payload)
        elif action_id.startswith("remove_source_"):  # Match the actual action ID prefix
            return await self.handle_remove_field(user_id, payload)

        else:
            logging.warning(f"Unknown action_id: {action_id}")
            return {"text": "Unknown action. Please try again."}
        
    # Add to SlackHandler class

    async def handle_remove_field(self, user_id: str, interaction_data: Dict[str, Any], source_id: str = None) -> Dict[str, Any]:
        """Handle removal of a data source from the UI"""
        try:
            # Log the full interaction data for debugging
            logger.info(f"Handle remove field called for user {user_id}")
            logger.info(f"Current user state: {json.dumps(self.user_states.get(user_id, {}))}")
            
            # Extract source ID from action_id or use provided source_id
            if not source_id:
                actions = interaction_data.get("actions", [{}])
                if not actions:
                    logger.error("No actions found in interaction data")
                    return {"text": "Error: No action found in interaction data"}
                
                action = actions[0]
                action_id = action.get("action_id", "")
                
                # Fix action ID - handle both formats for robustness
                if "remove_source_" in action_id:
                    source_id = action_id.replace("remove_source_", "")
                else:
                    source_id = action_id.replace("remove_field_", "")
            
            logger.info(f"Removing source with ID: {source_id}")
            
            # Get source info before removing
            source_type = ""
            source_value = ""
            removed_source = None
            if user_id in self.user_states and "sources" in self.user_states[user_id]:
                for src in self.user_states[user_id]["sources"]:
                    if src.get("id") == source_id:
                        source_type = src.get("type", "")
                        source_value = src.get("value", "")
                        removed_source = src
                        break
            
            logger.info(f"Found source to remove: {removed_source}")
            
            # Update user state
            if user_id in self.user_states:
                # Compare before and after to verify removal happened
                sources_before = len(self.user_states[user_id].get("sources", []))
                
                # Remove the source from sources list
                self.user_states[user_id]["sources"] = [
                    src for src in self.user_states[user_id].get("sources", [])
                    if src.get("id") != source_id
                ]
                
                sources_after = len(self.user_states[user_id].get("sources", []))
                
                # Verify source was actually removed
                if sources_before == sources_after and sources_before > 0:
                    logger.warning(f"Source {source_id} was not removed. Before: {sources_before}, After: {sources_after}")
                
                # Also remove any associated data
                if source_id in self.user_states[user_id].get("source_data", {}):
                    del self.user_states[user_id]["source_data"][source_id]
                
                logger.info(f"Updated user state after removal: {json.dumps(self.user_states[user_id])}")

            # Get the channel ID from the interaction_data
            channel_id = interaction_data.get("channel", {}).get("id", "")
            if not channel_id and user_id in self.user_states:
                channel_id = self.user_states[user_id].get("channel_id", "")
                
            # Get container info to determine if this is a modal or message
            container = interaction_data.get("container", {})
            container_type = container.get("type", "")
            logger.info(f"Container type: {container_type}")
            
            # Use the rebuild_source_blocks method to regenerate all blocks
            updated_response = await self._rebuild_source_blocks(user_id, channel_id, removed_source)
            
            if container_type == "view":
                # This is a modal view interaction
                view_id = container.get("view_id")
                if view_id:
                    try:
                        logger.info(f"Updating view {view_id} with {len(updated_response.get('blocks', []))} blocks")
                        self.client.views_update(
                            view_id=view_id,
                            view={
                                "type": "modal",
                                "callback_id": "configure_analysis",
                                "title": {"type": "plain_text", "text": "Data Sources"},
                                "blocks": updated_response.get("blocks", [])
                            }
                        )
                        return {"response_action": "update"}
                    except Exception as e:
                        logger.error(f"Error updating view: {str(e)}")
            else:
                # This is a message interaction
                message = interaction_data.get("message", {})
                ts = message.get("ts")
                
                if channel_id and ts:
                    try:
                        logger.info(f"Updating message in channel {channel_id} at ts {ts}")
                        # Log the specific blocks we're sending for troubleshooting
                        blocks = updated_response.get("blocks", [])
                        logger.info(f"Sending {len(blocks)} blocks to Slack")
                        
                        # Send the update
                        update_result = self.client.chat_update(
                            channel=channel_id,
                            ts=ts,
                            text="Data source configuration updated",
                            blocks=blocks
                        )
                        logger.info(f"Message update result: {update_result}")
                        return {"text": ""}  # Empty response since we've already updated
                    except Exception as e:
                        logger.error(f"Error updating message: {str(e)}")
                        # Try using response_url as fallback
                        response_url = interaction_data.get("response_url")
                        if response_url:
                            try:
                                requests.post(
                                    response_url,
                                    json=updated_response,
                                    headers={"Content-Type": "application/json"}
                                )
                                logger.info("Used response_url to update message")
                                return {"text": ""}
                            except Exception as url_e:
                                logger.error(f"Error using response_url: {str(url_e)}")
            
            # Return the blocks if all else fails
            return updated_response
                
        except Exception as e:
            logger.error(f"Error handling remove action: {str(e)}", exc_info=True)
            return {"text": f"Error removing source: {str(e)}"}

    async def _rebuild_source_blocks(self, user_id: str, channel_id: str, removed_source: Dict[str, Any] = None) -> Dict[str, Any]:
        """Rebuild the main interface blocks after source modification"""
        user_state = self.user_states.get(user_id, {})
        blocks = [
            {
                "type": "section",
                "block_id": "v5PCv",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Configure Your Analysis*\nAdd data sources and enter your analysis prompt.",
                    "verbatim": False
                }
            },
            {
                "type": "input",
                "block_id": "prompt_block",
                "label": {
                    "type": "plain_text",
                    "text": "Analysis Prompt",
                    "emoji": True
                },
                "element": {
                    "type": "plain_text_input",
                    "action_id": "prompt_input",
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Enter your analysis prompt...",
                        "emoji": True
                    }
                }
            },
            {
                "type": "divider",
                "block_id": "source_divider"
            },
            {
                "type": "section", 
                "block_id": "sources_header",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Your Data Sources*"
                }
            }
        ]

        # Add existing sources
        sources = user_state.get("sources", [])
        logger.info(f"Building UI with {len(sources)} sources")
        
        # Make sure removed source is really gone
        if removed_source:
            sources = [src for src in sources if src.get("id") != removed_source.get("id")]
            logger.info(f"Filtered sources after removal: {json.dumps(sources)}")
        
        # Update the user state with the filtered sources
        if user_id in self.user_states:
            self.user_states[user_id]["sources"] = sources
        
        # If no sources, add a message with non-empty text
        if not sources:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_No data sources added yet._"
                }
            })
            # Add a warning message about required sources
            blocks.append({
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "⚠️ *At least one data source is required before submitting.*"
                    }
                ]
            })
        else:
            # Add each source with remove button
            for source in sources:
                source_id = source.get("id", "")
                source_type = source.get("type", "")
                source_value = source.get("value", "")
                
                logger.info(f"Adding source to UI: {source_id} - {source_type} - {source_value}")
                
                # Use consistent display format based on source type
                display_text = f"*{source_type.title()} Source:* {source_value}"
                if source_type.lower() == "slack":
                    display_text = f"*Slack Channel:* {source_value}"
                elif source_type.lower() == "jira":
                    display_text = f"*JIRA Project:* {source_value}"
                
                # Add the source with a remove button in the same row
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": display_text 
                    },
                    "accessory": {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "×",
                            "emoji": True
                        },
                        "style": "danger",
                        "action_id": f"remove_source_{source_id}"  # Consistent format
                    }
                })
                
                # Add a divider after each source
                blocks.append({
                    "type": "divider"
                })

        # Add the "Add Data Source" button
        blocks.append({
            "type": "actions",
            "block_id": "source_actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "add_source",
                    "text": {
                        "type": "plain_text",
                        "text": "➕ Add Data Source",
                        "emoji": True
                    },
                    "style": "primary"
                }
            ]
        })

        # Add divider before submit button
        blocks.append({
            "type": "divider"
        })

        # Add submit button - disable it if no sources
        submit_button = {
            "type": "button",
            "action_id": "submit_prompt",
            "text": {
                "type": "plain_text",
                "text": "Submit Analysis",
                "emoji": True
            },
            "style": "primary"
        }
        
        # If no sources added, make the button less prominent
        if not sources:
            # Can't disable buttons in Slack, but we can remove the primary style
            # to make it less prominent and add a different text
            submit_button.pop("style", None)
            submit_button["text"]["text"] = "Submit Analysis (Add Sources First)"
        
        blocks.append({
            "type": "actions",
            "block_id": "submit_block",
            "elements": [submit_button]
        })

        return {
            "response_type": "in_channel",
            "replace_original": True,
            "text": "Configure your data sources",
            "blocks": blocks
        }
    async def handle_add_more_project_key(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the 'Add Another' button for JIRA project keys
        
        Args:
            user_id: The Slack user ID of the user
            payload: The payload from Slack containing action details
            
        Returns:
            Response to send back to Slack
        """
        try:
            # Get current channel ID
            current_state = self.user_states.get(user_id, {})
            channel_id = current_state.get('slack_channel_id')
            logging.info(f"Stored channel_id in user_state during add_more: {channel_id}")
            
            # Return the "add more" form
            return {
                "response_type": "in_channel",
                "replace_original": True,
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "Configure jira",
                            "emoji": True
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "*Adding another Project Key/ID*"
                        }
                    },
                    {
                        "type": "divider"
                    },
                    {
                        "type": "input",
                        "block_id": "project_key_additional_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "project_key_additional",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Enter JIRA project key or ID"
                            }
                        },
                        "label": {
                            "type": "plain_text",
                            "text": "Additional Project Key/ID"
                        }
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Add & Continue",
                                    "emoji": True
                                },
                                "style": "primary",
                                "action_id": "add_additional_project_key"
                            },
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "Done Adding",
                                    "emoji": True
                                },
                                "action_id": "return_to_form"
                            }
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "If you're experiencing issues or session errors:"
                            }
                        ]
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "🔄 Reload Page",
                                    "emoji": True
                                },
                                "style": "danger",
                                "action_id": "reload_page"
                            }
                        ]
                    }
                ],
                "text": "Adding another Project Key/ID"
            }
        except Exception as e:
            logging.error(f"Error handling add_more_project_key: {str(e)}")
            return self.create_error_message(f"Error: {str(e)}")

    async def handle_return_to_form(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle the 'Done Adding' button to return to the main form
        
        Args:
            user_id: The Slack user ID of the user
            payload: The payload from Slack containing action details
            
        Returns:
            Response to send back to Slack
        """
        try:
            # Get current state
            current_state = self.user_states.get(user_id, {})
            if not current_state:
                return self.create_error_message("Session expired. Please try again.")
                
            # Get selected sources and current index
            selected_sources = current_state.get('selected_sources', [])
            current_source_index = current_state.get('current_source_index', 0)
            
            # Make sure we're not out of bounds
            if current_source_index >= len(selected_sources):
                current_source_index = 0
                
            # Get current source
            if not selected_sources:
                return self.create_error_message("No sources selected. Please try again.")
            
            current_source = selected_sources[current_source_index]
            
            # Show the form for the current source
            channel_id = current_state.get('slack_channel_id')
            logging.info(f"Showing form for source {current_source} in channel {channel_id}")
            
            # Generate and return the appropriate form for the current source
            return await self.get_source_form(current_source, user_id)
            
        except Exception as e:
            logging.error(f"Error handling return_to_form: {str(e)}")
            return self.create_error_message(f"Error: {str(e)}")

    async def _lookup_user_name(self, user_id: str) -> str:
        """Look up a user's real name from their Slack ID"""
        if not user_id or not isinstance(user_id, str):
            return user_id
            
        # Skip if not a valid user ID format
        if not user_id.startswith('U'):
            return user_id
            
        # Check cache first
        if user_id in self.user_id_to_name_cache:
            logger.info(f"Found user name in cache for {user_id}: {self.user_id_to_name_cache[user_id]}")
            return self.user_id_to_name_cache[user_id]
            
        try:
            # Use users.info to get user details
            user_info = self.client.users_info(user=user_id)
            
            if user_info and user_info.get('ok'):
                user = user_info.get('user', {})
                
                # Try real_name first, fall back to display_name or name
                real_name = user.get('real_name')
                if real_name:
                    logger.info(f"Found real name for user {user_id}: {real_name}")
                    self.user_id_to_name_cache[user_id] = real_name
                    return real_name
                    
                # Try display name
                display_name = user.get('profile', {}).get('display_name')
                if display_name:
                    logger.info(f"Using display name for user {user_id}: {display_name}")
                    self.user_id_to_name_cache[user_id] = display_name
                    return display_name
                    
                # Fall back to username
                name = user.get('name')
                if name:
                    logger.info(f"Using username for user {user_id}: {name}")
                    self.user_id_to_name_cache[user_id] = name
                    return name
        except Exception as e:
            logger.warning(f"Error looking up user info for {user_id}: {str(e)}")
            
        # Return the original ID if lookup fails
        return user_id
        
    async def _replace_user_ids_with_names(self, text: str) -> str:
        """Replace all Slack user IDs in the text with real names"""
        if not text:
            return text
            
        # Find all user IDs in the text (format: U followed by alphanumeric chars)
        user_id_pattern = r'\b(U[A-Z0-9]{8,})\b'
        user_ids = set(re.findall(user_id_pattern, text))
        
        # If no user IDs found, return the original text
        if not user_ids:
            return text
            
        # Look up names for all unique user IDs
        user_id_to_name = {}
        for user_id in user_ids:
            name = await self._lookup_user_name(user_id)
            user_id_to_name[user_id] = name
            
        # Replace all occurrences of each user ID with the corresponding name
        result = text
        for user_id, name in user_id_to_name.items():
            if name != user_id:  # Only replace if we found a different name
                result = result.replace(user_id, f"*{name}*")
                
        return result
    
    async def post_message(self, channel_id: str, text: str = None, blocks: list = None, thread_ts: str = None, return_ts: bool = False) -> None:
        """Post a message to Slack
        
        Args:
            channel_id: The channel ID to post to
            text: The text to post
            blocks: Optional blocks for rich formatting
            thread_ts: Optional thread timestamp to post in a thread
            return_ts: Whether to return the timestamp of the posted message
            
        Returns:
            If return_ts is True, returns a dict with 'ts' key containing the timestamp
            Otherwise returns None
        """
        try:
            # Validate channel_id
            if not channel_id:
                logger.error("post_message called with empty channel_id")
                raise ValueError("Channel ID is required for posting messages")
            
            # Ensure text is always provided (required by Slack API)
            if text is None:
                if blocks:
                    text = "Message with rich content"  # Default text when only blocks are provided
                else:
                    text = "Processing your request..."  # Default fallback text
                
            logger.info(f"Posting message to channel {channel_id}" + (f" in thread {thread_ts}" if thread_ts else ""))
            logger.debug(f"Message blocks: {json.dumps(blocks, indent=2) if blocks else 'None'}")
            
            # Try to find the user_id and response_url associated with this channel
            user_id = None
            response_url = None
            
            for uid, state in self.user_states.items():
                if state.get("channel_id") == channel_id or state.get("slack_channel_id") == channel_id:
                    user_id = uid
                    response_url = state.get("response_url")
                    logger.info(f"Found user {user_id} associated with channel {channel_id}")
                    break
            
            # Always use the bot token for posting messages to ensure proper permissions
            client = self.client
            
            # Check if this is a direct message channel (starts with D)
            is_dm_channel = channel_id.startswith('D')
            
            # Prepare message payload
            message_payload = {
                "channel": channel_id,
                "text": text,
            }
            
            # Only add blocks if provided
            if blocks:
                message_payload["blocks"] = blocks
            
            # For DM channels, always include thread_ts if available to ensure threading
            if is_dm_channel and thread_ts:
                logger.info(f"Adding thread_ts for DM channel response: {thread_ts}")
                message_payload["thread_ts"] = thread_ts
            elif thread_ts:  # For non-DM channels too
                message_payload["thread_ts"] = thread_ts
            
            # For DM channels, we need special handling
            if is_dm_channel:
                logger.info(f"Direct message channel detected: {channel_id}")
                
                # For DM channels, if we're not already in a thread, we should create one
                if not thread_ts:
                    logger.info("Creating parent message for DM thread")
                    try:
                        # First post the parent message
                        parent_response = client.chat_postMessage(
                            channel=channel_id,
                            text=text,
                            blocks=blocks
                        )
                        
                        if parent_response["ok"]:
                            logger.info("Parent message posted successfully to DM channel")
                            parent_ts = parent_response.get("ts")
                            
                            # If return_ts was requested, return the timestamp
                            if return_ts:
                                return {"ts": parent_ts}
                                
                            # If this is just a single message (not expecting a follow-up), we're done
                            return
                            
                    except SlackApiError as e:
                        error_msg = str(e).lower()
                        if "channel_not_found" in error_msg and response_url:
                            logger.warning(f"DM channel not found: {channel_id}. Using response_url fallback.")
                            try:
                                # Post using response_url
                                payload = {
                                    "text": text,
                                    "response_type": "in_channel"  # Make it visible to everyone in the channel
                                }
                                
                                # Add blocks if provided
                                if blocks:
                                    payload["blocks"] = blocks
                                
                                response = requests.post(
                                    response_url,
                                    json=payload,
                                    headers={"Content-Type": "application/json"}
                                )
                                
                                if response.status_code == 200:
                                    logger.info("Successfully posted parent message using response_url")
                                    # Try to extract timestamp from response
                                    try:
                                        response_json = response.json() if response.text else {}
                                        if response_json.get("ts"):
                                            if return_ts:
                                                return {"ts": response_json.get("ts")}
                                            return
                                    except Exception as json_err:
                                        logger.warning(f"Could not extract timestamp from response_url response: {json_err}")
                                else:
                                    logger.error(f"Error posting to response_url: {response.status_code} {response.text}")
                            except Exception as url_e:
                                logger.error(f"Error using response_url fallback: {url_e}")
                        else:
                            logger.error(f"Error posting parent message to DM channel: {e}")
                            raise
                else:
                    # We already have a thread_ts, so we're posting a reply in a DM thread
                    logger.info(f"Posting reply in DM thread {thread_ts}")
                    try:
                        # Post message in thread
                        response = client.chat_postMessage(**message_payload)
                        
                        if response["ok"]:
                            logger.info("Successfully posted reply in DM thread")
                            if return_ts:
                                return {"ts": response.get("ts")}
                            return
                    except SlackApiError as e:
                        error_msg = str(e).lower()
                        if "channel_not_found" in error_msg and response_url:
                            logger.warning(f"DM channel not found for thread reply: {channel_id}. Using response_url fallback.")
                            try:
                                # For response_url, make sure to include thread_ts
                                payload = {
                                    "text": text,
                                    "thread_ts": thread_ts,
                                    "response_type": "in_channel"  # Make it visible to everyone in the channel
                                }
                                
                                # Add blocks if provided
                                if blocks:
                                    payload["blocks"] = blocks
                                
                                response = requests.post(
                                    response_url,
                                    json=payload,
                                    headers={"Content-Type": "application/json"}
                                )
                                
                                if response.status_code == 200:
                                    logger.info("Successfully posted thread reply using response_url")
                                    if return_ts:
                                        # Try to extract timestamp from response
                                        try:
                                            response_json = response.json() if response.text else {}
                                            if response_json.get("ts"):
                                                return {"ts": response_json.get("ts")}
                                        except Exception:
                                            pass
                                        return {"ts": None}
                                    return
                                else:
                                    logger.error(f"Error posting to response_url: {response.status_code} {response.text}")
                            except Exception as url_e:
                                logger.error(f"Error using response_url fallback for thread reply: {url_e}")
                        else:
                            logger.error(f"Error posting reply in DM thread: {e}")
                            raise
            
            # For non-DM channels or if we get here and we still need to try posting
            try:
                response = client.chat_postMessage(**message_payload)
                
                if response["ok"]:
                    logger.info(f"Message posted successfully using bot token{' in thread' if thread_ts else ''}")
                    if return_ts:
                        # Return the timestamp of the message for thread creation
                        return {"ts": response.get("ts")}
                    return
                    
            except SlackApiError as e:
                error_msg = str(e).lower()
                if "channel_not_found" in error_msg or "not_in_channel" in error_msg:
                    logger.error(f"Error posting message to channel {channel_id}: {e}")
                    # Return None timestamp if requested
                    if return_ts:
                        return {"ts": None}
                    raise
                
        except Exception as e:
            logger.error(f"Unexpected error posting message: {str(e)}")
            # Return None timestamp if requested
            if return_ts:
                return {"ts": None}
            raise

    def _get_jira_error_message(self, source_data):
        """Generate a helpful error message for Jira validation failures"""
        # Check for project_key format first (our newer format)
        if 'project_key' in source_data:
            project_keys = source_data.get('project_key')
            
            # Check if it looks like a JQL query
            if isinstance(project_keys, str) and any(keyword in project_keys.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                # This is likely a JQL query being treated as a project key
                return f"Invalid JQL query syntax. Please check your query format: \"{project_keys[:50]}...\""
            
            if isinstance(project_keys, list):
                for key in project_keys:
                    if isinstance(key, str) and any(keyword in key.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                        # This is likely a JQL query being treated as a project key
                        return f"Invalid JQL query syntax. Please check your query format: \"{key[:50]}...\""
                
                project_keys_str = ', '.join(str(key) for key in project_keys if key)
                return f"Invalid Jira project key(s): {project_keys_str}. Please check the project key and ensure the bot has access."
            else:
                return f"Invalid Jira project key: {project_keys}. Please check the project key and ensure the bot has access."
        
        # Check for JQL query
        if 'jql_query' in source_data and source_data.get('jql_query'):
            jql_query = source_data.get('jql_query')
            return f"Invalid JQL query. Please check your syntax: \"{jql_query[:50]}...\""
        
        # Fall back to board format (legacy format)
        elif 'board' in source_data or 'boards' in source_data:
            boards = source_data.get('boards', [source_data.get('board')]) if isinstance(source_data.get('boards'), list) else [source_data.get('boards', source_data.get('board'))]
            return f"Invalid Jira project(s): {', '.join(str(b) for b in boards if b)}. Please check the project key/ID and ensure the bot has access."
            
        # Generic fallback
        return "Invalid Jira inputs. Please check your project key/ID."
    
    def _get_source_display_text(self, source_type: str, source_value: str) -> str:
        """Generate appropriate display text for a source value based on its type."""
        if not source_type or not source_value:
            return f"*Value:* {source_value}"
            
        source_type = source_type.lower()
        
        if source_type == "slack":
            # Check if this is a thread URL
            if self._is_slack_thread_url(source_value):
                # For thread URLs, show a shortened version
                if len(source_value) > 60:
                    display_value = source_value[:30] + "..." + source_value[-25:]
                    return f"*Thread URL:* {display_value}"
                return f"*Thread URL:* {source_value}"
            else:
                # Regular channel name
                return f"*Channel Name:* {source_value}"
            
        elif source_type == "jira":
            return f"*JQL Query:* {source_value}"
            
        elif source_type == "google_docs":
            # For URLs, show a shorter version
            if len(source_value) > 60 and ('http://' in source_value or 'https://' in source_value):
                display_value = source_value[:30] + "..." + source_value[-25:]
                return f"*Document URL:* {display_value}"
            return f"*Document URL:* {source_value}"
            
        elif source_type == "google_sheets":
            # For URLs, show a shorter version
            if len(source_value) > 60 and ('http://' in source_value or 'https://' in source_value):
                display_value = source_value[:30] + "..." + source_value[-25:]
                return f"*Spreadsheet URL:* {display_value}"
            return f"*Spreadsheet URL:* {source_value}"
            
        elif source_type == "tableau":
            # For URLs, show a shorter version
            if len(source_value) > 60 and ('http://' in source_value or 'https://' in source_value):
                display_value = source_value[:30] + "..." + source_value[-25:]
                return f"*Dashboard URL:* {display_value}"
            return f"*Dashboard URL:* {source_value}"
            
        else:
            # Default case
            return f"*Value:* {source_value}"
    
    def _extract_project_from_jql(self, jql: str) -> str:
        """Extract project key from JQL query"""
        if not jql:
            return "DEFAULT"
            
        # Try several patterns to extract project
        # Pattern 1: project = ABC
        match = re.search(r'project\s*=\s*[\'"]?([A-Za-z0-9-]+)[\'"]?', jql, re.IGNORECASE)
        if match:
            return match.group(1)
            
        # Pattern 2: project in (ABC)
        match = re.search(r'project\s+in\s+\(\s*[\'"]?([A-Za-z0-9-]+)[\'"]?', jql, re.IGNORECASE)
        if match:
            return match.group(1)
            
        # Pattern 3: key ~ ABC-
        match = re.search(r'key\s*~\s*[\'"]?([A-Za-z0-9-]+)-', jql, re.IGNORECASE)
        if match:
            return match.group(1)
            
        # If all else fails, return a default
        return "DEFAULT"
    
    def _validate_google_url(self, source_data: Dict[str, Any], doc_type: str) -> bool:
        """Validate a Google Docs or Sheets URL
        
        Args:
            source_data: The source data dictionary
            doc_type: The type of document - 'document' for Docs or 'spreadsheet' for Sheets
            
        Returns:
            Boolean indicating if the URL is valid
        """
        try:
            # Check document URLs
            if doc_type == 'document':
                urls = source_data.get('document_urls', [])
                expected_pattern = '/document/d/'
            # Check spreadsheet URLs
            elif doc_type == 'spreadsheet':
                urls = source_data.get('spreadsheet_urls', [])
                expected_pattern = '/spreadsheets/d/'
            else:
                return False
                
            # Handle single URL or empty case
            if not urls:
                return False
            if isinstance(urls, str):
                urls = [urls]
                
            # Validate each URL
            for url in urls:
                if not url or not isinstance(url, str):
                    return False
                    
                # Check if it's a Google URL with the expected pattern
                if not (('docs.google.com' in url or 'drive.google.com' in url) and expected_pattern in url):
                    logger.warning(f"Invalid Google {doc_type} URL: {url}")
                    return False
                    
            return True
            
        except Exception as e:
            logger.error(f"Error validating Google URL: {str(e)}")
            return False
            
    def _validate_tableau_url(self, source_data: Dict[str, Any]) -> bool:
        """Validate a Tableau dashboard URL
        
        Args:
            source_data: The source data dictionary
            
        Returns:
            Boolean indicating if the URL is valid
        """
        try:
            # Get URL from source data
            url = source_data.get('view_url', '')
            
            # Check if URL exists
            if not url or not isinstance(url, str):
                return False
                
            # Validate Tableau URL format
            # Tableau URLs typically contain 'tableau' in the domain and '/views/' in the path
            if not ('tableau' in url.lower() and '/views/' in url.lower()):
                logger.warning(f"Invalid Tableau URL format: {url}")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Error validating Tableau URL: {str(e)}")
            return False

    async def handle_message_event(self, message_data: Dict[str, Any]) -> None:
        """Handle incoming message events, including thread replies
        
        Args:
            message_data: The message event data from Slack
        """
        try:
            # Ignore bot messages to prevent loops
            if message_data.get("bot_id") or message_data.get("subtype") == "bot_message":
                logger.debug("Ignoring bot message to prevent loops")
                return
            
            user_id = message_data.get("user")
            text = message_data.get("text", "").strip()
            channel_id = message_data.get("channel")
            thread_ts = message_data.get("thread_ts")  # This will exist if the message is in a thread
            
            # Check if this is a direct message (channel ID starts with D)
            is_dm_channel = channel_id and channel_id.startswith('D')
            if is_dm_channel:
                logger.info(f"Received direct message from user {user_id}: {text[:50]}...")
                
                # Update user state to track that this user has a DM channel
                if user_id and user_id not in self.user_states:
                    self.user_states[user_id] = {
                        "sources": [],
                        "prompt": "",
                        "channel_id": channel_id,
                        "slack_channel_id": channel_id,
                        "source_data": {},
                        "is_dm": True
                    }
                elif user_id in self.user_states:
                    # Keep track of DM channel for this user
                    self.user_states[user_id]["is_dm"] = True
                    # Make sure channel IDs are up to date
                    if self.user_states[user_id].get("channel_id") != channel_id:
                        self.user_states[user_id]["channel_id"] = channel_id
                        self.user_states[user_id]["slack_channel_id"] = channel_id
                
                # If no thread_ts in a DM, this is a new direct message to the bot
                if not thread_ts and user_id and text:
                    # Check if this is a thread that the user already has open
                    if user_id in self.thread_contexts:
                        # Look for a thread in this DM channel
                        dm_thread = None
                        for ts, context in self.thread_contexts[user_id].items():
                            if context.get("channel_id") == channel_id:
                                # Found an existing thread in this DM
                                dm_thread = ts
                                logger.info(f"Found existing DM thread {ts} for user {user_id}")
                                break
                        
                        if dm_thread:
                            # Treat this as a follow-up message to the existing thread
                            logger.info(f"Processing as follow-up to existing DM thread")
                            await self.process_followup_query(user_id, text, channel_id, dm_thread)
                            return
                    
                    # If we reach here, either no thread context exists or
                    # no thread was found in this DM channel, so handle it as a new query
                    logger.info(f"Processing as new direct message query")
                    
                    # Respond with a helpful message about using /test command
                    await self.post_message(
                        channel_id,
                        text="👋 Hello! I'm Engage Genius. To analyze data, please use the /test command to select data sources and enter a prompt."
                    )
                    return
            
            # If this isn't a thread message or we don't have required data, ignore it
            if not thread_ts or not user_id or not text or not channel_id:
                logger.info(f"Skipping non-thread message or missing data: thread_ts={thread_ts}, user={user_id}, channel={channel_id}")
                return
            
            # Check if this thread was started by the /genius command (Razorpay Genius app)
            # Check if the thread exists in any user's contexts and if it was from the correct source
            is_razorpay_thread = False
            thread_owner_id = None
            
            logger.info(f"Checking thread contexts for thread {thread_ts}")
            # Examine thread contexts to find if this is from /genius command
            for uid, contexts in self.thread_contexts.items():
                if thread_ts in contexts:
                    thread_context = contexts[thread_ts]
                    
                    # Check if this thread has an origin marker that identifies it as from /genius
                    # Look for command_source field in the context or check for an app identifier
                    command_source = thread_context.get("command_source", "")
                    app_identifier = thread_context.get("app_identifier", "")
                    
                    # Check if the thread context has an identifier marking it from the Razorpay Genius app
                    if (command_source == "genius" or 
                        app_identifier == "razorpay_genius" or 
                        # As a fallback, also look for a command name in the original query
                        thread_context.get("original_query", "").startswith("/genius")):
                        
                        is_razorpay_thread = True
                        thread_owner_id = uid
                        logger.info(f"Found thread from /genius command by user {uid}")
                        break
            
            # Only proceed if this is a thread started by the /genius command
            if not is_razorpay_thread:
                # Check if it's a thread from our /test command
                for uid, contexts in self.thread_contexts.items():
                    if thread_ts in contexts:
                        thread_context = contexts[thread_ts]
                        # If it's our thread (from /test) and in the same channel
                        if thread_context.get("channel_id") == channel_id:
                            thread_owner_id = uid
                            logger.info(f"Found thread from /test command by user {uid}")
                            break
                
                if not thread_owner_id:
                    logger.info(f"Ignoring thread message - not a Razorpay Genius or Engage Genius thread")
                    return
            
            logger.info(f"Received follow-up query from user {user_id} in thread {thread_ts}: {text}")
            
            # Try to reload contexts from disk if we don't have the specific thread context yet
            if not thread_owner_id or thread_ts not in self.thread_contexts.get(thread_owner_id, {}):
                logger.warning(f"Reloading thread contexts from disk")
                self._load_thread_contexts()
                # Look for the thread again
                for uid, contexts in self.thread_contexts.items():
                    if thread_ts in contexts:
                        thread_owner_id = uid
                        logger.info(f"Found thread context after reload, owned by user {uid}")
                        break
            
            # Process the thread if owner was found
            if thread_owner_id and thread_ts in self.thread_contexts[thread_owner_id]:
                logger.info(f"Processing follow-up query for thread {thread_ts}")
                # Use the thread owner's context
                await self.process_followup_query(thread_owner_id, text, channel_id, thread_ts)
            else:
                logger.info(f"Thread identified but context not found for {thread_ts}")
            
        except Exception as e:
            logger.error(f"Error handling message event: {str(e)}", exc_info=True)

    async def process_followup_query(self, user_id: str, text: str, channel_id: str, thread_ts: str) -> None:
        """Process a follow-up query using previously selected data sources
        
        Args:
            user_id: The Slack user ID
            text: The follow-up query text
            channel_id: The channel ID
            thread_ts: The thread timestamp
        """
        try:
            # Get the thread context
            thread_context = self.thread_contexts[user_id][thread_ts]
            sources = thread_context.get("sources", [])
            source_data = thread_context.get("source_data", {})
            
            # Check if this is a DM channel and if we have a response_url
            is_dm = thread_context.get("is_dm", False) or channel_id.startswith('D')
            response_url = thread_context.get("response_url")
            
            # For DM channels, ensure thread_ts is always populated
            original_thread_ts = thread_ts
            
            # Use stored thread_ts from context if available, otherwise use the provided one
            context_thread_ts = thread_context.get("thread_ts", thread_ts)
            if context_thread_ts != thread_ts:
                logger.info(f"Using stored thread_ts from context: {context_thread_ts} instead of {thread_ts}")
                thread_ts = context_thread_ts
            
            if is_dm:
                logger.info(f"Processing follow-up query in DM channel: {channel_id}, thread_ts: {thread_ts}")
                if response_url:
                    logger.info(f"Response URL available for DM channel: {response_url}")
                else:
                    logger.warning("No response_url available for DM channel")
            
            # Get conversation history or initialize if not present
            conversation_history = thread_context.get("conversation_history", [])
            
            if not sources or not source_data:
                # No sources in context, inform the user
                await self.post_message(
                    channel_id,
                    thread_ts=thread_ts,  # Always use thread_ts for DM channels
                    text="I don't have any data sources available for this thread. Please use the /test command to select sources."
                )
                return
            
            # Send acknowledgment
            await self.post_message(
                channel_id,
                thread_ts=thread_ts,  # Always use thread_ts for DM channels
                text=f"Processing your follow-up query using {len(sources)} previously used data sources..."
            )
            
            # Process data from all sources with the new query
            analysis_data = {}
            validation_errors = []
            
            for source_name, source_data_values in source_data.items():
                source_instance = self.source_registry.get_source(source_name)
                if not source_instance:
                    validation_errors.append(f"Source {source_name} is no longer available")
                    continue
                    
                try:
                    # Include the new query in the source data
                    if source_name.lower() == "slack":
                        source_data_with_query = {
                            **source_data_values,
                            'query': text,
                            'user_id': user_id,
                            'current_channel_id': channel_id
                        }
                    elif source_name.lower() == "jira":
                        source_data_with_query = {
                            **source_data_values,
                            'query': text
                        }
                    else:
                        source_data_with_query = {
                            **source_data_values,
                            'query': text
                        }
                    
                    data = await source_instance.fetch_data(source_data_with_query)
                    analysis_data[source_name] = data
                    logger.info(f"Successfully fetched follow-up data from {source_name}")
                    
                except Exception as e:
                    error_msg = f"Error processing {source_name} for follow-up: {str(e)}"
                    validation_errors.append(error_msg)
                    logger.error(error_msg, exc_info=True)
            
            # If there were validation errors, report them
            if validation_errors:
                error_message = "The following errors occurred during data retrieval:\n• " + "\n• ".join(validation_errors)
                
                # If we have no valid data sources, return an error
                if not analysis_data:
                    await self.post_message(
                        channel_id,
                        thread_ts=thread_ts,
                        text=error_message
                    )
                    return
                
                # Otherwise, notify about partial data retrieval
                await self.post_message(
                    channel_id,
                    thread_ts=thread_ts,
                    text=f"⚠️ Warning: {error_message}\n\nProceeding with analysis using available data sources."
                )
            
            # Add conversation history context to the query
            conversation_context = self._format_conversation_history(conversation_history)
            enhanced_query = f"{text}\n\nPrevious conversation:\n{conversation_context}"
            logger.info(f"Enhanced query with conversation history: {enhanced_query[:100]}...")
            
            # Generate analysis with conversation history context and pass query for context optimization
            analysis_result = await self.context_manager.generate_analysis(
                source_data=analysis_data, 
                query=enhanced_query
            )
            
            # Format the analysis response nicely for Slack
            if isinstance(analysis_result, dict):
                response_text = analysis_result.get("response", "")
                confidence = analysis_result.get("confidence", 0)
                metadata = analysis_result.get("metadata", {})
                
                # Replace user IDs with real names
                response_text = await self._replace_user_ids_with_names(response_text)
                
                # Add title with source information
                source_count = metadata.get("source_count", len(analysis_data.keys()))
                source_names = ", ".join(analysis_data.keys())
                title = f"*Follow-up Analysis Results from {source_count} source{'' if source_count == 1 else 's'}*"
                if source_count > 0:
                    title += f"\n_Sources: {source_names}_\n\n"
                
                # Fix Slack markdown formatting
                # Ensure header formatting is Slack-compatible (transform ### to *bold*)
                formatted_response = ""
                for line in response_text.split('\n'):
                    # Convert markdown headers to Slack bold
                    if line.startswith('###'):
                        line = f"{line.lstrip('# ')}"
                    elif line.startswith('##'):
                        line = f"{line.lstrip('# ')}"
                    elif line.startswith('#'):
                        line = f"{line.lstrip('# ')}"
                    
                    # Ensure lists are properly formatted for Slack
                    if (line.strip().startswith('- ') or 
                        line.strip().startswith('* ') or 
                        re.match(r'^\d+\.\s', line.strip())):  # Match numbered lists like "1. Item"
                        # Make sure there's proper spacing for list items
                        if formatted_response and not formatted_response.endswith('\n'):
                            formatted_response += '\n'
                    
                    # Enhance numbered list items with bullet points for better visibility in Slack
                    numbered_list_match = re.match(r'^(\d+)\.(\s.*)$', line.strip())
                    if numbered_list_match:
                        number = numbered_list_match.group(1)
                        content = numbered_list_match.group(2)
                        line = f"• {number}{content}"
                    
                    formatted_response += line + '\n'
                
                # Format confidence level
                confidence_text = ""
                if confidence > 0:
                    confidence_percent = int(confidence * 100)
                    confidence_text = f"\n*Confidence: {confidence_percent}%*"
                
                formatted_analysis = f"{title}{formatted_response}{confidence_text}"
            else:
                # Fallback if analysis is not a dict
                formatted_analysis = str(analysis_result)
            
            # Send response to the interaction channel in the thread
            # For DM channels with response_url, we need custom handling
            if is_dm and response_url:
                try:
                    logger.info(f"Posting follow-up analysis to DM channel using response_url with thread_ts: {thread_ts}")
                    
                    # Create payload for response_url with thread_ts
                    payload = {
                        "text": formatted_analysis
                    }
                    
                    # Always add thread_ts if available
                    if thread_ts:
                        payload["thread_ts"] = thread_ts
                    
                    # Post using response_url
                    response = requests.post(
                        response_url,
                        json=payload,
                        headers={"Content-Type": "application/json"}
                    )
                    
                    if response.status_code == 200:
                        logger.info("Successfully posted follow-up analysis to DM using response_url")
                        # Try to get timestamp from the response if possible
                        try:
                            response_json = response.json() if response.text else {}
                            if isinstance(response_json, dict) and response_json.get("ts"):
                                # Got a timestamp from the response - use this for future messages
                                new_thread_ts = response_json.get("ts")
                                # Update the thread context with this ts for future messages
                                if user_id in self.thread_contexts and thread_ts in self.thread_contexts[user_id]:
                                    self.thread_contexts[user_id][thread_ts]["thread_ts"] = new_thread_ts
                                    logger.info(f"Updated thread timestamp in context: {new_thread_ts}")
                        except Exception as json_error:
                            logger.warning(f"Could not parse response JSON: {json_error}")
                    else:
                        logger.error(f"Error posting to response_url: {response.status_code} {response.text}")
                        # Fall back to regular post_message
                        await self.post_message(
                            channel_id,
                            text=formatted_analysis,
                            thread_ts=thread_ts
                        )
                except Exception as e:
                    logger.error(f"Error posting to response_url: {str(e)}")
                    # Fall back to regular post_message
                    await self.post_message(
                        channel_id,
                        text=formatted_analysis,
                        thread_ts=thread_ts
                    )
            else:
                # Use standard post_message for non-DM channels
                await self.post_message(
                    channel_id,
                    text=formatted_analysis,
                    thread_ts=thread_ts
                )
            
            # Update conversation history
            conversation_history.append({"role": "user", "content": text})
            conversation_history.append({"role": "assistant", "content": response_text if isinstance(analysis_result, dict) else formatted_analysis})
            
            # Store updated conversation history
            self.thread_contexts[user_id][thread_ts]["conversation_history"] = conversation_history
            
            # Update last interaction time for this thread context
            self.thread_contexts[user_id][thread_ts]["last_interaction"] = time.time()
            
            # Save thread contexts to disk after updates
            self._save_thread_contexts()
            
        except Exception as e:
            logger.error(f"Error processing follow-up query: {str(e)}", exc_info=True)
            await self.post_message(
                channel_id,
                thread_ts=thread_ts,
                text=f"Error processing your follow-up query: {str(e)}"
            )

    def _format_conversation_history(self, conversation_history: List[Dict[str, str]]) -> str:
        """Format conversation history for inclusion in prompts
        
        Args:
            conversation_history: List of conversation messages with role and content
            
        Returns:
            Formatted conversation history string
        """
        if not conversation_history:
            return ""
        
        formatted_history = []
        for message in conversation_history:
            role = message.get("role", "")
            content = message.get("content", "")
            
            if role == "user":
                formatted_history.append(f"User: {content}")
            elif role == "assistant":
                formatted_history.append(f"Assistant: {content}")
        
        return "\n\n".join(formatted_history)
    
    async def handle_submit_prompt(self, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle submitting the analysis prompt with selected data sources"""
        try:
            # Get prompt text from the state
            state_values = payload.get("state", {}).get("values", {})
            prompt_text = ""
            
            if "prompt_block" in state_values:
                if "prompt_input" in state_values["prompt_block"]:
                    prompt_text = state_values["prompt_block"]["prompt_input"].get("value", "")
                elif "prompt" in state_values["prompt_block"]:
                    prompt_text = state_values["prompt_block"]["prompt"].get("value", "")
            
            if not prompt_text:
                return {
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "⚠️ *Validation Error*: Please enter a prompt before submitting"
                            }
                        },
                        {
                            "type": "divider"
                        },
                        {
                            "type": "actions",
                            "elements": [
                                {
                                    "type": "button",
                                    "text": {"type": "plain_text", "text": "Retry", "emoji": True},
                                    "action_id": "retry_submission",
                                    "style": "primary"
                                }
                            ]
                        }
                    ]
                }
            
            # Get channel ID from payload
            channel_id = payload.get("channel", {}).get("id", "")
            
            # Extract thread_ts for DM channels
            thread_ts = None
            is_dm = channel_id.startswith('D')
            
            if is_dm:
                # Try to extract thread_ts from various places in the payload
                if payload.get("container", {}).get("message_ts"):
                    thread_ts = payload["container"]["message_ts"]
                elif payload.get("message", {}).get("ts"):
                    thread_ts = payload["message"]["ts"]
                
                logger.info(f"Extracted thread_ts for DM channel: {thread_ts}")
            
            # Update the user's state with the prompt
            if user_id in self.user_states:
                self.user_states[user_id]["prompt"] = prompt_text
            
            # Update payload for handle_prompt_submission
            prompt_payload = {
                "user": {"id": user_id},
                "channel": {"id": channel_id},
                "prompt_text": prompt_text,
                "container": payload.get("container", {})  # Pass container info to track modal state
            }
            
            # Add state values if available
            if "state" in payload:
                prompt_payload["state"] = payload["state"]
            
            # Add thread_ts for DM channels
            if is_dm and thread_ts:
                prompt_payload["thread_ts"] = thread_ts
                logger.info(f"Added thread_ts to prompt payload: {thread_ts}")
            
            # Check if this is a DM channel and include response_url
            response_url = payload.get("response_url")
            if is_dm and response_url:
                prompt_payload["response_url"] = response_url
                prompt_payload["is_dm"] = True
                
                # Also update the user state
                if user_id in self.user_states:
                    self.user_states[user_id]["response_url"] = response_url
                    self.user_states[user_id]["is_dm"] = True
                    if thread_ts:
                        self.user_states[user_id]["thread_ts"] = thread_ts
            
            # Check if this is a modal view
            container_type = payload.get("container", {}).get("type", "")
            is_modal = container_type == "view"
            
            # For message-based interactions with a response_url, show a loading message
            if response_url:
                try:
                    # Replace original UI with a loading message
                    loading_payload = {
                        "replace_original": True,
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "🔍 *Processing your request...*\nGenerating insights based on your data sources."
                                }
                            }
                        ]
                    }
                    
                    # For DM channels, include thread_ts to ensure threaded response
                    if is_dm and thread_ts:
                        loading_payload["thread_ts"] = thread_ts
                    
                    logger.info(f"Replacing UI with loading indicator via response_url" + 
                               (f" in thread {thread_ts}" if thread_ts else ""))
                    
                    requests.post(
                        response_url,
                        json=loading_payload,
                        headers={"Content-Type": "application/json"}
                    )
                except Exception as e:
                    logger.error(f"Error using response_url to update UI: {str(e)}")
            
            # For modal views, return a loading view
            if is_modal:
                logger.info("Showing processing indicator in modal")
                
                # Start processing in the background without waiting for it to complete
                asyncio.create_task(self.handle_prompt_submission(prompt_payload))
                
                # Return a loading view that will stay open during processing
                return {
                    "response_action": "update",
                    "view": {
                        "type": "modal",
                        "title": {"type": "plain_text", "text": "Processing"},
                        "close": {"type": "plain_text", "text": "Close"},
                        "blocks": [
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": "🔍 *Processing your request...*\nYour results will appear shortly in the channel.\n\nThis modal will update when processing completes."
                                }
                            }
                        ]
                    }
                }
            else:
                # For message-based interactions, start processing in the background
                asyncio.create_task(self.handle_prompt_submission(prompt_payload))
                
                # Return an empty response since we've already updated the message via response_url
                return {"text": ""}
                
        except Exception as e:
            logger.error(f"Error in handle_submit_prompt: {str(e)}", exc_info=True)
            return {
                "response_type": "ephemeral",
                "replace_original": False,
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"❌ *Error*: {str(e)}"
                        }
                    },
                    {
                        "type": "divider"
                    },
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Retry", "emoji": True},
                                "action_id": "retry_submission",
                                "style": "primary"
                            },
                            {
                                "type": "button",
                                "text": {"type": "plain_text", "text": "Reset", "emoji": True}, 
                                "action_id": "reload_page"
                            }
                        ]
                    }
                ]
            }
    
    def _get_devrev_error_message(self, source_data):
        """Generate a helpful error message for DevRev validation failures"""
        # Check for project_key format first
        if 'project_key' in source_data:
            project_keys = source_data.get('project_key')
            
            # Check if it looks like a JQL query
            if isinstance(project_keys, str) and any(keyword in project_keys.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                # This is likely a JQL query being treated as a project key
                return f"Invalid JQL query syntax. Please check your query format: \"{project_keys[:50]}...\""
            
            if isinstance(project_keys, list):
                for key in project_keys:
                    if isinstance(key, str) and any(keyword in key.upper() for keyword in [' AND ', ' OR ', '=', '<', '>', 'ORDER BY', 'PROJECT =']):
                        # This is likely a JQL query being treated as a project key
                        return f"Invalid JQL query syntax. Please check your query format: \"{key[:50]}...\""
                
                project_keys_str = ', '.join(str(key) for key in project_keys if key)
                return f"Invalid DevRev project/part ID(s): {project_keys_str}. Please check the project/part ID and ensure the bot has access."
            else:
                return f"Invalid DevRev project/part ID: {project_keys}. Please check the project/part ID and ensure the bot has access."
        
        # Check for JQL query
        if 'jql_query' in source_data and source_data.get('jql_query'):
            jql_query = source_data.get('jql_query')
            return f"Invalid JQL query for DevRev. Please check your syntax: \"{jql_query[:50]}...\""
            
        # Generic fallback
        return "Invalid DevRev inputs. Please check your project/part ID or JQL query."
    
    def _is_slack_thread_url(self, url: str) -> bool:
        """
        Check if a URL is a Slack thread URL or permalink
        
        Args:
            url: The URL to check
            
        Returns:
            True if the URL appears to be a Slack thread URL or permalink, False otherwise
        """
        if not url or not isinstance(url, str):
            return False
        
        url = url.strip().lower()
        
        # Check for common Slack thread URL patterns
        thread_indicators = [
            '/archives/',  # https://workspace.slack.com/archives/C1234567890/p1234567890123456
            'thread_ts=',  # Query parameter indicating a thread
            '/thread/',    # https://app.slack.com/client/T1234567890/C1234567890/thread/...
            '/p',          # Permalink format (can be single message or thread)
        ]
        
        # Must be a slack URL and contain thread indicators
        is_slack_url = any(domain in url for domain in ['slack.com', 'slack-redir.net'])
        has_thread_indicator = any(indicator in url for indicator in thread_indicators)
        
        return is_slack_url and has_thread_indicator
    
    
    