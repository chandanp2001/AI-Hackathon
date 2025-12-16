from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import logging
import os
import time
import re
from typing import List, Dict, Any, Optional, Tuple, Union
from urllib.parse import urlparse, parse_qs

# Import LLM service for thread summarization
try:
    from services.llm.llm_service import generate_ai_analysis
except ImportError:
    # Fallback if import fails
    def generate_ai_analysis(prompt: str, max_tokens: int = 2000, temperature: float = 0.7) -> str:
        return "Error: LLM service not available for summarization"

# Configure logging
logger = logging.getLogger(__name__)

# Get Slack token from environment
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

def fetch_slack_messages(channel_id_or_name: str, days: int = 7, message_limit: int = 100) -> str:
    """
    Fetch messages from a Slack channel
    
    Args:
        channel_id_or_name: The ID or name of the channel to fetch messages from
        days: Number of days to look back for messages (default: 7)
        message_limit: Maximum number of messages to fetch (default: 100)
        
    Returns:
        Formatted string with channel messages
    """
    try:
        # Initialize Slack client
        client = WebClient(token=SLACK_BOT_TOKEN)
        
        # Determine if input is a channel ID or name
        channel_id = channel_id_or_name
        if not channel_id_or_name.startswith('C') or len(channel_id_or_name) != 11:
            # This looks like a channel name, not an ID - try to find the ID
            try:
                # List channels
                response = client.conversations_list(types="public_channel,private_channel")
                channels = response.get("channels", [])
                
                # Find channel by name
                found = False
                for channel in channels:
                    if channel["name"] == channel_id_or_name.lower().strip('#'):
                        channel_id = channel["id"]
                        found = True
                        break
                
                if not found:
                    return f"Error: Could not find channel with name '{channel_id_or_name}'"
                    
            except SlackApiError as e:
                logger.error(f"Error listing channels: {e}")
                return f"Error listing channels: {e}"
        
        # Verify the channel ID exists and we have access
        try:
            # Test access to the channel
            test_response = client.conversations_info(channel=channel_id)
            if not test_response.get("ok", False):
                return f"Error: Could not access channel with ID '{channel_id}'"
        except SlackApiError as e:
            # Check if this is a "channel_not_found" error
            if "channel_not_found" in str(e):
                return f"Error: Channel ID '{channel_id}' not found or not accessible"
            logger.error(f"Error verifying channel: {e}")
            return f"Error verifying channel: {e}"
        
        # Calculate timestamp for X days ago
        oldest_timestamp = time.time() - (days * 24 * 60 * 60)
        
        # Fetch messages from channel
        try:
            response = client.conversations_history(
                channel=channel_id,
                limit=message_limit,
                oldest=str(oldest_timestamp)
            )
            
            messages = response.get("messages", [])
            
            if not messages:
                return f"No messages found in the specified channel for the last {days} days"
            
            # Get channel info for display
            channel_info = client.conversations_info(channel=channel_id)
            channel_name = channel_info.get("channel", {}).get("name", channel_id)
            
            # Format messages
            formatted_messages = f"Messages from #{channel_name} for the last {days} days:\n\n"
            
            # Get user info for display names
            user_cache = {}
            
            for msg in messages:
                # Skip system messages and thread messages
                if msg.get("subtype") in ["channel_join", "channel_leave"]:
                    continue
                
                # Get user info if not cached
                user_id = msg.get("user", "Unknown")
                if user_id not in user_cache:
                    try:
                        user_info = client.users_info(user=user_id)
                        user_name = user_info.get("user", {}).get("real_name", user_id)
                        user_cache[user_id] = user_name
                    except:
                        user_cache[user_id] = f"User {user_id}"
                
                user_name = user_cache[user_id]
                
                # Format message text
                text = msg.get("text", "")
                ts = msg.get("ts", "")
                
                # Convert timestamp to readable format
                msg_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
                
                formatted_messages += f"[{msg_time}] {user_name}: {text}\n\n"
            
            return formatted_messages
            
        except SlackApiError as e:
            logger.error(f"Error fetching messages: {e}")
            return f"Error fetching messages: {e}"
            
    except Exception as e:
        logger.error(f"Error in fetch_slack_messages: {str(e)}")
        return f"Error: {str(e)}"

def fetch_and_summarize_slack_thread(thread_url: str) -> str:
    """
    Fetch messages from a Slack thread URL and provide an AI-generated summary
    
    Args:
        thread_url: The Slack thread URL (e.g., https://workspace.slack.com/archives/C1234567890/p1234567890123456?thread_ts=1234567890.123456)
        
    Returns:
        AI-generated summary of the thread conversation
        
    Raises:
        SlackApiError: If there's an error accessing Slack API
        ConnectionError: If there's an error connecting to external services
        TimeoutError: If the request times out
        ValueError: If the URL format is invalid
    """
    try:
        # Parse the Slack thread URL to extract channel ID and thread timestamp
        channel_id, thread_ts = _parse_slack_thread_url(thread_url)
        
        if not channel_id or not thread_ts:
            return "Error: Invalid Slack thread URL format. Please provide a valid thread URL."
        
        # Initialize Slack client
        client = WebClient(token=SLACK_BOT_TOKEN)
        
        # Verify we have access to the channel
        try:
            channel_info = client.conversations_info(channel=channel_id)
            if not channel_info.get("ok", False):
                return f"Error: Could not access channel with ID '{channel_id}'"
            channel_name = channel_info.get("channel", {}).get("name", channel_id)
        except SlackApiError as e:
            if "channel_not_found" in str(e):
                return f"Error: Channel not found or not accessible"
            logger.error(f"Error verifying channel access: {e}")
            return f"Error verifying channel access: {e}"
        
        # Fetch thread messages
        try:
            response = client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=200  # Fetch up to 200 messages in the thread
            )
            
            messages = response.get("messages", [])
            
            if not messages:
                return "No messages found in the specified thread"
            
            # Check if this is a single message (no replies) vs a thread
            if len(messages) == 1:
                # This might be a permalink to a single message, not a thread
                # Let's still process it but note that it's a single message
                logger.info(f"Processing single message (no thread replies found)")
                message_type = "single message"
            else:
                logger.info(f"Processing thread with {len(messages)} messages")
                message_type = "thread"
            
            # Format thread messages for AI analysis
            formatted_thread = _format_thread_for_analysis(messages, channel_name, client)
            
            # Generate AI summary of the thread/message
            if message_type == "single message":
                summary_prompt = f"""
                Please analyze the following Slack message and provide a comprehensive summary.
                
                Message from #{channel_name}:
                {formatted_thread}
                
                Please provide:
                1. A brief overview of the message content
                2. Key points or information shared
                3. Any questions asked or issues raised
                4. Context or implications of the message
                5. The sender's main intent or purpose
                
                Keep the summary concise but comprehensive, focusing on the most important information.
                """
            else:
                summary_prompt = f"""
                Please analyze the following Slack thread conversation and provide a comprehensive summary.
                
                Thread from #{channel_name}:
                {formatted_thread}
                
                Please provide:
                1. A brief overview of the main topic/discussion
                2. Key points discussed by participants
                3. Any decisions made or action items identified
                4. Important outcomes or conclusions
                5. Notable participants and their main contributions
                
                Keep the summary concise but comprehensive, focusing on the most important information.
                """
            
            logger.info(f"Generating AI summary for {message_type} with {len(messages)} messages")
            summary = generate_ai_analysis(
                summary_prompt, 
                max_tokens=1500, 
                temperature=0.3  # Lower temperature for more focused summaries
            )
            
            # Add metadata to the summary
            if message_type == "single message":
                thread_metadata = f"**Message Summary for #{channel_name}**\n"
                thread_metadata += f"📅 Message posted: {_format_timestamp(messages[0].get('ts', ''))}\n"
                thread_metadata += f"👤 Author: {messages[0].get('user', 'Unknown')}\n\n"
            else:
                thread_metadata = f"**Thread Summary for #{channel_name}**\n"
                thread_metadata += f"📅 Thread started: {_format_timestamp(messages[0].get('ts', ''))}\n"
                thread_metadata += f"💬 Total messages: {len(messages)}\n"
                thread_metadata += f"👥 Participants: {len(set(msg.get('user', 'Unknown') for msg in messages if msg.get('user')))}\n\n"
            
            return thread_metadata + summary
            
        except SlackApiError as e:
            logger.error(f"Error fetching thread messages: {e}")
            return f"Error fetching thread messages: {e}"
            
    except ValueError as e:
        logger.error(f"URL parsing error: {e}")
        return f"Error: {str(e)}"
    except ConnectionError as e:
        logger.error(f"Connection error: {e}")
        return f"Error: Connection failed - {str(e)}"
    except TimeoutError as e:
        logger.error(f"Timeout error: {e}")
        return f"Error: Request timed out - {str(e)}"
    except Exception as e:
        logger.error(f"Error in fetch_and_summarize_slack_thread: {str(e)}")
        return f"Error: {str(e)}"

def _parse_slack_thread_url(thread_url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse a Slack thread URL to extract channel ID and thread timestamp
    
    Args:
        thread_url: The Slack thread URL
        
    Returns:
        Tuple of (channel_id, thread_ts) or (None, None) if parsing fails
        
    Raises:
        ValueError: If the URL format is completely invalid
        
    Examples:
        https://workspace.slack.com/archives/C1234567890/p1234567890123456?thread_ts=1234567890.123456
        https://app.slack.com/client/T1234567890/C1234567890/thread/C1234567890-1234567890.123456
    """
    try:
        if not thread_url or not isinstance(thread_url, str):
            raise ValueError("Thread URL must be a non-empty string")
            
        parsed_url = urlparse(thread_url)
        
        if not parsed_url.netloc:
            raise ValueError("Invalid URL format - missing domain")
        
        # Handle different Slack URL formats
        if 'archives' in parsed_url.path:
            # Format: https://workspace.slack.com/archives/C1234567890/p1234567890123456?thread_ts=1234567890.123456
            # Or permalink: https://workspace.slack.com/archives/C1234567890/p1234567890123456
            path_parts = parsed_url.path.strip('/').split('/')
            if len(path_parts) >= 2 and path_parts[0] == 'archives':
                channel_id = path_parts[1]
                
                # Validate channel ID format
                if not (channel_id.startswith('C') and len(channel_id) == 11):
                    logger.warning(f"Invalid channel ID format: {channel_id}")
                    return None, None
                
                # First, try to extract thread_ts from query parameters
                query_params = parse_qs(parsed_url.query)
                thread_ts = query_params.get('thread_ts', [None])[0]
                
                if thread_ts and '.' in thread_ts:
                    return channel_id, thread_ts
                
                # If no thread_ts parameter, check if we have a permalink (p followed by timestamp)
                if len(path_parts) >= 3:
                    permalink_part = path_parts[2]
                    if permalink_part.startswith('p') and len(permalink_part) > 1:
                        # Extract timestamp from permalink format (p1234567890123456)
                        timestamp_str = permalink_part[1:]  # Remove 'p' prefix
                        
                        # Convert to proper timestamp format (add decimal point)
                        # Slack timestamps are typically 10 digits + 6 microsecond digits
                        if len(timestamp_str) >= 16 and timestamp_str.isdigit():
                            # Format: 1234567890123456 -> 1234567890.123456
                            thread_ts = f"{timestamp_str[:10]}.{timestamp_str[10:]}"
                            logger.info(f"Converted permalink timestamp {timestamp_str} to thread_ts {thread_ts}")
                            return channel_id, thread_ts
                        elif len(timestamp_str) >= 10 and timestamp_str.isdigit():
                            # Shorter format, pad with zeros
                            padded_timestamp = timestamp_str.ljust(16, '0')
                            thread_ts = f"{padded_timestamp[:10]}.{padded_timestamp[10:]}"
                            logger.info(f"Converted short permalink timestamp {timestamp_str} to thread_ts {thread_ts}")
                            return channel_id, thread_ts
        elif 'client' in parsed_url.path and 'thread' in parsed_url.path:
            # Format: https://app.slack.com/client/T1234567890/C1234567890/thread/C1234567890-1234567890.123456
            path_parts = parsed_url.path.strip('/').split('/')
            if len(path_parts) >= 5 and path_parts[0] == 'client' and path_parts[3] == 'thread':
                channel_id = path_parts[2]  # Channel ID is at index 2
                
                # Validate channel ID format
                if not (channel_id.startswith('C') and len(channel_id) == 11):
                    logger.warning(f"Invalid channel ID format: {channel_id}")
                    return None, None
                
                # Extract thread timestamp from the last part
                thread_part = path_parts[4]  # Thread part is at index 4
                if '-' in thread_part:
                    thread_ts = thread_part.split('-', 1)[1]
                    if '.' in thread_ts:
                        return channel_id, thread_ts
        
        # Try to extract from fragment (some Slack URLs use fragments)
        if parsed_url.fragment:
            # Handle fragment-based URLs
            fragment_parts = parsed_url.fragment.split('/')
            for i, part in enumerate(fragment_parts):
                if part.startswith('C') and len(part) == 11:  # Channel ID format
                    channel_id = part
                    # Look for thread timestamp in subsequent parts
                    if i + 1 < len(fragment_parts):
                        next_part = fragment_parts[i + 1]
                        if '.' in next_part and next_part.replace('.', '').replace('p', '').isdigit():
                            # Remove 'p' prefix if present (permalink format)
                            thread_ts = next_part.lstrip('p')
                            if '.' in thread_ts:
                                return channel_id, thread_ts
        
        logger.warning(f"Could not parse Slack thread URL: {thread_url}")
        return None, None
        
    except ValueError:
        # Re-raise ValueError for proper error handling
        raise
    except Exception as e:
        logger.error(f"Error parsing Slack thread URL: {e}")
        raise ValueError(f"Failed to parse thread URL: {str(e)}")

def _format_thread_for_analysis(messages: List[Dict[str, Any]], channel_name: str, client: WebClient) -> str:
    """
    Format thread messages for AI analysis
    
    Args:
        messages: List of message dictionaries from Slack API
        channel_name: Name of the channel
        client: Slack WebClient instance
        
    Returns:
        Formatted string ready for AI analysis
        
    Raises:
        SlackApiError: If there's an error fetching user information
    """
    formatted_messages = []
    user_cache = {}
    
    try:
        for i, msg in enumerate(messages):
            # Skip system messages
            if msg.get("subtype") in ["channel_join", "channel_leave", "bot_message"]:
                continue
                
            # Get user info if not cached
            user_id = msg.get("user", "Unknown")
            if user_id not in user_cache:
                try:
                    user_info = client.users_info(user=user_id)
                    user_data = user_info.get("user", {})
                    user_name = user_data.get("real_name") or user_data.get("display_name", user_id)
                    user_cache[user_id] = user_name
                except SlackApiError as e:
                    logger.warning(f"Could not fetch user info for {user_id}: {e}")
                    user_cache[user_id] = f"User {user_id}"
                except Exception as e:
                    logger.warning(f"Unexpected error fetching user info for {user_id}: {e}")
                    user_cache[user_id] = f"User {user_id}"
            
            user_name = user_cache[user_id]
            
            # Format message text
            text = msg.get("text", "")
            ts = msg.get("ts", "")
            
            # Convert timestamp to readable format
            msg_time = _format_timestamp(ts)
            
            # Indicate if this is the original thread message
            thread_indicator = " [THREAD START]" if i == 0 else ""
            
            formatted_messages.append(f"[{msg_time}] {user_name}{thread_indicator}: {text}")
        
        return "\n".join(formatted_messages)
        
    except Exception as e:
        logger.error(f"Error formatting thread messages: {e}")
        return f"Error formatting messages: {str(e)}"

def _format_timestamp(ts: str) -> str:
    """
    Format a Slack timestamp to a readable format
    
    Args:
        ts: Slack timestamp string
        
    Returns:
        Formatted timestamp string
    """
    try:
        if ts and isinstance(ts, str):
            timestamp = float(ts)
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (ValueError, TypeError, OSError) as e:
        logger.warning(f"Could not format timestamp '{ts}': {e}")
    return "Unknown time" 